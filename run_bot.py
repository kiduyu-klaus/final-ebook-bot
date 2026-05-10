"""
run_bot.py  —  Bot Runner

Entry point for the EBook Telegram Bot.
Handles logging setup, graceful error reporting, and delegates
to bot.main() for the actual polling loop.

Usage:
    python3 run_bot.py

Called by passenger_wsgi.py as a subprocess when deploying on cPanel.
"""

import logging
import sys
from pathlib import Path

# Ensure the project root is on the path regardless of working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))


def setup_logging() -> None:
    """Configure root logger with timestamp + level formatting."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
        ],
    )


def main() -> None:
    """Set up logging and run the bot."""
    setup_logging()
    logger = logging.getLogger(__name__)
    logger.info("Bot runner starting…")

    try:
        from bot import main as run_bot
        run_bot()
    except KeyboardInterrupt:
        logger.info("Bot runner stopped by user.")
    except Exception as exc:
        logger.exception("Bot runner encountered a fatal error: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
