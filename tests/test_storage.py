"""Тесты SQLite-хранилища: проверяют то, что раньше делал PostgreSQL.

Специально без pytest-asyncio: каждый тест сам крутит asyncio.run.
"""

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _fresh(tmp_path):
    """Отдельная база на каждый тест."""
    os.environ["DB_PATH"] = str(tmp_path / "test.sqlite3")
    import importlib
    import storage as storage_module
    importlib.reload(storage_module)
    return storage_module


def _run(coro_factory, tmp_path):
    mod = _fresh(tmp_path)

    async def wrapper():
        conn = await mod.get_pool()
        await mod.init_db(conn)
        st = mod.UserStorage(conn)
        try:
            return await coro_factory(st, mod)
        finally:
            await conn.close()

    return asyncio.run(wrapper())


def test_ensure_user_ne_zatirayet_imya_pustotoy(tmp_path):
    """Второй /start без username не должен стирать сохранённый username."""
    async def scenario(st, mod):
        await st.ensure_user(1, username="vlad", first_name="Влад")
        await st.ensure_user(1, username=None, first_name=None)
        return await st.all_users_info()

    users = _run(scenario, tmp_path)
    assert len(users) == 1
    assert users[0]["username"] == "vlad"
    assert users[0]["first_name"] == "Влад"


def test_created_at_eto_datetime(tmp_path):
    """/users зовёт .strftime() у created_at — значит это должен быть datetime."""
    async def scenario(st, mod):
        await st.ensure_user(1, username="u")
        return await st.all_users_info()

    users = _run(scenario, tmp_path)
    assert isinstance(users[0]["created_at"], datetime)
    users[0]["created_at"].strftime("%d.%m.%Y %H:%M")


def test_credentials_roundtrip_i_perezapis(tmp_path):
    async def scenario(st, mod):
        assert await st.get_credentials(7) is None
        await st.save_credentials(7, "login1", "pass1")
        first = await st.get_credentials(7)
        await st.save_credentials(7, "login2", "pass2")
        return first, await st.get_credentials(7), await st.all_user_ids()

    first, second, ids = _run(scenario, tmp_path)
    assert first == {"login": "login1", "password": "pass1"}
    assert second == {"login": "login2", "password": "pass2"}
    assert ids == [7]


def test_credentials_ne_teryayutsya_posle_ensure_user(tmp_path):
    """Сохранили пароль, потом человек снова нажал /start — пароль должен уцелеть."""
    async def scenario(st, mod):
        await st.save_credentials(5, "l", "p")
        await st.ensure_user(5, username="x", first_name="Y")
        return await st.get_credentials(5)

    assert _run(scenario, tmp_path) == {"login": "l", "password": "p"}


def test_monitor_defaults_i_chastichnye_apdeyty(tmp_path):
    async def scenario(st, mod):
        empty = await st.get_monitor_config(42)
        await st.set_monitor_config(42, interval_minutes=30)
        only_interval = await st.get_monitor_config(42)
        await st.set_monitor_config(42, active=True)
        await st.set_monitor_config(42, whitelist="08:00-20:00")
        await st.set_last_check(42, 1755950000.5)
        return empty, only_interval, await st.get_monitor_config(42)

    empty, only_interval, full = _run(scenario, tmp_path)
    assert empty["active"] is False and empty["interval_minutes"] == 15
    assert only_interval["interval_minutes"] == 30
    assert only_interval["active"] is False       # не включился сам собой
    assert only_interval["whitelist"] == ""
    assert full["active"] is True                 # именно bool, не 1
    assert full["interval_minutes"] == 30         # прежняя настройка уцелела
    assert full["whitelist"] == "08:00-20:00"
    assert full["last_check"] == 1755950000.5


def test_monitor_vyklyuchaetsya(tmp_path):
    async def scenario(st, mod):
        await st.set_monitor_config(3, active=True)
        await st.set_monitor_config(3, active=False)
        return await st.get_monitor_config(3)

    assert _run(scenario, tmp_path)["active"] is False


def test_snapshot_ocenok_s_kirillicey(tmp_path):
    async def scenario(st, mod):
        assert await st.get_grades_snapshot(9) == {}
        snap = {"Математический анализ": {"КР 1": "5", "Зачёт": "зачтено"}}
        await st.save_grades_snapshot(9, snap)
        return await st.get_grades_snapshot(9)

    assert _run(scenario, tmp_path) == {
        "Математический анализ": {"КР 1": "5", "Зачёт": "зачтено"}
    }


def test_snapshot_ne_zatirayet_login(tmp_path):
    """Мониторинг пишет снимок оценок — учётка при этом должна остаться."""
    async def scenario(st, mod):
        await st.save_credentials(11, "l", "p")
        await st.save_grades_snapshot(11, {"a": 1})
        await st.set_display_settings(11, {"show_avg": True})
        return await st.get_credentials(11), await st.get_grades_snapshot(11), \
               await st.get_display_settings(11)

    creds, snap, ds = _run(scenario, tmp_path)
    assert creds == {"login": "l", "password": "p"}
    assert snap == {"a": 1}
    assert ds == {"show_avg": True}


def test_display_settings_default_pustoy(tmp_path):
    async def scenario(st, mod):
        first = await st.get_display_settings(4)
        await st.set_display_settings(4, {"hide_zero": True})
        return first, await st.get_display_settings(4)

    first, second = _run(scenario, tmp_path)
    assert first == {}
    assert second == {"hide_zero": True}


def test_baza_perezhivaet_perezapusk(tmp_path):
    """Главное, ради чего база вообще есть: данные живы после рестарта бота."""
    mod = _fresh(tmp_path)

    async def write():
        conn = await mod.get_pool()
        await mod.init_db(conn)
        st = mod.UserStorage(conn)
        await st.ensure_user(1, username="vlad")
        await st.save_credentials(1, "l", "p")
        await st.set_monitor_config(1, active=True, interval_minutes=20)
        await conn.close()

    async def read():
        conn = await mod.get_pool()
        await mod.init_db(conn)          # повторный старт не должен ломать таблицы
        st = mod.UserStorage(conn)
        out = (await st.get_credentials(1), await st.get_monitor_config(1),
               await st.all_user_ids())
        await conn.close()
        return out

    asyncio.run(write())
    creds, cfg, ids = asyncio.run(read())
    assert creds == {"login": "l", "password": "p"}
    assert cfg["active"] is True and cfg["interval_minutes"] == 20
    assert ids == [1]
