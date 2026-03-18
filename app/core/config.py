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

# Magic Link — kept for reference, replaced by Privy
MAGIC_SECRET_KEY = os.getenv("MAGIC_SECRET_KEY", "")

# Privy — for verifying auth tokens sent from the iOS app
PRIVY_APP_ID = os.getenv("PRIVY_APP_ID", "").strip()
PRIVY_APP_SECRET = os.getenv("PRIVY_APP_SECRET", "").strip()
PRIVY_VERIFICATION_KEY = os.getenv("PRIVY_VERIFICATION_KEY", "").strip().replace("\\n", "\n")

# Polygon RPC — for token approvals, balance reads
POLYGON_RPC_URL = os.getenv("POLYGON_RPC_URL", "")

# Polymarket — CLOB API credentials (for reading markets + submitting orders)
POLYMARKET_API_KEY = os.getenv("POLYMARKET_API_KEY", "")
POLYMARKET_API_SECRET = os.getenv("POLYMARKET_API_SECRET", "")
POLYMARKET_API_PASSPHRASE = os.getenv("POLYMARKET_API_PASSPHRASE", "")

# Polymarket — relayer credentials (for Safe deployment + gasless transactions)
POLYMARKET_RELAYER_URL = os.getenv("POLYMARKET_RELAYER_URL", "https://relayer-v2.polymarket.com")
POLYMARKET_RELAYER_KEY = os.getenv("POLYMARKET_RELAYER_KEY", "")
POLYMARKET_RELAYER_ADDRESS = os.getenv("POLYMARKET_RELAYER_ADDRESS", "")

# Polymarket — builder credentials (for order attribution + fee sharing)
POLYMARKET_BUILDER_KEY = os.getenv("POLYMARKET_BUILDER_KEY", "")
POLYMARKET_BUILDER_ADDRESS = os.getenv("POLYMARKET_BUILDER_ADDRESS", "")

# Polymarket — CLOB API host
CLOB_HOST = os.getenv("CLOB_HOST", "https://clob.polymarket.com")

# Encryption key for storing user CLOB credentials in DB (Fernet 32-byte URL-safe base64)
# Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")
