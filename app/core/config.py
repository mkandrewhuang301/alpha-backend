from dotenv import load_dotenv
import os

load_dotenv()

KALSHI_API_KEY = os.getenv("KALSHI_API_KEY_ID")
KALSHI_PRIVATE_KEY = os.getenv("KALSHI_PRIVATE_KEY", "").replace("\\n", "\n")
KALSHI_BASE_URL = os.getenv("KALSHI_BASE_URL", "https://trading-api.kalshi.com/trade-api/v2")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# PostgreSQL via Supabase — format: postgresql+asyncpg://user:pass@host:port/db
DATABASE_URL = os.getenv("DATABASE_URL", "")

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")