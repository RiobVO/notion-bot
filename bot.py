import asyncio
import json
import logging
import os
import re
import shlex
import datetime
from datetime import date, timedelta
from typing import Optional, Tuple, Dict, Any, List, Set

import pytz
from dotenv import load_dotenv
from notion_client import Client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder, CallbackQueryHandler, CommandHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters,
)

__all__ = ["main"]
__version__ = "1.0.0"


load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("notion_bot")

BOT_TOKEN    = os.getenv("BOT_TOKEN", "")
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
DATABASE_ID  = os.getenv("DATABASE_ID", "")
CHAT_ID      = os.getenv("CHAT_ID", "")
TIMEZONE_STR = os.getenv("TIMEZONE", "Europe/Moscow")

if not all([BOT_TOKEN, NOTION_TOKEN, DATABASE_ID]):
    raise RuntimeError("Required environment variables are not set.")

# ── Валидация CHAT_ID ──────────────────────────────────────────────────────
# CHAT_ID_INT используется для авторизации и напоминаний
CHAT_ID_INT: Optional[int] = None
if CHAT_ID:
    try:
        CHAT_ID_INT = int(CHAT_ID)
    except ValueError:
        raise RuntimeError(f"CHAT_ID должен быть числом, получено: {CHAT_ID!r}")

# ── Часовой пояс ───────────────────────────────────────────────────────────
try:
    TZ = pytz.timezone(TIMEZONE_STR)
except pytz.UnknownTimeZoneError:
    raise RuntimeError(
        f"Неизвестный часовой пояс: {TIMEZONE_STR!r}. "
        f"Используй стандартные имена, например 'Europe/Moscow'."
    )

notion = Client(auth=NOTION_TOKEN)


def _safe_mask(value: str, keep: int = 4) -> str:
    """Маскирует чувствительные данные в логах/отладке."""
    if not value:
        return "<empty>"
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * (len(value) - keep) + value[-keep:]


def log_event(event: str, **fields: Any) -> None:
    """Структурное логирование бизнес-событий в JSON-формате."""
    payload: Dict[str, Any] = {"event": event, "version": __version__, **fields}
    try:
        logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    except Exception:
        # Фоллбек, чтобы логирование никогда не ломало логику бота
        logger.info("event=%s fields=%s", event, fields)

# ── Названия полей в Notion ────────────────────────────────────────────────
ENTRY_PROP   = "Entry"
DATE_PROP    = "date"
CLOCK_PROP   = "clock"
TYPE_PROP    = "Type"
TOPIC_PROP   = "Topic"
COMMENT_PROP = "comment"
GOAL_PROP    = "Goal (h)"

# ── Состояния /quick диалога ───────────────────────────────────────────────
CHOOSE_TYPE, CHOOSE_TOPIC, ENTER_HOURS, ENTER_GOAL, ENTER_COMMENT = range(5)

# ── Настройки — хранятся в settings.json ──────────────────────────────────
# Путь относительно расположения файла скрипта (не рабочей директории)
SETTINGS_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
DEFAULT_TYPES  = ["Practice", "Theory", "Repeat", "Project"]
DEFAULT_TOPICS = ["SQL", "Python", "Алгоритмы", "Циклы", "Функции", "ООП", "API", "Django"]
MAX_NAME_LEN   = 50

# ── Разделитель для сообщений (модульная константа, не дублируем в каждой функции) ──
D = "━━━━━━━━━━━━━━━━━━━━━"

# ── Тексты кнопок главного меню (нужны для определения fallback в диалогах) ──
MENU_BUTTONS = frozenset({
    "➕ Быстрая запись",
    "📊 Статус сегодня",
    "📅 Неделя",
    "🔥 Серия",
    "📈 Итог месяца",
    "❓ Помощь",
})


# ══════════════════════════════════════════════════════════════════════════════
#  АВТОРИЗАЦИЯ
# ══════════════════════════════════════════════════════════════════════════════

def _is_authorized(update: Update) -> bool:
    """Проверяет, является ли отправитель авторизованным пользователем.
    Если CHAT_ID не задан в .env — пускаем всех (режим разработки)."""
    if CHAT_ID_INT is None:
        return True
    user = update.effective_user
    return user is not None and user.id == CHAT_ID_INT


async def _check_auth(update: Update) -> bool:
    """Проверяет авторизацию и отвечает отказом если нет доступа.
    Возвращает True если авторизован."""
    if not _is_authorized(update):
        await update.effective_message.reply_text("⛔ Нет доступа.")
        return False
    return True


# ══════════════════════════════════════════════════════════════════════════════
#  НАСТРОЙКИ (кеш в памяти — не читаем файл при каждом запросе)
# ══════════════════════════════════════════════════════════════════════════════

_settings_cache: Optional[dict] = None


def _load_settings() -> Dict[str, List[str]]:
    global _settings_cache
    if _settings_cache is not None:
        return _settings_cache
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                _settings_cache = json.load(f)
                return _settings_cache
        except Exception:
            pass
    _settings_cache = {"types": DEFAULT_TYPES[:], "topics": DEFAULT_TOPICS[:]}
    return _settings_cache


def _save_settings(data: Dict[str, List[str]]) -> None:
    global _settings_cache
    _settings_cache = data  # обновляем кеш сразу
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _get_types() -> List[str]:
    return _load_settings()["types"]


def _get_topics() -> List[str]:
    return _load_settings()["topics"]


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _today() -> date:
    """Текущая дата в заданном часовом поясе (не дата сервера!)."""
    return datetime.datetime.now(TZ).date()


def _main_menu() -> ReplyKeyboardMarkup:
    """Постоянное меню кнопок внизу экрана."""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("➕ Быстрая запись"), KeyboardButton("📊 Статус сегодня")],
            [KeyboardButton("📅 Неделя"),         KeyboardButton("🔥 Серия")],
            [KeyboardButton("📈 Итог месяца"),    KeyboardButton("❓ Помощь")],
        ],
        resize_keyboard=True,
    )


# ── Notion API (синхронные вызовы запускаем в отдельном потоке, ─────────────
# ── чтобы не блокировать async event loop бота) ─────────────────────────────

def _query_notion_sync(start: date, end: date) -> List[Dict[str, Any]]:
    """Синхронный запрос к Notion с пагинацией."""
    results, cursor = [], None
    while True:
        kwargs: dict = dict(
            database_id=DATABASE_ID,
            filter={"and": [
                {"property": DATE_PROP, "date": {"on_or_after":  start.isoformat()}},
                {"property": DATE_PROP, "date": {"on_or_before": end.isoformat()}},
            ]},
        )
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.databases.query(**kwargs)
        results.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp["next_cursor"]
    return results


async def _query_notion(start: date, end: date) -> List[Dict[str, Any]]:
    """Асинхронная обёртка: не блокирует event loop."""
    return await asyncio.to_thread(_query_notion_sync, start, end)


def _save_sync(clock: float, entry_type: str, topic: str,
               goal: Optional[float], comment: str) -> None:
    """Синхронное сохранение записи в Notion."""
    today_str = _today().isoformat()
    props = {
        ENTRY_PROP:   {"title":        [{"text": {"content": f"{topic} — {entry_type}"}}]},
        DATE_PROP:    {"date":         {"start": today_str}},
        CLOCK_PROP:   {"number":       clock},
        TYPE_PROP:    {"select":       {"name": entry_type}},
        TOPIC_PROP:   {"multi_select": [{"name": topic}]},
        COMMENT_PROP: {"rich_text":    [{"text": {"content": comment}}]},
    }
    if goal is not None:
        props[GOAL_PROP] = {"number": goal}
    notion.pages.create(parent={"database_id": DATABASE_ID}, properties=props)


async def _save(clock: float, entry_type: str, topic: str,
                goal: Optional[float], comment: str) -> None:
    """Асинхронная обёртка: не блокирует event loop."""
    await asyncio.to_thread(_save_sync, clock, entry_type, topic, goal, comment)


# ── Парсеры полей страниц Notion ──────────────────────────────────────────

def _num(page: Dict[str, Any], name: str) -> Optional[float]:
    return page["properties"].get(name, {}).get("number")

def _title(page: Dict[str, Any], name: str) -> str:
    items = page["properties"].get(name, {}).get("title", [])
    return items[0]["plain_text"] if items else ""

def _select(page: Dict[str, Any], name: str) -> str:
    sel = page["properties"].get(name, {}).get("select")
    return sel["name"] if sel else ""

def _date_str(page: Dict[str, Any], name: str) -> Optional[str]:
    d = page["properties"].get(name, {}).get("date")
    return d["start"] if d else None


def _bar(clock: float, goal: float, width: int = 10) -> str:
    """Прогресс-бар заданной ширины (по умолчанию 10 символов)."""
    if goal <= 0:
        return ""
    pct    = min(clock / goal, 1.0)
    filled = round(pct * width)
    return "█" * filled + "░" * (width - filled) + f" {round(pct * 100)}%"


def _reply_saved(clock, entry_type, topic, goal, comment) -> str:
    goal_text = f"{goal} ч" if goal else "—"
    bar       = f"\n  {_bar(clock, goal)}" if goal else ""
    return (
        f"{D}\n"
        f"✅ ЗАПИСАНО\n"
        f"{D}\n"
        f"📅  {_today().strftime('%d.%m.%Y')}\n"
        f"⏱  {clock} ч  ▸  {entry_type}  ▸  {topic}\n"
        f"🎯  Цель: {goal_text}{bar}\n"
        f"💬  {comment or '—'}\n"
        f"{D}"
    )


def _parse(text: str) -> Tuple[float, str, str, Optional[float], str]:
    payload = text.removeprefix("/log").strip()
    if not payload:
        raise ValueError("❌ Формат: /log 2.5 Practice SQL 3 | комментарий")
    main, comment = payload, ""
    if "|" in payload:
        main, comment = payload.split("|", 1)
        comment = comment.strip()
    parts = shlex.split(main)
    if len(parts) < 3:
        raise ValueError("❌ Нужно минимум 3 аргумента: часы тип тема")
    clock = float(parts[0])
    if not (0 < clock <= 24):
        raise ValueError("❌ Часы должны быть в диапазоне (0, 24].")
    entry_type = parts[1]
    topic      = parts[2]
    goal = None
    if len(parts) >= 4:
        goal = float(parts[3])
        if goal <= 0:
            raise ValueError("❌ Цель должна быть положительным числом.")
    return clock, entry_type, topic, goal, comment


# ══════════════════════════════════════════════════════════════════════════════
#  КОМАНДЫ
# ══════════════════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_auth(update):
        return
    log_event(
        "command_start",
        user_id=update.effective_user.id if update.effective_user else None,
        chat_id=update.effective_chat.id if update.effective_chat else None,
    )
    await update.message.reply_text(
        f"{D}\n"
        f"     NOTION LOGGER 📓\n"
        f"{D}\n\n"
        f"Логирую твою учёбу в Notion.\n"
        f"Используй кнопки внизу 👇\n\n"
        f"Быстрая справка — /help\n"
        f"{D}",
        reply_markup=_main_menu(),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_auth(update):
        return
    await update.message.reply_text(
        f"{D}\n"
        f"📝  ЗАПИСЬ\n"
        f"{D}\n"
        f"➕ Кнопка или /quick — через меню\n"
        f"/log [ч] [тип] [тема] [цель] | [комм]\n"
        f"Пример: /log 2.5 Practice SQL 3 | JOIN\n\n"
        f"{D}\n"
        f"📊  СТАТИСТИКА\n"
        f"{D}\n"
        f"/status — сегодня\n"
        f"/week — эта неделя\n"
        f"/summary — месяц\n"
        f"/streak — серия дней\n\n"
        f"{D}\n"
        f"⚙️  НАСТРОЙКИ\n"
        f"{D}\n"
        f"/topics — темы и типы\n"
        f"/addtopic [название]\n"
        f"/removetopic [название]\n"
        f"/addtype [название]\n"
        f"/removetype [название]\n"
        f"/health — технический статус\n"
        f"/debug — отладочная информация\n"
        f"{D}",
        reply_markup=_main_menu(),
    )


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_auth(update):
        return
    log_event(
        "command_ping",
        user_id=update.effective_user.id if update.effective_user else None,
        chat_id=update.effective_chat.id if update.effective_chat else None,
    )
    await update.message.reply_text("🏓 Жив! (версия бота: v" + __version__ + ")", reply_markup=_main_menu())


async def health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Быстрый технический /health для проверки доступности бота и Notion."""
    if not await _check_auth(update):
        return
    try:
        # Лёгкий запрос в Notion: проверяем только доступ к базе, без тяжёлой выборки
        await asyncio.to_thread(
            notion.databases.retrieve,
            **{"database_id": DATABASE_ID},
        )
        status = "✅ OK"
        details = "Telegram и Notion доступны."
    except Exception as exc:  # noqa: BLE001
        logger.exception("Healthcheck failed")
        status = "⚠️ DEGRADED"
        details = f"Проблема с Notion: {type(exc).__name__}"
    log_event(
        "command_health",
        user_id=update.effective_user.id if update.effective_user else None,
        chat_id=update.effective_chat.id if update.effective_chat else None,
        status=status,
    )
    await update.message.reply_text(
        f"{D}\n"
        f"🔍 HEALTHCHECK\n"
        f"{D}\n"
        f"Статус: {status}\n"
        f"Версия: v{__version__}\n"
        f"{details}\n"
        f"{D}",
        reply_markup=_main_menu(),
    )


async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отладочная информация для владельца бота."""
    if not await _check_auth(update):
        return
    settings = _load_settings()
    reminder_hour_raw = os.getenv("REMINDER_HOUR", "21")
    reminder_minute_raw = os.getenv("REMINDER_MINUTE", "0")
    try:
        reminder_hour = int(reminder_hour_raw)
        reminder_minute = int(reminder_minute_raw)
    except ValueError:
        reminder_hour, reminder_minute = 21, 0
    msg = (
        f"{D}\n"
        f"🛠 DEBUG\n"
        f"{D}\n"
        f"Версия: v{__version__}\n"
        f"TZ: {TIMEZONE_STR}\n"
        f"CHAT_ID настроен: {'да' if CHAT_ID_INT is not None else 'нет'}\n"
        f"DB_ID: {_safe_mask(DATABASE_ID)}\n"
        f"Напоминание: {reminder_hour:02d}:{reminder_minute:02d}\n"
        f"Типов: {len(settings['types'])}, тем: {len(settings['topics'])}\n"
        f"{D}"
    )
    log_event(
        "command_debug",
        user_id=update.effective_user.id if update.effective_user else None,
        chat_id=update.effective_chat.id if update.effective_chat else None,
    )
    await update.message.reply_text(msg, reply_markup=_main_menu())


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Намеренно без авторизации — нужно, чтобы узнать свой ID до настройки CHAT_ID
    await update.message.reply_text(
        f"Твой chat_id: `{update.effective_chat.id}`\n"
        f"Вставь его в .env как CHAT_ID={update.effective_chat.id}",
        parse_mode="Markdown",
    )


# ── Обработка кнопок меню ─────────────────────────────────────────────────

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    if not await _check_auth(update):
        return None
    text = update.message.text
    if text == "➕ Быстрая запись":
        return await quick_start(update, context)
    elif text == "📊 Статус сегодня":
        await status(update, context)
    elif text == "📅 Неделя":
        await week(update, context)
    elif text == "🔥 Серия":
        await streak(update, context)
    elif text == "📈 Итог месяца":
        await summary(update, context)
    elif text == "❓ Помощь":
        await help_cmd(update, context)
    return None


# ── /log ──────────────────────────────────────────────────────────────────

async def log_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_auth(update):
        return
    if not update.message or not update.message.text:
        return
    raw = update.message.text
    try:
        clock, entry_type, topic, goal, comment = _parse(raw)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        log_event(
            "command_log_parse_error",
            user_id=update.effective_user.id if update.effective_user else None,
            chat_id=update.effective_chat.id if update.effective_chat else None,
            text=raw,
            error=str(exc),
        )
        return
    except Exception:
        await update.message.reply_text("❌ Не удалось разобрать команду. /help")
        log_event(
            "command_log_unexpected_parse_error",
            user_id=update.effective_user.id if update.effective_user else None,
            chat_id=update.effective_chat.id if update.effective_chat else None,
            text=raw,
        )
        return
    try:
        await _save(clock, entry_type, topic, goal, comment)
    except Exception as exc:
        logger.exception("Notion save error")
        await update.message.reply_text(f"❌ Ошибка записи в Notion:\n{exc}")
        log_event(
            "command_log_notion_error",
            user_id=update.effective_user.id if update.effective_user else None,
            chat_id=update.effective_chat.id if update.effective_chat else None,
            clock=clock,
            entry_type=entry_type,
            topic=topic,
        )
        return
    log_event(
        "command_log_success",
        user_id=update.effective_user.id if update.effective_user else None,
        chat_id=update.effective_chat.id if update.effective_chat else None,
        clock=clock,
        entry_type=entry_type,
        topic=topic,
        goal=goal,
    )
    await update.message.reply_text(_reply_saved(clock, entry_type, topic, goal, comment))


# ── /quick (кнопки) ───────────────────────────────────────────────────────

def _kb_types() -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(t, callback_data=f"type:{t}")] for t in _get_types()]
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


def _kb_topics() -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(t, callback_data=f"topic:{t}")] for t in _get_topics()]
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


async def quick_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await _check_auth(update):
        return ConversationHandler.END
    log_event(
        "command_quick_start",
        user_id=update.effective_user.id if update.effective_user else None,
        chat_id=update.effective_chat.id if update.effective_chat else None,
    )
    context.user_data.clear()
    await update.message.reply_text("Выбери тип занятия:", reply_markup=_kb_types())
    return CHOOSE_TYPE


async def quick_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    context.user_data["type"] = q.data.split(":", 1)[1]
    await q.edit_message_text("Выбери тему:", reply_markup=_kb_topics())
    return CHOOSE_TOPIC


async def quick_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    context.user_data["topic"] = q.data.split(":", 1)[1]
    await q.edit_message_text(
        "Сколько часов? (например: 1.5)\n"
        "Для отмены: /cancel"
    )
    return ENTER_HOURS


async def quick_hours(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        clock = float(update.message.text.strip().replace(",", "."))
        if not (0 < clock <= 24):
            raise ValueError
        context.user_data["clock"] = clock
    except ValueError:
        await update.message.reply_text("❌ Введи число больше 0 и не более 24, например 1.5")
        return ENTER_HOURS
    await update.message.reply_text(
        "Цель на сегодня в часах? (0 — пропустить)\n"
        "Для отмены: /cancel"
    )
    return ENTER_GOAL


async def quick_goal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        goal = float(update.message.text.strip().replace(",", "."))
        context.user_data["goal"] = goal if goal > 0 else None
    except ValueError:
        await update.message.reply_text("❌ Введи число, например 3")
        return ENTER_GOAL
    await update.message.reply_text(
        "Комментарий? (напиши — чтобы пропустить)\n"
        "Для отмены: /cancel"
    )
    return ENTER_COMMENT


async def quick_comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text    = update.message.text.strip()
    comment = "" if text == "—" else text
    d       = context.user_data
    try:
        await _save(d["clock"], d["type"], d["topic"], d.get("goal"), comment)
    except Exception as exc:
        logger.exception("Notion save error")
        await update.message.reply_text(f"❌ Ошибка записи в Notion:\n{exc}")
        return ConversationHandler.END
    log_event(
        "command_quick_success",
        user_id=update.effective_user.id if update.effective_user else None,
        chat_id=update.effective_chat.id if update.effective_chat else None,
        clock=d["clock"],
        entry_type=d["type"],
        topic=d["topic"],
        goal=d.get("goal"),
    )
    await update.message.reply_text(
        _reply_saved(d["clock"], d["type"], d["topic"], d.get("goal"), comment)
    )
    return ConversationHandler.END


async def quick_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отмена диалога через /cancel или нажатие кнопки главного меню."""
    context.user_data.clear()
    await update.message.reply_text("✋ Диалог отменён.", reply_markup=_main_menu())
    return ConversationHandler.END


async def quick_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отмена диалога через inline-кнопку ❌ Отмена."""
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("✋ Отменено.")
    context.user_data.clear()
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Возвращаюсь в меню.",
        reply_markup=_main_menu(),
    )
    return ConversationHandler.END


# ── /status ───────────────────────────────────────────────────────────────

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_auth(update):
        return
    today   = _today()
    entries = await _query_notion(today, today)

    if not entries:
        await update.message.reply_text(
            f"{D}\n"
            f"📊 СЕГОДНЯ  {today.strftime('%d.%m.%Y')}\n"
            f"{D}\n"
            f"📭 Записей нет\n\n"
            f"Не теряй день — нажми ➕"
        )
        log_event(
            "command_status_empty",
            user_id=update.effective_user.id if update.effective_user else None,
            chat_id=update.effective_chat.id if update.effective_chat else None,
        )
        return

    total, lines = 0.0, []
    for e in entries:
        clock = _num(e, CLOCK_PROP) or 0
        goal  = _num(e, GOAL_PROP)
        entry = _title(e, ENTRY_PROP) or _select(e, TYPE_PROP)
        total += clock
        bar   = f"\n    {_bar(clock, goal)}" if goal else ""
        lines.append(f"  ▸ {entry}  {clock}ч{bar}")

    await update.message.reply_text(
        f"{D}\n"
        f"📊 СЕГОДНЯ  {today.strftime('%d.%m.%Y')}\n"
        f"{D}\n"
        + "\n".join(lines) +
        f"\n{D}\n"
        f"⏱  Итого:  {round(total, 2)} ч"
    )
    log_event(
        "command_status",
        user_id=update.effective_user.id if update.effective_user else None,
        chat_id=update.effective_chat.id if update.effective_chat else None,
        total_hours=round(total, 2),
        entries_count=len(entries),
    )


# ── /week ─────────────────────────────────────────────────────────────────

async def week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_auth(update):
        return
    today      = _today()
    week_start = today - timedelta(days=today.weekday())
    entries    = await _query_notion(week_start, today)

    by_date: dict[str, float] = {}
    for e in entries:
        d = _date_str(e, DATE_PROP)
        if d:
            by_date[d] = by_date.get(d, 0) + (_num(e, CLOCK_PROP) or 0)

    DAY = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"]
    lines, total = [], 0.0
    max_h = max(by_date.values()) if by_date else 1

    for i in range((today - week_start).days + 1):
        day   = week_start + timedelta(days=i)
        hours = by_date.get(day.isoformat(), 0)
        total += hours
        label = f"{DAY[day.weekday()]} {day.strftime('%d.%m')}"
        if hours:
            filled = round((hours / max_h) * 10)
            bar    = "█" * filled + "░" * (10 - filled)
            lines.append(f"  ✅ {label}  {bar}  {round(hours, 2)}ч")
        else:
            mark = "🔴" if day < today else "⬜"
            lines.append(f"  {mark} {label}  ░░░░░░░░░░  —")

    await update.message.reply_text(
        f"{D}\n"
        f"📅 НЕДЕЛЯ  {week_start.strftime('%d.%m')} — {today.strftime('%d.%m')}\n"
        f"{D}\n"
        + "\n".join(lines) +
        f"\n{D}\n"
        f"⏱  Итого:  {round(total, 2)} ч"
    )
    log_event(
        "command_week",
        user_id=update.effective_user.id if update.effective_user else None,
        chat_id=update.effective_chat.id if update.effective_chat else None,
        total_hours=round(total, 2),
    )


# ── /summary (итог месяца) ────────────────────────────────────────────────

async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_auth(update):
        return
    today       = _today()
    month_start = today.replace(day=1)
    entries     = await _query_notion(month_start, today)

    if not entries:
        await update.message.reply_text(
            f"{D}\n📈 МЕСЯЦ ПУСТ\n{D}\nНачни логировать — ➕"
        )
        return

    total_hours  = 0.0
    by_type:  dict[str, float] = {}
    by_topic: dict[str, float] = {}
    days_active: set[str] = set()

    for e in entries:
        clock      = _num(e, CLOCK_PROP) or 0
        etype      = _select(e, TYPE_PROP)
        topic_list = e["properties"].get(TOPIC_PROP, {}).get("multi_select", [])
        d          = _date_str(e, DATE_PROP)
        total_hours += clock
        if etype:
            by_type[etype] = by_type.get(etype, 0) + clock
        for t in topic_list:
            n = t.get("name", "")
            by_topic[n] = by_topic.get(n, 0) + clock
        if d:
            days_active.add(d)

    days_passed = (today - month_start).days + 1
    active_days = len(days_active)
    avg         = round(total_hours / active_days, 2) if active_days else 0
    month_name  = today.strftime("%B %Y").upper()

    max_t = max(by_type.values()) if by_type else 1
    type_lines = "\n".join(
        f"  {'█' * round((v/max_t)*10)}{'░' * (10-round((v/max_t)*10))}  {k}  {round(v,2)}ч"
        for k, v in sorted(by_type.items(), key=lambda x: -x[1])
    )

    topic_lines = "\n".join(
        f"  {i+1}. {k}  —  {round(v,2)}ч"
        for i, (k, v) in enumerate(sorted(by_topic.items(), key=lambda x: -x[1])[:5])
    )

    await update.message.reply_text(
        f"{D}\n"
        f"📈 {month_name}\n"
        f"{D}\n"
        f"⏱  Часов:      {round(total_hours, 2)}\n"
        f"📆  Дней:       {active_days}/{days_passed}\n"
        f"📊  В среднем:  {avg}ч/день\n"
        f"{D}\n"
        f"🗂  ПО ТИПУ\n{type_lines}\n"
        f"{D}\n"
        f"🎯  ТОП ТЕМЫ\n{topic_lines}\n"
        f"{D}"
    )
    log_event(
        "command_summary",
        user_id=update.effective_user.id if update.effective_user else None,
        chat_id=update.effective_chat.id if update.effective_chat else None,
        total_hours=round(total_hours, 2),
        active_days=active_days,
        days_passed=days_passed,
    )


# ── /streak ───────────────────────────────────────────────────────────────

async def streak(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_auth(update):
        return
    today = _today()
    # Запрашиваем данные за 366 дней — покрывает любую реальную серию
    # (был баг: раньше только 60 дней, серия > 60 дн. считалась неверно)
    entries = await _query_notion(today - timedelta(days=365), today)
    active  = {_date_str(e, DATE_PROP) for e in entries if _date_str(e, DATE_PROP)}
    check   = today if today.isoformat() in active else today - timedelta(days=1)
    count   = 0
    while check.isoformat() in active:
        count += 1
        check -= timedelta(days=1)

    if count == 0:
        msg = "💀 Серия сброшена. Залогируй сегодня — нажми ➕"
    elif count < 3:
        msg = f"🔥 {count} дн. подряд. Начало положено."
    elif count < 7:
        msg = f"🔥 {count} дн. подряд. Разгоняешься."
    elif count < 14:
        msg = f"🔥🔥 {count} дн. подряд. Уже привычка."
    else:
        msg = f"🔥🔥🔥 {count} дн. подряд. Машина."
    log_event(
        "command_streak",
        user_id=update.effective_user.id if update.effective_user else None,
        chat_id=update.effective_chat.id if update.effective_chat else None,
        streak_days=count,
    )
    await update.message.reply_text(msg)


# ── /topics, /addtopic, /removetopic, /addtype, /removetype ──────────────

async def topics_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_auth(update):
        return
    s = _load_settings()
    types_text  = "\n".join(f"  • {t}" for t in s["types"])
    topics_text = "\n".join(f"  • {t}" for t in s["topics"])
    await update.message.reply_text(
        f"{D}\n"
        f"🗂 ТИПЫ\n{types_text}\n"
        f"{D}\n"
        f"🎯 ТЕМЫ\n{topics_text}\n"
        f"{D}\n\n"
        f"Добавить тему: /addtopic Название\n"
        f"Удалить тему:  /removetopic Название\n"
        f"Добавить тип:  /addtype Название\n"
        f"Удалить тип:   /removetype Название"
    )
    log_event(
        "command_topics",
        user_id=update.effective_user.id if update.effective_user else None,
        chat_id=update.effective_chat.id if update.effective_chat else None,
        types_count=len(s["types"]),
        topics_count=len(s["topics"]),
    )


async def addtopic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_auth(update):
        return
    if not context.args:
        await update.message.reply_text("❌ Используй: /addtopic НазваниеТемы")
        return
    name = " ".join(context.args).strip()
    if len(name) > MAX_NAME_LEN:
        await update.message.reply_text(f"❌ Слишком длинное название (макс {MAX_NAME_LEN} символов).")
        return
    s = _load_settings()
    if name in s["topics"]:
        await update.message.reply_text(f"⚠️ Тема «{name}» уже есть.")
        return
    s["topics"].append(name)
    _save_settings(s)
    await update.message.reply_text(f"✅ Тема «{name}» добавлена. Теперь она в /quick")
    log_event(
        "command_addtopic",
        user_id=update.effective_user.id if update.effective_user else None,
        chat_id=update.effective_chat.id if update.effective_chat else None,
        name=name,
    )


async def removetopic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_auth(update):
        return
    if not context.args:
        await update.message.reply_text("❌ Используй: /removetopic НазваниеТемы")
        return
    name = " ".join(context.args).strip()
    s = _load_settings()
    if name not in s["topics"]:
        await update.message.reply_text(f"⚠️ Темы «{name}» нет в списке.")
        return
    s["topics"].remove(name)
    _save_settings(s)
    await update.message.reply_text(f"🗑 Тема «{name}» удалена.")
    log_event(
        "command_removetopic",
        user_id=update.effective_user.id if update.effective_user else None,
        chat_id=update.effective_chat.id if update.effective_chat else None,
        name=name,
    )


async def addtype(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_auth(update):
        return
    if not context.args:
        await update.message.reply_text("❌ Используй: /addtype НазваниеТипа")
        return
    name = " ".join(context.args).strip()
    if len(name) > MAX_NAME_LEN:
        await update.message.reply_text(f"❌ Слишком длинное название (макс {MAX_NAME_LEN} символов).")
        return
    s = _load_settings()
    if name in s["types"]:
        await update.message.reply_text(f"⚠️ Тип «{name}» уже есть.")
        return
    s["types"].append(name)
    _save_settings(s)
    await update.message.reply_text(f"✅ Тип «{name}» добавлен. Теперь он в /quick")
    log_event(
        "command_addtype",
        user_id=update.effective_user.id if update.effective_user else None,
        chat_id=update.effective_chat.id if update.effective_chat else None,
        name=name,
    )


async def removetype(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_auth(update):
        return
    if not context.args:
        await update.message.reply_text("❌ Используй: /removetype НазваниеТипа")
        return
    name = " ".join(context.args).strip()
    s = _load_settings()
    if name not in s["types"]:
        await update.message.reply_text(f"⚠️ Типа «{name}» нет в списке.")
        return
    s["types"].remove(name)
    _save_settings(s)
    await update.message.reply_text(f"🗑 Тип «{name}» удалён.")
    log_event(
        "command_removetype",
        user_id=update.effective_user.id if update.effective_user else None,
        chat_id=update.effective_chat.id if update.effective_chat else None,
        name=name,
    )


# ── Ежедневное напоминание ─────────────────────────────────────────────────

async def daily_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not CHAT_ID_INT:
        return
    today   = _today()
    entries = await _query_notion(today, today)
    if entries:
        total = sum(_num(e, CLOCK_PROP) or 0 for e in entries)
        await context.bot.send_message(
            chat_id=CHAT_ID_INT,
            text=(
                f"{D}\n"
                f"🌙 ИТОГ ДНЯ\n"
                f"{D}\n"
                f"⏱  Залогировано: {round(total, 2)} ч\n"
                f"📅  {today.strftime('%d.%m.%Y')}\n\n"
                f"Красавчик. Если есть ещё — жми ➕\n"
                f"{D}"
            ),
        )
        log_event(
            "job_daily_reminder_sent_with_entries",
            chat_id=CHAT_ID_INT,
            total_hours=round(total, 2),
        )
    else:
        await context.bot.send_message(
            chat_id=CHAT_ID_INT,
            text=(
                f"{D}\n"
                f"⚠️ ДЕНЬ ЕЩЁ НЕ ЗАПИСАН\n"
                f"{D}\n"
                f"📅  {today.strftime('%d.%m.%Y')}\n\n"
                f"Не дай дню пройти впустую.\n"
                f"Жми ➕ и залогируй.\n"
                f"{D}"
            ),
        )
        log_event(
            "job_daily_reminder_sent_without_entries",
            chat_id=CHAT_ID_INT,
        )


# ── error handler ─────────────────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error", exc_info=context.error)
    # Пишем в структурный лог минимальную инфу, не трогая чувствительные данные
    log_event(
        "unhandled_error",
        error_type=type(context.error).__name__ if getattr(context, "error", None) else None,
    )
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("❌ Внутренняя ошибка. Попробуй позже.")


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Аккуратный ответ на неизвестные команды /что-то."""
    if not await _check_auth(update):
        return
    if not update.message or not update.message.text:
        return
    cmd = update.message.text.split()[0]
    log_event(
        "unknown_command",
        user_id=update.effective_user.id if update.effective_user else None,
        chat_id=update.effective_chat.id if update.effective_chat else None,
        command=cmd,
    )
    await update.message.reply_text(
        f"❓ Не знаю такую команду: {cmd}\n"
        f"Посмотри доступные команды: /help",
        reply_markup=_main_menu(),
    )


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Фильтр для текстов кнопок главного меню
    menu_filter = filters.Regex(
        "^(" + "|".join(re.escape(b) for b in MENU_BUTTONS) + ")$"
    )
    # Фильтр: текстовые сообщения, которые НЕ являются командами и НЕ являются
    # кнопками меню. Используется в состояниях диалога, чтобы нажатие кнопки
    # главного меню не перехватывалось состоянием и уходило в fallback.
    not_menu_text = filters.TEXT & ~filters.COMMAND & ~menu_filter

    quick_conv = ConversationHandler(
        entry_points=[
            CommandHandler("quick", quick_start),
            MessageHandler(filters.Regex("^➕ Быстрая запись$"), quick_start),
        ],
        states={
            CHOOSE_TYPE: [
                CallbackQueryHandler(quick_cancel_cb, pattern="^cancel$"),
                CallbackQueryHandler(quick_type,      pattern="^type:"),
            ],
            CHOOSE_TOPIC: [
                CallbackQueryHandler(quick_cancel_cb, pattern="^cancel$"),
                CallbackQueryHandler(quick_topic,     pattern="^topic:"),
            ],
            ENTER_HOURS:   [MessageHandler(not_menu_text, quick_hours)],
            ENTER_GOAL:    [MessageHandler(not_menu_text, quick_goal)],
            ENTER_COMMENT: [MessageHandler(not_menu_text, quick_comment)],
        },
        fallbacks=[
            CommandHandler("cancel", quick_cancel),
            # Нажатие любой кнопки главного меню отменяет диалог
            MessageHandler(menu_filter, quick_cancel),
        ],
    )

    app.add_handler(CommandHandler("start",       start))
    app.add_handler(CommandHandler("help",        help_cmd))
    app.add_handler(CommandHandler("ping",        ping))
    app.add_handler(CommandHandler("health",      health))
    app.add_handler(CommandHandler("debug",       debug))
    app.add_handler(CommandHandler("myid",        myid))
    app.add_handler(CommandHandler("log",         log_entry))
    app.add_handler(CommandHandler("status",      status))
    app.add_handler(CommandHandler("week",        week))
    app.add_handler(CommandHandler("summary",     summary))
    app.add_handler(CommandHandler("streak",      streak))
    app.add_handler(CommandHandler("topics",      topics_cmd))
    app.add_handler(CommandHandler("addtopic",    addtopic))
    app.add_handler(CommandHandler("removetopic", removetopic))
    app.add_handler(CommandHandler("addtype",     addtype))
    app.add_handler(CommandHandler("removetype",  removetype))
    app.add_handler(quick_conv)

    # Обработчик кнопок меню (текстовые сообщения)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_handler))
    # Аккуратный обработчик неизвестных /команд
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    app.add_error_handler(error_handler)

    # Ежедневное напоминание
    # Время задаётся через .env: REMINDER_HOUR (default=21), REMINDER_MINUTE (default=0)
    # Часовой пояс берётся из TIMEZONE (default='Europe/Moscow')
    if CHAT_ID_INT:
        try:
            hour   = int(os.getenv("REMINDER_HOUR",   "21"))
            minute = int(os.getenv("REMINDER_MINUTE", "0"))
        except ValueError:
            hour, minute = 21, 0
        reminder_time = datetime.time(hour, minute, 0, tzinfo=TZ)
        app.job_queue.run_daily(daily_reminder, time=reminder_time)

    app.run_polling()


if __name__ == "__main__":
    main()
