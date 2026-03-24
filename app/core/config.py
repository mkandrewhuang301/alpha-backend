from dotenv import load_dotenv
import os

load_dotenv()

KALSHI_API_KEY = os.getenv("KALSHI_API_KEY_ID")
KALSHI_PRIVATE_KEY = os.getenv("KALSHI_PRIVATE_KEY", "").replace("\\n", "\n")
KALSHI_BASE_API_URL = os.getenv("KALSHI_BASE_API_URL", "https://api.elections.kalshi.com/trade-api/v2")
KALSHI_WS_URL = os.getenv("KALSHI_WS_URL", "wss://api.elections.kalshi.com/trade-api/ws/v2")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")
SPORTRADAR_API_KEY = os.getenv("SPORTRADAR_API_KEY", "")
SPORTRADAR_TIER = os.getenv("SPORTRADAR_TIER", "trial")  # "trial" or "production"
FIREBASE_CREDENTIALS_JSON = os.getenv("FIREBASE_CREDENTIALS_JSON", "")
FCM_ENABLED = os.getenv("FCM_ENABLED", "false").lower() in ("true", "1", "yes")
INTELLIGENCE_ENABLED = os.getenv("INTELLIGENCE_ENABLED", "false").lower() in ("true", "1", "yes")
NEWSAPI_POLL_INTERVAL_SECONDS = int(os.getenv("NEWSAPI_POLL_INTERVAL_SECONDS", "120"))

# PostgreSQL via Supabase — format: postgresql+asyncpg://user:pass@host:port/db
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Redis — used by arq worker queue and ticker price cache
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

# Development sandbox mode — restricts ingestion and WebSocket to a small
# subset of markets to stay within free-tier Redis Cloud ops/sec limits.
DEV_MODE = os.getenv("DEV_MODE", "false").lower() in ("true", "1", "yes")

# Supabase — used for Auth JWT verification and user management
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

# Optional: Supabase JWT secret for HS256 fallback (older Supabase projects).
# If not set, only RS256/JWKS verification is used.
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "")