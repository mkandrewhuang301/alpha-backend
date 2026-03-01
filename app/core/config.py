from dotenv import load_dotenv
import os

load_dotenv()

KALSHI_API_KEY = os.getenv("KALSHI_API_KEY_ID")
KALSHI_PRIVATE_KEY = os.getenv("KALSHI_PRIVATE_KEY", "").replace("\\n", "\n")
KALSHI_BASE_API_URL = os.getenv("KALSHI_BASE_API_URL", "https://api.elections.kalshi.com/trade-api/v2")
KALSHI_WS_URL = os.getenv("KALSHI_WS_URL", "wss://api.elections.kalshi.com/trade-api/ws/v2")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# PostgreSQL via Supabase — format: postgresql+asyncpg://user:pass@host:port/db
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Redis — used by arq worker queue and ticker price cache
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")