import asyncio
import asyncpg
from app.core.config import DATABASE_URL

url = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")

async def run():
    conn = await asyncpg.connect(url)
    await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS eoa_address TEXT")
    await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_users_eoa_address ON users (eoa_address)")
    await conn.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS safe_address TEXT")
    await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_accounts_safe_address ON accounts (safe_address)")
    await conn.close()
    print("Done — columns added")

asyncio.run(run())
