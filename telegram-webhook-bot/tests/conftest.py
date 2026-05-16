"""
Shared test setup.

Sets TESTING=1 *before* `app` is imported so the module-level scheduler,
Telegram polling thread and watchdog stay dormant during tests. Also pins
fake values for the two env vars that `app` reads at import time, so the
test process doesn't accidentally talk to a real Telegram bot.
"""
import os

os.environ["TESTING"] = "1"
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "0")
