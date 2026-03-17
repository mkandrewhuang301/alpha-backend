"""
Group alert workers — auto-inject system messages when markets move or resolve.

check_price_alerts (every 5min, prod only):
    Detects markets that have moved >= 10% since they were shared.
    Inserts a system message in affected groups.

check_resolution_alerts (every 2min, prod only):
    Finds recently resolved markets, updates trade message metadata,
    and inserts resolution system messages.

Both workers are safe to fail (try/except around body) — never crash arq process.
"""

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

logger = logging.getLogger(__name__)


async def check_price_alerts(ctx: dict) -> None:
    """
    Find markets that have moved >= 10% since they were shared in a group.
    Insert system messages in affected groups. Deduplicate within 60-min window.
    """
    try:
        from sqlalchemy import select

        from app.core.database import async_session_factory
        from app.core.redis import get_redis
        from app.models.db import GroupMessage, Market, MarketOutcome

        redis = await get_redis()

        async with async_session_factory() as db:
            # Find recently shared market messages (last 24h, not deleted)
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            stmt = select(GroupMessage).where(
                GroupMessage.type == "market",
                GroupMessage.is_deleted.is_(False),
                GroupMessage.created_at > cutoff,
            )
            result = await db.execute(stmt)
            market_messages = result.scalars().all()

            if not market_messages:
                return

            # Build unique market_id → [(group_id, shared_price), ...] map
            market_to_groups: dict[str, list[tuple]] = {}
            for msg in market_messages:
                meta = msg.msg_metadata or {}
                market_id = meta.get("market_id")
                shared_price = meta.get("shared_yes_price")
                if not market_id or shared_price is None:
                    continue
                if market_id not in market_to_groups:
                    market_to_groups[market_id] = []
                market_to_groups[market_id].append((str(msg.group_id), float(shared_price)))

            for market_id_str, group_data in market_to_groups.items():
                try:
                    market_uuid = UUID(market_id_str)
                except ValueError:
                    continue

                market = await db.get(Market, market_uuid)
                if not market or market.status in ("resolved", "canceled"):
                    continue

                # Get current live price from Redis via yes outcome execution_asset_id
                current_price = None
                try:
                    outcome_stmt = select(MarketOutcome.execution_asset_id).where(
                        MarketOutcome.market_id == market_uuid,
                        MarketOutcome.side == "yes",
                    )
                    outcome_result = await db.execute(outcome_stmt)
                    yes_asset = outcome_result.scalar_one_or_none()
                    if yes_asset:
                        redis_key = f"ticker:{market.exchange}:{yes_asset}"
                        tick = await redis.hgetall(redis_key)
                        if tick and tick.get("price"):
                            current_price = float(tick["price"])
                except Exception:
                    pass

                if current_price is None:
                    continue

                # Check each group that shared this market
                seen_groups: set[str] = set()
                for group_id_str, shared_price in group_data:
                    if group_id_str in seen_groups:
                        continue
                    seen_groups.add(group_id_str)

                    if shared_price <= 0:
                        continue
                    price_change_pct = abs(current_price - shared_price) / shared_price * 100
                    if price_change_pct < 10.0:
                        continue

                    # Dedup: skip if identical alert in last 60 min
                    dedup_key = f"alert_dedup:{group_id_str}:{market_id_str}"
                    if await redis.get(dedup_key):
                        continue

                    direction = "up" if current_price > shared_price else "down"
                    detail = (
                        f"Market moved {direction} {price_change_pct:.0f}% "
                        f"since shared (was {shared_price:.0%}, now {current_price:.0%})"
                    )
                    system_msg = GroupMessage(
                        group_id=UUID(group_id_str),
                        sender_id=None,
                        type="system",
                        content=None,
                        msg_metadata={
                            "event": "price_alert",
                            "market_id": market_id_str,
                            "detail": detail,
                        },
                    )
                    db.add(system_msg)
                    await redis.set(dedup_key, "1", ex=3600)

            await db.commit()

    except Exception as exc:
        logger.error("[group_alerts] check_price_alerts failed: %s", exc, exc_info=True)


async def check_resolution_alerts(ctx: dict) -> None:
    """
    Find markets that resolved in the last 3 minutes.
    Update trade message metadata with resolved=true + profit_pct.
    Insert resolution system messages in affected groups.
    """
    try:
        from sqlalchemy import select, text as sa_text

        from app.core.database import async_session_factory
        from app.core.redis import get_redis
        from app.models.db import GroupMessage, Market

        redis = await get_redis()

        async with async_session_factory() as db:
            # Find markets resolved in the last 3 minutes
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=3)
            stmt = select(Market).where(
                Market.status == "resolved",
                Market.resolve_time.isnot(None),
                Market.resolve_time > cutoff,
            )
            result = await db.execute(stmt)
            resolved_markets = result.scalars().all()

            if not resolved_markets:
                return

            for market in resolved_markets:
                market_id_str = str(market.id)

                # Find group messages referencing this market via idx_messages_market_ref
                msgs_stmt = (
                    select(GroupMessage)
                    .where(
                        GroupMessage.type.in_(["market", "trade"]),
                        GroupMessage.is_deleted.is_(False),
                    )
                    .where(sa_text("metadata->>'market_id' = :mid"))
                    .params(mid=market_id_str)
                )
                msgs_result = await db.execute(msgs_stmt)
                affected_messages = msgs_result.scalars().all()

                if not affected_messages:
                    continue

                affected_groups: set[str] = set()

                for msg in affected_messages:
                    affected_groups.add(str(msg.group_id))

                    # Update trade messages with resolution outcome
                    if msg.type == "trade":
                        meta = dict(msg.msg_metadata or {})
                        meta["resolved"] = True
                        try:
                            snapshot = meta.get("snapshot") or {}
                            entry_price = float(snapshot.get("price", 0))
                            side = snapshot.get("side", "yes")
                            won = (
                                (side == "yes" and market.result == "yes") or
                                (side == "no" and market.result == "no")
                            )
                            if entry_price > 0:
                                exit_price = 1.0 if won else 0.0
                                meta["profit_pct"] = round(
                                    (exit_price - entry_price) / entry_price * 100, 1
                                )
                        except Exception:
                            pass
                        msg.msg_metadata = meta

                # Insert resolution system messages per affected group (with dedup)
                for group_id_str in affected_groups:
                    dedup_key = f"resolve_dedup:{group_id_str}:{market_id_str}"
                    if await redis.get(dedup_key):
                        continue

                    detail = f"Market resolved: {market.result or 'outcome determined'}"
                    system_msg = GroupMessage(
                        group_id=UUID(group_id_str),
                        sender_id=None,
                        type="system",
                        content=None,
                        msg_metadata={
                            "event": "resolution",
                            "market_id": market_id_str,
                            "detail": detail,
                        },
                    )
                    db.add(system_msg)
                    await redis.set(dedup_key, "1", ex=86400)

            await db.commit()

    except Exception as exc:
        logger.error("[group_alerts] check_resolution_alerts failed: %s", exc, exc_info=True)
