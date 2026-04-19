"""
Simple JSON file storage for credentials and monitor config.
No external DB needed — perfect for a personal bot.
"""

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

USERS_FILE = DATA_DIR / "users.json"
MONITOR_FILE = DATA_DIR / "monitor.json"


class UserStorage:
    def __init__(self):
        self._users: dict = self._load(USERS_FILE)
        self._monitor: dict = self._load(MONITOR_FILE)

    # ── Persist ───────────────────────────────────────────────────────────────

    @staticmethod
    def _load(path: Path) -> dict:
        if path.exists():
            try:
                return json.loads(path.read_text("utf-8"))
            except Exception as e:
                logger.error("Failed to load %s: %s", path, e)
        return {}

    def _save_users(self):
        USERS_FILE.write_text(json.dumps(self._users, ensure_ascii=False, indent=2), "utf-8")

    def _save_monitor(self):
        MONITOR_FILE.write_text(json.dumps(self._monitor, ensure_ascii=False, indent=2), "utf-8")

    # ── Credentials ───────────────────────────────────────────────────────────

    def save_credentials(self, user_id: int, login: str, password: str):
        key = str(user_id)
        if key not in self._users:
            self._users[key] = {}
        self._users[key]["login"] = login
        self._users[key]["password"] = password
        self._save_users()

    def get_credentials(self, user_id: int) -> Optional[dict]:
        data = self._users.get(str(user_id), {})
        if "login" in data and "password" in data:
            return {"login": data["login"], "password": data["password"]}
        return None

    def all_user_ids(self) -> list[int]:
        return [int(k) for k in self._users.keys()]

    # ── Monitor config ────────────────────────────────────────────────────────

    def set_monitor_config(
        self,
        user_id: int,
        *,
        interval_minutes: int | None = None,
        whitelist: str | None = None,
        active: bool | None = None,
    ):
        key = str(user_id)
        if key not in self._monitor:
            self._monitor[key] = {"interval_minutes": 15, "whitelist": "", "active": False}
        if interval_minutes is not None:
            self._monitor[key]["interval_minutes"] = interval_minutes
        if whitelist is not None:
            self._monitor[key]["whitelist"] = whitelist
        if active is not None:
            self._monitor[key]["active"] = active
        self._save_monitor()

    def get_monitor_config(self, user_id: int) -> dict:
        return self._monitor.get(str(user_id), {
            "interval_minutes": 15, "whitelist": "", "active": False
        })



    def save_grades_snapshot(self, user_id: int, snapshot: dict):
        key = str(user_id)
        if key not in self._users:
            self._users[key] = {}
        self._users[key]["grades_snapshot"] = snapshot
        self._save_users()

    def get_grades_snapshot(self, user_id: int) -> dict:
        return self._users.get(str(user_id), {}).get("grades_snapshot", {})

    # ── Display settings ──────────────────────────────────────────────────────

    def get_display_settings(self, user_id: int) -> dict:
        return dict(self._users.get(str(user_id), {}).get("display_settings", {}))

    def set_display_settings(self, user_id: int, settings: dict):
        key = str(user_id)
        if key not in self._users:
            self._users[key] = {}
        self._users[key]["display_settings"] = settings
        self._save_users()

    # ── Monitor last_check ────────────────────────────────────────────────────

    def set_last_check(self, user_id: int, timestamp: float):
        key = str(user_id)
        if key not in self._monitor:
            self._monitor[key] = {"interval_minutes": 15, "whitelist": "", "active": False}
        self._monitor[key]["last_check"] = timestamp
        self._save_monitor()
