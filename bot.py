"""Telegram bot that publishes curated Hacker News posts with layered context.
Handles both discussion styles:
- Forum supergroup (Topics ON): comment via message_thread_id.
- Regular channel (Topics OFF): comment by replying to the auto-forwarded message.
"""

import os
import time

from dotenv import load_dotenv

from clients.telegram import TelegramAPIError, TelegramClient
from publish.handlers import (
    handle_callback_query,
    handle_message,
    set_telegram,
)
from utils import configure_file_logging, logger, format_http_error


log_path = configure_file_logging("log_bot.log")
logger.info("Bot logging configured", log_path=str(log_path))

load_dotenv()
TOKEN = os.getenv("TG_TOKEN")
if not TOKEN:
    raise SystemExit("TG_TOKEN env var is required")

logger.info("Environment loaded", token_set=bool(TOKEN))

telegram = TelegramClient(TOKEN)
set_telegram(telegram)


def main() -> None:
    """Continuously poll Telegram for updates and dispatch them to handlers."""
    # Offset tracks the highest update we've processed so we resume cleanly
    # after network hiccups or restarts.
    offset = None
    while True:
        try:
            # Long-poll the Bot API so a single process can serialise all
            # updates; we process messages and callbacks in lock-step to keep
            # state transitions predictable.
            for update in telegram.get_updates(offset=offset):
                offset = update["update_id"] + 1
                handle_message(update)
                handle_callback_query(update)
        except KeyboardInterrupt:
            logger.info("Stopping bot on keyboard interrupt")
            break
        except TelegramAPIError as api_err:
            error_text = format_http_error(api_err)
            if "read timed out" in error_text.lower():
                continue
            logger.error("Telegram API error", error=error_text)
            time.sleep(5)
        except Exception as exc:
            logger.exception("Unhandled error in main loop", error=str(exc))
            time.sleep(5)


if __name__ == "__main__":
    main()
