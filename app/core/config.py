from dotenv import load_dotenv
import os

load_dotenv()

KALSHI_API_KEY = os.getenv("KALSHI_API_KEY_ID")
KALSHI_PRIVATE_KEY = os.getenv("KALSHI_PRIVATE_KEY", "").replace("\\n", "\n")
KALSHI_BASE_API_URL = os.getenv("KALSHI_BASE_API_URL", "https://api.elections.kalshi.com/trade-api/v2")
KALSHI_WS_URL = os.getenv("KALSHI_WS_URL", "wss://api.elections.kalshi.com/trade-api/ws/v2")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")

# PostgreSQL via Supabase — format: postgresql+asyncpg://user:pass@host:port/db
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Redis — used by arq worker queue and ticker price cache
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

# Development sandbox mode — restricts ingestion and WebSocket to a small
# subset of markets to stay within free-tier Redis Cloud ops/sec limits.
DEV_MODE = os.getenv("DEV_MODE", "false").lower() in ("true", "1", "yes")

# Magic Link — for verifying DID tokens sent from the iOS app
MAGIC_SECRET_KEY = os.getenv("MAGIC_SECRET_KEY", "")

# Polygon RPC — for Safe deployment, token approvals, balance reads
POLYGON_RPC_URL = os.getenv("POLYGON_RPC_URL", "")

# Polymarket — CLOB API credentials (for reading markets + submitting orders)
POLYMARKET_API_KEY = os.getenv("POLYMARKET_API_KEY", "")
POLYMARKET_API_SECRET = os.getenv("POLYMARKET_API_SECRET", "")
POLYMARKET_API_PASSPHRASE = os.getenv("POLYMARKET_API_PASSPHRASE", "")

# Polymarket — builder credentials (for order attribution + fee sharing)
POLYMARKET_BUILDER_KEY = os.getenv("POLYMARKET_BUILDER_KEY", "")
POLYMARKET_BUILDER_ADDRESS = os.getenv("POLYMARKET_BUILDER_ADDRESS", "")