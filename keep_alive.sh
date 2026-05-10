#!/bin/bash
# keep_alive.sh  —  Restart bot.py if it is not already running.
#
# Schedule this in cPanel → Cron Jobs to run every 5 minutes:
#   */5 * * * *  /bin/bash /home1/sflatran/final_ebook_bot/keep_alive.sh
#
# The script is idempotent: if the bot is already running it exits silently.

PYTHON="/home1/sflatran/virtualenv/final_ebook_bot/3.10/bin/python3"
SCRIPT="/home1/sflatran/final_ebook_bot/bot.py"
LOGFILE="/home1/sflatran/final_ebook_bot/bot.log"
PIDFILE="/home1/sflatran/final_ebook_bot/bot.pid"

# ── Check if already running ──────────────────────────────────────────────────
if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    if kill -0 "$PID" 2>/dev/null; then
        # Process is alive — nothing to do
        exit 0
    fi
fi

# ── Start the bot in the background ──────────────────────────────────────────
nohup "$PYTHON" "$SCRIPT" >> "$LOGFILE" 2>&1 &
echo $! > "$PIDFILE"
echo "$(date)  Bot started (PID $!)" >> "$LOGFILE"
