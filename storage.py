"""
Storage — SQLite бэкенд.
Все данные живут в одном файле базы, не теряются при рестарте бота.

Путь к файлу — переменная окружения DB_PATH.
По умолчанию: <папка бота>/data/etis_bot.sqlite3
Бэкап = обычная копия этого файла (вместе с -wal, если бот запущен).

Раньше здесь был PostgreSQL (аддон Railway). После переезда на свой сервер
внешняя СУБД не нужна: пользователей десятки, нагрузка — пара запросов в минуту.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

logger = logging.getLogger(__name__)

DB_PATH: str = os.environ.get(
    "DB_PATH", str(Path(__file__).parent / "data" / "etis_bot.sqlite3")
)


async def get_pool() -> aiosqlite.Connection:
    """Открывает соединение с базой. Вызывается один раз при старте бота.

    Имя get_pool осталось от Postgres-версии: aiosqlite сам выстраивает все
    запросы в очередь на своём потоке, поэтому одно соединение и есть «пул».
    """
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    # WAL — чтобы чтение не блокировалось записью; busy_timeout на всякий случай.
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA busy_timeout=5000")
    await conn.commit()
    return conn


async def init_db(conn: aiosqlite.Connection):
    """Создаёт таблицы если их нет."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            first_name  TEXT,
            login       TEXT,
            password    TEXT,
            display_settings  TEXT NOT NULL DEFAULT '{}',
            grades_snapshot   TEXT NOT NULL DEFAULT '{}',
            created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS monitor (
            user_id           INTEGER PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
            interval_minutes  INTEGER NOT NULL DEFAULT 15,
            whitelist         TEXT    NOT NULL DEFAULT '',
            active            INTEGER NOT NULL DEFAULT 0,
            last_check        REAL
        )
    """)
    await conn.commit()


def _parse_ts(raw) -> Optional[datetime]:
    """CURRENT_TIMESTAMP в SQLite пишется как UTC-строка 'YYYY-MM-DD HH:MM:SS'.
    Отдаём datetime в местной зоне сервера (на ноде TZ=Europe/Moscow),
    чтобы в /users стояло московское время, а не UTC."""
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()


class UserStorage:
    def __init__(self, conn: aiosqlite.Connection):
        self._db = conn

    # ── Регистрация пользователя ──────────────────────────────────────────────

    async def ensure_user(self, user_id: int, username: str | None = None,
                          first_name: str | None = None):
        """Создаёт запись пользователя если её нет. Обновляет username/first_name."""
        await self._db.execute("""
            INSERT INTO users (user_id, username, first_name)
            VALUES (?, ?, ?)
            ON CONFLICT (user_id) DO UPDATE
                SET username   = COALESCE(excluded.username,   users.username),
                    first_name = COALESCE(excluded.first_name, users.first_name),
                    updated_at = CURRENT_TIMESTAMP
        """, (user_id, username, first_name))
        await self._db.commit()

    # ── Credentials ───────────────────────────────────────────────────────────

    async def save_credentials(self, user_id: int, login: str, password: str):
        await self._db.execute("""
            INSERT INTO users (user_id, login, password)
            VALUES (?, ?, ?)
            ON CONFLICT (user_id) DO UPDATE
                SET login    = excluded.login,
                    password = excluded.password,
                    updated_at = CURRENT_TIMESTAMP
        """, (user_id, login, password))
        await self._db.commit()

    async def get_credentials(self, user_id: int) -> Optional[dict]:
        async with self._db.execute(
            "SELECT login, password FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        if row and row["login"] and row["password"]:
            return {"login": row["login"], "password": row["password"]}
        return None

    async def all_user_ids(self) -> list[int]:
        async with self._db.execute("SELECT user_id FROM users") as cur:
            rows = await cur.fetchall()
        return [r["user_id"] for r in rows]

    async def all_users_info(self) -> list[dict]:
        """Все пользователи с username/first_name — для команды /users."""
        async with self._db.execute(
            "SELECT user_id, username, first_name, created_at FROM users "
            "ORDER BY created_at, user_id"
        ) as cur:
            rows = await cur.fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["created_at"] = _parse_ts(d.get("created_at"))
            out.append(d)
        return out

    # ── Monitor config ────────────────────────────────────────────────────────

    async def _ensure_monitor_row(self, user_id: int):
        await self._db.execute(
            "INSERT INTO monitor (user_id) VALUES (?) ON CONFLICT DO NOTHING",
            (user_id,),
        )

    async def set_monitor_config(
        self,
        user_id: int,
        *,
        interval_minutes: int | None = None,
        whitelist: str | None = None,
        active: bool | None = None,
    ):
        await self._ensure_monitor_row(user_id)
        if interval_minutes is not None:
            await self._db.execute(
                "UPDATE monitor SET interval_minutes=? WHERE user_id=?",
                (interval_minutes, user_id),
            )
        if whitelist is not None:
            await self._db.execute(
                "UPDATE monitor SET whitelist=? WHERE user_id=?",
                (whitelist, user_id),
            )
        if active is not None:
            await self._db.execute(
                "UPDATE monitor SET active=? WHERE user_id=?",
                (1 if active else 0, user_id),
            )
        await self._db.commit()

    async def get_monitor_config(self, user_id: int) -> dict:
        async with self._db.execute(
            "SELECT * FROM monitor WHERE user_id=?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        if row:
            cfg = dict(row)
            cfg["active"] = bool(cfg.get("active"))
            return cfg
        return {"interval_minutes": 15, "whitelist": "", "active": False, "last_check": None}

    async def set_last_check(self, user_id: int, timestamp: float):
        await self._ensure_monitor_row(user_id)
        await self._db.execute(
            "UPDATE monitor SET last_check=? WHERE user_id=?", (timestamp, user_id)
        )
        await self._db.commit()

    # ── Grades snapshot ───────────────────────────────────────────────────────

    async def save_grades_snapshot(self, user_id: int, snapshot: dict):
        await self._db.execute("""
            INSERT INTO users (user_id, grades_snapshot)
            VALUES (?, ?)
            ON CONFLICT (user_id) DO UPDATE
                SET grades_snapshot = excluded.grades_snapshot,
                    updated_at = CURRENT_TIMESTAMP
        """, (user_id, json.dumps(snapshot, ensure_ascii=False)))
        await self._db.commit()

    async def get_grades_snapshot(self, user_id: int) -> dict:
        async with self._db.execute(
            "SELECT grades_snapshot FROM users WHERE user_id=?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        if row and row["grades_snapshot"]:
            data = row["grades_snapshot"]
            return data if isinstance(data, dict) else json.loads(data)
        return {}

    # ── Display settings ──────────────────────────────────────────────────────

    async def get_display_settings(self, user_id: int) -> dict:
        async with self._db.execute(
            "SELECT display_settings FROM users WHERE user_id=?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        if row and row["display_settings"]:
            data = row["display_settings"]
            return dict(data) if isinstance(data, dict) else json.loads(data)
        return {}

    async def set_display_settings(self, user_id: int, settings: dict):
        await self._db.execute("""
            INSERT INTO users (user_id, display_settings)
            VALUES (?, ?)
            ON CONFLICT (user_id) DO UPDATE
                SET display_settings = excluded.display_settings,
                    updated_at = CURRENT_TIMESTAMP
        """, (user_id, json.dumps(settings, ensure_ascii=False)))
        await self._db.commit()
