import json
import logging
import os
import shlex
import datetime
from datetime import date, timedelta
from typing import Optional
import pytz
from dotenv import load_dotenv
from notion_client import Client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder, CallbackQueryHandler, CommandHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("notion_bot")

BOT_TOKEN    = os.getenv("BOT_TOKEN", "")
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
DATABASE_ID  = os.getenv("DATABASE_ID", "")
# ID твоего чата — бот будет слать напоминания сюда
# Узнать: напиши боту /myid после запуска
CHAT_ID      = os.getenv("CHAT_ID", "")

if not BOT_TOKEN or not NOTION_TOKEN or not DATABASE_ID:
    raise RuntimeError("Задай BOT_TOKEN, NOTION_TOKEN, DATABASE_ID в .env файле.")

notion = Client(auth=NOTION_TOKEN)

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

# ── Типы и темы — хранятся в settings.json ────────────────────────────────
SETTINGS_FILE   = "settings.json"
DEFAULT_TYPES   = ["Practice", "Theory", "Repeat", "Project"]
DEFAULT_TOPICS  = ["SQL", "Python", "Алгоритмы", "Циклы", "Функции", "ООП", "API", "Django"]

def _load_settings() -> dict:
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"types": DEFAULT_TYPES[:], "topics": DEFAULT_TOPICS[:]}

def _save_settings(data: dict) -> None:
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _get_types() -> list:
    return _load_settings()["types"]

def _get_topics() -> list:
    return _load_settings()["topics"]


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

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


def _query_notion(start: date, end: date) -> list[dict]:
    results, cursor = [], None
    while True:
        kwargs = dict(
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


def _num(page: dict, name: str) -> Optional[float]:
    return page["properties"].get(name, {}).get("number")

def _title(page: dict, name: str) -> str:
    items = page["properties"].get(name, {}).get("title", [])
    return items[0]["plain_text"] if items else ""

def _select(page: dict, name: str) -> str:
    sel = page["properties"].get(name, {}).get("select")
    return sel["name"] if sel else ""

def _date_str(page: dict, name: str) -> Optional[str]:
    d = page["properties"].get(name, {}).get("date")
    return d["start"] if d else None

def _bar(clock: float, goal: float) -> str:
    if goal <= 0:
        return ""
    pct    = min(clock / goal, 1.0)
    filled = round(pct * 10)
    return "█" * filled + "░" * (10 - filled) + f" {round(pct * 100)}%"

def _save(clock: float, entry_type: str, topic: str,
          goal: Optional[float], comment: str) -> None:
    today = date.today().isoformat()
    props = {
        ENTRY_PROP:   {"title":        [{"text": {"content": f"{topic} — {entry_type}"}}]},
        DATE_PROP:    {"date":         {"start": today}},
        CLOCK_PROP:   {"number":       clock},
        TYPE_PROP:    {"select":       {"name": entry_type}},
        TOPIC_PROP:   {"multi_select": [{"name": topic}]},
        COMMENT_PROP: {"rich_text":    [{"text": {"content": comment}}]},
    }
    if goal is not None:
        props[GOAL_PROP] = {"number": goal}
    notion.pages.create(parent={"database_id": DATABASE_ID}, properties=props)

def _parse(text: str):
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

def _reply_saved(clock, entry_type, topic, goal, comment) -> str:
    D         = "━━━━━━━━━━━━━━━━━━━━━"
    goal_text = f"{goal} ч" if goal else "—"
    bar       = f"\n  {_bar(clock, goal)}" if goal else ""
    return (
        f"{D}\n"
        f"✅ ЗАПИСАНО\n"
        f"{D}\n"
        f"📅  {date.today().strftime('%d.%m.%Y')}\n"
        f"⏱  {clock} ч  ▸  {entry_type}  ▸  {topic}\n"
        f"🎯  Цель: {goal_text}{bar}\n"
        f"💬  {comment or '—'}\n"
        f"{D}"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  КОМАНДЫ
# ══════════════════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    D = "━━━━━━━━━━━━━━━━━━━━━"
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
    D = "━━━━━━━━━━━━━━━━━━━━━"
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
        f"{D}",
        reply_markup=_main_menu(),
    )

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🏓 Жив!", reply_markup=_main_menu())

async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"Твой chat_id: `{update.effective_chat.id}`\n"
        f"Вставь его в .env как CHAT_ID={update.effective_chat.id}",
        parse_mode="Markdown"
    )


# ── Обработка кнопок меню ─────────────────────────────────────────────────

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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


# ── /log ──────────────────────────────────────────────────────────────────

async def log_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    try:
        clock, entry_type, topic, goal, comment = _parse(update.message.text)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    except Exception:
        await update.message.reply_text("❌ Не удалось разобрать команду. /help")
        return
    try:
        _save(clock, entry_type, topic, goal, comment)
    except Exception as exc:
        logger.exception("Notion save error")
        await update.message.reply_text(f"❌ Ошибка записи в Notion:\n{exc}")
        return
    await update.message.reply_text(_reply_saved(clock, entry_type, topic, goal, comment))


# ── /quick (кнопки) ───────────────────────────────────────────────────────

def _kb_types() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t, callback_data=f"type:{t}")] for t in _get_types()]
    )

def _kb_topics() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t, callback_data=f"topic:{t}")] for t in _get_topics()]
    )

async def quick_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
    await q.edit_message_text("Сколько часов? (например: 1.5)")
    return ENTER_HOURS

async def quick_hours(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        clock = float(update.message.text.strip().replace(",", "."))
        if not (0 < clock <= 24):
            raise ValueError
        context.user_data["clock"] = clock
    except ValueError:
        await update.message.reply_text("❌ Введи число от 0 до 24, например 1.5")
        return ENTER_HOURS
    await update.message.reply_text("Цель на сегодня в часах? (0 — пропустить)")
    return ENTER_GOAL

async def quick_goal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        goal = float(update.message.text.strip().replace(",", "."))
        context.user_data["goal"] = goal if goal > 0 else None
    except ValueError:
        await update.message.reply_text("❌ Введи число, например 3")
        return ENTER_GOAL
    await update.message.reply_text("Комментарий? (напиши — чтобы пропустить)")
    return ENTER_COMMENT

async def quick_comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text    = update.message.text.strip()
    comment = "" if text == "—" else text
    d       = context.user_data
    try:
        _save(d["clock"], d["type"], d["topic"], d.get("goal"), comment)
    except Exception as exc:
        logger.exception("Notion save error")
        await update.message.reply_text(f"❌ Ошибка записи в Notion:\n{exc}")
        return ConversationHandler.END
    await update.message.reply_text(
        _reply_saved(d["clock"], d["type"], d["topic"], d.get("goal"), comment)
    )
    return ConversationHandler.END

async def quick_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Отменено.", reply_markup=_main_menu())
    return ConversationHandler.END


# ── /status ───────────────────────────────────────────────────────────────

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    today   = date.today()
    entries = _query_notion(today, today)
    D = "━━━━━━━━━━━━━━━━━━━━━"

    if not entries:
        await update.message.reply_text(
            f"{D}\n"
            f"📊 СЕГОДНЯ  {today.strftime('%d.%m.%Y')}\n"
            f"{D}\n"
            f"📭 Записей нет\n\n"
            f"Не теряй день — нажми ➕"
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


# ── /week ─────────────────────────────────────────────────────────────────

async def week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    today      = date.today()
    week_start = today - timedelta(days=today.weekday())
    entries    = _query_notion(week_start, today)
    D = "━━━━━━━━━━━━━━━━━━━━━"

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
            filled = round((hours / max_h) * 8)
            bar    = "█" * filled + "░" * (8 - filled)
            lines.append(f"  ✅ {label}  {bar}  {round(hours, 2)}ч")
        else:
            mark = "🔴" if day < today else "⬜"
            lines.append(f"  {mark} {label}  ░░░░░░░░  —")

    await update.message.reply_text(
        f"{D}\n"
        f"📅 НЕДЕЛЯ  {week_start.strftime('%d.%m')} — {today.strftime('%d.%m')}\n"
        f"{D}\n"
        + "\n".join(lines) +
        f"\n{D}\n"
        f"⏱  Итого:  {round(total, 2)} ч"
    )


# ── /summary (итог месяца) ────────────────────────────────────────────────

async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    today       = date.today()
    month_start = today.replace(day=1)
    entries     = _query_notion(month_start, today)
    D = "━━━━━━━━━━━━━━━━━━━━━"

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

    # типы с баром
    max_t = max(by_type.values()) if by_type else 1
    type_lines = "\n".join(
        f"  {'█' * round((v/max_t)*8)}{'░' * (8-round((v/max_t)*8))}  {k}  {round(v,2)}ч"
        for k, v in sorted(by_type.items(), key=lambda x: -x[1])
    )

    # топ 5 тем
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


# ── /streak ───────────────────────────────────────────────────────────────

async def streak(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    today   = date.today()
    entries = _query_notion(today - timedelta(days=60), today)
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
    await update.message.reply_text(msg)


# ── /topics, /addtopic, /removetopic, /addtype, /removetype ──────────────

async def topics_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = _load_settings()
    D = "━━━━━━━━━━━━━━━━━━━━━"
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

async def addtopic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("❌ Используй: /addtopic НазваниеТемы")
        return
    name = " ".join(context.args).strip()
    s    = _load_settings()
    if name in s["topics"]:
        await update.message.reply_text(f"⚠️ Тема «{name}» уже есть.")
        return
    s["topics"].append(name)
    _save_settings(s)
    await update.message.reply_text(f"✅ Тема «{name}» добавлена. Теперь она в /quick")

async def removetopic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("❌ Используй: /removetopic НазваниеТемы")
        return
    name = " ".join(context.args).strip()
    s    = _load_settings()
    if name not in s["topics"]:
        await update.message.reply_text(f"⚠️ Темы «{name}» нет в списке.")
        return
    s["topics"].remove(name)
    _save_settings(s)
    await update.message.reply_text(f"🗑 Тема «{name}» удалена.")

async def addtype(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("❌ Используй: /addtype НазваниеТипа")
        return
    name = " ".join(context.args).strip()
    s    = _load_settings()
    if name in s["types"]:
        await update.message.reply_text(f"⚠️ Тип «{name}» уже есть.")
        return
    s["types"].append(name)
    _save_settings(s)
    await update.message.reply_text(f"✅ Тип «{name}» добавлен. Теперь он в /quick")

async def removetype(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("❌ Используй: /removetype НазваниеТипа")
        return
    name = " ".join(context.args).strip()
    s    = _load_settings()
    if name not in s["types"]:
        await update.message.reply_text(f"⚠️ Типа «{name}» нет в списке.")
        return
    s["types"].remove(name)
    _save_settings(s)
    await update.message.reply_text(f"🗑 Тип «{name}» удалён.")


# ── Ежедневное напоминание ─────────────────────────────────────────────────

async def daily_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not CHAT_ID:
        return
    today   = date.today()
    entries = _query_notion(today, today)
    D = "━━━━━━━━━━━━━━━━━━━━━"
    if entries:
        total = sum(_num(e, CLOCK_PROP) or 0 for e in entries)
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=(
                f"{D}\n"
                f"🌙 ИТОГ ДНЯ\n"
                f"{D}\n"
                f"⏱  Залогировано: {round(total, 2)} ч\n"
                f"📅  {today.strftime('%d.%m.%Y')}\n\n"
                f"Красавчик. Если есть ещё — жми ➕\n"
                f"{D}"
            )
        )
    else:
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=(
                f"{D}\n"
                f"⚠️ ДЕНЬ ЕЩЁ НЕ ЗАПИСАН\n"
                f"{D}\n"
                f"📅  {today.strftime('%d.%m.%Y')}\n\n"
                f"Не дай дню пройти впустую.\n"
                f"Жми ➕ и залогируй.\n"
                f"{D}"
            )
        )


# ── error handler ─────────────────────────────────────────────────────────

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("❌ Внутренняя ошибка. Попробуй позже.")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    quick_conv = ConversationHandler(
        entry_points=[
            CommandHandler("quick", quick_start),
            MessageHandler(filters.Regex("^➕ Быстрая запись$"), quick_start),
        ],
        states={
            CHOOSE_TYPE:   [CallbackQueryHandler(quick_type,    pattern="^type:")],
            CHOOSE_TOPIC:  [CallbackQueryHandler(quick_topic,   pattern="^topic:")],
            ENTER_HOURS:   [MessageHandler(filters.TEXT & ~filters.COMMAND, quick_hours)],
            ENTER_GOAL:    [MessageHandler(filters.TEXT & ~filters.COMMAND, quick_goal)],
            ENTER_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, quick_comment)],
        },
        fallbacks=[CommandHandler("cancel", quick_cancel)],
    )

    app.add_handler(CommandHandler("start",       start))
    app.add_handler(CommandHandler("help",        help_cmd))
    app.add_handler(CommandHandler("ping",        ping))
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

    app.add_error_handler(error_handler)

    # Ежедневное напоминание в 21:00
    if CHAT_ID:
        app.job_queue.run_daily(
            daily_reminder,
            time=datetime.time(12, 0, 0, tzinfo=pytz.utc),
        )
    app.run_polling()


if __name__ == "__main__":
    main()