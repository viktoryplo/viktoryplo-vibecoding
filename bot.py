import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

load_dotenv()

DB_PATH = Path(__file__).resolve().parent / "tracker.db"
TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMG = "https://image.tmdb.org/t/p/w500"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

MEDIA_EMOJI = {"movie": "🎬", "tv": "📺"}


# ── TMDB API ──────────────────────────────────────────────────────────────────

def tmdb(endpoint: str, **params) -> dict:
    params["api_key"] = os.getenv("TMDB_API_KEY", "")
    params.setdefault("language", "ru-RU")
    resp = requests.get(f"{TMDB_BASE}{endpoint}", params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _parse_results(data: dict, default_type: str = "movie") -> list[dict]:
    out: list[dict] = []
    for item in data.get("results", [])[:10]:
        mt = item.get("media_type", default_type)
        if mt not in ("movie", "tv"):
            continue
        title = item.get("title") or item.get("name") or "—"
        date = item.get("release_date") or item.get("first_air_date") or ""
        year = date[:4] if date else "—"
        overview = (item.get("overview") or "")[:150]
        if len(item.get("overview") or "") > 150:
            overview += "…"
        out.append({
            "id": item["id"],
            "type": mt,
            "title": title,
            "year": year,
            "rating": item.get("vote_average", 0),
            "overview": overview,
        })
    return out


def search_multi(query: str) -> list[dict]:
    return _parse_results(tmdb("/search/multi", query=query))


def get_popular() -> list[dict]:
    return _parse_results(tmdb("/movie/popular"))


def get_trending() -> list[dict]:
    return _parse_results(tmdb("/trending/all/day"))


def get_top_rated() -> list[dict]:
    return _parse_results(tmdb("/movie/top_rated"))


# ── Database ──────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with closing(_conn()) as c:
        cur = c.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                uid         INTEGER NOT NULL,
                tmdb_id     INTEGER NOT NULL,
                title       TEXT NOT NULL,
                media_type  TEXT NOT NULL,
                year        TEXT,
                tmdb_rating REAL,
                added_at    TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ratings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                uid         INTEGER NOT NULL,
                tmdb_id     INTEGER NOT NULL,
                title       TEXT NOT NULL,
                media_type  TEXT NOT NULL,
                year        TEXT,
                score       INTEGER NOT NULL,
                rated_at    TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                uid      INTEGER NOT NULL,
                username TEXT,
                text     TEXT NOT NULL,
                ts       TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS usage_log (
                id  INTEGER PRIMARY KEY AUTOINCREMENT,
                uid INTEGER NOT NULL,
                cmd TEXT NOT NULL,
                ts  TEXT NOT NULL
            )
        """)
        c.commit()


def log_cmd(uid: int, cmd: str) -> None:
    with closing(_conn()) as c:
        c.execute(
            "INSERT INTO usage_log (uid, cmd, ts) VALUES (?, ?, ?)",
            (uid, cmd, datetime.utcnow().isoformat()),
        )
        c.commit()


def wl_add(uid: int, tmdb_id: int, title: str, media_type: str, year: str, rating: float) -> bool:
    with closing(_conn()) as c:
        if c.execute("SELECT 1 FROM watchlist WHERE uid=? AND tmdb_id=?", (uid, tmdb_id)).fetchone():
            return False
        c.execute(
            "INSERT INTO watchlist (uid, tmdb_id, title, media_type, year, tmdb_rating, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uid, tmdb_id, title, media_type, year, rating, datetime.utcnow().isoformat()),
        )
        c.commit()
        return True


def wl_list(uid: int) -> list[sqlite3.Row]:
    with closing(_conn()) as c:
        return c.execute(
            "SELECT * FROM watchlist WHERE uid=? ORDER BY added_at DESC", (uid,)
        ).fetchall()


def wl_remove(uid: int, item_id: int) -> bool:
    with closing(_conn()) as c:
        cur = c.execute("DELETE FROM watchlist WHERE id=? AND uid=?", (item_id, uid))
        c.commit()
        return cur.rowcount > 0


def rate_item(uid: int, item_id: int, score: int) -> str | None:
    with closing(_conn()) as c:
        row = c.execute("SELECT * FROM watchlist WHERE id=? AND uid=?", (item_id, uid)).fetchone()
        if not row:
            return None
        c.execute(
            "INSERT INTO ratings (uid, tmdb_id, title, media_type, year, score, rated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uid, row["tmdb_id"], row["title"], row["media_type"], row["year"],
             score, datetime.utcnow().isoformat()),
        )
        c.execute("DELETE FROM watchlist WHERE id=?", (item_id,))
        c.commit()
        return row["title"]


def rated_list(uid: int) -> list[sqlite3.Row]:
    with closing(_conn()) as c:
        return c.execute(
            "SELECT * FROM ratings WHERE uid=? ORDER BY rated_at DESC", (uid,)
        ).fetchall()


def save_feedback(uid: int, username: str | None, text: str) -> None:
    with closing(_conn()) as c:
        c.execute(
            "INSERT INTO feedback (uid, username, text, ts) VALUES (?, ?, ?, ?)",
            (uid, username, text, datetime.utcnow().isoformat()),
        )
        c.commit()


def get_stats() -> dict:
    with closing(_conn()) as c:
        return {
            "users": c.execute("SELECT COUNT(DISTINCT uid) FROM usage_log").fetchone()[0],
            "commands": c.execute("SELECT COUNT(*) FROM usage_log").fetchone()[0],
            "wl": c.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0],
            "rated": c.execute("SELECT COUNT(*) FROM ratings").fetchone()[0],
            "fb": c.execute("SELECT COUNT(*) FROM feedback").fetchone()[0],
        }


# ── Formatting ────────────────────────────────────────────────────────────────

def fmt_item(item: dict, idx: int) -> str:
    emoji = MEDIA_EMOJI.get(item["type"], "🎬")
    r = f"⭐ {item['rating']:.1f}" if item.get("rating") else ""
    line = f"{idx}. {emoji} *{item['title']}* ({item['year']}) {r}"
    if item.get("overview"):
        line += f"\n    _{item['overview']}_"
    return line


def fmt_list(items: list[dict], title: str) -> tuple[str, InlineKeyboardMarkup | None]:
    if not items:
        return f"{title}\n\nНичего не найдено.", None

    lines = [f"{title}\n"]
    buttons: list[InlineKeyboardButton] = []
    for i, item in enumerate(items, 1):
        lines.append(fmt_item(item, i))
        buttons.append(
            InlineKeyboardButton(f"➕ {i}", callback_data=f"add_{item['type']}_{item['id']}")
        )

    rows = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
    lines.append("\n_Нажмите ➕, чтобы добавить в «Буду смотреть»_")
    return "\n".join(lines), InlineKeyboardMarkup(rows)


# ── Handlers ──────────────────────────────────────────────────────────────────

MAIN_MENU = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("🔍 Поиск", callback_data="m_search"),
        InlineKeyboardButton("🔥 Популярное", callback_data="m_popular"),
    ],
    [
        InlineKeyboardButton("📈 Тренды", callback_data="m_trending"),
        InlineKeyboardButton("🏆 Лучшее", callback_data="m_top"),
    ],
    [
        InlineKeyboardButton("📋 Буду смотреть", callback_data="m_wl"),
        InlineKeyboardButton("⭐ Просмотрено", callback_data="m_watched"),
    ],
    [InlineKeyboardButton("📖 Помощь", callback_data="m_help")],
])


async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log_cmd(update.effective_user.id, "start")
    await update.message.reply_text(
        f"Привет, {update.effective_user.first_name}! 🎬\n\n"
        "Я — *MovieTracker*, бот для отслеживания фильмов и сериалов.\n"
        "Ищи, сохраняй и оценивай то, что смотришь!\n\n"
        "Выбери действие или введи /help",
        parse_mode="Markdown",
        reply_markup=MAIN_MENU,
    )


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log_cmd(update.effective_user.id, "help")
    await update.message.reply_text(
        "📖 *Команды MovieTracker:*\n\n"
        "*Поиск и каталог:*\n"
        "/search <название> — поиск фильма или сериала\n"
        "/popular — популярные прямо сейчас\n"
        "/trending — тренды дня\n"
        "/top — лучшие по рейтингу TMDB\n\n"
        "*Мои списки:*\n"
        "/watchlist — буду смотреть\n"
        "/watched — уже посмотрел с оценками\n"
        "/rate <#id> <1-10> — оценить и перенести в просмотренное\n"
        "/remove <#id> — убрать из «Буду смотреть»\n\n"
        "*Прочее:*\n"
        "/feedback <текст> — обратная связь\n"
        "/stats — статистика бота",
        parse_mode="Markdown",
    )


async def _send_catalog(update: Update, ctx: ContextTypes.DEFAULT_TYPE, fetch, title: str) -> None:
    try:
        results = fetch()
    except requests.RequestException:
        await update.message.reply_text("⚠️ TMDB временно недоступен. Попробуйте позже.")
        return
    ctx.user_data["cache"] = {it["id"]: it for it in results}
    text, markup = fmt_list(results, title)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)


async def search_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log_cmd(update.effective_user.id, "search")
    query = " ".join(ctx.args).strip() if ctx.args else ""
    if not query:
        await update.message.reply_text(
            "Формат: /search <название>\nПример: /search Интерстеллар"
        )
        return
    try:
        results = search_multi(query)
    except requests.RequestException:
        await update.message.reply_text("⚠️ TMDB временно недоступен. Попробуйте позже.")
        return
    ctx.user_data["cache"] = {it["id"]: it for it in results}
    text, markup = fmt_list(results, f"🔍 Результаты по «{query}»:")
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)


async def popular_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log_cmd(update.effective_user.id, "popular")
    await _send_catalog(update, ctx, get_popular, "🔥 *Популярные фильмы:*")


async def trending_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log_cmd(update.effective_user.id, "trending")
    await _send_catalog(update, ctx, get_trending, "📈 *Тренды дня:*")


async def top_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log_cmd(update.effective_user.id, "top")
    await _send_catalog(update, ctx, get_top_rated, "🏆 *Лучшие по рейтингу:*")


async def watchlist_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log_cmd(update.effective_user.id, "watchlist")
    items = wl_list(update.effective_user.id)
    if not items:
        await update.message.reply_text(
            "📋 Список «Буду смотреть» пуст.\nИщи фильмы через /search и добавляй!"
        )
        return
    lines = ["📋 *Буду смотреть:*\n"]
    for row in items:
        e = MEDIA_EMOJI.get(row["media_type"], "🎬")
        r = f"⭐ {row['tmdb_rating']:.1f}" if row["tmdb_rating"] else ""
        lines.append(f"#{row['id']} {e} *{row['title']}* ({row['year']}) {r}")
    lines.append("\n_/rate <#id> <1-10> — оценить_")
    lines.append("_/remove <#id> — убрать из списка_")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def watched_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log_cmd(update.effective_user.id, "watched")
    items = rated_list(update.effective_user.id)
    if not items:
        await update.message.reply_text("⭐ Вы пока ничего не оценили.")
        return
    lines = ["⭐ *Просмотрено:*\n"]
    for row in items:
        e = MEDIA_EMOJI.get(row["media_type"], "🎬")
        s = "⭐" * min(row["score"], 5)
        lines.append(f"{e} *{row['title']}* ({row['year']}) — {row['score']}/10 {s}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def rate_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log_cmd(update.effective_user.id, "rate")
    if not ctx.args or len(ctx.args) < 2:
        await update.message.reply_text("Формат: /rate <#id> <оценка 1-10>")
        return
    try:
        item_id = int(ctx.args[0].lstrip("#"))
        score = int(ctx.args[1])
    except ValueError:
        await update.message.reply_text("ID и оценка должны быть числами.")
        return
    if not 1 <= score <= 10:
        await update.message.reply_text("Оценка должна быть от 1 до 10.")
        return
    title = rate_item(update.effective_user.id, item_id, score)
    if title:
        s = "⭐" * min(score, 5)
        await update.message.reply_text(
            f"✅ *{title}* — {score}/10 {s}\nПеренесено в «Просмотрено».",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text("Фильм не найден в вашем списке «Буду смотреть».")


async def remove_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log_cmd(update.effective_user.id, "remove")
    if not ctx.args:
        await update.message.reply_text("Формат: /remove <#id>")
        return
    try:
        item_id = int(ctx.args[0].lstrip("#"))
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return
    if wl_remove(update.effective_user.id, item_id):
        await update.message.reply_text("🗑 Удалено из «Буду смотреть».")
    else:
        await update.message.reply_text("Не найдено в вашем списке.")


async def feedback_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log_cmd(update.effective_user.id, "feedback")
    text = " ".join(ctx.args).strip() if ctx.args else ""
    if not text:
        await update.message.reply_text("Формат: /feedback <ваш отзыв>")
        return
    save_feedback(update.effective_user.id, update.effective_user.username, text)
    await update.message.reply_text("✅ Спасибо за отзыв!")


async def stats_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log_cmd(update.effective_user.id, "stats")
    s = get_stats()
    await update.message.reply_text(
        "📊 *Статистика MovieTracker:*\n\n"
        f"👥 Пользователей: {s['users']}\n"
        f"💬 Команд выполнено: {s['commands']}\n"
        f"📋 В «Буду смотреть»: {s['wl']}\n"
        f"⭐ Оценено фильмов: {s['rated']}\n"
        f"📝 Отзывов: {s['fb']}",
        parse_mode="Markdown",
    )


# ── Callback (inline buttons) ────────────────────────────────────────────────

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    cb = query.data
    uid = update.effective_user.id

    # ➕ add to watchlist
    if cb.startswith("add_"):
        parts = cb.split("_", 2)  # add, type, tmdb_id
        if len(parts) < 3:
            await query.answer("Ошибка данных", show_alert=True)
            return
        media_type, tmdb_id = parts[1], int(parts[2])

        cache = ctx.user_data.get("cache", {})
        item = cache.get(tmdb_id)

        if not item:
            try:
                ep = f"/{'movie' if media_type == 'movie' else 'tv'}/{tmdb_id}"
                d = tmdb(ep)
                title = d.get("title") or d.get("name") or "—"
                date = d.get("release_date") or d.get("first_air_date") or ""
                item = {
                    "id": tmdb_id, "type": media_type, "title": title,
                    "year": date[:4] if date else "—",
                    "rating": d.get("vote_average", 0),
                }
            except Exception:
                await query.answer("⚠️ Не удалось получить данные", show_alert=True)
                return

        ok = wl_add(uid, tmdb_id, item["title"], item["type"], item["year"], item.get("rating", 0))
        if ok:
            await query.answer(f"✅ «{item['title']}» добавлен!", show_alert=True)
        else:
            await query.answer(f"Уже в списке: «{item['title']}»", show_alert=True)
        return

    await query.answer()

    # menu buttons
    if cb == "m_search":
        await query.edit_message_text(
            "🔍 Введите команду:\n/search <название фильма или сериала>"
        )

    elif cb in ("m_popular", "m_trending", "m_top"):
        fetch = {"m_popular": get_popular, "m_trending": get_trending, "m_top": get_top_rated}[cb]
        titles = {"m_popular": "🔥 *Популярные фильмы:*", "m_trending": "📈 *Тренды дня:*",
                  "m_top": "🏆 *Лучшие по рейтингу:*"}
        try:
            results = fetch()
        except requests.RequestException:
            await query.edit_message_text("⚠️ TMDB временно недоступен.")
            return
        ctx.user_data["cache"] = {it["id"]: it for it in results}
        text, markup = fmt_list(results, titles[cb])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)

    elif cb == "m_wl":
        items = wl_list(uid)
        if not items:
            await query.edit_message_text("📋 Список пуст. Найди фильм через /search")
            return
        lines = ["📋 *Буду смотреть:*\n"]
        for row in items:
            e = MEDIA_EMOJI.get(row["media_type"], "🎬")
            r = f"⭐ {row['tmdb_rating']:.1f}" if row["tmdb_rating"] else ""
            lines.append(f"#{row['id']} {e} *{row['title']}* ({row['year']}) {r}")
        lines.append("\n_/rate <#id> <1-10> — оценить_")
        await query.edit_message_text("\n".join(lines), parse_mode="Markdown")

    elif cb == "m_watched":
        items = rated_list(uid)
        if not items:
            await query.edit_message_text("⭐ Пока ничего не оценено.")
            return
        lines = ["⭐ *Просмотрено:*\n"]
        for row in items:
            e = MEDIA_EMOJI.get(row["media_type"], "🎬")
            lines.append(f"{e} *{row['title']}* ({row['year']}) — {row['score']}/10")
        await query.edit_message_text("\n".join(lines), parse_mode="Markdown")

    elif cb == "m_help":
        await query.edit_message_text(
            "📖 *Команды:*\n\n"
            "/search · /popular · /trending · /top\n"
            "/watchlist · /watched · /rate · /remove\n"
            "/feedback · /stats",
            parse_mode="Markdown",
        )


async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error: %s", ctx.error)


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN не найден. Создайте .env (см. .env.example)")

    init_db()

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CommandHandler("popular", popular_cmd))
    app.add_handler(CommandHandler("trending", trending_cmd))
    app.add_handler(CommandHandler("top", top_cmd))
    app.add_handler(CommandHandler("watchlist", watchlist_cmd))
    app.add_handler(CommandHandler("watched", watched_cmd))
    app.add_handler(CommandHandler("rate", rate_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("feedback", feedback_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_error_handler(error_handler)

    logger.info("MovieTracker bot started")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
