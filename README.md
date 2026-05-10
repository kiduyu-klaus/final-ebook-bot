# EBook Telegram Bot — Deployment Guide (cPanel, polling mode)

## File structure

```
ebookbot/
├── bot.py              ← Bot logic + infinity_polling
├── indexer.py          ← EPUB folder scanner
├── config.py           ← All settings live here
├── keep_alive.sh       ← Cron script that keeps the bot running
├── requirements.txt
├── books_index.json    ← Auto-generated; do not edit manually
├── bot.log             ← Runtime log (auto-created)
└── bot.pid             ← PID file (auto-created)
```

---

## Step 1 — Upload files

Upload the `ebookbot/` folder to your cPanel home directory:

    /home/yourusername/ebookbot/

---

## Step 2 — Edit config.py

| Key               | Example value |
|-------------------|---------------|
| `BOT_TOKEN`       | `7123456789:AAF…` (from @BotFather) |
| `EBOOKS_DIR`      | `/home/yourusername/public_html/ebooks` |
| `EBOOKS_BASE_URL` | `https://yourdomain.com/ebooks` |
| `INDEX_FILE`      | `/home/yourusername/ebookbot/books_index.json` |

---

## Step 3 — Create a Python virtualenv in cPanel

1. cPanel → **Setup Python App**
2. Click **Create Application**
3. Fill in:
   - **Python version**: 3.11 (or latest)
   - **Application root**: `ebookbot`
   - **Application URL**: *(any — only used for WSGI apps, ignored here)*
4. Click **Create**, then copy the `source` activate command shown.

---

## Step 4 — Install dependencies

SSH into your server:

```bash
source /home/yourusername/virtualenv/ebookbot/3.11/bin/activate
pip install -r ~/ebookbot/requirements.txt
```

---

## Step 5 — Build the book index

```bash
python3 ~/ebookbot/indexer.py
```

Expected output:
```
✅  Indexed 142 books  →  /home/yourusername/ebookbot/books_index.json
```

---

## Step 6 — Edit keep_alive.sh

Open `keep_alive.sh` and set the correct paths for `PYTHON`, `SCRIPT`, `LOGFILE`, and `PIDFILE`.

---

## Step 7 — Start the bot

```bash
chmod +x ~/ebookbot/keep_alive.sh
bash ~/ebookbot/keep_alive.sh
```

Confirm it is running:

```bash
cat ~/ebookbot/bot.pid          # shows the PID
tail -f ~/ebookbot/bot.log      # live log output
```

---

## Step 8 — Add the cron job (keep-alive)

In cPanel → **Cron Jobs**, add a new job:

| Field   | Value |
|---------|-------|
| Minute  | `*/5` |
| Hour    | `*`   |
| Day     | `*`   |
| Month   | `*`   |
| Weekday | `*`   |
| Command | `/bin/bash /home/yourusername/ebookbot/keep_alive.sh` |

This checks every 5 minutes whether the bot is still running and restarts it if it crashed.

---

## Step 9 — Schedule re-indexing (optional)

Add a second cron job to pick up newly added books automatically:

```
0 */6 * * *  /home/yourusername/virtualenv/ebookbot/3.11/bin/python3 \
             /home/yourusername/ebookbot/indexer.py \
             >> /home/yourusername/ebookbot/indexer.log 2>&1
```

---

## Stopping the bot

```bash
kill $(cat ~/ebookbot/bot.pid)
rm ~/ebookbot/bot.pid
```

---

## Bot commands summary

| Command | Description |
|---------|-------------|
| `/start` | Welcome message + library count |
| `/search <title>` | Search books by keywords |
| `/search` | List all books |
| `/help` | Usage instructions |
| `/reindex` | Rebuild index (admin only if `ADMIN_IDS` is set) |

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Bot does not respond | `tail -f bot.log` — look for errors; check `BOT_TOKEN` |
| `No books found` for everything | Run `python3 indexer.py` and verify `INDEX_FILE` path in `config.py` |
| Bot keeps dying | Check `bot.log` for crash reason; `keep_alive.sh` will restart it within 5 min |
| `ModuleNotFoundError` | Re-run `pip install -r requirements.txt` inside the virtualenv |
