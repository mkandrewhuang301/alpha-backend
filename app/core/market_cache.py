"""
Generalized Redis cache for real-time market ticker data.

Key schema: ticker:{exchange}:{execution_asset_id}

For Kalshi, execution_asset_id is a composite: {market_ticker}-{side}
    e.g. ticker:kalshi:KXINFLATION-24-yes, ticker:kalshi:KXINFLATION-24-no

For Polymarket, execution_asset_id is the ERC-1155 token ID (already globally unique)
    e.g. ticker:polymarket:0x1234...

Each key is a Redis HSET with fields:
    price, bid, bid_size, ask, ask_size, volume_24h
"""

from decimal import Decimal, ROUND_HALF_UP

import redis.asyncio as aioredis


def _make_key(exchange: str, asset_id: str) -> str:
    """Build a Redis key from exchange and execution asset ID."""
    return f"ticker:{exchange}:{asset_id}"


def _normalize_cents(value) -> str:
    """
    Convert Kalshi cents (0-100 int) to a normalized Decimal string (0.00-1.00).
    Passes through None/0 as "0".
    """
    if value is None:
        return "0"
    d = Decimal(str(value)) / Decimal("100")
    return str(d.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))


class MarketCacheManager:
    """Thin wrapper around Redis for reading/writing per-outcome ticker data."""

    def __init__(self, redis: aioredis.Redis):
        self._redis = redis

    async def update_ticker(
        self,
        exchange: str,
        asset_id: str,
        data: dict[str, str],
    ) -> None:
        """
        Write ticker data for a single outcome.

        Expected fields in data: price, bid, bid_size, ask, ask_size, volume_24h.
        All values should already be strings.
        """
        key = _make_key(exchange, asset_id)
        await self._redis.hset(key, mapping=data)

    async def get_ticker(self, exchange: str, asset_id: str) -> dict[str, str]:
        """Fetch all cached fields for a single outcome. Returns empty dict if missing."""
        key = _make_key(exchange, asset_id)
        return await self._redis.hgetall(key)

    async def get_tickers(
        self,
        exchange: str,
        asset_ids: list[str],
    ) -> dict[str, dict[str, str]]:
        """
        Batch-fetch ticker data for multiple outcomes via Redis pipeline.
        Returns {asset_id: {field: value}} — missing keys map to empty dict.
        """
        if not asset_ids:
            return {}

        pipe = self._redis.pipeline(transaction=False)
        for aid in asset_ids:
            pipe.hgetall(_make_key(exchange, aid))

        results = await pipe.execute()
        return {aid: (res or {}) for aid, res in zip(asset_ids, results)}
