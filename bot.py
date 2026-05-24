"""
bot.py  —  EBook Telegram Bot  (polling mode)
Run with:  python3 bot.py

Commands:
  /start                Welcome message + library size
  /help                 Usage instructions
  /search <query>       Search by title keywords
  /author <name>        Search by author
  /list_categories      Browse books by category
  /list_subcategories   Browse books by subcategory
  /reindex              Re-scan EPUB folder (admin only)
  /check_ebooks         Check all DB books for missing EPUB files (admin only)

Plain text messages are also treated as search queries.
"""

import io
import json
import logging
import threading
import urllib.parse
from html import escape
from typing import List, Dict, Optional

import requests
import telebot
from telebot import types

from config import (
    BOT_TOKEN,
    RESULTS_PER_PAGE, MAX_RESULTS,
    BOT_NAME, ADMIN_IDS,
    EBOOKS_BASE_URL,
)
from indexer import load_books, build_index, save_index
from db import (
    get_book_details, clear_cache, get_all_categories, get_books_by_category,
    _get_conn, normalize_book_url, get_all_subcategories_with_counts,
    get_books_by_subcategory, get_author_candidates_by_name,
    get_author_details_by_id, get_or_create_user, is_user_banned, increment_user_downloads,
    log_user_download, get_user_download_history, get_user_download_count,
    search_user_downloads, get_user_by_telegram_id,
    add_bookmark, remove_bookmark, get_bookmarks, get_bookmark_count, is_bookmarked,
    get_library_overview,
)

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
#  User registration & access control helpers
# ══════════════════════════════════════════════════════════════════════════════

def _ensure_user_registered(msg: types.Message) -> Optional[dict]:
    """
    Ensure the user is registered in the bot_users table.
    Returns the user dict if successful, None if registration failed.
    """
    if not msg.from_user:
        return None

    user = get_or_create_user(
        telegram_id=msg.from_user.id,
        username=msg.from_user.username,
        first_name=msg.from_user.first_name,
        last_name=msg.from_user.last_name,
        language_code=msg.from_user.language_code,
    )
    return user


def _check_user_access(msg: types.Message) -> bool:
    """
    Check if user has access (not banned).
    Sends a ban message if user is banned.
    Returns True if user has access, False otherwise.
    """
    if not msg.from_user:
        return False

    if is_user_banned(msg.from_user.id):
        bot.send_message(
            msg.chat.id,
            "⛔ You have been banned from using this bot.",
        )
        return False

    return True


def _get_db_user_id(telegram_id: int) -> Optional[int]:
    """
    Get the internal bot_users.id from a Telegram user ID.
    Returns None if user not found.
    """
    user = get_user_by_telegram_id(telegram_id)
    if user:
        log.info(
            "Resolved telegram_id=%s to db_user_id=%s",
            telegram_id,
            user.get("id"),
        )
        return user.get("id")
    log.warning("No bot_users row found for telegram_id=%s", telegram_id)
    return None


def _check_callback_access(call: types.CallbackQuery) -> bool:
    """
    Check if user has access (not banned) for callback queries.
    Returns True if user has access, False otherwise.
    """
    if not call.from_user:
        return False

    if is_user_banned(call.from_user.id):
        bot.answer_callback_query(
            call.id,
            "⛔ You have been banned from using this bot.",
        )
        return False

    return True


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


def _absolute_media_url(raw_url: str) -> str:
    """Resolve relative media paths to absolute URLs."""
    if not raw_url:
        return ""
    value = raw_url.strip()
    if value.startswith(("http://", "https://")):
        return value
    return f"{EBOOKS_BASE_URL.rstrip('/')}/{value.lstrip('/')}"


def send_author_card(chat_id: int, author_row: dict) -> None:
    """Send an author details card from authors table columns."""
    if not author_row:
        return

    name = escape((author_row.get("name") or "Unknown").strip())
    info = (author_row.get("info") or "").strip()
    status = "Active" if int(author_row.get("status") or 0) == 1 else "Inactive"

    lines = [
        f"👤 <b>{name}</b>",
        f"🆔 ID: <code>{author_row.get('id', '')}</code>",
        f"📌 Status: <b>{status}</b>",
    ]
    if info:
        lines.append(f"📝 {escape(info[:800])}")

    card_text = "\n".join(lines)

    buttons = []
    facebook = (author_row.get("facebook_url") or "").strip()
    instagram = (author_row.get("instagram_url") or "").strip()
    youtube = (author_row.get("youtube_url") or "").strip()
    website = (author_row.get("website_url") or "").strip()

    if facebook:
        buttons.append(types.InlineKeyboardButton("Facebook", url=facebook))
    if instagram:
        buttons.append(types.InlineKeyboardButton("Instagram", url=instagram))
    if youtube:
        buttons.append(types.InlineKeyboardButton("YouTube", url=youtube))
    if website:
        buttons.append(types.InlineKeyboardButton("Website", url=website))

    reply_markup = None
    if buttons:
        reply_markup = types.InlineKeyboardMarkup(row_width=2)
        reply_markup.add(*buttons)

    image_url = _absolute_media_url(author_row.get("image") or "")
    if image_url:
        try:
            bot.send_photo(
                chat_id,
                photo=image_url,
                caption=card_text[:1024],  # Telegram caption limit
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
            return
        except Exception as exc:
            log.warning("Failed to send author image card for '%s': %s", name, exc)

    bot.send_message(chat_id, card_text, parse_mode="HTML", reply_markup=reply_markup)


def build_author_candidates_page(query: str, candidates: List[dict], page: int):
    """
    Return (text, InlineKeyboardMarkup) for paginated author disambiguation.
    Buttons format:
      authorpick:<author_id>
      authorpg:<page>
    """
    total = len(candidates)
    total_pages = max(1, -(-total // RESULTS_PER_PAGE))
    page = max(0, min(page, total_pages - 1))

    start = page * RESULTS_PER_PAGE
    slice_ = candidates[start : start + RESULTS_PER_PAGE]

    lines = [
        f"🔎 Multiple authors found for <b>{escape(query)}</b>.",
        f"Please choose the correct author (page {page + 1} of {total_pages}):\n",
    ]
    for i, author in enumerate(slice_, start=start + 1):
        status = "Active" if int(author.get("status") or 0) == 1 else "Inactive"
        lines.append(f"<b>{i}.</b> {escape((author.get('name') or 'Unknown').strip())} ({status})")

    text = "\n".join(lines).strip()

    kb = types.InlineKeyboardMarkup(row_width=1)
    for author in slice_:
        author_name = (author.get("name") or "Unknown").strip()
        kb.add(
            types.InlineKeyboardButton(
                f"👤 {author_name[:45]}",
                callback_data=f"authorpick:{author.get('id')}",
            )
        )

    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("◀ Prev", callback_data=f"authorpg:{page - 1}"))
    if page < total_pages - 1:
        nav.append(types.InlineKeyboardButton("Next ▶", callback_data=f"authorpg:{page + 1}"))
    if nav:
        kb.row(*nav)

    return text, kb


def show_author_and_books(chat_id: int, user_id: int, author_row: dict) -> None:
    """Send selected author card, then show paginated books for that author."""
    author_name = (author_row.get("name") or "").strip()
    if not author_name:
        bot.send_message(chat_id, "⚠️ Invalid author selection.")
        return

    send_author_card(chat_id, author_row)

    results = search_books_by_author(author_name)
    if not results:
        bot.send_message(
            chat_id,
            f"📚 No indexed books found for <b>{escape(author_name)}</b> yet.",
        )
        return

    with _sessions_lock:
        if user_id not in _sessions:
            _sessions[user_id] = {}
        _sessions[user_id]["results"] = results
        _sessions[user_id]["page"] = 0

    text, kb = build_page(results, 0)
    bot.send_message(chat_id, text, reply_markup=kb, disable_web_page_preview=True)


def _build_book_detail_keyboard(db: dict, db_user_id: int = None):
    """
    Build inline keyboard for the detail card.
    Adds a button to browse all books by the primary author when available.
    Adds a bookmark button if db_user_id (internal DB id) is provided.
    """
    if not db:
        return None

    author_id = db.get("primary_author_id")
    authors = db.get("authors") or []
    
    buttons = []
    
    # Author browse button
    if author_id:
        author_name = (authors[0] if authors else "Author").strip()
        if len(author_name) > 30:
            author_name = author_name[:30] + "…"
        buttons.append(
            types.InlineKeyboardButton(
                f"📚 More by {author_name}",
                callback_data=f"bookauthor:{int(author_id)}",
            )
        )
    
    # Bookmark button - requires internal DB user ID, not Telegram ID
    if db_user_id:
        filename = db.get("filename", "")
        if filename:
            bookmarked = is_bookmarked(db_user_id, filename)
            if bookmarked:
                buttons.append(
                    types.InlineKeyboardButton(
                        "🔖 Bookmarked",
                        callback_data=f"bm:del:{filename}",
                    )
                )
            else:
                buttons.append(
                    types.InlineKeyboardButton(
                        "🔖 Bookmark",
                        callback_data=f"bm:add:{filename}",
                    )
                )
    
    if not buttons:
        return None
    
    kb = types.InlineKeyboardMarkup(row_width=1)
    for btn in buttons:
        kb.add(btn)
    return kb


def _build_quick_menu_keyboard(telegram_user_id: Optional[int] = None):
    """
    Build a persistent chat keyboard for commands that do not require extra input.
    Keeps the existing Telegram command menu intact; this is an extra shortcut layer.
    """
    kb = types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        one_time_keyboard=False,
        row_width=3,
        input_field_placeholder="Choose a command",
    )

    kb.add(
        types.KeyboardButton("🏠 Start"),
        types.KeyboardButton("❓ Help"),
        types.KeyboardButton("🔎 Search"),
        types.KeyboardButton("🗂 Categories"),
        types.KeyboardButton("🏷 Subcategories"),
        types.KeyboardButton("📥 My History"),
        types.KeyboardButton("🔖 My Books"),
    )

    if telegram_user_id and ADMIN_IDS and telegram_user_id in ADMIN_IDS:
        kb.add(
            types.KeyboardButton("🔄 Reindex"),
            types.KeyboardButton("🩺 Check Ebooks"),
        )

    return kb


_MENU_LABEL_TO_COMMAND = {
    "🏠 Start": "/start",
    "❓ Help": "/help",
    "🔎 Search": "/search",
    "🗂 Categories": "/list_categories",
    "🏷 Subcategories": "/list_subcategories",
    "📥 My History": "/myhistory",
    "🔖 My Books": "/mybooks",
    "🔄 Reindex": "/reindex",
    "🩺 Check Ebooks": "/check_ebooks",
}


def _dispatch_quick_menu_label(msg: types.Message) -> bool:
    """
    Route emoji quick-menu labels to existing command handlers.
    Returns True when handled, False otherwise.
    """
    if not msg.text:
        return False

    command = _MENU_LABEL_TO_COMMAND.get(msg.text.strip())
    if not command:
        return False

    msg.text = command
    if command == "/start":
        cmd_start(msg)
    elif command == "/help":
        cmd_help(msg)
    elif command == "/search":
        cmd_search(msg)
    elif command == "/list_categories":
        cmd_list_categories(msg)
    elif command == "/list_subcategories":
        cmd_list_subcategories(msg)
    elif command == "/myhistory":
        cmd_myhistory(msg)
    elif command == "/mybooks":
        cmd_mybooks(msg)
    elif command == "/reindex":
        cmd_reindex(msg)
    elif command == "/check_ebooks":
        cmd_check_ebooks(msg)
    else:
        return False

    return True


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


def build_category_page(categories: List[dict], page: int):
    """
    Returns (text, InlineKeyboardMarkup) for a paginated category list.
    Buttons format: "cat:<category_id>"
    """
    total       = len(categories)
    total_pages = max(1, -(-total // RESULTS_PER_PAGE))
    page        = max(0, min(page, total_pages - 1))

    start  = page * RESULTS_PER_PAGE
    slice_ = categories[start : start + RESULTS_PER_PAGE]

    # ── Build message text ────────────────────────────────────────────────────
    lines = [
        f"<b>📚 Browse by Category</b>  —  page {page + 1} of {total_pages}\n",
    ]
    for i, cat in enumerate(slice_, start=start + 1):
        book_count = cat.get("book_count", 0)
        lines.append(f"<b>{i}.</b> {cat['category_name']} ({book_count} books)")

    text = "\n".join(lines).strip()

    # ── Build keyboard ────────────────────────────────────────────────────────
    keyboard = types.InlineKeyboardMarkup(row_width=1)

    for cat in slice_:
        book_count = cat.get("book_count", 0)
        keyboard.add(
            types.InlineKeyboardButton(
                f"🏷  {cat['category_name']} ({book_count})",
                callback_data=f"cat:{cat['id']}",
            )
        )

    # Navigation row
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("◀ Prev", callback_data=f"catpg:{page - 1}"))
    if page < total_pages - 1:
        nav.append(types.InlineKeyboardButton("Next ▶", callback_data=f"catpg:{page + 1}"))
    if nav:
        keyboard.row(*nav)

    return text, keyboard


def build_category_books_page(cat_name: str, books: List[dict], page: int):
    """
    Returns (text, InlineKeyboardMarkup) for books within a category.
    Buttons format: "catdl:<book_index>"
    """
    total       = len(books)
    total_pages = max(1, -(-total // RESULTS_PER_PAGE))
    page        = max(0, min(page, total_pages - 1))

    start  = page * RESULTS_PER_PAGE
    slice_ = books[start : start + RESULTS_PER_PAGE]

    # ── Build message text ────────────────────────────────────────────────────
    lines = [
        f"<b>📖 {cat_name}</b>  —  page {page + 1} of {total_pages}\n",
    ]
    for i, book in enumerate(slice_, start=start + 1):
        authors = ", ".join(book.get("authors", [])) if book.get("authors") else "Unknown"
        lines.append(
            f"<b>{i}.</b> {book['title']}\n"
            f"     ✍️  {authors}\n"
            f"     📖 {book['book_access']}"
        )
        lines.append("")

    text = "\n".join(lines).strip()

    # ── Build keyboard ────────────────────────────────────────────────────────
    keyboard = types.InlineKeyboardMarkup(row_width=1)

    for abs_idx, book in enumerate(slice_, start=start):
        short_title = book["title"][:35] + "…" if len(book["title"]) > 35 else book["title"]
        keyboard.add(
            types.InlineKeyboardButton(
                f"⬇️  {short_title}",
                callback_data=f"catdl:{abs_idx}",
            )
        )

    # Navigation row
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("◀ Prev", callback_data=f"catbookpg:{page - 1}"))
    if page < total_pages - 1:
        nav.append(types.InlineKeyboardButton("Next ▶", callback_data=f"catbookpg:{page + 1}"))
    if nav:
        keyboard.row(*nav)

    return text, keyboard


def build_subcategory_page(subcategories: List[dict], page: int):
    """
    Returns (text, InlineKeyboardMarkup) for a paginated subcategory list.
    Buttons format: "subcat:<subcategory_id>"
    """
    total       = len(subcategories)
    total_pages = max(1, -(-total // RESULTS_PER_PAGE))
    page        = max(0, min(page, total_pages - 1))

    start  = page * RESULTS_PER_PAGE
    slice_ = subcategories[start : start + RESULTS_PER_PAGE]

    # ── Build message text ────────────────────────────────────────────────────
    lines = [
        f"<b>🏷 Browse by Subcategory</b>  —  page {page + 1} of {total_pages}\n",
    ]
    for i, subcat in enumerate(slice_, start=start + 1):
        book_count = subcat.get("book_count", 0)
        lines.append(f"<b>{i}.</b> {subcat['sub_category_name']} ({book_count} books)")

    text = "\n".join(lines).strip()

    # ── Build keyboard ────────────────────────────────────────────────────────
    keyboard = types.InlineKeyboardMarkup(row_width=1)

    for subcat in slice_:
        keyboard.add(
            types.InlineKeyboardButton(
                f"📁  {subcat['sub_category_name']} ({subcat.get('book_count', 0)})",
                callback_data=f"subcat:{subcat['id']}",
            )
        )

    # Navigation row
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("◀ Prev", callback_data=f"subcatpg:{page - 1}"))
    if page < total_pages - 1:
        nav.append(types.InlineKeyboardButton("Next ▶", callback_data=f"subcatpg:{page + 1}"))
    if nav:
        keyboard.row(*nav)

    # Back to categories button
    keyboard.add(
        types.InlineKeyboardButton("⬆️ Back to Categories", callback_data="back_to_cats")
    )

    return text, keyboard


def build_subcategory_books_page(subcat_name: str, books: List[dict], page: int):
    """
    Returns (text, InlineKeyboardMarkup) for books within a subcategory.
    Buttons format: "subcatdl:<book_index>"
    """
    total       = len(books)
    total_pages = max(1, -(-total // RESULTS_PER_PAGE))
    page        = max(0, min(page, total_pages - 1))

    start  = page * RESULTS_PER_PAGE
    slice_ = books[start : start + RESULTS_PER_PAGE]

    # ── Build message text ────────────────────────────────────────────────────
    lines = [
        f"<b>📚 {subcat_name}</b>  —  page {page + 1} of {total_pages}\n",
    ]
    for i, book in enumerate(slice_, start=start + 1):
        authors = ", ".join(book.get("authors", [])) if book.get("authors") else "Unknown"
        lines.append(
            f"<b>{i}.</b> {book['title']}\n"
            f"     ✍️  {authors}\n"
            f"     📖 {book.get('book_access', 'Free')}"
        )
        lines.append("")

    text = "\n".join(lines).strip()

    # ── Build keyboard ────────────────────────────────────────────────────────
    keyboard = types.InlineKeyboardMarkup(row_width=1)

    for abs_idx, book in enumerate(slice_, start=start):
        short_title = book["title"][:35] + "…" if len(book["title"]) > 35 else book["title"]
        keyboard.add(
            types.InlineKeyboardButton(
                f"⬇️  {short_title}",
                callback_data=f"subcatdl:{abs_idx}",
            )
        )

    # Navigation row
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("◀ Prev", callback_data=f"subcatbookpg:{page - 1}"))
    if page < total_pages - 1:
        nav.append(types.InlineKeyboardButton("Next ▶", callback_data=f"subcatbookpg:{page + 1}"))
    if nav:
        keyboard.row(*nav)

    # Back button
    keyboard.add(
        types.InlineKeyboardButton("⬆️ Back to Subcategories", callback_data="back_to_subcats")
    )

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

def _build_download_keyboard(filename: str, db_user_id: int) -> Optional[types.InlineKeyboardMarkup]:
    """
    Build inline keyboard for the download document message.
    Shows Bookmark/My Books buttons for saving and viewing bookmarks.
    Requires db_user_id (internal DB id, not Telegram ID).
    """
    if not filename or not db_user_id:
        return None
    
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    
    # Bookmark toggle button - uses internal DB user ID
    bookmarked = is_bookmarked(db_user_id, filename)
    if bookmarked:
        keyboard.add(
            types.InlineKeyboardButton(
                "🔖 Bookmarked",
                callback_data=f"bm:del:{filename}",
            )
        )
    else:
        keyboard.add(
            types.InlineKeyboardButton(
                "🔖 Bookmark",
                callback_data=f"bm:add:{filename}",
            )
        )
    
    # My Books button
    keyboard.add(
        types.InlineKeyboardButton(
            "📚 My Books",
            callback_data="open_mybooks",
        )
    )
    
    return keyboard


def download_book(chat_id: int, book: dict, telegram_id: int = None) -> None:
    """
    1. Fetch rich metadata from the database.
    2. Send a cover photo + detail caption (or text-only card if no cover).
    3. Fetch the EPUB and send it as a document.
    4. Log download to user_downloads table.
    """
    # ── Step 1: DB lookup ─────────────────────────────────────────────────────
    db_info = get_book_details(book["filename"])  # may be None
    
    # Ensure db_info has filename for bookmark checking
    if db_info is None:
        db_info = {}
    if "filename" not in db_info:
        db_info["filename"] = book["filename"]
    if "title" not in db_info or not db_info["title"]:
        db_info["title"] = book.get("title", "Unknown")

    # Get internal DB user ID from Telegram ID
    db_user_id = _get_db_user_id(telegram_id) if telegram_id else None
    log.info(
        "download_book start: chat_id=%s telegram_id=%s db_user_id=%s file=%s",
        chat_id,
        telegram_id,
        db_user_id,
        book.get("filename"),
    )

    # ── Step 2: Detail card ───────────────────────────────────────────────────
    caption = _build_detail_caption(book, db_info)
    cover_url = db_info.get("image", "")
    detail_kb = _build_book_detail_keyboard(db_info, db_user_id)

    if cover_url:
        try:
            bot.send_photo(
                chat_id,
                photo=cover_url,
                caption=caption,
                parse_mode="HTML",
                reply_markup=detail_kb,
            )
        except Exception:
            # Cover fetch failed — fall back to text card
            bot.send_message(chat_id, caption, parse_mode="HTML",
                             reply_markup=detail_kb,
                             disable_web_page_preview=True)
    else:
        bot.send_message(chat_id, caption, parse_mode="HTML",
                         reply_markup=detail_kb,
                         disable_web_page_preview=True)

    # ── Step 3: EPUB download ─────────────────────────────────────────────────
    status = bot.send_message(chat_id, "⏳ Fetching your EPUB, please wait…")

    try:
        # Build the full download URL.
        # Search results already contain a full HTTP URL; DB/category results
        # contain a relative Windows path (e.g. upload\Author\book.epub).
        raw_url = book["url"]
        if raw_url.startswith("http://") or raw_url.startswith("https://"):
            full_url = raw_url          # already absolute — use as-is
        else:
            url_path = normalize_book_url(raw_url).replace("\\", "/")
            full_url = f"{EBOOKS_BASE_URL.rstrip('/')}/{url_path}"
        response = requests.get(full_url, timeout=60, stream=True)
        response.raise_for_status()

        file_data      = io.BytesIO(response.content)
        file_data.name = book["filename"]

        # Build keyboard with bookmark and mybooks buttons (uses internal DB user ID)
        download_kb = _build_download_keyboard(book["filename"], db_user_id)

        bot.send_document(
            chat_id,
            document = file_data,
            caption  = f"📥 <b>{book['title']}</b>",
            parse_mode = "HTML",
            reply_markup = download_kb,
        )

        # ── Step 4: Increment download counter & log to history ─────────────────────
        # Use internal DB user ID, not Telegram ID
        if db_user_id:
            inc_ok = increment_user_downloads(telegram_id)  # increment by telegram_id
            # Log download to user_downloads table using internal DB id
            db_id = (db_info or {}).get("id")
            log_ok = log_user_download(
                user_id=db_user_id,  # Internal DB id, not Telegram id
                book_id=db_id,
                filename=book["filename"],
                title=book.get("title", db_info.get("title", "Unknown") if db_info else "Unknown"),
            )
            log.info(
                "download logging result: telegram_id=%s db_user_id=%s increment_ok=%s log_ok=%s db_book_id=%s file=%s",
                telegram_id,
                db_user_id,
                inc_ok,
                log_ok,
                db_id,
                book.get("filename"),
            )
        else:
            log.warning(
                "Skipping download logging because db_user_id is missing: telegram_id=%s file=%s",
                telegram_id,
                book.get("filename"),
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
    """Handle /start command with payload for deep links."""
    if not msg.from_user:
        return False

    # Decode URL-encoded payload
    payload = urllib.parse.unquote_plus(payload)
    log.info("Start payload received: '%s' (user: %d)", payload, msg.from_user.id)

    # Handle direct file download from inline search button
    if payload.startswith("file:"):
        parts = payload.split(":", 1)
        if len(parts) < 2:
            log.warning("Invalid file payload format: %s", payload)
            bot.send_message(msg.chat.id, "⚠️ Invalid download link.")
            return True

        filename = parts[1].strip()
        if not filename:
            log.warning("Empty filename in payload: %s", payload)
            bot.send_message(msg.chat.id, "⚠️ Invalid download link.")
            return True

        log.info("Looking for book with filename: '%s'", filename)

        # Load books from index
        books = load_books()
        log.info("Books index loaded: %d books", len(books))

        # Try exact match first
        book = next((b for b in books if b["filename"] == filename), None)

        # If not found, try case-insensitive match
        if not book:
            log.info("Exact match not found, trying case-insensitive match for: '%s'", filename)
            book = next((b for b in books if b["filename"].lower() == filename.lower()), None)

        if book:
            log.info("Download initiated: '%s' (user: %d)", book["title"], msg.from_user.id)
            bot.send_message(msg.chat.id, f"📥 Sending <b>{book['title']}</b>...", parse_mode="HTML")
            download_book(msg.chat.id, book, msg.from_user.id)
            return True
        else:
            log.warning("Book not found: '%s' (user: %d)", filename, msg.from_user.id)
            bot.send_message(
                msg.chat.id,
                f"⚠️ Book not found in library.\n\n"
                f"Filename: <code>{filename}</code>\n\n"
                "Try /reindex if this persists.",
                parse_mode="HTML"
            )
            return True

    # Handle session-based download (from regular search results)
    if payload.startswith("dl:"):
        try:
            abs_idx = int(payload.split(":", 1)[1])
        except (IndexError, ValueError):
            log.warning("Invalid dl payload format: %s", payload)
            return False

        with _sessions_lock:
            session = _sessions.get(msg.from_user.id)

        if not session or abs_idx >= len(session.get("results", [])):
            log.warning("Session expired or invalid index: user=%d, index=%d", msg.from_user.id, abs_idx)
            bot.send_message(msg.chat.id, "⚠️ Session expired — please search again.")
            return True

        download_book(msg.chat.id, session["results"][abs_idx], msg.from_user.id)
        return True

    return False


@bot.message_handler(commands=["start"])
def cmd_start(msg: types.Message):
    if not msg.text or not msg.from_user:
        return

    # Register user and check ban status
    user = _ensure_user_registered(msg)
    if user is None:
        log.warning("Failed to register user %s", msg.from_user.id)
    
    if not _check_user_access(msg):
        return

    parts = msg.text.split(maxsplit=1)
    if len(parts) > 1 and _handle_start_payload(msg, parts[1].strip()):
        return

    books = load_books()
    overview = get_library_overview()

    total_books = int(overview.get("total_books") or 0) or len(books)
    total_authors = int(overview.get("total_authors") or 0)
    top_categories = overview.get("top_categories") or []
    top_subcategories = overview.get("top_subcategories") or []

    def _format_top_list(items: List[dict]) -> str:
        if not items:
            return "• None yet"
        lines = []
        for i, item in enumerate(items[:5], start=1):
            name = escape(str(item.get("name") or "Uncategorized"))
            count = int(item.get("book_count") or 0)
            lines.append(f"{i}. {name} ({count})")
        return "\n".join(lines)

    library_info = (
        "<b>Library Information</b>\n"
        f"• Total books: <b>{total_books}</b>\n"
        f"• Total authors: <b>{total_authors}</b>\n\n"
        "<b>Top 5 Categories</b>\n"
        f"{_format_top_list(top_categories)}\n\n"
        "<b>Top 5 Subcategories</b>\n"
        f"{_format_top_list(top_subcategories)}"
    )

    bot.send_message(
        msg.chat.id,
        f"👋 Welcome to <b>{BOT_NAME}</b>!\n\n"
        f"{library_info}\n\n"
        "<b>Quick Commands</b>\n"
        "• <code>/start</code> — Open welcome/dashboard (example: <code>/start</code>)\n"
        "• <code>/help</code> — Show usage guide (example: <code>/help</code>)\n"
        "• <code>/search &lt;title&gt;</code> — Search by title (example: <code>/search atomic habits</code>)\n"
        "• <code>/search</code> — List all indexed books (example: <code>/search</code>)\n"
        "• <code>/author &lt;name&gt;</code> — Browse by author (example: <code>/author lee child</code>)\n"
        "• <code>/list_categories</code> — Browse by category (example: <code>/list_categories</code>)\n"
        "• <code>/list_subcategories</code> — Browse by subcategory (example: <code>/list_subcategories</code>)\n"
        "• <code>/myhistory</code> — Your download history (example: <code>/myhistory</code>)\n"
        "• <code>/searchhistory &lt;keyword&gt;</code> — Search your history (example: <code>/searchhistory harry</code>)\n"
        "• <code>/mybooks</code> — Your bookmarked books (example: <code>/mybooks</code>)\n"
        "• <code>/reindex</code> — Re-scan EPUB folder (admin only; example: <code>/reindex</code>)\n"
        "• <code>/check_ebooks</code> — Check missing EPUB files (admin only; example: <code>/check_ebooks</code>)\n\n"
        "💡 <b>Tip:</b> You can search for books in any chat by typing <code>@{(bot.get_me().username if not _BOT_USERNAME else _BOT_USERNAME)} keyword</code>",
        reply_markup=_build_quick_menu_keyboard(msg.from_user.id),
    )


@bot.inline_handler(lambda query: True)
def query_text(inline_query):
    try:
        query = inline_query.query.strip()
        # Use existing search logic
        results = search_books(query)
        
        # Pagination for inline results (Telegram supports up to 50 results per request)
        offset = int(inline_query.offset) if inline_query.offset else 0
        limit = 20
        slice_ = results[offset : offset + limit]
        next_offset = str(offset + limit) if len(results) > offset + limit else ""

        inline_results = []
        for i, book in enumerate(slice_):
            # Create a unique ID for this result
            result_id = f"{book['filename']}_{offset + i}"
            
            # Prepare the message text that will be sent when a user clicks the result
            author = book.get("author", "Unknown Author")
            description = f"✍️ {author}\n📦 {book['size_mb']} MB"
            
            # Create the inline result with a deep link button to private chat
            # When user selects this, Telegram opens the private chat with the deep link
            bot_username = bot.get_me().username
            start_payload = f"file:{urllib.parse.quote_plus(book['filename'])}"
            start_url = f"https://t.me/{bot_username}?start={start_payload}"

            # Create inline keyboard with deep link button
            inline_keyboard = types.InlineKeyboardMarkup()
            inline_keyboard.add(
                types.InlineKeyboardButton(
                    "📥 Open in Private Chat to Download",
                    url=start_url
                )
            )

            r = types.InlineQueryResultArticle(
                id=result_id,
                title=book["title"],
                input_message_content=types.InputTextMessageContent(
                    message_text=(
                        f"📖 <b>{book['title']}</b>\n"
                        f"✍️ Author: {author}\n"
                        f"📦 Size: {book['size_mb']} MB\n\n"
                        f"👇 Tap the button below to download this book."
                    ),
                    parse_mode="HTML"
                ),
                reply_markup=inline_keyboard,
                description=description,
            )
            inline_results.append(r)

        bot.answer_inline_query(
            inline_query.id, 
            inline_results, 
            cache_time=300, 
            next_offset=next_offset,
            is_personal=False
        )
    except Exception as e:
        log.error(f"Inline query error: {e}")


@bot.message_handler(commands=["help"])
def cmd_help(msg: types.Message):
    if not msg.from_user:
        return
    
    # Ensure user is registered
    _ensure_user_registered(msg)
    
    if not _check_user_access(msg):
        return
    
    bot.send_message(
        msg.chat.id,
        f"<b>{BOT_NAME} — Help</b>\n\n"
        "<b>Commands</b>\n"
        "  /start             — Welcome &amp; library size\n"
        "  /search &lt;title&gt;   — Search books (partial words OK)\n"
        "  /search            — List every book\n"
        "  /author &lt;name&gt;    — Browse by author\n"
        "  /list_categories   — Browse by category\n"
        "  /help              — This message\n\n"
        "<b>Examples</b>\n"
        "  <code>/search harry potter</code>\n"
        "  <code>/author tolkien</code>\n"
        "  <code>/search 1984</code>\n"
        "  <code>/list_categories</code>\n\n"
        "💡 You can also just <b>type any text</b> to search directly.\n"
        "🔗 Tap <b>⬇️ Download</b> to grab a book.",
        reply_markup=_build_quick_menu_keyboard(msg.from_user.id),
    )


def _build_history_page(db_user_id: int, page: int) -> tuple:
    """
    Build paginated download history page.
    Returns (text, InlineKeyboardMarkup).
    """
    history = get_user_download_history(db_user_id, limit=RESULTS_PER_PAGE, offset=page * RESULTS_PER_PAGE)
    total_count = get_user_download_count(db_user_id)
    total_pages = max(1, -(-total_count // RESULTS_PER_PAGE))
    page = max(0, min(page, total_pages - 1))

    if not history:
        return "📭 <b>No download history yet.</b>\n\nStart downloading books to see your history here!", None

    lines = [
        f"📥 <b>Your Download History</b>\n"
        f"Total: {total_count} download(s) — page {page + 1} of {total_pages}\n",
    ]

    for i, record in enumerate(history, start=page * RESULTS_PER_PAGE + 1):
        title = escape(record.get("title", "Unknown"))
        downloaded_at = record.get("downloaded_at", "")
        if downloaded_at:
            # Format datetime
            if isinstance(downloaded_at, str):
                try:
                    from datetime import datetime
                    dt = datetime.strptime(downloaded_at.split(".")[0], "%Y-%m-%d %H:%M:%S")
                    downloaded_at = dt.strftime("%b %d, %Y")
                except Exception:
                    downloaded_at = downloaded_at[:10]
        lines.append(f"<b>{i}.</b> {title}")
        lines.append(f"    📅 {downloaded_at}")
        lines.append("")

    text = "\n".join(lines).strip()

    # Build keyboard with redownload buttons
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for record in history:
        short_title = record["title"][:35] + "…" if len(record["title"]) > 35 else record["title"]
        keyboard.add(
            types.InlineKeyboardButton(
                f"⬇️ {short_title}",
                callback_data=f"histdl:{record['id']}",
            )
        )

    # Navigation
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("◀ Prev", callback_data=f"histpg:{page - 1}"))
    if page < total_pages - 1:
        nav.append(types.InlineKeyboardButton("Next ▶", callback_data=f"histpg:{page + 1}"))
    if nav:
        keyboard.row(*nav)

    return text, keyboard


def _build_bookmarks_page(user_id: int, page: int) -> tuple:
    """
    Build paginated bookmarks page.
    Returns (text, InlineKeyboardMarkup).
    """
    bookmarks = get_bookmarks(user_id, limit=RESULTS_PER_PAGE, offset=page * RESULTS_PER_PAGE)
    total_count = get_bookmark_count(user_id)
    total_pages = max(1, -(-total_count // RESULTS_PER_PAGE))
    page = max(0, min(page, total_pages - 1))

    if not bookmarks:
        return "📭 <b>No bookmarks yet.</b>\n\nTap '🔖 Bookmark' on any book card to save it here!", None

    lines = [
        f"🔖 <b>Your Bookmarks</b>\n"
        f"Total: {total_count} bookmark(s) — page {page + 1} of {total_pages}\n",
    ]

    for i, record in enumerate(bookmarks, start=page * RESULTS_PER_PAGE + 1):
        title = escape(record.get("title", "Unknown"))
        bookmarked_at = record.get("bookmarked_at", "")
        if bookmarked_at:
            if isinstance(bookmarked_at, str):
                try:
                    from datetime import datetime
                    dt = datetime.strptime(bookmarked_at.split(".")[0], "%Y-%m-%d %H:%M:%S")
                    bookmarked_at = dt.strftime("%b %d, %Y")
                except Exception:
                    bookmarked_at = bookmarked_at[:10]
        lines.append(f"<b>{i}.</b> {title}")
        lines.append(f"    📅 {bookmarked_at}")
        lines.append("")

    text = "\n".join(lines).strip()

    # Build keyboard with download and remove buttons
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for record in bookmarks:
        short_title = record["title"][:30] + "…" if len(record["title"]) > 30 else record["title"]
        # Download button
        keyboard.add(
            types.InlineKeyboardButton(
                f"⬇️ {short_title}",
                callback_data=f"bmdl:{record['filename']}",
            )
        )
        # Remove button
        keyboard.add(
            types.InlineKeyboardButton(
                f"🗑 Remove",
                callback_data=f"bm:del:{record['filename']}",
            )
        )

    # Navigation
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("◀ Prev", callback_data=f"bmpg:{page - 1}"))
    if page < total_pages - 1:
        nav.append(types.InlineKeyboardButton("Next ▶", callback_data=f"bmpg:{page + 1}"))
    if nav:
        keyboard.row(*nav)

    return text, keyboard


@bot.message_handler(commands=["myhistory"])
def cmd_myhistory(msg: types.Message):
    if not msg.from_user:
        return

    # Ensure user is registered
    _ensure_user_registered(msg)

    if not _check_user_access(msg):
        return

    telegram_id = msg.from_user.id
    db_user_id = _get_db_user_id(telegram_id)
    if not db_user_id:
        log.warning("cmd_myhistory: cannot resolve db_user_id for telegram_id=%s", telegram_id)
        bot.send_message(
            msg.chat.id,
            "⚠️ Could not load your history yet. Please try /start and download a book first.",
        )
        return

    text, keyboard = _build_history_page(db_user_id, 0)
    bot.send_message(
        msg.chat.id,
        text,
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


@bot.message_handler(commands=["mybooks"])
def cmd_mybooks(msg: types.Message):
    """Show user's bookmarked books."""
    if not msg.from_user:
        return

    # Ensure user is registered
    _ensure_user_registered(msg)

    if not _check_user_access(msg):
        return

    user_id = msg.from_user.id
    text, keyboard = _build_bookmarks_page(user_id, 0)
    bot.send_message(
        msg.chat.id,
        text,
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


@bot.message_handler(commands=["searchhistory"])
def cmd_searchhistory(msg: types.Message):
    """Search user's download history."""
    if not msg.text or not msg.from_user:
        return

    # Ensure user is registered
    user = _ensure_user_registered(msg)
    if user is None:
        log.warning("Failed to register user %s", msg.from_user.id)

    if not _check_user_access(msg):
        return

    parts = msg.text.split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else ""

    if not query:
        bot.send_message(
            msg.chat.id,
            "🔍 <b>Search Download History</b>\n\n"
            "Usage: <code>/searchhistory keyword</code>\n\n"
            "Example: <code>/searchhistory harry potter</code>",
            parse_mode="HTML",
        )
        return

    telegram_id = msg.from_user.id
    db_user_id = _get_db_user_id(telegram_id)
    if not db_user_id:
        log.warning("cmd_searchhistory: cannot resolve db_user_id for telegram_id=%s", telegram_id)
        bot.send_message(
            msg.chat.id,
            "⚠️ Could not load your history yet. Please try /start and download a book first.",
        )
        return

    results = search_user_downloads(db_user_id, query, limit=50)

    if not results:
        log.info(
            "searchhistory returned no rows: telegram_id=%s db_user_id=%s query='%s'",
            telegram_id,
            db_user_id,
            query,
        )
        bot.send_message(
            msg.chat.id,
            f"😔 No downloads found matching <b>{escape(query)}</b>.\n\n"
            "Try a different keyword.",
            parse_mode="HTML",
        )
        return

    # Store results in session for redownload
    with _sessions_lock:
        if telegram_id not in _sessions:
            _sessions[telegram_id] = {}
        _sessions[telegram_id]["history_results"] = results
        _sessions[telegram_id]["history_page"] = 0

    # Build message
    lines = [
        f"🔍 Found <b>{len(results)}</b> download(s) matching <b>{escape(query)}</b>\n",
    ]

    for i, record in enumerate(results[:RESULTS_PER_PAGE], start=1):
        title = escape(record.get("title", "Unknown"))
        lines.append(f"<b>{i}.</b> {title}")

    text = "\n".join(lines).strip()

    # Build keyboard
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    for i, record in enumerate(results[:RESULTS_PER_PAGE]):
        short_title = record["title"][:35] + "…" if len(record["title"]) > 35 else record["title"]
        keyboard.add(
            types.InlineKeyboardButton(
                f"⬇️ {short_title}",
                callback_data=f"histdl:{record['id']}",
            )
        )

    # Pagination if more than RESULTS_PER_PAGE
    if len(results) > RESULTS_PER_PAGE:
        keyboard.row(
            types.InlineKeyboardButton("Next ▶", callback_data="histnext:1")
        )

    bot.send_message(
        msg.chat.id,
        text,
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_web_page_preview=True,
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
    author_candidates = get_author_candidates_by_name(query, 100)

    if len(author_candidates) > 1:
        with _sessions_lock:
            if msg.from_user.id not in _sessions:
                _sessions[msg.from_user.id] = {}
            _sessions[msg.from_user.id]["author_candidates"] = author_candidates
            _sessions[msg.from_user.id]["author_query"] = query
            _sessions[msg.from_user.id]["author_candidates_page"] = 0

        text, kb = build_author_candidates_page(query, author_candidates, 0)
        bot.send_message(msg.chat.id, text, reply_markup=kb, disable_web_page_preview=True)
        return

    if len(author_candidates) == 1:
        show_author_and_books(msg.chat.id, msg.from_user.id, author_candidates[0])
        return

    # Fallback if no DB author profile matched: use index-only author search.
    results = search_books_by_author(query)
    if not results:
        bot.send_message(
            msg.chat.id,
            f"😔 No books found for author <b>{escape(query)}</b>.\n\nTry a partial name — e.g. <code>/author king</code>",
        )
        return
    with _sessions_lock:
        _sessions[msg.from_user.id] = {"results": results, "page": 0}
    text, kb = build_page(results, 0)
    bot.send_message(msg.chat.id, text, reply_markup=kb, disable_web_page_preview=True)


@bot.message_handler(commands=["list_categories"])
def cmd_list_categories(msg: types.Message):
    if not msg.from_user:
        return
    
    categories = get_all_categories()
    if not categories:
        bot.send_message(msg.chat.id, "😔 No categories available.")
        return
    
    with _sessions_lock:
        _sessions[msg.from_user.id] = {
            "categories": categories,
            "category_page": 0,
            "current_category_id": None,
            "category_books": [],
            "category_books_page": 0,
        }
    
    text, kb = build_category_page(categories, 0)
    bot.send_message(msg.chat.id, text, reply_markup=kb, disable_web_page_preview=True)


@bot.message_handler(commands=["list_subcategories"])
def cmd_list_subcategories(msg: types.Message):
    """List all available subcategories with book counts."""
    if not msg.from_user:
        return
    
    subcategories = get_all_subcategories_with_counts()
    if not subcategories:
        bot.send_message(msg.chat.id, "😔 No subcategories available.")
        return
    
    with _sessions_lock:
        _sessions[msg.from_user.id] = {
            "subcategories": subcategories,
            "subcategory_page": 0,
            "current_subcategory_id": None,
            "subcategory_books": [],
            "subcategory_books_page": 0,
        }
    
    text, kb = build_subcategory_page(subcategories, 0)
    bot.send_message(msg.chat.id, text, reply_markup=kb, disable_web_page_preview=True)


@bot.message_handler(commands=["check_ebooks"])
def cmd_check_ebooks(msg: types.Message):
    """Admin command: Check all DB books for missing EPUB files and save to JSON."""
    if not msg.from_user:
        return
    uid = msg.from_user.id
    if ADMIN_IDS and uid not in ADMIN_IDS:
        bot.send_message(msg.chat.id, "⛔ You are not authorised to run this command.")
        return

    cpu_count = _os.cpu_count() or 4
    max_workers = 50

    bot.send_message(
        msg.chat.id,
        f"🔍 Checking all books in database for missing EPUB files (using {max_workers} parallel workers)...",
    )

    def run_check():
        try:
            # Keep connection open for entire function
            conn = _get_conn()

            # Get all books from DB
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT b.id, b.title, b.url, b.author_ids, c.category_name
                    FROM books b
                    LEFT JOIN categories c ON c.id = b.cat_id
                    WHERE b.status = 1 AND b.url IS NOT NULL AND b.url != ''
                    """
                )
                books = cur.fetchall()

            total_books = len(books)

            # Get author names map (use same connection)
            author_map = {}
            with conn.cursor() as cur:
                cur.execute("SELECT id, name FROM authors WHERE status = 1")
                for row in cur.fetchall():
                    author_map[row["id"]] = row["name"]

            # Prepare all URLs and their metadata
            from db import normalize_book_url
            url_tasks = []
            for book in books:
                # Normalize URL to ensure it starts with 'upload\\' not 'upload77\\' etc
                normalized_url = normalize_book_url(book["url"])
                url_path = normalized_url.replace("\\", "/")
                full_url = f"{EBOOKS_BASE_URL.rstrip('/')}/{url_path}"

                # Build author names
                author_names = []
                if book["author_ids"]:
                    ids = [aid.strip() for aid in book["author_ids"].split(",") if aid.strip().isdigit()]
                    for aid in ids:
                        if aid in author_map:
                            author_names.append(author_map[aid])

                author_str = ", ".join(author_names) if author_names else "Unknown"

                url_tasks.append({
                    "url": full_url,
                    "metadata": {
                        "id": book["id"],
                        "title": book["title"],
                        "author": author_str,
                        "category": book["category_name"] or "Unknown",
                        "original_url": book["url"],
                    }
                })

            # Close DB connection before starting HTTP checks
            conn.close()

            # Concurrent checking with ThreadPoolExecutor
            from concurrent.futures import ThreadPoolExecutor, as_completed
            import time

            missing_ebooks = []
            missing_lock = threading.Lock()
            checked_count = [0]  # Use list for mutability in closure
            progress_lock = threading.Lock()
            progress_msg = [None]  # Mutable holder for progress message object
            spinner = ["⏳", "⌛", "🔄", "🌀"]
            log.info(
                "check_ebooks starting: total_books=%s max_workers=%s cpu_count=%s",
                total_books,
                max_workers,
                cpu_count,
            )

            def check_url(task):
                url = task["url"]
                metadata = task["metadata"]
                try:
                    response = requests.head(url, timeout=8, allow_redirects=True)
                    file_exists = response.status_code == 200
                except Exception:
                    file_exists = False

                with progress_lock:
                    checked_count[0] += 1

                if not file_exists:
                    with missing_lock:
                        missing_ebooks.append({
                            "url": url,
                            **metadata
                        })

                return file_exists

            def build_progress_text(emoji: str) -> str:
                with progress_lock:
                    checked = checked_count[0]
                with missing_lock:
                    missing = len(missing_ebooks)
                return (
                    f"{emoji} Checking books... {checked}/{total_books}\n"
                    f"❌ Missing EPUB files so far: {missing}"
                )

            def update_progress_message(text: str):
                try:
                    if progress_msg[0]:
                        bot.edit_message_text(
                            text,
                            chat_id=msg.chat.id,
                            message_id=progress_msg[0].message_id,
                        )
                    else:
                        progress_msg[0] = bot.send_message(msg.chat.id, text)
                except Exception:
                    # Fallback: send a new message if edit fails
                    try:
                        progress_msg[0] = bot.send_message(msg.chat.id, text)
                    except Exception:
                        pass

            # Initial progress message
            update_progress_message(build_progress_text(spinner[0]))

            # Use ThreadPoolExecutor for parallel requests
            next_progress_at = time.time() + 5
            spinner_tick = 0
            try:
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = [executor.submit(check_url, task) for task in url_tasks]
                    for future in as_completed(futures):
                        try:
                            future.result()
                        except Exception as exc:
                            log.warning("URL check failed: %s", exc)

                        now = time.time()
                        if now >= next_progress_at:
                            spinner_tick += 1
                            emoji = spinner[spinner_tick % len(spinner)]
                            update_progress_message(build_progress_text(emoji))
                            next_progress_at = now + 5
            except RuntimeError as exc:
                # Most common in constrained environments: "can't start new thread"
                log.exception(
                    "Thread pool creation failed for check_ebooks (workers=%s): %s",
                    max_workers,
                    exc,
                )
                bot.send_message(
                    msg.chat.id,
                    "⚠️ Thread limit reached. Falling back to single-thread scan (slower but stable)...",
                )
                for task in url_tasks:
                    try:
                        check_url(task)
                    except Exception as task_exc:
                        log.warning("Sequential URL check failed: %s", task_exc)

                    now = time.time()
                    if now >= next_progress_at:
                        spinner_tick += 1
                        emoji = spinner[spinner_tick % len(spinner)]
                        update_progress_message(build_progress_text(emoji))
                        next_progress_at = now + 5

            # Post final progress state
            update_progress_message(build_progress_text("✅"))

            # Save results to JSON file
            output_file = "./missing_ebooks.json"
            result_data = {
                "checked_at": __import__("datetime").datetime.now().isoformat(),
                "total_checked": total_books,
                "missing_count": len(missing_ebooks),
                "missing_ebooks": missing_ebooks,
            }

            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(result_data, f, ensure_ascii=False, indent=2)

            log.info("Check ebooks completed: %d/%d missing", len(missing_ebooks), total_books)

            # Send final summary
            summary = (
                f"✅ <b>Check Complete!</b>\n\n"
                f"📚 Total books checked: <b>{total_books}</b>\n"
                f"❌ Missing EPUB files: <b>{len(missing_ebooks)}</b>\n"
                f"💾 Results saved to: <code>{output_file}</code>"
            )
            bot.send_message(msg.chat.id, summary, parse_mode="HTML")

            # Send the JSON file as a document
            try:
                with open(output_file, "rb") as f:
                    bot.send_document(
                        msg.chat.id,
                        document=f,
                        caption=f"📋 Missing EPUB files report ({len(missing_ebooks)} items)",
                    )
            except Exception as send_exc:
                log.warning("Failed to send JSON document: %s", send_exc)
                bot.send_message(msg.chat.id, f"⚠️ Could not send JSON file: {send_exc}")

        except Exception as exc:
            log.exception("Check ebooks failed")
            bot.send_message(msg.chat.id, f"❌ Check failed:\n<code>{exc}</code>", parse_mode="HTML")

    # Run check in background thread to avoid timeout
    threading.Thread(target=run_check, daemon=True).start()


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
    if _dispatch_quick_menu_label(msg):
        return
    _do_search(msg.chat.id, msg.from_user.id, msg.text.strip())


# ══════════════════════════════════════════════════════════════════════════════
#  Inline keyboard callback — author disambiguation
# ══════════════════════════════════════════════════════════════════════════════

@bot.callback_query_handler(func=lambda c: c.data.startswith("authorpg:"))
def handle_author_candidates_page_callback(call: types.CallbackQuery):
    if not call.data or not call.message or not call.from_user:
        return

    user_id = call.from_user.id
    new_page = int(call.data.split(":", 1)[1])

    with _sessions_lock:
        session = _sessions.get(user_id)

    if not session or "author_candidates" not in session:
        bot.answer_callback_query(call.id, "⚠️ Session expired — run /author again.")
        return

    query = session.get("author_query", "author")
    candidates = session["author_candidates"]
    text, kb = build_author_candidates_page(query, candidates, new_page)

    with _sessions_lock:
        if user_id in _sessions:
            _sessions[user_id]["author_candidates_page"] = new_page

    try:
        bot.edit_message_text(
            text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        pass

    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("authorpick:"))
def handle_author_pick_callback(call: types.CallbackQuery):
    if not call.data or not call.message or not call.from_user:
        return

    user_id = call.from_user.id
    try:
        author_id = int(call.data.split(":", 1)[1])
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "⚠️ Invalid author selection.")
        return

    with _sessions_lock:
        session = _sessions.get(user_id)

    if not session or "author_candidates" not in session:
        bot.answer_callback_query(call.id, "⚠️ Session expired — run /author again.")
        return

    selected = next(
        (a for a in session["author_candidates"] if int(a.get("id") or 0) == author_id),
        None,
    )
    if not selected:
        bot.answer_callback_query(call.id, "⚠️ Author not found in session.")
        return

    bot.answer_callback_query(call.id, f"👤 Selected {selected.get('name', 'author')}")

    # Remove the chooser message to keep chat clean.
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass

    show_author_and_books(call.message.chat.id, user_id, selected)


# ══════════════════════════════════════════════════════════════════════════════
#  Inline keyboard callback — book detail author shortcut
# ══════════════════════════════════════════════════════════════════════════════

@bot.callback_query_handler(func=lambda c: c.data.startswith("bookauthor:"))
def handle_book_author_callback(call: types.CallbackQuery):
    if not call.data or not call.message or not call.from_user:
        return

    try:
        author_id = int(call.data.split(":", 1)[1])
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "⚠️ Invalid author link.")
        return

    author_row = get_author_details_by_id(author_id)
    if not author_row:
        bot.answer_callback_query(call.id, "⚠️ Author not found.")
        return

    bot.answer_callback_query(call.id, f"👤 {author_row.get('name', 'Author')}")
    show_author_and_books(call.message.chat.id, call.from_user.id, author_row)


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
    download_book(call.message.chat.id, book, call.from_user.id)  # pass telegram_id


# ══════════════════════════════════════════════════════════════════════════════
#  Category browser callbacks
# ══════════════════════════════════════════════════════════════════════════════

@bot.callback_query_handler(func=lambda c: c.data.startswith("catpg:"))
def handle_category_page_callback(call: types.CallbackQuery):
    """Handle category list pagination."""
    if not call.data or not call.message or not call.from_user:
        return
    
    user_id = call.from_user.id
    new_page = int(call.data.split(":", 1)[1])
    
    with _sessions_lock:
        session = _sessions.get(user_id)
    
    if not session or "categories" not in session:
        bot.answer_callback_query(call.id, "⚠️ Session expired — use /list_categories again.")
        return
    
    text, kb = build_category_page(session["categories"], new_page)
    
    with _sessions_lock:
        if user_id in _sessions:
            _sessions[user_id]["category_page"] = new_page
    
    try:
        bot.edit_message_text(
            text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        pass
    
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("cat:"))
def handle_category_selection(call: types.CallbackQuery):
    """Handle category selection — show books in that category."""
    if not call.data or not call.message or not call.from_user:
        return
    
    user_id = call.from_user.id
    cat_id = int(call.data.split(":", 1)[1])
    
    # Fetch books for this category
    books = get_books_by_category(cat_id)
    
    if not books:
        bot.answer_callback_query(call.id, "📚 No books in this category.")
        return
    
    # Find category name
    with _sessions_lock:
        session = _sessions.get(user_id)
    
    cat_name = "Category"
    if session and "categories" in session:
        for cat in session["categories"]:
            if cat["id"] == cat_id:
                cat_name = cat["category_name"]
                break
    
    # Store books and category info in session
    with _sessions_lock:
        if user_id not in _sessions:
            _sessions[user_id] = {}
        _sessions[user_id]["current_category_id"] = cat_id
        _sessions[user_id]["category_books"] = books
        _sessions[user_id]["category_books_page"] = 0
        _sessions[user_id]["category_name"] = cat_name
    
    text, kb = build_category_books_page(cat_name, books, 0)
    
    try:
        bot.edit_message_text(
            text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        pass
    
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("catbookpg:"))
def handle_category_books_page_callback(call: types.CallbackQuery):
    """Handle book list pagination within a category."""
    if not call.data or not call.message or not call.from_user:
        return
    
    user_id = call.from_user.id
    new_page = int(call.data.split(":", 1)[1])
    
    with _sessions_lock:
        session = _sessions.get(user_id)
    
    if not session or "category_books" not in session:
        bot.answer_callback_query(call.id, "⚠️ Session expired — use /list_categories again.")
        return
    
    cat_name = session.get("category_name", "Category")
    text, kb = build_category_books_page(cat_name, session["category_books"], new_page)
    
    with _sessions_lock:
        if user_id in _sessions:
            _sessions[user_id]["category_books_page"] = new_page
    
    try:
        bot.edit_message_text(
            text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        pass
    
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("catdl:"))
def handle_category_book_download(call: types.CallbackQuery):
    """Handle book download from category browser."""
    if not call.data or not call.message or not call.from_user:
        return
    
    user_id = call.from_user.id
    abs_idx = int(call.data.split(":", 1)[1])
    
    with _sessions_lock:
        session = _sessions.get(user_id)
    
    if not session or "category_books" not in session or abs_idx >= len(session["category_books"]):
        bot.answer_callback_query(call.id, "⚠️ Session expired — please browse categories again.")
        return
    
    book_data = session["category_books"][abs_idx]
    
    # Convert DB book data to format expected by download_book
    book = {
        "filename": book_data.get("filename", "book.epub"),
        "title": book_data.get("title", "Unknown"),
        "url": book_data.get("url", ""),
        "size_mb": book_data.get("size_mb", 0),
    }
    
    bot.answer_callback_query(call.id, f"⬇️ Loading '{book['title'][:30]}'…")
    download_book(call.message.chat.id, book, call.from_user.id)  # pass telegram_id


# ══════════════════════════════════════════════════════════════════════════════
#  Inline keyboard callback — subcategory browser
# ══════════════════════════════════════════════════════════════════════════════

@bot.callback_query_handler(func=lambda c: c.data == "back_to_cats")
def handle_back_to_categories(call: types.CallbackQuery):
    """Return to categories list from subcategory view."""
    if not call.message or not call.from_user:
        return
    
    user_id = call.from_user.id
    
    with _sessions_lock:
        session = _sessions.get(user_id)
    
    if not session or "categories" not in session:
        categories = get_all_categories()
        if not categories:
            bot.answer_callback_query(call.id, "😔 Session expired.")
            return
        with _sessions_lock:
            _sessions[user_id] = {
                "categories": categories,
                "category_page": 0,
            }
    else:
        categories = session["categories"]
    
    text, kb = build_category_page(categories, 0)
    
    try:
        bot.edit_message_text(
            text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        pass
    
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data == "back_to_subcats")
def handle_back_to_subcategories(call: types.CallbackQuery):
    """Return to subcategories list from book view."""
    if not call.message or not call.from_user:
        return
    
    user_id = call.from_user.id
    
    with _sessions_lock:
        session = _sessions.get(user_id)
    
    if not session or "subcategories" not in session:
        subcategories = get_all_subcategories_with_counts()
        if not subcategories:
            bot.answer_callback_query(call.id, "😔 Session expired.")
            return
        with _sessions_lock:
            _sessions[user_id] = {
                "subcategories": subcategories,
                "subcategory_page": 0,
            }
    else:
        subcategories = session["subcategories"]
    
    text, kb = build_subcategory_page(subcategories, 0)
    
    try:
        bot.edit_message_text(
            text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        pass
    
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("subcatpg:"))
def handle_subcategory_page_callback(call: types.CallbackQuery):
    """Handle subcategory list pagination."""
    if not call.data or not call.message or not call.from_user:
        return
    
    user_id = call.from_user.id
    new_page = int(call.data.split(":", 1)[1])
    
    with _sessions_lock:
        session = _sessions.get(user_id)
    
    if not session or "subcategories" not in session:
        bot.answer_callback_query(call.id, "⚠️ Session expired — use /list_subcategories again.")
        return
    
    text, kb = build_subcategory_page(session["subcategories"], new_page)
    
    with _sessions_lock:
        if user_id in _sessions:
            _sessions[user_id]["subcategory_page"] = new_page
    
    try:
        bot.edit_message_text(
            text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        pass
    
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("subcat:"))
def handle_subcategory_selection(call: types.CallbackQuery):
    """Handle subcategory selection — show books in that subcategory."""
    if not call.data or not call.message or not call.from_user:
        return
    
    user_id = call.from_user.id
    subcat_id = int(call.data.split(":", 1)[1])
    
    # Fetch books for this subcategory
    books = get_books_by_subcategory(subcat_id)
    
    if not books:
        bot.answer_callback_query(call.id, "📚 No books in this subcategory.")
        return
    
    # Find subcategory name
    with _sessions_lock:
        session = _sessions.get(user_id)
    
    subcat_name = "Subcategory"
    if session and "subcategories" in session:
        for subcat in session["subcategories"]:
            if subcat["id"] == subcat_id:
                subcat_name = subcat["sub_category_name"]
                break
    
    # Store books and subcategory info in session
    with _sessions_lock:
        if user_id not in _sessions:
            _sessions[user_id] = {}
        _sessions[user_id]["current_subcategory_id"] = subcat_id
        _sessions[user_id]["subcategory_books"] = books
        _sessions[user_id]["subcategory_books_page"] = 0
        _sessions[user_id]["subcategory_name"] = subcat_name
    
    text, kb = build_subcategory_books_page(subcat_name, books, 0)
    
    try:
        bot.edit_message_text(
            text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        pass
    
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("subcatbookpg:"))
def handle_subcategory_books_page_callback(call: types.CallbackQuery):
    """Handle book list pagination within a subcategory."""
    if not call.data or not call.message or not call.from_user:
        return
    
    user_id = call.from_user.id
    new_page = int(call.data.split(":", 1)[1])
    
    with _sessions_lock:
        session = _sessions.get(user_id)
    
    if not session or "subcategory_books" not in session:
        bot.answer_callback_query(call.id, "⚠️ Session expired — use /list_subcategories again.")
        return
    
    subcat_name = session.get("subcategory_name", "Subcategory")
    text, kb = build_subcategory_books_page(subcat_name, session["subcategory_books"], new_page)
    
    with _sessions_lock:
        if user_id in _sessions:
            _sessions[user_id]["subcategory_books_page"] = new_page
    
    try:
        bot.edit_message_text(
            text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        pass
    
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("subcatdl:"))
def handle_subcategory_book_download(call: types.CallbackQuery):
    """Handle book download from subcategory browser."""
    if not call.data or not call.message or not call.from_user:
        return
    
    user_id = call.from_user.id
    abs_idx = int(call.data.split(":", 1)[1])
    
    with _sessions_lock:
        session = _sessions.get(user_id)
    
    if not session or "subcategory_books" not in session or abs_idx >= len(session["subcategory_books"]):
        bot.answer_callback_query(call.id, "⚠️ Session expired — please browse subcategories again.")
        return
    
    book_data = session["subcategory_books"][abs_idx]
    
    # Convert DB book data to format expected by download_book
    book = {
        "filename": book_data.get("filename", "book.epub"),
        "title": book_data.get("title", "Unknown"),
        "url": book_data.get("url", ""),
        "size_mb": book_data.get("size_mb", 0),
    }
    
    bot.answer_callback_query(call.id, f"⬇️ Loading '{book['title'][:30]}'…")
    download_book(call.message.chat.id, book, call.from_user.id)  # pass telegram_id


# ══════════════════════════════════════════════════════════════════════════════
#  Inline keyboard callback — download history
# ══════════════════════════════════════════════════════════════════════════════

@bot.callback_query_handler(func=lambda c: c.data.startswith("histpg:"))
def handle_history_page_callback(call: types.CallbackQuery):
    """Handle download history pagination."""
    if not call.data or not call.message or not call.from_user:
        return

    if not _check_callback_access(call):
        return

    telegram_id = call.from_user.id
    db_user_id = _get_db_user_id(telegram_id)
    if not db_user_id:
        log.warning("histpg callback: cannot resolve db_user_id for telegram_id=%s", telegram_id)
        bot.answer_callback_query(call.id, "⚠️ Session expired — use /myhistory again.")
        return

    new_page = int(call.data.split(":", 1)[1])

    text, kb = _build_history_page(db_user_id, new_page)

    try:
        bot.edit_message_text(
            text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        pass

    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("histdl:"))
def handle_history_download_callback(call: types.CallbackQuery):
    """Handle redownload from history."""
    if not call.data or not call.message or not call.from_user:
        return

    if not _check_callback_access(call):
        return

    telegram_id = call.from_user.id
    db_user_id = _get_db_user_id(telegram_id)
    if not db_user_id:
        log.warning("histdl callback: cannot resolve db_user_id for telegram_id=%s", telegram_id)
        bot.answer_callback_query(call.id, "⚠️ Session expired — use /myhistory again.")
        return

    try:
        record_id = int(call.data.split(":", 1)[1])
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "⚠️ Invalid selection.")
        return

    # Fetch the download record
    history = get_user_download_history(db_user_id, limit=100)
    record = next((r for r in history if r["id"] == record_id), None)

    if not record:
        log.warning(
            "History record not found: telegram_id=%s db_user_id=%s record_id=%s",
            telegram_id,
            db_user_id,
            record_id,
        )
        bot.answer_callback_query(call.id, "⚠️ Record not found. Please try /myhistory again.")
        return

    # Reconstruct book dict for download_book
    book = {
        "filename": record["filename"],
        "title": record["title"],
        "url": "",  # URL not stored in history
    }

    # Try to get size from index
    books = load_books()
    matching = [b for b in books if b["filename"] == record["filename"]]
    if matching:
        book["url"] = matching[0].get("url", "")
        book["size_mb"] = matching[0].get("size_mb", 0)
    else:
        book["size_mb"] = 0

    if not book["url"]:
        bot.answer_callback_query(call.id, "⚠️ File no longer available in the library.")
        return

    bot.answer_callback_query(call.id, f"⬇️ Re-downloading '{record['title'][:30]}'…")
    download_book(call.message.chat.id, book, call.from_user.id)  # pass telegram_id


# ══════════════════════════════════════════════════════════════════════════════
#  Inline keyboard callback — bookmarks
# ══════════════════════════════════════════════════════════════════════════════

@bot.callback_query_handler(func=lambda c: c.data.startswith("bm:add:"))
def handle_bookmark_add_callback(call: types.CallbackQuery):
    """Handle adding a bookmark."""
    if not call.data or not call.message or not call.from_user:
        return

    if not _check_callback_access(call):
        return

    user_id = call.from_user.id
    filename = call.data.split(":", 2)[2] if len(call.data.split(":")) > 2 else ""

    if not filename:
        bot.answer_callback_query(call.id, "⚠️ Invalid bookmark.")
        return

    # Get book title for the bookmark
    books = load_books()
    matching = next((b for b in books if b["filename"] == filename), None)
    title = matching.get("title", "Unknown") if matching else filename

    if add_bookmark(user_id, filename, title):
        bot.answer_callback_query(call.id, f"🔖 Bookmarked '{title[:30]}'")
        
        # Update the keyboard to show "Bookmarked" instead of "Bookmark"
        # by editing the message
        try:
            db_info = get_book_details(filename)
            if db_info:
                db_info["filename"] = filename
                if "title" not in db_info or not db_info["title"]:
                    db_info["title"] = title
            else:
                db_info = {"filename": filename, "title": title}
            
            # Update keyboard
            new_kb = _build_book_detail_keyboard(db_info, user_id)
            if new_kb:
                bot.edit_message_reply_markup(
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    reply_markup=new_kb,
                )
        except Exception:
            pass
    else:
        bot.answer_callback_query(call.id, "⚠️ Failed to bookmark.")


@bot.callback_query_handler(func=lambda c: c.data.startswith("bm:del:"))
def handle_bookmark_remove_callback(call: types.CallbackQuery):
    """Handle removing a bookmark."""
    if not call.data or not call.message or not call.from_user:
        return

    if not _check_callback_access(call):
        return

    user_id = call.from_user.id
    filename = call.data.split(":", 2)[2] if len(call.data.split(":")) > 2 else ""

    if not filename:
        bot.answer_callback_query(call.id, "⚠️ Invalid selection.")
        return

    if remove_bookmark(user_id, filename):
        # If called from book detail page, update the keyboard
        if call.message.reply_to_message or call.message.text:
            try:
                db_info = get_book_details(filename)
                if db_info:
                    db_info["filename"] = filename
                else:
                    db_info = {"filename": filename}
                
                new_kb = _build_book_detail_keyboard(db_info, user_id)
                if new_kb:
                    bot.edit_message_reply_markup(
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                        reply_markup=new_kb,
                    )
                bot.answer_callback_query(call.id, "🗑 Bookmark removed")
            except Exception:
                bot.answer_callback_query(call.id, "🗑 Bookmark removed")
        else:
            bot.answer_callback_query(call.id, "🗑 Bookmark removed")
    else:
        bot.answer_callback_query(call.id, "⚠️ Bookmark not found or already removed.")


@bot.callback_query_handler(func=lambda c: c.data == "open_mybooks")
def handle_open_mybooks_callback(call: types.CallbackQuery):
    """Open the user's bookmarks page."""
    if not call.message or not call.from_user:
        return

    if not _check_callback_access(call):
        return

    user_id = call.from_user.id
    text, keyboard = _build_bookmarks_page(user_id, 0)
    
    # Delete the original message with the document and buttons
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass

    # Send the bookmarks page
    bot.send_message(
        call.message.chat.id,
        text,
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("bmpg:"))
def handle_bookmarks_page_callback(call: types.CallbackQuery):
    """Handle bookmarks page pagination."""
    if not call.data or not call.message or not call.from_user:
        return

    if not _check_callback_access(call):
        return

    user_id = call.from_user.id
    new_page = int(call.data.split(":", 1)[1])

    text, kb = _build_bookmarks_page(user_id, new_page)

    try:
        bot.edit_message_text(
            text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        pass

    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("bmdl:"))
def handle_bookmark_download_callback(call: types.CallbackQuery):
    """Handle download from bookmarks."""
    if not call.data or not call.message or not call.from_user:
        return

    if not _check_callback_access(call):
        return

    user_id = call.from_user.id
    filename = call.data.split(":", 1)[1] if len(call.data.split(":")) > 1 else ""

    if not filename:
        bot.answer_callback_query(call.id, "⚠️ Invalid selection.")
        return

    # Get book info from index
    books = load_books()
    matching = next((b for b in books if b["filename"] == filename), None)

    if not matching:
        bot.answer_callback_query(call.id, "⚠️ File no longer available in the library.")
        return

    book = {
        "filename": matching["filename"],
        "title": matching.get("title", "Unknown"),
        "url": matching.get("url", ""),
        "size_mb": matching.get("size_mb", 0),
    }

    bot.answer_callback_query(call.id, f"⬇️ Downloading '{book['title'][:30]}'…")
    download_book(call.message.chat.id, book, call.from_user.id)  # pass telegram_id


def _register_commands() -> None:
    """Register bot commands so they appear in Telegram's menu button."""
    commands = [
        types.BotCommand("start",              "Welcome message & library size"),
        types.BotCommand("search",             "Search books by title keywords"),
        types.BotCommand("author",             "Show books by an author"),
        types.BotCommand("list_categories",   "Browse books by category"),
        types.BotCommand("list_subcategories","Browse books by subcategory"),
        types.BotCommand("myhistory",          "View your download history"),
        types.BotCommand("searchhistory",       "Search your download history"),
        types.BotCommand("mybooks",           "View your bookmarked books"),
        types.BotCommand("reindex",            "Re-scan the EPUB folder (admin)"),
        types.BotCommand("check_ebooks",      "Check all DB books for missing files (admin)"),
        types.BotCommand("help",              "Show usage instructions"),
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
