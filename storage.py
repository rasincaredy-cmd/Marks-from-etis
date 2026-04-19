"""
Storage — PostgreSQL бэкенд.
Все данные живут в БД, не теряются при редеплое.

Railway: Add Service → Database → PostgreSQL
Переменная DATABASE_URL подставляется Railway автоматически.
"""

import json
import logging
import os
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)

DATABASE_URL: str = os.environ.get("DATABASE_URL", "")


async def get_pool() -> asyncpg.Pool:
    """Создаёт пул соединений. Вызывается один раз при старте бота."""
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL не задан.\n"
            "Railway: Add Service → Database → PostgreSQL — URL подставится автоматически."
        )
    return await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)


async def init_db(pool: asyncpg.Pool):
    """Создаёт таблицы если их нет."""
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     BIGINT PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
                login       TEXT,
                password    TEXT,
                display_settings  JSONB NOT NULL DEFAULT '{}',
                grades_snapshot   JSONB NOT NULL DEFAULT '{}',
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS monitor (
                user_id           BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                interval_minutes  INT  NOT NULL DEFAULT 15,
                whitelist         TEXT NOT NULL DEFAULT '',
                active            BOOL NOT NULL DEFAULT FALSE,
                last_check        DOUBLE PRECISION
            )
        """)


class UserStorage:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    # ── Регистрация пользователя ──────────────────────────────────────────────

    async def ensure_user(self, user_id: int, username: str | None = None,
                          first_name: str | None = None):
        """Создаёт запись пользователя если её нет. Обновляет username/first_name."""
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (user_id, username, first_name)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id) DO UPDATE
                    SET username   = COALESCE(EXCLUDED.username,   users.username),
                        first_name = COALESCE(EXCLUDED.first_name, users.first_name),
                        updated_at = NOW()
            """, user_id, username, first_name)

    # ── Credentials ───────────────────────────────────────────────────────────

    async def save_credentials(self, user_id: int, login: str, password: str):
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (user_id, login, password)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id) DO UPDATE
                    SET login    = EXCLUDED.login,
                        password = EXCLUDED.password,
                        updated_at = NOW()
            """, user_id, login, password)

    async def get_credentials(self, user_id: int) -> Optional[dict]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT login, password FROM users WHERE user_id = $1", user_id
            )
        if row and row["login"] and row["password"]:
            return {"login": row["login"], "password": row["password"]}
        return None

    async def all_user_ids(self) -> list[int]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id FROM users")
        return [r["user_id"] for r in rows]

    async def all_users_info(self) -> list[dict]:
        """Все пользователи с username/first_name — для команды /users."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id, username, first_name, created_at FROM users ORDER BY created_at"
            )
        return [dict(r) for r in rows]

    # ── Monitor config ────────────────────────────────────────────────────────

    async def _ensure_monitor_row(self, conn, user_id: int):
        await conn.execute("""
            INSERT INTO monitor (user_id) VALUES ($1)
            ON CONFLICT DO NOTHING
        """, user_id)

    async def set_monitor_config(
        self,
        user_id: int,
        *,
        interval_minutes: int | None = None,
        whitelist: str | None = None,
        active: bool | None = None,
    ):
        async with self._pool.acquire() as conn:
            await self._ensure_monitor_row(conn, user_id)
            if interval_minutes is not None:
                await conn.execute(
                    "UPDATE monitor SET interval_minutes=$1 WHERE user_id=$2",
                    interval_minutes, user_id
                )
            if whitelist is not None:
                await conn.execute(
                    "UPDATE monitor SET whitelist=$1 WHERE user_id=$2",
                    whitelist, user_id
                )
            if active is not None:
                await conn.execute(
                    "UPDATE monitor SET active=$1 WHERE user_id=$2",
                    active, user_id
                )

    async def get_monitor_config(self, user_id: int) -> dict:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM monitor WHERE user_id=$1", user_id
            )
        if row:
            return dict(row)
        return {"interval_minutes": 15, "whitelist": "", "active": False, "last_check": None}

    async def set_last_check(self, user_id: int, timestamp: float):
        async with self._pool.acquire() as conn:
            await self._ensure_monitor_row(conn, user_id)
            await conn.execute(
                "UPDATE monitor SET last_check=$1 WHERE user_id=$2",
                timestamp, user_id
            )

    # ── Grades snapshot ───────────────────────────────────────────────────────

    async def save_grades_snapshot(self, user_id: int, snapshot: dict):
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (user_id, grades_snapshot)
                VALUES ($1, $2::jsonb)
                ON CONFLICT (user_id) DO UPDATE
                    SET grades_snapshot = EXCLUDED.grades_snapshot,
                        updated_at = NOW()
            """, user_id, json.dumps(snapshot, ensure_ascii=False))

    async def get_grades_snapshot(self, user_id: int) -> dict:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT grades_snapshot FROM users WHERE user_id=$1", user_id
            )
        if row and row["grades_snapshot"]:
            data = row["grades_snapshot"]
            return data if isinstance(data, dict) else json.loads(data)
        return {}

    # ── Display settings ──────────────────────────────────────────────────────

    async def get_display_settings(self, user_id: int) -> dict:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT display_settings FROM users WHERE user_id=$1", user_id
            )
        if row and row["display_settings"]:
            data = row["display_settings"]
            return dict(data) if isinstance(data, dict) else json.loads(data)
        return {}

    async def set_display_settings(self, user_id: int, settings: dict):
        async with self._pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (user_id, display_settings)
                VALUES ($1, $2::jsonb)
                ON CONFLICT (user_id) DO UPDATE
                    SET display_settings = EXCLUDED.display_settings,
                        updated_at = NOW()
            """, user_id, json.dumps(settings, ensure_ascii=False))
