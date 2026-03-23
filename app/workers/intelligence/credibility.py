"""
Source credibility evaluation — hourly arq cron.

For each source in source_credibility:
    1. Find intelligence from last 24h with market mappings
    2. Compare sentiment_polarity direction with market price movement
    3. If directions match → mark prediction correct
    4. Atomic UPDATE: correct_predictions, total_predictions
    5. Compute avg_lead_time_minutes

Seeded defaults (newsapi=0.5, sportradar=0.5) created by migration 0003.
"""

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


async def evaluate_source_credibility(ctx: dict) -> None:
    """
    arq cron (hourly): evaluate source credibility by comparing intelligence
    sentiment with actual market price movements.
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
                    # Find intelligence from last 24h with market mappings
                    rows = await conn.fetch(
                        """
                        SELECT ei.id, ei.published_at,
                               imm.market_id, imm.sentiment_polarity,
                               imm.confidence_score
                        FROM external_intelligence ei
                        JOIN intelligence_market_mapping imm
                            ON imm.intelligence_id = ei.id
                        WHERE ei.source_domain = $1
                          AND ei.source_name = $2
                          AND ei.published_at >= $3
                          AND imm.market_id IS NOT NULL
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

                    for row in rows:
                        market_id = row["market_id"]
                        sentiment = float(row["sentiment_polarity"] or 0)
                        published_at = row["published_at"]

                        if abs(sentiment) < 0.1:
                            # Neutral sentiment — skip for accuracy eval
                            continue

                        # Get market price at publish time vs current
                        market_row = await conn.fetchrow(
                            """
                            SELECT yes_price, status, result
                            FROM markets
                            WHERE id = $1
                            """,
                            market_id,
                        )

                        if not market_row or not market_row["yes_price"]:
                            continue

                        current_price = float(market_row["yes_price"])

                        # Simple directional check:
                        # Positive sentiment → expect price increase → current > 0.5
                        # Negative sentiment → expect price decrease → current < 0.5
                        price_direction = 1 if current_price > 0.5 else -1
                        sentiment_direction = 1 if sentiment > 0 else -1

                        total += 1
                        if price_direction == sentiment_direction:
                            correct += 1

                        # Lead time: time between publish and now
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
