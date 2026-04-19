"""
Background grade monitor.
- Runs a per-user asyncio task.
- Respects time whitelist windows.
- Detects new grades and score changes.
"""

import asyncio
import logging
from datetime import datetime, time as dtime
import re

from aiogram import Bot

logger = logging.getLogger(__name__)


def _parse_whitelist(whitelist_str: str) -> list[tuple[dtime, dtime]]:
    """Parse "HH:MM-HH:MM,HH:MM-HH:MM" into list of (start, end) time pairs."""
    if not whitelist_str.strip():
        return []
    windows = []
    for part in whitelist_str.split(","):
        part = part.strip()
        m = re.match(r"(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})", part)
        if m:
            start = dtime(int(m.group(1)), int(m.group(2)))
            end = dtime(int(m.group(3)), int(m.group(4)))
            windows.append((start, end))
    return windows


def _is_in_window(windows: list[tuple[dtime, dtime]]) -> bool:
    """Return True if current time is within any of the windows (or no windows defined)."""
    if not windows:
        return True
    now = datetime.now().time()
    for start, end in windows:
        if start <= end:
            if start <= now <= end:
                return True
        else:
            # overnight window
            if now >= start or now <= end:
                return True
    return False


def _grades_to_snapshot(grades: dict) -> dict:
    """Flatten grades dict to a hashable snapshot for comparison."""
    snap = {}
    for subject, rows in grades.items():
        snap[subject] = [
            {
                "current_score": r.get("current_score"),
                "max_score": r.get("max_score"),
            }
            for r in rows
        ]
    return snap


def _diff_grades(old: dict, new: dict) -> list[str]:
    """Return list of human-readable change descriptions."""
    changes = []

    for subject, new_rows in new.items():
        old_rows = old.get(subject, [])

        for i, new_row in enumerate(new_rows):
            new_score = new_row.get("current_score")
            if i < len(old_rows):
                old_score = old_rows[i].get("current_score")
                if old_score != new_score:
                    if old_score is None and new_score is not None:
                        changes.append(
                            f"📥 *{subject}* — КТ{i+1}: появилась оценка *{new_score}*"
                        )
                    elif old_score is not None and new_score is None:
                        changes.append(
                            f"🗑 *{subject}* — КТ{i+1}: оценка удалена (было {old_score})"
                        )
                    else:
                        changes.append(
                            f"✏️ *{subject}* — КТ{i+1}: {old_score} → *{new_score}*"
                        )
            else:
                # New row appeared
                if new_score is not None:
                    changes.append(
                        f"📥 *{subject}* — КТ{i+1}: новая оценка *{new_score}*"
                    )

    # Detect new subjects
    for subject in new:
        if subject not in old:
            changes.append(f"📚 Новый предмет в оценках: *{subject}*")

    return changes


class GradeMonitor:
    def __init__(self, bot: Bot, storage):
        self._bot = bot
        self._storage = storage
        self._tasks: dict[int, asyncio.Task] = {}

    def is_active(self, user_id: int) -> bool:
        task = self._tasks.get(user_id)
        return task is not None and not task.done()

    def start(self, user_id: int):
        self.stop(user_id)
        self._storage.set_monitor_config(user_id, active=True)
        task = asyncio.create_task(self._monitor_loop(user_id))
        self._tasks[user_id] = task
        logger.info("Monitor started for user %d", user_id)

    def stop(self, user_id: int):
        task = self._tasks.pop(user_id, None)
        if task and not task.done():
            task.cancel()
        self._storage.set_monitor_config(user_id, active=False)
        logger.info("Monitor stopped for user %d", user_id)

    async def restore_active_monitors(self):
        """Called on bot startup — re-activate monitors that were running before restart."""
        for uid in self._storage.all_user_ids():
            cfg = self._storage.get_monitor_config(uid)
            if cfg.get("active"):
                logger.info("Restoring monitor for user %d", uid)
                self.start(uid)

    async def _monitor_loop(self, user_id: int):
        from etis_parser import ETISParser

        logger.info("Monitor loop running for user %d", user_id)
        parser: ETISParser | None = None

        while True:
            try:
                cfg = self._storage.get_monitor_config(user_id)
                interval = cfg.get("interval_minutes", 15)
                whitelist_str = cfg.get("whitelist", "")
                windows = _parse_whitelist(whitelist_str)

                if _is_in_window(windows):
                    creds = self._storage.get_credentials(user_id)
                    if creds:
                        # Проверяем / восстанавливаем сессию
                        session_ok = False
                        if parser:
                            try:
                                html = await parser._fetch(
                                    "https://student.psu.ru/pls/stu_cus_et/stu.signs",
                                    {"p_mode": "current"}
                                )
                                session_ok = bool(html and 'name="p_username"' not in html)
                            except Exception:
                                pass
                            if not session_ok:
                                await parser.close()
                                parser = None

                        if not session_ok:
                            parser = ETISParser()
                            session_ok = await parser.login(creds["login"], creds["password"])
                            if not session_ok:
                                logger.warning("Re-login failed for user %d during monitor", user_id)
                                await parser.close()
                                parser = None

                        if session_ok and parser:
                            try:
                                grades = await parser.get_grades()
                                new_snap = _grades_to_snapshot(grades)
                                old_snap = self._storage.get_grades_snapshot(user_id)

                                changes = _diff_grades(old_snap, new_snap)
                                if changes:
                                    text = "🔔 *Изменения в оценках!*\n\n" + "\n".join(changes)
                                    await self._bot.send_message(
                                        user_id, text, parse_mode="Markdown"
                                    )
                                    self._storage.save_grades_snapshot(user_id, new_snap)
                                    logger.info("Sent %d changes to user %d", len(changes), user_id)
                                elif not old_snap:
                                    self._storage.save_grades_snapshot(user_id, new_snap)
                                    logger.info("Saved initial snapshot for user %d", user_id)

                                # Записываем время последней проверки
                                import time as _time
                                self._storage.set_last_check(user_id, _time.time())
                            except Exception as e:
                                logger.error("Grade fetch error for %d: %s", user_id, e)
                else:
                    logger.debug("User %d outside time window, skipping check", user_id)

            except asyncio.CancelledError:
                logger.info("Monitor loop cancelled for user %d", user_id)
                if parser:
                    await parser.close()
                return
            except Exception as e:
                logger.error("Unexpected monitor error for %d: %s", user_id, e)

            await asyncio.sleep(interval * 60)
