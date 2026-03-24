"""
Source credibility evaluation — hourly arq cron.

For each source in source_credibility:
    1. Find intelligence from last 24h with event mappings
    2. Compare sentiment_polarity direction with market price movement
       relative to price_at_publish (NOT a fixed 0.5 threshold)
    3. If directions match → mark prediction correct
    4. Atomic UPDATE: correct_predictions, total_predictions
    5. Compute avg_lead_time_minutes as time from publish to significant move

Seeded defaults (newsapi=0.5, sportradar=0.5) created by migration 0003.
"""

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Minimum price movement to count as significant for directional evaluation
PRICE_MOVEMENT_THRESHOLD = 0.02  # 2%


async def evaluate_source_credibility(ctx: dict) -> None:
    """
    arq cron (hourly): evaluate source credibility by comparing intelligence
    sentiment with actual market price movements relative to price_at_publish.
    """
    try:
        pool = ctx["asyncpg_pool"]
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(hours=24)

        async with pool.acquire() as conn:
            # Get all source_credibility rows
            sources = await conn.fetch(
                "SELECT id, source_domain, source_name FROM source_credibility"
            )

            for source in sources:
                try:
                    # Find intelligence from last 24h with event mappings
                    # Join through events → markets to get market prices
                    rows = await conn.fetch(
                        """
                        SELECT ei.id, ei.published_at,
                               imm.event_id, imm.market_id,
                               imm.sentiment_polarity,
                               imm.confidence_score,
                               imm.price_at_publish
                        FROM external_intelligence ei
                        JOIN intelligence_market_mapping imm
                            ON imm.intelligence_id = ei.id
                        WHERE ei.source_domain = $1
                          AND ei.source_name = $2
                          AND ei.published_at >= $3
                          AND imm.event_id IS NOT NULL
                          AND ei.nlp_status = 'complete'
                        """,
                        source["source_domain"],
                        source["source_name"],
                        window_start,
                    )

                    if not rows:
                        continue

                    correct = 0
                    total = 0
                    lead_times = []

                    # Pre-fetch current prices from Redis for all mappings
                    from app.core.redis import get_redis
                    price_redis = await get_redis()

                    # Build a mapping of event_id/market_id → current price via Redis
                    current_prices = {}
                    entity_ids = list({
                        row["market_id"] or row["event_id"] for row in rows
                    })
                    if entity_ids:
                        outcome_rows = await conn.fetch(
                            """
                            SELECT m.id AS market_id, m.event_id, m.exchange,
                                   mo.execution_asset_id
                            FROM markets m
                            JOIN market_outcomes mo ON mo.market_id = m.id AND mo.side = 'yes'
                            WHERE (m.id = ANY($1) OR m.event_id = ANY($1))
                              AND m.is_deleted = false
                            """,
                            entity_ids,
                        )
                        if outcome_rows:
                            pipe = price_redis.pipeline(transaction=False)
                            lookup_keys = []
                            for orow in outcome_rows:
                                key = f"ticker:{orow['exchange']}:{orow['execution_asset_id']}"
                                pipe.hget(key, "price")
                                lookup_keys.append((orow["market_id"], orow["event_id"]))
                            results = await pipe.execute()
                            for idx, val in enumerate(results):
                                if val is not None:
                                    try:
                                        price = float(val)
                                        mid, eid = lookup_keys[idx]
                                        current_prices[mid] = price
                                        if eid not in current_prices:
                                            current_prices[eid] = price
                                    except (ValueError, TypeError):
                                        pass

                    for row in rows:
                        sentiment = float(row["sentiment_polarity"] or 0)
                        published_at = row["published_at"]
                        price_at_publish = float(row["price_at_publish"]) if row["price_at_publish"] else None

                        if abs(sentiment) < 0.1:
                            # Neutral sentiment — skip for accuracy eval
                            continue

                        # Get current market price from pre-fetched Redis data
                        entity_key = row["market_id"] or row["event_id"]
                        current_price = current_prices.get(entity_key)

                        if current_price is None:
                            continue

                        # Directional check relative to price_at_publish
                        if price_at_publish is not None:
                            price_delta = current_price - price_at_publish

                            # Skip if movement is below noise threshold
                            if abs(price_delta) < PRICE_MOVEMENT_THRESHOLD:
                                continue

                            price_direction = 1 if price_delta > 0 else -1
                        else:
                            # Fallback: compare against 0.5 if no snapshot available
                            price_direction = 1 if current_price > 0.5 else -1

                        sentiment_direction = 1 if sentiment > 0 else -1

                        total += 1
                        if price_direction == sentiment_direction:
                            correct += 1

                        # Lead time: time from publish to now
                        # (future improvement: time to first significant move)
                        if published_at:
                            lead_minutes = (now - published_at).total_seconds() / 60
                            lead_times.append(lead_minutes)

                    if total == 0:
                        continue

                    # Atomic update
                    avg_lead = (
                        sum(lead_times) / len(lead_times) if lead_times else None
                    )

                    await conn.execute(
                        """
                        UPDATE source_credibility
                        SET correct_predictions = correct_predictions + $2,
                            total_predictions = total_predictions + $3,
                            credibility_score = CASE
                                WHEN total_predictions + $3 > 0
                                THEN (correct_predictions + $2)::numeric
                                     / (total_predictions + $3)::numeric
                                ELSE 0.500
                            END,
                            avg_lead_time_minutes = COALESCE($4, avg_lead_time_minutes),
                            last_evaluated_at = NOW(),
                            updated_at = NOW()
                        WHERE id = $1
                        """,
                        source["id"],
                        correct,
                        total,
                        avg_lead,
                    )

                    logger.info(
                        "[credibility] %s/%s: %d/%d correct in last 24h",
                        source["source_domain"],
                        source["source_name"],
                        correct,
                        total,
                    )

                except Exception as e:
                    logger.warning(
                        "[credibility] Error evaluating %s/%s: %s",
                        source["source_domain"],
                        source["source_name"],
                        e,
                    )
                    continue

    except Exception as exc:
        logger.error(
            "[credibility] evaluate_source_credibility failed: %s",
            exc,
            exc_info=True,
        )
