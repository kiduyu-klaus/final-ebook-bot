"""
bot.py  —  EBook Telegram Bot  (polling mode)
Run with:  python3 bot.py

Commands:
  /start              Welcome message + library size
  /help               Usage instructions
  /search <query>     Search by title keywords
  /reindex            Re-scan EPUB folder (admin only)

Plain text messages are also treated as search queries.
"""

import io
import logging
import threading
import urllib.parse
from typing import List, Dict

import requests
import telebot
from telebot import types

from config import (
    BOT_TOKEN,
    RESULTS_PER_PAGE, MAX_RESULTS,
    BOT_NAME, ADMIN_IDS,
)
from indexer import load_books, build_index, save_index
from db import get_book_details, clear_cache

# ── Logging ───────────────────────────────────────────────────────────────────
import os as _os
_LOG_DIR  = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "logs")
_os.makedirs(_LOG_DIR, exist_ok=True)
_BOT_LOG  = _os.path.join(_LOG_DIR, "bot.log")

_log_formatter = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s")

_file_handler   = logging.FileHandler(_BOT_LOG, encoding="utf-8")
_file_handler.setFormatter(_log_formatter)

_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_log_formatter)

logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _stream_handler])
log = logging.getLogger(__name__)
log.info("Logging to %s", _BOT_LOG)

# ── Bot object ────────────────────────────────────────────────────────────────
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

_BOT_USERNAME: str = ""

# ── Session store  {user_id: {"results": [...], "page": int}} ─────────────────
# Keeps the last search result per user so pagination callbacks work.
_sessions: Dict[int, dict] = {}
_sessions_lock = threading.Lock()


# ══════════════════════════════════════════════════════════════════════════════
#  Search helpers
# ══════════════════════════════════════════════════════════════════════════════

def search_books(query: str) -> List[dict]:
    """
    Return books whose title contains EVERY word in the query.
    Empty query → return all books (up to MAX_RESULTS).
    """
    books = load_books()
    if not query.strip():
        return books[:MAX_RESULTS]

    words = query.lower().split()

    def matches(book: dict) -> bool:
        return all(w in book["_search"] for w in words)

    return [b for b in books if matches(b)][:MAX_RESULTS]


def search_books_by_author(query: str) -> List[dict]:
    """
    Return books whose author name contains every word in the query.
    Uses the _author_search field built by the indexer — no DB call needed.
    Falls back to a URL-derived author string for legacy index entries that
    pre-date the author field.
    """
    if not query.strip():
        return []

    words = query.lower().split()
    books = load_books()
    matched: List[dict] = []

    for book in books:
        # Prefer the pre-computed index field; fall back to URL path parsing
        author_str = book.get("_author_search") or ""
        if not author_str:
            parts = book.get("url", "").replace("\\", "/").split("/")
            author_str = parts[-2].replace("_", " ").lower() if len(parts) >= 2 else ""

        if all(w in author_str for w in words):
            matched.append(book)
        if len(matched) >= MAX_RESULTS:
            break

    return matched


# ══════════════════════════════════════════════════════════════════════════════
#  Message / keyboard builders
# ══════════════════════════════════════════════════════════════════════════════

def book_line(rank: int, book: dict) -> str:
    """Text-only entry showing title, author (if known), and size."""
    author = book.get("author", "")
    author_line = f"\n     ✍️  {author}" if author else ""
    return (
        f"<b>{rank}.</b> {book['title']}{author_line}\n"
        f"     📦 {book['size_mb']} MB"
    )


def build_page(results: List[dict], page: int):
    """
    Returns (text, InlineKeyboardMarkup) for the requested page (0-indexed).

    Keyboard layout:
      [ ⬇️ #1 ]
      [ ⬇️ #2 ]
      ...
      [ ◀ Prev ]  [ Next ▶ ]

    callback_data for download buttons: "dl:<absolute_index>"
    absolute_index is the 0-based position in the full results list.
    """
    total       = len(results)
    total_pages = max(1, -(-total // RESULTS_PER_PAGE))   # ceiling division
    page        = max(0, min(page, total_pages - 1))       # clamp

    start  = page * RESULTS_PER_PAGE
    slice_ = results[start : start + RESULTS_PER_PAGE]

    # ── Build message text ────────────────────────────────────────────────────
    lines = [
        f"<b>Found {total} book{'s' if total != 1 else ''}</b>"
        f"  —  page {page + 1} of {total_pages}\n",
    ]
    for i, book in enumerate(slice_, start=start + 1):
        lines.append(book_line(i, book))
        lines.append("")   # blank separator

    text = "\n".join(lines).strip()

    # ── Build keyboard ────────────────────────────────────────────────────────
    keyboard = types.InlineKeyboardMarkup(row_width=1)

    # One download button per book, labelled with a truncated title
    for abs_idx, book in enumerate(slice_, start=start):
        short_title = book["title"][:40] + "…" if len(book["title"]) > 40 else book["title"]
        keyboard.add(
            types.InlineKeyboardButton(
                f"⬇️  {short_title}",
                callback_data=f"dl:{abs_idx}",   # abs_idx ≤ 199, well within 64-byte limit
            )
        )

    # Navigation row
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("◀ Prev", callback_data=f"pg:{page - 1}"))
    if page < total_pages - 1:
        nav.append(types.InlineKeyboardButton("Next ▶", callback_data=f"pg:{page + 1}"))
    if nav:
        keyboard.row(*nav)

    return text, keyboard


# ══════════════════════════════════════════════════════════════════════════════
#  Shared search dispatcher
# ══════════════════════════════════════════════════════════════════════════════

def _do_search(chat_id: int, user_id: int, query: str) -> None:
    results = search_books(query)

    if not results:
        label = f"<b>{query}</b>" if query else "the library"
        bot.send_message(
            chat_id,
            f"😔 No books found for {label}.\n\n"
            "Try different keywords — partial words work.\n"
            "Use /help for tips.",
        )
        return

    with _sessions_lock:
        _sessions[user_id] = {"results": results, "page": 0}

    text, kb = build_page(results, 0)
    bot.send_message(chat_id, text, reply_markup=kb, disable_web_page_preview=True)


# ══════════════════════════════════════════════════════════════════════════════
#  Book detail card builder
# ══════════════════════════════════════════════════════════════════════════════

def _build_detail_caption(book: dict, db: dict) -> str:
    """
    Build the rich caption shown alongside the cover photo (or as a plain
    message when no cover is available).
    """
    title       = db.get("title") or book["title"]
    authors     = db.get("authors") or []
    category    = db.get("category") or ""
    sub_cat     = db.get("sub_category") or ""
    access      = db.get("book_access") or "Free"
    description = db.get("description") or ""

    author_line   = ", ".join(authors) if authors else "Unknown"
    category_line = category
    if sub_cat:
        category_line += f"  ›  {sub_cat}"

    # Trim description to a readable length
    max_desc = 600
    if len(description) > max_desc:
        description = description[:max_desc].rsplit(" ", 1)[0] + "…"

    lines = [
        f"📖 <b>{title}</b>",
        f"✍️  {author_line}",
    ]
    if category_line:
        lines.append(f"🏷  {category_line}")
    lines.append(f"💰 {access}  •  📦 {book['size_mb']} MB")
    if description:
        lines.append("")
        lines.append(f"<i>{description}</i>")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  Download handler
# ══════════════════════════════════════════════════════════════════════════════

def download_book(chat_id: int, book: dict) -> None:
    """
    1. Fetch rich metadata from the database.
    2. Send a cover photo + detail caption (or text-only card if no cover).
    3. Fetch the EPUB and send it as a document.
    """
    # ── Step 1: DB lookup ─────────────────────────────────────────────────────
    db_info = get_book_details(book["filename"])  # may be None

    # ── Step 2: Detail card ───────────────────────────────────────────────────
    caption = _build_detail_caption(book, db_info or {})
    cover_url = (db_info or {}).get("image", "")

    if cover_url:
        try:
            bot.send_photo(
                chat_id,
                photo=cover_url,
                caption=caption,
                parse_mode="HTML",
            )
        except Exception:
            # Cover fetch failed — fall back to text card
            bot.send_message(chat_id, caption, parse_mode="HTML",
                             disable_web_page_preview=True)
    else:
        bot.send_message(chat_id, caption, parse_mode="HTML",
                         disable_web_page_preview=True)

    # ── Step 3: EPUB download ─────────────────────────────────────────────────
    status = bot.send_message(chat_id, "⏳ Fetching your EPUB, please wait…")

    try:
        response = requests.get(book["url"], timeout=60, stream=True)
        response.raise_for_status()

        file_data      = io.BytesIO(response.content)
        file_data.name = book["filename"]

        bot.send_document(
            chat_id,
            document = file_data,
            caption  = f"📥 <b>{book['title']}</b>",
        )

        bot.delete_message(chat_id, status.message_id)
        log.info("Sent '%s' to chat %s", book["title"], chat_id)

    except requests.exceptions.Timeout:
        bot.edit_message_text(
            "❌ Request timed out — the server took too long to respond.",
            chat_id    = chat_id,
            message_id = status.message_id,
        )
    except requests.exceptions.HTTPError as exc:
        bot.edit_message_text(
            f"❌ Server returned an error: <code>{exc.response.status_code}</code>",
            chat_id    = chat_id,
            message_id = status.message_id,
            parse_mode = "HTML",
        )
    except Exception as exc:
        log.exception("Download failed for '%s'", book["title"])
        bot.edit_message_text(
            f"❌ Download failed:\n<code>{exc}</code>",
            chat_id    = chat_id,
            message_id = status.message_id,
            parse_mode = "HTML",
        )


# ══════════════════════════════════════════════════════════════════════════════
#  Command handlers
# ══════════════════════════════════════════════════════════════════════════════

def _handle_start_payload(msg: types.Message, payload: str) -> bool:
    payload = urllib.parse.unquote_plus(payload)
    if not payload.startswith("dl:"):
        return False
    if not msg.from_user:
        return False

    try:
        abs_idx = int(payload.split(":", 1)[1])
    except (IndexError, ValueError):
        return False

    with _sessions_lock:
        session = _sessions.get(msg.from_user.id)

    if not session or abs_idx >= len(session["results"]):
        bot.send_message(msg.chat.id, "⚠️ Session expired — please search again.")
        return True

    download_book(msg.chat.id, session["results"][abs_idx])
    return True


@bot.message_handler(commands=["start"])
def cmd_start(msg: types.Message):
    if not msg.text or not msg.from_user:
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) > 1 and _handle_start_payload(msg, parts[1].strip()):
        return

    books = load_books()
    bot.send_message(
        msg.chat.id,
        f"👋 Welcome to <b>{BOT_NAME}</b>!\n\n"
        f"Library contains <b>{len(books)}</b> EPUB book(s).\n\n"
        "🔍 <b>Search by title:</b>  <code>/search atomic habits</code>\n"
        "📚 <b>List all books:</b>   <code>/search</code>\n"
        "❓ <b>Help:</b>             /help",
    )


@bot.message_handler(commands=["help"])
def cmd_help(msg: types.Message):
    bot.send_message(
        msg.chat.id,
        f"<b>{BOT_NAME} — Help</b>\n\n"
        "<b>Commands</b>\n"
        "  /start             — Welcome &amp; library size\n"
        "  /search &lt;title&gt;   — Search books (partial words OK)\n"
        "  /search            — List every book\n"
        "  /help              — This message\n\n"
        "<b>Examples</b>\n"
        "  <code>/search harry potter</code>\n"
        "  <code>/search tolkien</code>\n"
        "  <code>/search 1984</code>\n\n"
        "💡 You can also just <b>type any text</b> to search directly.\n"
        "🔗 Tap <b>⬇️ Download</b> to grab a book.",
    )


@bot.message_handler(commands=["search"])
def cmd_search(msg: types.Message):
    if not msg.text or not msg.from_user:
        return
    parts = msg.text.split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else ""
    _do_search(msg.chat.id, msg.from_user.id, query)


@bot.message_handler(commands=["author"])
def cmd_author(msg: types.Message):
    if not msg.text or not msg.from_user:
        return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        bot.send_message(
            msg.chat.id,
            "✍️ Please provide an author name.\n\nExample: <code>/author stephen king</code>",
        )
        return
    query = parts[1].strip()
    results = search_books_by_author(query)
    if not results:
        bot.send_message(
            msg.chat.id,
            f"😔 No books found for author <b>{query}</b>.\n\nTry a partial name — e.g. <code>/author king</code>",
        )
        return
    with _sessions_lock:
        _sessions[msg.from_user.id] = {"results": results, "page": 0}
    text, kb = build_page(results, 0)
    bot.send_message(msg.chat.id, text, reply_markup=kb, disable_web_page_preview=True)


@bot.message_handler(commands=["reindex"])
def cmd_reindex(msg: types.Message):
    if not msg.from_user:
        return
    uid = msg.from_user.id
    if ADMIN_IDS and uid not in ADMIN_IDS:
        bot.send_message(msg.chat.id, "⛔ You are not authorised to run this command.")
        return

    bot.send_message(msg.chat.id, "🔄 Re-scanning book folder, please wait…")
    try:
        idx = build_index()
        save_index(idx)
        clear_cache()   # flush DB lookup cache after re-index
        bot.send_message(
            msg.chat.id,
            f"✅ Index rebuilt — <b>{idx['total']}</b> book(s) found.",
        )
        log.info("Reindex by user %d: %d books", uid, idx["total"])
    except Exception as exc:
        log.exception("Reindex failed")
        bot.send_message(msg.chat.id, f"❌ Reindex failed:\n<code>{exc}</code>")


# Plain text → treat as search query
@bot.message_handler(func=lambda m: m.text and not m.text.startswith("/"))
def handle_text(msg: types.Message):
    if not msg.text or not msg.from_user:
        return
    _do_search(msg.chat.id, msg.from_user.id, msg.text.strip())


# ══════════════════════════════════════════════════════════════════════════════
#  Inline keyboard callback — pagination
# ══════════════════════════════════════════════════════════════════════════════

@bot.callback_query_handler(func=lambda c: c.data.startswith("pg:"))
def handle_page_callback(call: types.CallbackQuery):
    if not call.data or not call.message:
        return
    user_id  = call.from_user.id
    new_page = int(call.data.split(":", 1)[1])

    with _sessions_lock:
        session = _sessions.get(user_id)

    if not session:
        bot.answer_callback_query(call.id, "⚠️ Session expired — please search again.")
        return

    text, kb = build_page(session["results"], new_page)

    with _sessions_lock:
        if user_id in _sessions:
            _sessions[user_id]["page"] = new_page

    try:
        bot.edit_message_text(
            text,
            chat_id                  = call.message.chat.id,
            message_id               = call.message.message_id,
            reply_markup             = kb,
            parse_mode               = "HTML",
            disable_web_page_preview = True,
        )
    except Exception:
        pass   # Telegram raises if message content is unchanged — safe to ignore

    bot.answer_callback_query(call.id)


# ══════════════════════════════════════════════════════════════════════════════
#  Inline keyboard callback — download file
# ══════════════════════════════════════════════════════════════════════════════

@bot.callback_query_handler(func=lambda c: c.data.startswith("dl:"))
def handle_download_callback(call: types.CallbackQuery):
    if not call.data or not call.message:
        return
    user_id = call.from_user.id
    abs_idx = int(call.data.split(":", 1)[1])

    with _sessions_lock:
        session = _sessions.get(user_id)

    if not session or abs_idx >= len(session["results"]):
        bot.answer_callback_query(call.id, "⚠️ Session expired — please search again.")
        return

    book = session["results"][abs_idx]

    # Acknowledge the button tap immediately (Telegram requires this within 10 s)
    bot.answer_callback_query(call.id, f"⬇️ Loading '{book['title'][:30]}'…")
    download_book(call.message.chat.id, book)


def _register_commands() -> None:
    """Register bot commands so they appear in Telegram's menu button."""
    commands = [
        types.BotCommand("start",   "Welcome message & library size"),
        types.BotCommand("search",  "Search books by title keywords"),
        types.BotCommand("author",  "Show books by an author"),
        types.BotCommand("reindex", "Re-scan the EPUB folder (admin)"),
        types.BotCommand("help",    "Show usage instructions"),
    ]
    bot.set_my_commands(commands)
    log.info("Bot commands registered (%d commands)", len(commands))


def main() -> None:
    """Start the bot — called by run_bot.py or directly."""
    _register_commands()
    log.info("%s starting (polling) …", BOT_NAME)
    bot.infinity_polling(
        timeout              = 20,
        long_polling_timeout = 5,
        logger_level         = logging.WARNING,
    )


if __name__ == "__main__":
    main()