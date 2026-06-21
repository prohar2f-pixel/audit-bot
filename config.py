import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]
OWNER_TELEGRAM_ID = int(os.environ.get("OWNER_TELEGRAM_ID", "5089980481"))
PAGESPEED_API_KEY = os.environ.get("PAGESPEED_API_KEY", "")
OWNER_TELEGRAM_USERNAME = os.environ.get("OWNER_TELEGRAM_USERNAME", "")
OWNER_BIO = os.environ.get("OWNER_BIO", "")
