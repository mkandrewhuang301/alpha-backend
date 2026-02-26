from dotenv import load_dotenv
import os

load_dotenv()

KALSHI_API_KEY = os.getenv("KALSHI_API_KEY_ID")
KALSHI_PRIVATE_KEY = os.getenv("KALSHI_PRIVATE_KEY")
KALSHI_BASE_URL = os.getenv("KALSHI_BASE_URL")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")