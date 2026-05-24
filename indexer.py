"""
indexer.py  —  Scans EBOOKS_DIR for .epub files and writes books_index.json.

Run manually:
    python3 indexer.py

Or schedule via cPanel Cron Jobs (every 6 hours):
    0 */6 * * *  /home/yourusername/virtualenv/ebookbot/3.x/bin/python3 \
                 /home/yourusername/ebookbot/indexer.py >> /home/yourusername/ebookbot/indexer.log 2>&1
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

import requests

from config import EBOOKS_DIR, EBOOKS_BASE_URL, INDEX_FILE
from db import _get_conn

log = logging.getLogger(__name__)


# ── Title cleaning ────────────────────────────────────────────────────────────

def clean_title(filename: str) -> str:
    """
    Turn a raw filename into a human-readable title.
    Examples:
      "the-great-gatsby_fitzgerald.epub"  →  "The Great Gatsby Fitzgerald"
      "1984.George.Orwell.epub"           →  "1984 George Orwell"
    """
    name = os.path.splitext(filename)[0]        # drop .epub
    name = re.sub(r"[_\-\.]+", " ", name)       # separators → space
    name = re.sub(r"\s+", " ", name).strip()
    return name.title()


# ── Author extraction ────────────────────────────────────────────────────────

def extract_author(full_path: str) -> str:
    """
    Extract a human-readable author name from the immediate parent folder.

    Directory structure:  EBOOKS_DIR / Author_Name / book.epub
    Examples:
      .../Alex_Archer/Serpents_Kiss.epub   →  "Alex Archer"
      .../J.D._Robb/Naked_in_Death.epub   →  "J.D. Robb"
      .../R.L._Stine/Goosebumps.epub      →  "R.L. Stine"

    Falls back to an empty string if the file sits directly in EBOOKS_DIR.
    """
    parent = os.path.basename(os.path.dirname(full_path))
    if not parent or os.path.abspath(os.path.dirname(full_path)) == os.path.abspath(EBOOKS_DIR):
        return ""
    # Replace underscores with spaces but preserve dots (initials like J.D.)
    name = parent.replace("_", " ").strip()
    return name


# ── URL helpers ───────────────────────────────────────────────────────────────

def path_to_url(full_path: str) -> str:
    """Build a public download URL from an absolute file path.
    Example: /home/.../ebooks/Alex_Archer/book.epub
          →  https://yourdomain.com/kbooks/upload/Alex_Archer/book.epub
    """
    rel = os.path.relpath(full_path, EBOOKS_DIR)
    encoded_parts = [seg.replace(" ", "%20") for seg in rel.split(os.sep)]
    return EBOOKS_BASE_URL.rstrip("/") + "/upload/" + "/".join(encoded_parts)


# ── DB URL → local file helpers ──────────────────────────────────────────────

def _normalize_db_url(url: str) -> str:
    """
    Normalize a DB URL value:
    - convert backslashes to slashes
    - decode URL-encoded characters
    - collapse upload77/upload12/... to upload
    """
    if not url:
        return ""

    value = unquote(url.strip()).replace("\\", "/")
    value = re.sub(r"(?i)^upload\d+/", "upload/", value)
    return value


def _db_url_to_local_path(url: str) -> str:
    """
    Convert a DB books.url value to an absolute file path under EBOOKS_DIR.

    Supported forms:
      - upload\\Author\\Book.epub
      - upload/Author/Book.epub
      - https://domain/.../upload/Author/Book.epub
    """
    value = _normalize_db_url(url)
    if not value:
        return ""

    # If absolute URL, keep only path component
    if value.lower().startswith(("http://", "https://")):
        value = urlparse(value).path.lstrip("/")
        value = _normalize_db_url(value)

    # Prefer the part after the "upload/" segment
    marker = "upload/"
    lower = value.lower()
    if marker in lower:
        idx = lower.find(marker)
        rel_after_upload = value[idx + len(marker):].lstrip("/")
    else:
        rel_after_upload = value.lstrip("/")

    # EBOOKS_DIR already points to ".../upload"
    rel_parts = [p for p in rel_after_upload.split("/") if p]
    return os.path.normpath(os.path.join(EBOOKS_DIR, *rel_parts))


def _db_url_to_public_url(url: str) -> str:
    """
    Convert DB url to a public absolute URL.

    Examples:
      upload\\A\\B.epub -> https://.../upload/A/B.epub
      upload/A/B.epub   -> https://.../upload/A/B.epub
      https://x/y.epub  -> https://x/y.epub
    """
    value = _normalize_db_url(url)
    if not value:
        return ""

    if value.lower().startswith(("http://", "https://")):
        return value

    return f"{EBOOKS_BASE_URL.rstrip('/')}/{value.lstrip('/')}"


def _url_exists(session: requests.Session, url: str, timeout: int = 10) -> tuple[bool, int]:
    """
    Return (exists, status_code) for URL.
    Uses HEAD first, then GET fallback for servers that block HEAD.
    """
    try:
        resp = session.head(url, allow_redirects=True, timeout=timeout)
        if resp.status_code == 200:
            return True, resp.status_code
        if resp.status_code in (403, 405):
            # Some hosts block HEAD; fallback to a lightweight GET.
            resp = session.get(url, allow_redirects=True, timeout=timeout, stream=True)
            return resp.status_code == 200, resp.status_code
        return False, resp.status_code
    except Exception:
        return False, 0


# ── Core indexing ─────────────────────────────────────────────────────────────

def build_index() -> dict:
    """
    Walk EBOOKS_DIR recursively, collect every .epub file,
    extract the author from the immediate parent directory name,
    and return the full index as a dict ready for JSON serialisation.
    """
    if not os.path.isdir(EBOOKS_DIR):
        raise FileNotFoundError(f"EBOOKS_DIR not found: {EBOOKS_DIR}")

    books = []

    for root, _dirs, files in os.walk(EBOOKS_DIR):
        for filename in sorted(files, key=str.lower):
            if not filename.lower().endswith(".epub"):
                continue

            full_path = os.path.join(root, filename)

            try:
                size_bytes = os.path.getsize(full_path)
            except OSError as exc:
                log.warning("Skipping %s: %s", full_path, exc)
                continue

            title  = clean_title(filename)
            author = extract_author(full_path)

            books.append({
                "title":          title,
                "filename":       filename,
                "url":            path_to_url(full_path),
                "size_mb":        round(size_bytes / (1024 * 1024), 2),
                "author":         author,
                # Pre-lowercased for fast case-insensitive search
                "_search":        title.lower(),
                "_author_search": author.lower(),
            })

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total":        len(books),
        "books":        books,
    }


def save_index(index: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(INDEX_FILE)), exist_ok=True)
    with open(INDEX_FILE, "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=2)
    log.info("Index saved: %d books → %s", index["total"], INDEX_FILE)


def load_books() -> list:
    """Load the books list from the JSON index. Returns [] if not built yet."""
    if not os.path.exists(INDEX_FILE):
        return []
    with open(INDEX_FILE, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("books", [])


def check_missing_ebooks_in_db(output_file: str = "./missing_ebooks.json") -> dict:
    """
    Check books table URLs by requesting the actual file URL and write a JSON report.
    Returns a summary dict.
    """
    conn = _get_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, title, url
                FROM books
                WHERE status = 1 AND url IS NOT NULL AND url != ''
                ORDER BY id ASC
                """
            )
            rows = cur.fetchall()

    missing = []
    checked = 0
    with requests.Session() as session:
        for row in rows:
            public_url = _db_url_to_public_url(row["url"])
            exists, status_code = _url_exists(session, public_url)
            checked += 1

            if not exists:
                missing.append(
                    {
                        "id": row["id"],
                        "title": row["title"] or "",
                        "url": row["url"],
                        "checked_url": public_url,
                        "http_status": status_code,
                        "resolved_path": _db_url_to_local_path(row["url"]),
                    }
                )

            if checked % 500 == 0:
                log.info("Checked %d/%d URLs, missing so far: %d", checked, len(rows), len(missing))

    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "ebooks_dir": EBOOKS_DIR,
        "total_checked": checked,
        "missing_count": len(missing),
        "missing_ebooks": missing,
    }

    with open(output_file, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)

    return report


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    log.info("Scanning %s …", EBOOKS_DIR)
    idx = build_index()
    save_index(idx)
    print(f"✅  Indexed {idx['total']} books  →  {INDEX_FILE}")

    try:
        report = check_missing_ebooks_in_db("./missing_ebooks.json")
        print(
            "🔎  DB URL check complete: "
            f"{report['missing_count']} missing of {report['total_checked']} "
            "→ ./missing_ebooks.json"
        )
    except Exception as exc:
        log.warning("DB missing-file check skipped/failed: %s", exc)
