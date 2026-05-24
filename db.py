"""
db.py  —  MySQL helper for the EBook Bot.

Looks up rich book metadata (cover, authors, category, sub-category,
description) from the sflatran_kbooks database by matching the book's
filename stored in the JSON index against the `url` column in `books`.

Connection settings come from config.py (DB_HOST, DB_PORT, DB_USER,
DB_PASSWORD, DB_NAME) or fall back to environment variables.
"""

import os
import re

# Root directory where EPUBs are stored — mirrors config.py / indexer.py
_EBOOKS_ROOT = "/home1/sflatran/public_html/kbooks/public"


def _size_mb_from_url(url: str) -> float:
    """Resolve a DB url value (e.g. 'upload\\subfolder\\book.epub') to an
    absolute path and return its size in MB, or 0.0 if the file is missing."""
    if not url:
        return 0.0
    rel = url.replace("\\", "/")
    full_path = os.path.join(_EBOOKS_ROOT, rel)
    try:
        return round(os.path.getsize(full_path) / (1024 * 1024), 2)
    except OSError:
        return 0.0


def normalize_book_url(url: str) -> str:
    """
    Ensure a book URL always starts with 'upload\\' instead of
    'upload77\\' or any other variant.
    
    Args:
        url: The original URL from the database
        
    Returns:
        Normalized URL starting with 'upload\\' (never None)
    """
    if not url:
        return ""
    
    # Replace any 'uploadXX\\' pattern at the start with 'upload\\'
    normalized = re.sub(r'^upload\d+\\+', r'upload\\', url, count=1)
    return normalized


def extract_author_from_url(url: str) -> str:
    """
    Extract author name from URL path when author is unknown.
    
    The URL format is typically: upload\\Author_Name\\book.epub
    This function extracts the author folder name and replaces underscores with spaces.
    
    Args:
        url: The normalized URL from the database
        
    Returns:
        Author name extracted from the URL path, or empty string if not extractable
    """
    if not url:
        return ""
    
    # Remove the 'upload\\' prefix and get the path components
    normalized = normalize_book_url(url)
    if not normalized or not normalized.startswith('upload\\'):
        return ""
    
    path_after_upload = normalized[7:]  # Remove 'upload\\'
    if not path_after_upload:
        return ""
    
    parts = path_after_upload.split('\\')
    if len(parts) >= 2:
        # The middle folder is the author name
        author_folder = parts[1]
        # Replace underscores with spaces
        return author_folder.replace('_', ' ')
    
    return ""


import logging
from functools import lru_cache
from typing import Optional

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pymysql
    import pymysql.cursors

try:
    import pymysql          # noqa: F811
    import pymysql.cursors  # noqa: F811
    _PYMYSQL = True
except ImportError:
    _PYMYSQL = False

from config import DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME

log = logging.getLogger(__name__)


# ── Connection factory ────────────────────────────────────────────────────────

def _get_conn():
    if not _PYMYSQL:
        raise RuntimeError(
            "pymysql is not installed. Run: pip install pymysql"
        )
    return pymysql.connect(
        host=DB_HOST,
        port=int(DB_PORT),
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
    )


# ── Core lookup ───────────────────────────────────────────────────────────────

@lru_cache(maxsize=512)
def get_book_details(filename: str) -> Optional[dict]:
    """
    Return enriched metadata for a book matched by filename.

    The `url` column in `books` stores paths like:
        upload\\Nora_Roberts\\Captivated_-_Nora_Roberts.epub
    We match on the final filename component (case-insensitive).

    Returns a dict with:
        title, description, image, authors (list of str),
        category, sub_category, book_access
    Returns None if the book is not found or DB is unavailable.
    """
    try:
        conn = _get_conn()
        with conn:
            with conn.cursor() as cur:
                # Match on the filename part of the stored url
                # Normalize URL to ensure it starts with 'upload\\' not 'upload77\\' etc
                normalized_filename = normalize_book_url(filename)
                cur.execute(
                    """
                    SELECT
                        b.id,
                        b.title,
                        b.description,
                        b.image,
                        b.author_ids,
                        b.book_access,
                        b.cat_id,
                        b.sub_cat_id,
                        c.category_name,
                        sc.sub_category_name
                    FROM books b
                    LEFT JOIN categories   c  ON c.id  = b.cat_id
                    LEFT JOIN sub_categories sc ON sc.id = b.sub_cat_id
                    WHERE b.url LIKE %s
                    LIMIT 1
                    """,
                    (f"%{normalized_filename}",),
                )
                row = cur.fetchone()

                if not row:
                    return None

                # Resolve author names from comma-separated author_ids
                author_names = []
                primary_author_id = None
                if row["author_ids"]:
                    ids = [
                        aid.strip()
                        for aid in row["author_ids"].split(",")
                        if aid.strip().isdigit()
                    ]
                    if ids:
                        primary_author_id = int(ids[0])
                        placeholders = ",".join(["%s"] * len(ids))
                        cur.execute(
                            f"SELECT name FROM authors WHERE id IN ({placeholders})",
                            ids,
                        )
                        author_names = [r["name"] for r in cur.fetchall()]
                
                # If no author found from author_ids, extract from URL
                if not author_names:
                    author_names = [extract_author_from_url(row["url"])] if row["url"] else []

                return {
                    "title":        row["title"],
                    "description":  row["description"] or "",
                    "image":        row["image"] or "",
                    "authors":      author_names,
                    "primary_author_id": primary_author_id,
                    "category":     row["category_name"] or "",
                    "sub_category": row["sub_category_name"] or "",
                    "book_access":  row["book_access"] or "Free",
                }

    except Exception as exc:
        log.warning("DB lookup failed for '%s': %s", filename, exc)
        return None


@lru_cache(maxsize=256)
def get_author_details_by_name(query: str) -> Optional[dict]:
    """
    Return best-matching author details for a name query.

    Matches case-insensitively against authors.name and ranks:
    1) exact match
    2) prefix match
    3) contains match
    Active authors (status=1) are preferred.
    """
    q = (query or "").strip()
    if not q:
        return None

    q_lower = q.lower()
    like_contains = f"%{q_lower}%"
    like_prefix = f"{q_lower}%"

    try:
        conn = _get_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        id,
                        name,
                        info,
                        image,
                        facebook_url,
                        instagram_url,
                        youtube_url,
                        website_url,
                        status
                    FROM authors
                    WHERE LOWER(name) LIKE %s
                    ORDER BY
                        CASE WHEN status = 1 THEN 0 ELSE 1 END,
                        CASE
                            WHEN LOWER(name) = %s THEN 0
                            WHEN LOWER(name) LIKE %s THEN 1
                            ELSE 2
                        END,
                        CHAR_LENGTH(name) ASC
                    LIMIT 1
                    """,
                    (like_contains, q_lower, like_prefix),
                )
                row = cur.fetchone()
                return row or None
    except Exception as exc:
        log.warning("Author lookup failed for '%s': %s", q, exc)
        return None


@lru_cache(maxsize=128)
def get_author_candidates_by_name(query: str, limit: int = 100) -> list:
    """
    Return ranked author candidates for a name query.

    Ranking:
    1) active authors first (status=1)
    2) exact name match
    3) prefix match
    4) contains match
    5) shorter names first
    """
    q = (query or "").strip()
    if not q:
        return []

    q_lower = q.lower()
    like_contains = f"%{q_lower}%"
    like_prefix = f"{q_lower}%"
    safe_limit = max(1, min(int(limit or 100), 200))

    try:
        conn = _get_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                        id,
                        name,
                        info,
                        image,
                        facebook_url,
                        instagram_url,
                        youtube_url,
                        website_url,
                        status
                    FROM authors
                    WHERE LOWER(name) LIKE %s
                    ORDER BY
                        CASE WHEN status = 1 THEN 0 ELSE 1 END,
                        CASE
                            WHEN LOWER(name) = %s THEN 0
                            WHEN LOWER(name) LIKE %s THEN 1
                            ELSE 2
                        END,
                        CHAR_LENGTH(name) ASC
                    LIMIT {safe_limit}
                    """,
                    (like_contains, q_lower, like_prefix),
                )
                return list(cur.fetchall() or [])
    except Exception as exc:
        log.warning("Author candidates lookup failed for '%s': %s", q, exc)
        return []


@lru_cache(maxsize=256)
def get_author_details_by_id(author_id: int) -> Optional[dict]:
    """Return author details row by authors.id, or None if not found."""
    try:
        author_id = int(author_id)
    except (TypeError, ValueError):
        return None

    if author_id <= 0:
        return None

    try:
        conn = _get_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        id,
                        name,
                        info,
                        image,
                        facebook_url,
                        instagram_url,
                        youtube_url,
                        website_url,
                        status
                    FROM authors
                    WHERE id = %s
                    LIMIT 1
                    """,
                    (author_id,),
                )
                row = cur.fetchone()
                return row or None
    except Exception as exc:
        log.warning("Author lookup by id failed for '%s': %s", author_id, exc)
        return None


@lru_cache(maxsize=1)
def get_library_overview() -> dict:
    """
    Return library summary stats for bot welcome screens.

    Keys:
      total_books: int
      total_authors: int
      top_categories: list[{name, book_count}]
      top_subcategories: list[{name, book_count}]
    """
    overview = {
        "total_books": 0,
        "total_authors": 0,
        "top_categories": [],
        "top_subcategories": [],
    }

    try:
        conn = _get_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM books
                    WHERE status = 1
                    """
                )
                row = cur.fetchone() or {}
                overview["total_books"] = int(row.get("total") or 0)

                cur.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM authors
                    WHERE status = 1
                    """
                )
                row = cur.fetchone() or {}
                overview["total_authors"] = int(row.get("total") or 0)

                cur.execute(
                    """
                    SELECT
                        COALESCE(NULLIF(TRIM(c.category_name), ''), 'Uncategorized') AS name,
                        COUNT(b.id) AS book_count
                    FROM books b
                    LEFT JOIN categories c ON c.id = b.cat_id
                    WHERE b.status = 1
                    GROUP BY COALESCE(NULLIF(TRIM(c.category_name), ''), 'Uncategorized')
                    ORDER BY book_count DESC, name ASC
                    LIMIT 5
                    """
                )
                overview["top_categories"] = [
                    {
                        "name": row.get("name") or "Uncategorized",
                        "book_count": int(row.get("book_count") or 0),
                    }
                    for row in (cur.fetchall() or [])
                ]

                cur.execute(
                    """
                    SELECT
                        COALESCE(NULLIF(TRIM(sc.sub_category_name), ''), 'Uncategorized') AS name,
                        COUNT(b.id) AS book_count
                    FROM books b
                    LEFT JOIN sub_categories sc ON sc.id = b.sub_cat_id
                    WHERE b.status = 1
                    GROUP BY COALESCE(NULLIF(TRIM(sc.sub_category_name), ''), 'Uncategorized')
                    ORDER BY book_count DESC, name ASC
                    LIMIT 5
                    """
                )
                overview["top_subcategories"] = [
                    {
                        "name": row.get("name") or "Uncategorized",
                        "book_count": int(row.get("book_count") or 0),
                    }
                    for row in (cur.fetchall() or [])
                ]
    except Exception as exc:
        log.warning("Failed to fetch library overview: %s", exc)

    return overview


def clear_cache():
    """Call after /reindex so stale lookups are evicted."""
    get_book_details.cache_clear()
    get_author_details_by_name.cache_clear()
    get_author_candidates_by_name.cache_clear()
    get_author_details_by_id.cache_clear()
    get_library_overview.cache_clear()
    get_all_categories.cache_clear()
    get_books_by_category.cache_clear()
    get_all_subcategories_with_counts.cache_clear()
    get_books_by_subcategory.cache_clear()


# ── Bot User Management ────────────────────────────────────────────────────────

def get_user_by_telegram_id(telegram_id: int) -> Optional[dict]:
    """
    Return bot user details by telegram_id, or None if not found.
    """
    try:
        telegram_id = int(telegram_id)
    except (TypeError, ValueError):
        return None

    try:
        conn = _get_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, telegram_id, username, first_name, last_name,
                           language_code, is_banned, is_admin, first_seen,
                           last_active, total_downloads
                    FROM bot_users
                    WHERE telegram_id = %s
                    LIMIT 1
                    """,
                    (telegram_id,),
                )
                row = cur.fetchone()
                return row or None
    except Exception as exc:
        log.warning("User lookup by telegram_id failed for '%s': %s", telegram_id, exc)
        return None


def create_bot_user(
    telegram_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    language_code: Optional[str] = None,
) -> Optional[dict]:
    """
    Create a new bot user record and return the created record.
    Returns None if creation fails.
    """
    try:
        telegram_id = int(telegram_id)
    except (TypeError, ValueError):
        return None

    try:
        conn = _get_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO bot_users
                        (telegram_id, username, first_name, last_name, language_code,
                         is_banned, is_admin, first_seen, last_active, total_downloads)
                    VALUES
                        (%s, %s, %s, %s, %s, 0, 0, NOW(), NOW(), 0)
                    """,
                    (telegram_id, username, first_name, last_name, language_code),
                )
                conn.commit()
                user_id = cur.lastrowid

                # Fetch and return the created record
                cur.execute(
                    """
                    SELECT id, telegram_id, username, first_name, last_name,
                           language_code, is_banned, is_admin, first_seen,
                           last_active, total_downloads
                    FROM bot_users
                    WHERE id = %s
                    """,
                    (user_id,),
                )
                return cur.fetchone()

    except Exception as exc:
        log.warning("Failed to create bot user for telegram_id '%s': %s", telegram_id, exc)
        return None


def update_user_last_active(telegram_id: int) -> bool:
    """
    Update the last_active timestamp for a user.
    Returns True if successful, False otherwise.
    """
    try:
        telegram_id = int(telegram_id)
    except (TypeError, ValueError):
        return False

    try:
        conn = _get_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE bot_users
                    SET last_active = NOW()
                    WHERE telegram_id = %s
                    """,
                    (telegram_id,),
                )
                conn.commit()
                return cur.rowcount > 0
    except Exception as exc:
        log.warning("Failed to update last_active for telegram_id '%s': %s", telegram_id, exc)
        return False


def increment_user_downloads(telegram_id: int) -> bool:
    """
    Increment the total_downloads counter for a user.
    Returns True if successful, False otherwise.
    """
    try:
        telegram_id = int(telegram_id)
    except (TypeError, ValueError):
        log.warning("increment_user_downloads invalid telegram_id: %r", telegram_id)
        return False

    try:
        conn = _get_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE bot_users
                    SET total_downloads = total_downloads + 1,
                        last_active = NOW()
                    WHERE telegram_id = %s
                    """,
                    (telegram_id,),
                )
                conn.commit()
                ok = cur.rowcount > 0
                log.info(
                    "increment_user_downloads telegram_id=%s rowcount=%s ok=%s",
                    telegram_id,
                    cur.rowcount,
                    ok,
                )
                return ok
    except Exception as exc:
        log.warning("Failed to increment downloads for telegram_id '%s': %s", telegram_id, exc)
        return False


def ban_user(telegram_id: int, banned: bool = True) -> bool:
    """
    Ban or unban a user.
    Returns True if successful, False otherwise.
    """
    try:
        telegram_id = int(telegram_id)
    except (TypeError, ValueError):
        return False

    try:
        conn = _get_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE bot_users
                    SET is_banned = %s
                    WHERE telegram_id = %s
                    """,
                    (1 if banned else 0, telegram_id),
                )
                conn.commit()
                return cur.rowcount > 0
    except Exception as exc:
        log.warning("Failed to %s user telegram_id '%s': %s",
                    "ban" if banned else "unban", telegram_id, exc)
        return False


def is_user_banned(telegram_id: int) -> bool:
    """
    Check if a user is banned.
    Returns True if banned, False otherwise.
    """
    user = get_user_by_telegram_id(telegram_id)
    if user is None:
        return False
    return bool(user.get("is_banned"))


def is_user_admin(telegram_id: int) -> bool:
    """
    Check if a user is an admin.
    Returns True if admin, False otherwise.
    """
    user = get_user_by_telegram_id(telegram_id)
    if user is None:
        return False
    return bool(user.get("is_admin"))


def log_user_download(
    user_id: int,
    book_id: Optional[int],
    filename: str,
    title: str,
) -> bool:
    """
    Log a download to the user_downloads table.
    Returns True if successful, False otherwise.
    """
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        log.warning(
            "log_user_download invalid user_id: %r (book_id=%r filename=%r)",
            user_id,
            book_id,
            filename,
        )
        return False

    try:
        safe_filename = (filename or "").strip()
        safe_title = (title or "").strip()
        if not safe_filename:
            log.warning("log_user_download empty filename for user_id=%s", user_id)
            return False
        if not safe_title:
            safe_title = safe_filename

        log.info(
            "log_user_download attempt: user_id=%s book_id=%r filename=%r title=%r",
            user_id,
            book_id,
            safe_filename,
            safe_title,
        )

        conn = _get_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO user_downloads
                        (user_id, book_id, filename, title, downloaded_at)
                    VALUES
                        (%s, %s, %s, %s, NOW())
                    """,
                    (user_id, book_id, safe_filename, safe_title),
                )
                conn.commit()
                ok = cur.rowcount > 0
                log.info(
                    "log_user_download success: user_id=%s rowcount=%s lastrowid=%s ok=%s",
                    user_id,
                    cur.rowcount,
                    getattr(cur, "lastrowid", None),
                    ok,
                )
                return ok
    except Exception as exc:
        log.warning(
            "Failed to log download for user %d, book '%s': %s",
            user_id, filename, exc
        )
        return False


def get_user_download_history(
    user_id: int,
    limit: int = 50,
    offset: int = 0,
) -> list:
    """
    Get download history for a user.
    Returns list of download records with most recent first.
    """
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        log.warning("get_user_download_history invalid user_id: %r", user_id)
        return []

    safe_limit = max(1, min(int(limit or 50), 100))
    safe_offset = max(0, int(offset or 0))

    try:
        conn = _get_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, user_id, book_id, filename, title, downloaded_at
                    FROM user_downloads
                    WHERE user_id = %s
                    ORDER BY downloaded_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (user_id, safe_limit, safe_offset),
                )
                rows = list(cur.fetchall() or [])
                log.info(
                    "get_user_download_history user_id=%s limit=%s offset=%s rows=%s",
                    user_id,
                    safe_limit,
                    safe_offset,
                    len(rows),
                )
                return rows
    except Exception as exc:
        log.warning(
            "Failed to fetch download history for user %d: %s",
            user_id, exc
        )
        return []


def get_user_download_count(user_id: int) -> int:
    """
    Get total number of downloads for a user.
    """
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        log.warning("get_user_download_count invalid user_id: %r", user_id)
        return 0

    try:
        conn = _get_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) as total
                    FROM user_downloads
                    WHERE user_id = %s
                    """,
                    (user_id,),
                )
                row = cur.fetchone()
                total = row["total"] if row else 0
                log.info("get_user_download_count user_id=%s total=%s", user_id, total)
                return total
    except Exception as exc:
        log.warning(
            "Failed to get download count for user %d: %s",
            user_id, exc
        )
        return 0


def search_user_downloads(
    user_id: int,
    query: str,
    limit: int = 50,
) -> list:
    """
    Search a user's download history by title.
    Returns list of matching download records ordered by most recent.
    """
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        log.warning("search_user_downloads invalid user_id: %r", user_id)
        return []

    q = (query or "").strip()
    if not q:
        return []

    safe_limit = max(1, min(int(limit or 50), 100))

    try:
        conn = _get_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, user_id, book_id, filename, title, downloaded_at
                    FROM user_downloads
                    WHERE user_id = %s AND LOWER(title) LIKE %s
                    ORDER BY downloaded_at DESC
                    LIMIT %s
                    """,
                    (user_id, f"%{q.lower()}%", safe_limit),
                )
                rows = list(cur.fetchall() or [])
                log.info(
                    "search_user_downloads user_id=%s query=%r limit=%s rows=%s",
                    user_id,
                    q,
                    safe_limit,
                    len(rows),
                )
                return rows
    except Exception as exc:
        log.warning(
            "Failed to search downloads for user %d with query '%s': %s",
            user_id, q, exc
        )
        return []


# ── Bookmark Management ───────────────────────────────────────────────────────

def add_bookmark(
    user_id: int,
    filename: str,
    title: str,
    book_id: Optional[int] = None,
) -> bool:
    """
    Add a bookmark for a user. Uses INSERT IGNORE so duplicates are handled
    silently. Returns True if successful (or already exists), False otherwise.
    """
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return False

    if not filename:
        return False

    try:
        conn = _get_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT IGNORE INTO user_bookmarks
                        (user_id, book_id, filename, title, bookmarked_at)
                    VALUES
                        (%s, %s, %s, %s, NOW())
                    """,
                    (user_id, book_id, filename, title),
                )
                conn.commit()
                return True
    except Exception as exc:
        log.warning(
            "Failed to add bookmark for user %d, file '%s': %s",
            user_id, filename, exc
        )
        return False


def remove_bookmark(user_id: int, filename: str) -> bool:
    """
    Remove a bookmark for a user by filename.
    Returns True if a row was deleted, False otherwise.
    """
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return False

    if not filename:
        return False

    try:
        conn = _get_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM user_bookmarks
                    WHERE user_id = %s AND filename = %s
                    """,
                    (user_id, filename),
                )
                conn.commit()
                return cur.rowcount > 0
    except Exception as exc:
        log.warning(
            "Failed to remove bookmark for user %d, file '%s': %s",
            user_id, filename, exc
        )
        return False


def get_bookmarks(
    user_id: int,
    limit: int = 50,
    offset: int = 0,
) -> list:
    """
    Get bookmarked books for a user, ordered by most recent first.
    Returns list of bookmark records.
    """
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return []

    safe_limit = max(1, min(int(limit or 50), 100))
    safe_offset = max(0, int(offset or 0))

    try:
        conn = _get_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, user_id, book_id, filename, title, bookmarked_at
                    FROM user_bookmarks
                    WHERE user_id = %s
                    ORDER BY bookmarked_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (user_id, safe_limit, safe_offset),
                )
                return list(cur.fetchall() or [])
    except Exception as exc:
        log.warning(
            "Failed to fetch bookmarks for user %d: %s",
            user_id, exc
        )
        return []


def get_bookmark_count(user_id: int) -> int:
    """
    Get total number of bookmarks for a user.
    """
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return 0

    try:
        conn = _get_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) as total
                    FROM user_bookmarks
                    WHERE user_id = %s
                    """,
                    (user_id,),
                )
                row = cur.fetchone()
                return row["total"] if row else 0
    except Exception as exc:
        log.warning(
            "Failed to get bookmark count for user %d: %s",
            user_id, exc
        )
        return 0


def is_bookmarked(user_id: int, filename: str) -> bool:
    """
    Check if a book is bookmarked by a user.
    """
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return False

    if not filename:
        return False

    try:
        conn = _get_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1 FROM user_bookmarks
                    WHERE user_id = %s AND filename = %s
                    LIMIT 1
                    """,
                    (user_id, filename),
                )
                return cur.fetchone() is not None
    except Exception as exc:
        log.warning(
            "Failed to check bookmark status for user %d, file '%s': %s",
            user_id, filename, exc
        )
        return False


def get_or_create_user(
    telegram_id: int,
    username: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    language_code: Optional[str] = None,
) -> Optional[dict]:
    """
    Get existing user or create a new one if not exists.
    Also updates last_active timestamp for existing users.
    Returns the user dict or None on failure.
    """
    # Try to get existing user
    user = get_user_by_telegram_id(telegram_id)
    if user:
        # Update last_active for existing user
        update_user_last_active(telegram_id)
        return user

    # Create new user if not exists
    return create_bot_user(telegram_id, username, first_name, last_name, language_code)


# ── Category lookups ──────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def get_all_categories() -> list:
    """
    Return all active categories from the database that have at least 5 books.
    Returns a list of dicts with: id, category_name, category_image
    Returns empty list if DB is unavailable.
    """
    try:
        conn = _get_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT c.id, c.category_name, c.category_image, COUNT(b.id) AS book_count
                    FROM categories c
                    LEFT JOIN books b ON b.cat_id = c.id AND b.status = 1
                    WHERE c.status = 1
                    GROUP BY c.id, c.category_name, c.category_image
                    HAVING COUNT(b.id) >= 5
                    ORDER BY book_count DESC, c.category_name ASC
                    """
                )
                return [
                    {
                        "id": row["id"],
                        "category_name": row["category_name"],
                        "category_image": row["category_image"],
                        "book_count": row["book_count"],
                    }
                    for row in cur.fetchall()
                ]
    except Exception as exc:
        log.warning("Failed to fetch categories: %s", exc)
        return []


@lru_cache(maxsize=32)
def get_books_by_category(cat_id: int) -> list:
    """
    Return all books in a specific category from the database.
    Returns a list of dicts with: id, title, image, authors, description, url, size_mb (0 for DB books)
    Returns empty list if no books found or DB is unavailable.
    """
    try:
        conn = _get_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        b.id,
                        b.title,
                        b.description,
                        b.image,
                        b.author_ids,
                        b.url,
                        b.book_access
                    FROM books b
                    WHERE b.cat_id = %s AND b.status = 1
                    ORDER BY b.title ASC
                    """,
                    (cat_id,),
                )
                rows = cur.fetchall()

                books = []
                for row in rows:
                    # Resolve author names
                    author_names = []
                    if row.get("author_ids"):
                        ids = [
                            aid.strip()
                            for aid in row["author_ids"].split(",")
                            if aid.strip().isdigit()
                        ]
                        if ids:
                            placeholders = ",".join(["%s"] * len(ids))
                            cur.execute(
                                f"SELECT name FROM authors WHERE id IN ({placeholders})",
                                ids,
                            )
                            author_names = [r["name"] for r in cur.fetchall()]
                    
                    # If no author found from author_ids, extract from URL
                    if not author_names:
                        author_from_url = extract_author_from_url(row.get("url", ""))
                        if author_from_url:
                            author_names = [author_from_url]

                    books.append({
                        "id":          row["id"],
                        "filename":    normalize_book_url(row.get("url") or "").split("\\")[-1] if row.get("url") else "",
                        "title":       row["title"],
                        "description": row.get("description") or "",
                        "image":       row.get("image") or "",
                        "authors":     author_names,
                        "url":         normalize_book_url(row.get("url") or ""),
                        "book_access": row.get("book_access") or "Free",
                        "size_mb":     _size_mb_from_url(row.get("url") or ""),
                    })

                return books
    except Exception as exc:
        log.warning("Failed to fetch books for category %d: %s", cat_id, exc)
        return []

# ── Subcategory lookups ───────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def get_all_subcategories_with_counts() -> list:
    """
    Return all active subcategories with at least 5 books and their book counts.
    Returns a list of dicts with: id, sub_category_name, cat_id, book_count
    """
    try:
        conn = _get_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        sc.id,
                        sc.sub_category_name,
                        sc.cat_id,
                        COUNT(b.id) AS book_count
                    FROM sub_categories sc
                    LEFT JOIN books b
                        ON b.sub_cat_id = sc.id AND b.status = 1
                    GROUP BY sc.id, sc.sub_category_name, sc.cat_id
                    HAVING COUNT(b.id) >= 5
                    ORDER BY book_count DESC, sc.sub_category_name ASC
                    """
                )
                return [
                    {
                        "id":                row["id"],
                        "sub_category_name": row["sub_category_name"],
                        "cat_id":            row["cat_id"],
                        "book_count":        row["book_count"],
                    }
                    for row in cur.fetchall()
                ]
    except Exception as exc:
        log.warning("Failed to fetch subcategories: %s", exc)
        return []


@lru_cache(maxsize=32)
def get_books_by_subcategory(subcat_id: int) -> list:
    """
    Return all books in a specific subcategory from the database.
    Returns a list of dicts with: id, filename, title, description,
    image, authors, url, book_access, size_mb
    """
    try:
        conn = _get_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        b.id,
                        b.title,
                        b.description,
                        b.image,
                        b.author_ids,
                        b.url,
                        b.book_access
                    FROM books b
                    WHERE b.sub_cat_id = %s AND b.status = 1
                    ORDER BY b.title ASC
                    """,
                    (subcat_id,),
                )
                rows = cur.fetchall()

                books = []
                for row in rows:
                    author_names = []
                    if row.get("author_ids"):
                        ids = [
                            aid.strip()
                            for aid in row["author_ids"].split(",")
                            if aid.strip().isdigit()
                        ]
                        if ids:
                            placeholders = ",".join(["%s"] * len(ids))
                            cur.execute(
                                f"SELECT name FROM authors WHERE id IN ({placeholders})",
                                ids,
                            )
                            author_names = [r["name"] for r in cur.fetchall()]
                    if not author_names:
                        author_from_url = extract_author_from_url(row.get("url", ""))
                        if author_from_url:
                            author_names = [author_from_url]

                    books.append({
                        "id":          row["id"],
                        "filename":    normalize_book_url(row.get("url") or "").split("\\")[-1] if row.get("url") else "",
                        "title":       row["title"],
                        "description": row.get("description") or "",
                        "image":       row.get("image") or "",
                        "authors":     author_names,
                        "url":         normalize_book_url(row.get("url") or ""),
                        "book_access": row.get("book_access") or "Free",
                        "size_mb":     _size_mb_from_url(row.get("url") or ""),
                    })
                return books
    except Exception as exc:
        log.warning("Failed to fetch books for subcategory %d: %s", subcat_id, exc)
        return []
