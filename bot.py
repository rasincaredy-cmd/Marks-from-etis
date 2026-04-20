import asyncio
import logging
import re
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

import config
from etis_parser import ETISParser, GRADES_URL
from storage import UserStorage, get_pool, init_db
from monitor import GradeMonitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)

bot     = Bot(token=config.BOT_TOKEN)
dp      = Dispatcher(storage=MemoryStorage())
storage: UserStorage = None   # инициализируется в main()
monitor: GradeMonitor = None

# id администратора — читается из переменной окружения ADMIN_ID
import os
ADMIN_ID: int = int(os.environ.get("ADMIN_ID", "0"))

# Кэш авторизованных парсеров
_parsers: dict[int, ETISParser] = {}

# Лишние чанки длинных сообщений
_chunks: dict[int, dict] = {}


# ─── States ───────────────────────────────────────────────────────────────────

class LoginStates(StatesGroup):
    waiting_login    = State()
    waiting_password = State()

class MonitorStates(StatesGroup):
    waiting_interval  = State()
    waiting_whitelist = State()

class AdminStates(StatesGroup):
    waiting_send_id   = State()
    waiting_send_text = State()


# ─── Keyboards ────────────────────────────────────────────────────────────────

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔐 Войти в ЕТИС",        callback_data="login")],
        [InlineKeyboardButton(text="📊 Оценки",               callback_data="grades_current"),
         InlineKeyboardButton(text="📅 Расписание",           callback_data="timetable")],
        [InlineKeyboardButton(text="🔔 Мониторинг оценок",    callback_data="monitor_menu")],
    ])

def grades_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить",             callback_data="grades_current"),
         InlineKeyboardButton(text="📚 Другой триместр",      callback_data="grades_pick_term")],
        [InlineKeyboardButton(text="⚙️ Настройки отображения",callback_data="grades_settings")],
        [InlineKeyboardButton(text="🏠 Меню",                 callback_data="main_menu")],
    ])

def timetable_nav_kb(week: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="◀️",                   callback_data=f"timetable_week_{week - 1}"),
            InlineKeyboardButton(text=f"Неделя {week}",       callback_data="noop"),
            InlineKeyboardButton(text="▶️",                   callback_data=f"timetable_week_{week + 1}"),
        ],
        [InlineKeyboardButton(text="📍 Текущая неделя",       callback_data="timetable")],
        [InlineKeyboardButton(text="⚙️ Настройки отображения",callback_data="timetable_settings")],
        [InlineKeyboardButton(text="🏠 Меню",                 callback_data="main_menu")],
    ])

def monitor_menu_kb(user_id: int) -> InlineKeyboardMarkup:
    active = monitor.is_active(user_id)
    toggle_btn = (
        InlineKeyboardButton(text="🛑 Остановить мониторинг", callback_data="monitor_stop")
        if active else
        InlineKeyboardButton(text="▶️ Запустить мониторинг",  callback_data="monitor_start")
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [toggle_btn],
        [InlineKeyboardButton(text="⚙️ Настроить интервал",      callback_data="monitor_set_interval")],
        [InlineKeyboardButton(text="🕐 Настроить временное окно", callback_data="monitor_set_whitelist")],
        [InlineKeyboardButton(text="📋 Текущие настройки",       callback_data="monitor_status")],
        [InlineKeyboardButton(text="🏠 Меню",                    callback_data="main_menu")],
    ])

def grades_settings_kb(s: dict) -> InlineKeyboardMarkup:
    def btn(key: str, label: str) -> InlineKeyboardButton:
        icon = "✅" if s.get(key, False) else "☐"
        return InlineKeyboardButton(text=f"{icon} {label}", callback_data=f"gs_toggle_{key}")
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("show_theme",        "Тема КТ")],
        [btn("show_work_type",    "Вид работы")],
        [btn("show_control_type", "Вид контроля")],
        [InlineKeyboardButton(text="◀️ К оценкам", callback_data="grades_current")],
        [InlineKeyboardButton(text="🏠 Меню",      callback_data="main_menu")],
    ])

def timetable_settings_kb(s: dict) -> InlineKeyboardMarkup:
    icon = "✅" if s.get("hide_consultations", False) else "☐"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{icon} Скрывать консультации",
            callback_data="ts_toggle_hide_consultations"
        )],
        [InlineKeyboardButton(text="◀️ К расписанию", callback_data="timetable")],
        [InlineKeyboardButton(text="🏠 Меню",          callback_data="main_menu")],
    ])

def term_pick_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"Триместр {t}", callback_data=f"grades_term_{t}")]
            for t in range(1, 7)]
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def back_kb(target: str = "main_menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Меню", callback_data=target)]
    ])


# ─── Helpers ──────────────────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID


async def ensure_logged_in(user_id: int) -> ETISParser | None:
    creds = await storage.get_credentials(user_id)
    if not creds:
        return None
    parser = _parsers.get(user_id)
    if parser:
        try:
            html = await parser._fetch(GRADES_URL, {"p_mode": "current"})
            if html and 'name="p_username"' not in html:
                return parser
        except Exception:
            pass
        await parser.close()
        _parsers.pop(user_id, None)
    parser = ETISParser()
    if await parser.login(creds["login"], creds["password"]):
        _parsers[user_id] = parser
        return parser
    await parser.close()
    return None


def _split_text(text: str, limit: int = 4000) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut == -1:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts


async def _safe_edit(message, text: str, **kwargs) -> bool:
    try:
        await message.edit_text(text, **kwargs)
        return True
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return True
        if "message to edit not found" in str(e):
            return False
        raise


async def _clear_extra_chunks(user_id: int, chat_id: int):
    info = _chunks.pop(user_id, None)
    if not info:
        return
    for msg_id in info.get("extra_ids", []):
        try:
            await bot.delete_message(chat_id, msg_id)
        except Exception:
            pass


async def _send_long(cb: CallbackQuery, text: str, final_kb: InlineKeyboardMarkup):
    user_id = cb.from_user.id
    chat_id = cb.message.chat.id
    await _clear_extra_chunks(user_id, chat_id)

    chunks = _split_text(text)
    extra_ids: list[int] = []
    anchor_message = cb.message

    for i, chunk in enumerate(chunks):
        is_last = (i == len(chunks) - 1)
        kb = final_kb if is_last else None
        if i == 0:
            ok = await _safe_edit(anchor_message, chunk, parse_mode="Markdown", reply_markup=kb)
            if not ok:
                sent = await cb.message.answer(chunk, parse_mode="Markdown", reply_markup=kb)
                anchor_message = sent
        else:
            sent = await anchor_message.answer(chunk, parse_mode="Markdown", reply_markup=kb)
            extra_ids.append(sent.message_id)

    if extra_ids:
        _chunks[user_id] = {"anchor_id": anchor_message.message_id, "extra_ids": extra_ids}


async def _send_menu(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup,
                     state: FSMContext | None = None):
    user_id = cb.from_user.id
    chat_id = cb.message.chat.id
    await _clear_extra_chunks(user_id, chat_id)
    if state:
        await state.clear()
    try:
        await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            pass
        elif "message to edit not found" in str(e):
            await cb.message.answer(text, parse_mode="Markdown", reply_markup=kb)
        else:
            raise
    await cb.answer()


# ─── /start ───────────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await storage.ensure_user(
        message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
    )
    await message.answer("Главное меню ЕТИС-бота 🎓", reply_markup=main_menu_kb())


# ─── Main menu ────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "main_menu")
async def cb_main_menu(cb: CallbackQuery, state: FSMContext):
    await _send_menu(cb, "Главное меню ЕТИС-бота 🎓", main_menu_kb(), state=state)


@dp.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


# ─── Login ────────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "login")
async def cb_login(cb: CallbackQuery, state: FSMContext):
    await _send_menu(
        cb,
        "🔐 *Авторизация в ЕТИС*\n\n"
        "Введите логин (фамилию или email):\n\n"
        "_Данные хранятся только локально._",
        back_kb(),
    )
    await state.set_state(LoginStates.waiting_login)


@dp.message(LoginStates.waiting_login)
async def process_login(message: Message, state: FSMContext):
    await state.update_data(login=message.text.strip())
    await message.answer("🔑 Введите пароль:")
    await state.set_state(LoginStates.waiting_password)


@dp.message(LoginStates.waiting_password)
async def process_password(message: Message, state: FSMContext):
    data = await state.get_data()
    login = data["login"]
    password = message.text.strip()
    await message.answer("⏳ Проверяю данные...")

    parser = ETISParser()
    ok = await parser.login(login, password)
    if ok:
        _parsers[message.from_user.id] = parser
        await storage.save_credentials(message.from_user.id, login, password)
        await state.clear()
        await message.answer("✅ Авторизация прошла успешно!", reply_markup=main_menu_kb())
    else:
        await parser.close()
        await state.clear()
        await message.answer("❌ Ошибка авторизации. Проверьте логин и пароль.",
                             reply_markup=main_menu_kb())


# ─── Grades ───────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "grades_current")
async def cb_grades_current(cb: CallbackQuery):
    await _show_grades(cb, term=None)


@dp.callback_query(F.data == "grades_pick_term")
async def cb_grades_pick_term(cb: CallbackQuery):
    await _send_menu(cb, "Выберите триместр:", term_pick_kb())


@dp.callback_query(F.data.startswith("grades_term_"))
async def cb_grades_term(cb: CallbackQuery):
    term = int(cb.data.split("_")[-1])
    await _show_grades(cb, term=term)


async def _show_grades(cb: CallbackQuery, term: int | None):
    await _clear_extra_chunks(cb.from_user.id, cb.message.chat.id)
    ok = await _safe_edit(cb.message, "⏳ Загружаю оценки...")
    if not ok:
        msg = await cb.message.answer("⏳ Загружаю оценки...")
        cb = cb.model_copy(update={"message": msg})
    await cb.answer()

    parser = await ensure_logged_in(cb.from_user.id)
    if not parser:
        await _safe_edit(cb.message, "❌ Не могу войти в ЕТИС. Войдите снова.",
                         reply_markup=main_menu_kb())
        return

    try:
        grades = await parser.get_grades(term=term)
    except Exception as e:
        logger.error("Grades error: %s", e)
        await _safe_edit(cb.message, "❌ Ошибка при загрузке оценок.", reply_markup=grades_kb())
        return

    if not grades:
        await _safe_edit(cb.message, "📭 Оценок не найдено.", reply_markup=grades_kb())
        return

    ds = await storage.get_display_settings(cb.from_user.id)
    show_theme        = ds.get("show_theme", False)
    show_work_type    = ds.get("show_work_type", False)
    show_control_type = ds.get("show_control_type", False)

    label = f"Триместр {term}" if term else "текущий триместр"
    text = f"📊 *Оценки ({label})*\n\n"

    for subject, rows in grades.items():
        total_rating = sum(r["rating_score"] for r in rows if r.get("rating_score") is not None)
        total_max    = sum(r["max_score"]    for r in rows if r.get("max_score")    is not None)
        text += f"📌 *{subject}*\n"
        for i, row in enumerate(rows, 1):
            r_str = str(row["rating_score"]) if row.get("rating_score") is not None else "—"
            m_str = str(row["max_score"])    if row.get("max_score")    is not None else "—"
            p_str = str(row["passing_score"])if row.get("passing_score")is not None else "—"
            line = f"  КТ{i}: {r_str}/{m_str} (проход: {p_str})"
            if show_work_type    and row.get("work_type"):    line += f" | {row['work_type']}"
            if show_control_type and row.get("control_type"): line += f" | {row['control_type']}"
            if row.get("date"):   line += f" {row['date']}"
            if row.get("is_red"): line += " ⚠️"
            text += line + "\n"
            if show_theme and row.get("theme"):
                text += f"    _↳ {row['theme']}_\n"
        text += f"  _Итого в рейтинге: {total_rating}/{total_max}_\n\n"

    await _send_long(cb, text, grades_kb())


# ─── Timetable ────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "timetable")
async def cb_timetable(cb: CallbackQuery):
    await _show_timetable(cb, week=None)


@dp.callback_query(F.data.startswith("timetable_week_"))
async def cb_timetable_week(cb: CallbackQuery):
    week = int(cb.data.split("_")[-1])
    await _show_timetable(cb, week=week)


async def _show_timetable(cb: CallbackQuery, week: int | None):
    await _clear_extra_chunks(cb.from_user.id, cb.message.chat.id)
    ok = await _safe_edit(cb.message, "⏳ Загружаю расписание...")
    if not ok:
        msg = await cb.message.answer("⏳ Загружаю расписание...")
        cb = cb.model_copy(update={"message": msg})
    await cb.answer()

    parser = await ensure_logged_in(cb.from_user.id)
    if not parser:
        await _safe_edit(cb.message, "❌ Не могу войти в ЕТИС. Войдите снова.",
                         reply_markup=main_menu_kb())
        return

    try:
        result = await parser.get_timetable(week=week)
    except Exception as e:
        logger.error("Timetable error: %s", e)
        await _safe_edit(cb.message, "❌ Ошибка при загрузке расписания.", reply_markup=back_kb())
        return

    days         = result["days"]
    current_week = result["week"]
    week_label   = result.get("week_label", f"Неделя {current_week}")

    if not days:
        await _safe_edit(cb.message, f"📭 Расписание на {week_label} не найдено.",
                         reply_markup=timetable_nav_kb(current_week))
        return

    ds = await storage.get_display_settings(cb.from_user.id)
    hide_cons = ds.get("hide_consultations", False)

    text = f"📅 *Расписание — {week_label}*\n\n"
    for day in days:
        visible = [p for p in day["pairs"]
                   if not (hide_cons and "консультац" in p["subject"].lower())]
        text += f"*{day['name']}*\n"
        if not visible:
            text += "  нет занятий\n"
        for pair in visible:
            text += f"  {pair['num']} пара ({pair['time']})\n"
            text += f"    📖 {pair['subject']}\n"
            if pair.get("teacher"): text += f"    👤 {pair['teacher']}\n"
            if pair.get("room"):    text += f"    🚪 {pair['room']}\n"
        text += "\n"

    await _send_long(cb, text, timetable_nav_kb(current_week))


# ─── Display settings ─────────────────────────────────────────────────────────

@dp.callback_query(F.data == "grades_settings")
async def cb_grades_settings(cb: CallbackQuery):
    s = await storage.get_display_settings(cb.from_user.id)
    await _send_menu(cb,
        "⚙️ *Настройки отображения оценок*\n\nВключи что хочешь видеть у каждой КТ:",
        grades_settings_kb(s))


@dp.callback_query(F.data.startswith("gs_toggle_"))
async def cb_gs_toggle(cb: CallbackQuery):
    key = cb.data.removeprefix("gs_toggle_")
    s = await storage.get_display_settings(cb.from_user.id)
    s[key] = not s.get(key, False)
    await storage.set_display_settings(cb.from_user.id, s)
    try:
        await cb.message.edit_reply_markup(reply_markup=grades_settings_kb(s))
    except TelegramBadRequest:
        pass
    await cb.answer("✅ Сохранено")


@dp.callback_query(F.data == "timetable_settings")
async def cb_timetable_settings(cb: CallbackQuery):
    s = await storage.get_display_settings(cb.from_user.id)
    await _send_menu(cb, "⚙️ *Настройки отображения расписания*", timetable_settings_kb(s))


@dp.callback_query(F.data.startswith("ts_toggle_"))
async def cb_ts_toggle(cb: CallbackQuery):
    key = cb.data.removeprefix("ts_toggle_")
    s = await storage.get_display_settings(cb.from_user.id)
    s[key] = not s.get(key, False)
    await storage.set_display_settings(cb.from_user.id, s)
    try:
        await cb.message.edit_reply_markup(reply_markup=timetable_settings_kb(s))
    except TelegramBadRequest:
        pass
    await cb.answer("✅ Сохранено")


# ─── Monitor ──────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "monitor_menu")
async def cb_monitor_menu(cb: CallbackQuery):
    await _send_menu(cb,
        "🔔 *Мониторинг оценок*\n\nАвтоматически проверяет появление новых оценок и изменения.",
        monitor_menu_kb(cb.from_user.id))


@dp.callback_query(F.data == "monitor_start")
async def cb_monitor_start(cb: CallbackQuery):
    user_id = cb.from_user.id
    if not await storage.get_credentials(user_id):
        await cb.answer("❌ Сначала войдите в ЕТИС!", show_alert=True)
        return
    monitor.start(user_id)
    await _send_menu(cb, "✅ Мониторинг запущен!", monitor_menu_kb(user_id))


@dp.callback_query(F.data == "monitor_stop")
async def cb_monitor_stop(cb: CallbackQuery):
    monitor.stop(cb.from_user.id)
    await _send_menu(cb, "🛑 Мониторинг остановлен.", monitor_menu_kb(cb.from_user.id))


@dp.callback_query(F.data == "monitor_status")
async def cb_monitor_status(cb: CallbackQuery):
    user_id = cb.from_user.id
    cfg     = await storage.get_monitor_config(user_id)
    active  = monitor.is_active(user_id)
    last_ts = cfg.get("last_check")
    last_str = datetime.fromtimestamp(last_ts).strftime("%d.%m.%Y %H:%M:%S") if last_ts else "ещё не было"
    text = (
        f"📋 *Настройки мониторинга*\n\n"
        f"Статус: {'🟢 активен' if active else '🔴 остановлен'}\n"
        f"Интервал: каждые *{cfg.get('interval_minutes', 15)}* мин\n"
        f"Временное окно: {cfg.get('whitelist') or 'не ограничено (весь день)'}\n"
        f"Последняя проверка: {last_str}\n"
    )
    await _send_menu(cb, text, monitor_menu_kb(user_id))


@dp.callback_query(F.data == "monitor_set_interval")
async def cb_monitor_set_interval(cb: CallbackQuery, state: FSMContext):
    await _send_menu(cb,
        "⚙️ Введите интервал проверки в минутах (например: `15`):",
        back_kb("monitor_menu"))
    await state.set_state(MonitorStates.waiting_interval)


@dp.message(MonitorStates.waiting_interval)
async def process_interval(message: Message, state: FSMContext):
    try:
        minutes = int(message.text.strip())
        if minutes < 1:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите целое число минут (минимум 1).")
        return
    await storage.set_monitor_config(message.from_user.id, interval_minutes=minutes)
    await state.clear()
    await message.answer(
        f"✅ Интервал установлен: каждые *{minutes}* мин.",
        parse_mode="Markdown",
        reply_markup=monitor_menu_kb(message.from_user.id)
    )


@dp.callback_query(F.data == "monitor_set_whitelist")
async def cb_monitor_set_whitelist(cb: CallbackQuery, state: FSMContext):
    await _send_menu(cb,
        "🕐 *Временное окно проверки*\n\n"
        "Введите диапазоны через запятую:\n"
        "Пример: `06:00-21:00` или `06:00-13:00,15:00-22:00`\n\n"
        "Чтобы убрать ограничение, введите `-`",
        back_kb("monitor_menu"))
    await state.set_state(MonitorStates.waiting_whitelist)


@dp.message(MonitorStates.waiting_whitelist)
async def process_whitelist(message: Message, state: FSMContext):
    raw = message.text.strip()
    if raw == "-":
        await storage.set_monitor_config(message.from_user.id, whitelist="")
        await state.clear()
        await message.answer(
            "✅ Временное окно убрано — мониторинг работает круглосуточно.",
            reply_markup=monitor_menu_kb(message.from_user.id)
        )
        return
    pattern = r"^\d{1,2}:\d{2}-\d{1,2}:\d{2}(,\d{1,2}:\d{2}-\d{1,2}:\d{2})*$"
    if not re.match(pattern, raw):
        await message.answer(
            "❌ Неверный формат. Пример: `06:00-21:00` или `06:00-13:00,15:00-22:00`",
            parse_mode="Markdown"
        )
        return
    await storage.set_monitor_config(message.from_user.id, whitelist=raw)
    await state.clear()
    await message.answer(
        f"✅ Временное окно установлено: *{raw}*",
        parse_mode="Markdown",
        reply_markup=monitor_menu_kb(message.from_user.id)
    )


# ─── Admin ────────────────────────────────────────────────────────────────────

@dp.message(Command("users"))
async def cmd_users(message: Message):
    if not is_admin(message.from_user.id):
        return
    users = await storage.all_users_info()
    if not users:
        await message.answer("Пользователей нет.")
        return
    lines = [f"👥 Пользователи ({len(users)}):\n"]
    for u in users:
        uid   = u["user_id"]
        name  = u.get("first_name") or ""
        uname = f"@{u['username']}" if u.get("username") else "нет username"
        dt    = u["created_at"].strftime("%d.%m.%Y %H:%M") if u.get("created_at") else ""
        lines.append(f"{uid} | {name} | {uname} | {dt}")
    await message.answer("\n".join(lines))


@dp.message(Command("send"))
async def cmd_send(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await message.answer("Введите user\\_id получателя:", parse_mode="Markdown")
    await state.set_state(AdminStates.waiting_send_id)


@dp.message(AdminStates.waiting_send_id)
async def process_send_id(message: Message, state: FSMContext):
    try:
        uid = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите числовой user_id.")
        return
    await state.update_data(target_id=uid)
    await message.answer("Введите текст сообщения:")
    await state.set_state(AdminStates.waiting_send_text)


@dp.message(AdminStates.waiting_send_text)
async def process_send_text(message: Message, state: FSMContext):
    data = await state.get_data()
    target_id = data["target_id"]
    await state.clear()
    try:
        await bot.send_message(target_id, message.text)
        await message.answer(f"✅ Отправлено пользователю `{target_id}`", parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Не удалось отправить: {e}")


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    global storage, monitor

    logger.info("Connecting to database...")
    pool = await get_pool()
    await init_db(pool)
    logger.info("Database ready")

    storage = UserStorage(pool)
    monitor = GradeMonitor(bot, storage)

    await monitor.restore_active_monitors()
    logger.info("Starting ETIS bot...")
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
