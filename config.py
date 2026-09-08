import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.environ["BOT_TOKEN"]
ADMIN_USER_ID: int = int(os.environ["ADMIN_USER_ID"])
DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///bot.db")
ENCRYPTION_KEY: str = os.getenv("ENCRYPTION_KEY", "")
LOG_FILE: str = os.getenv("LOG_FILE", "bot.log")

MIN_INTERVAL_SECONDS: int = 5
DEFAULT_INTERVAL_SECONDS: int = 30
DUPLICATE_CHECK_DAYS: int = 30
