# =============================================================
#  config.py  —  EBook Bot Configuration
#  Fill in all values before deploying to cPanel
# =============================================================

import os

# ── Telegram ──────────────────────────────────────────────────
# SECURITY: set BOT_TOKEN as a cPanel environment variable, not here.
# In cPanel → Setup Python App → Environment variables, add BOT_TOKEN.
# Fallback to the string below only for local testing.
# ── Telegram ──────────────────────────────────────────────────
BOT_TOKEN   = "8404714838:AAG4IeItbW6SfPVJVe1VMghE34OGPxFPjUU"  

# ── Database ──────────────────────────────────────────────────
# DB_HOST     = os.environ.get("DB_HOST",     "localhost")
# DB_PORT     = int(os.environ.get("DB_PORT", "3306"))
# DB_USER     = os.environ.get("DB_USER",     "sflatran_kbook1")
# DB_PASSWORD = os.environ.get("DB_PASSWORD", "kiduyuKLAUS2801")
# DB_NAME     = os.environ.get("DB_NAME",     "sflatran_kbooks")

# ── Database ──────────────────────────────────────────────────
DB_HOST     = os.environ.get("DB_HOST",     "localhost")
DB_PORT     = int(os.environ.get("DB_PORT", "3306"))
DB_USER     = os.environ.get("DB_USER",     "root")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_NAME     = os.environ.get("DB_NAME",     "final_klaus_ebooks_library")

# ── File Server ───────────────────────────────────────────────
# Absolute server path to the directory containing your EPUBs.
# On cPanel this is typically: /home/<username>/public_html/kbooks
EBOOKS_DIR      = "/home1/sflatran/kbooks/public/upload"



# Public base URL for direct downloads.
EBOOKS_BASE_URL = "https://sflatransport.com/kbooks"

# Path where the JSON index will be written (inside the bot folder).
INDEX_FILE      = "./books_index.json"

# ── Behaviour ─────────────────────────────────────────────────
RESULTS_PER_PAGE = 5      # Books shown per page in search results
MAX_RESULTS      = 200    # Hard cap on total results returned
BOT_NAME         = "📚 eBook Bot"

# ── Admin user IDs (Telegram numeric IDs) ─────────────────────
# Only these users can run /reindex.  Leave empty [] to allow all.
ADMIN_IDS = [5387934188]   # e.g. [123456789, 987654321]