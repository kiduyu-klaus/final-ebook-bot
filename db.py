"""
db.py  —  MySQL helper for the EBook Bot.

Looks up rich book metadata (cover, authors, category, sub-category,
description) from the sflatran_kbooks database by matching the book's
filename stored in the JSON index against the `url` column in `books`.

Connection settings come from config.py (DB_HOST, DB_PORT, DB_USER,
DB_PASSWORD, DB_NAME) or fall back to environment variables.
"""

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
                    (f"%{filename}",),
                )
                row = cur.fetchone()

                if not row:
                    return None

                # Resolve author names from comma-separated author_ids
                author_names = []
                if row["author_ids"]:
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

                return {
                    "title":        row["title"],
                    "description":  row["description"] or "",
                    "image":        row["image"] or "",
                    "authors":      author_names,
                    "category":     row["category_name"] or "",
                    "sub_category": row["sub_category_name"] or "",
                    "book_access":  row["book_access"] or "Free",
                }

    except Exception as exc:
        log.warning("DB lookup failed for '%s': %s", filename, exc)
        return None


def clear_cache():
    """Call after /reindex so stale lookups are evicted."""
    get_book_details.cache_clear()