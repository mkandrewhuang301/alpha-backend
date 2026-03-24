"""
Sportradar ingestion worker — polls injuries + transactions every 2 minutes.

Pipeline:
    Sportradar REST → Redis dedup → PostgreSQL bulk insert → enqueue NLP batch

Data types ingested:
    injuries     — player injury status (out/doubtful/questionable/probable/dtd)
    transactions — trades, signings, cuts, IR placements, waivers

Sports coverage:
    NBA (basketball) — v8 API, in-season
    NFL (football)   — v7 API, transactions valuable year-round (free agency, draft)
    MLB (baseball)   — v7 API, probable pitchers are highest-signal data
    NHL (hockey)     — v7 API (add to ACTIVE_SPORTS when ready)
    Soccer           — different competition-based API, separate file needed

Impact level mapping (injuries):
    out / doubtful         → high
    questionable           → medium
    probable / day-to-day  → low

Impact level mapping (transactions):
    trade / cut / released / injured_reserve / ir → high
    signed / waived / claimed / dfa               → medium
    practice_squad / contract_extension           → low

Safe to fail: outer try/except prevents arq process crash.
"""

import logging
from datetime import datetime, timezone

import httpx

from app.core.config import SPORTRADAR_API_KEY, SPORTRADAR_TIER
from app.workers.intelligence.base import (
    bulk_insert_intelligence,
    check_redis_dedup,
    content_hash,
)

logger = logging.getLogger(__name__)

SPORTRADAR_BASE_URL = "https://api.sportradar.com"

# Sport → (api_path, api_version)
# Each sport has its own Sportradar API version — do NOT assume they share one.
# Soccer is excluded: it uses a competition-based URL structure (not league-wide)
# and needs a separate ingestion file.
SPORT_API_CONFIG: dict[str, tuple[str, str]] = {
    "basketball": ("nba", "v8"),
    "football":   ("nfl", "v7"),
    "baseball":   ("mlb", "v7"),
    "hockey":     ("nhl", "v7"),
}

# Sportradar sports to actively ingest. Subset of SPORT_API_CONFIG.
# Add "hockey" once NHL prediction market volume warrants it.
ACTIVE_SPORTS = {"basketball", "football", "baseball"}

# Injury status → impact_level
# Covers both traditional (NFL/NBA) and MLB IL statuses (D10/D15/D60)
STATUS_IMPACT_MAP = {
    # Traditional statuses (NFL/NBA)
    "out":          "high",
    "doubtful":     "high",
    "questionable": "medium",
    "probable":     "low",
    "day-to-day":   "low",
    # MLB Injured List
    "d60":  "high",   # 60-day IL
    "d15":  "high",   # 15-day IL
    "d10":  "high",   # 10-day IL
    "d7":   "medium", # 7-day IL
    "il60": "high",
    "il15": "high",
    "il10": "high",
    # Other statuses
    "restricted":   "low",
    "suspended":    "low",
    "bereavement":  "low",
    "paternity":    "low",
}

# Transaction type → impact_level
# Covers all Sportradar transaction type strings across NBA/NFL/MLB.
TRANSACTION_IMPACT_MAP = {
    # High — immediate roster/availability impact
    "trade":              "high",
    "traded":             "high",
    "cut":                "high",
    "released":           "high",
    "injured_reserve":    "high",
    "ir":                 "high",
    "placed_on_ir":       "high",
    "placed_on_il":       "high",   # MLB injured list
    "il_10_day":          "high",
    "il_15_day":          "high",
    "il_60_day":          "high",
    "dl":                 "high",   # MLB disabled list (legacy term)
    "waived_terminated":  "high",
    # Medium — changes roster composition but not immediate availability
    "signed":             "medium",
    "free_agent_signing": "medium",
    "waived":             "medium",
    "claimed":            "medium",
    "waivers":            "medium",
    "designated_for_assignment": "medium",  # MLB DFA
    "dfa":                "medium",
    "activated":          "medium",   # return from IR/IL
    "reinstated":         "medium",
    # Low — roster depth / administrative
    "practice_squad":          "low",
    "practice_squad_signed":   "low",
    "practice_squad_released": "low",
    "contract_extension":      "low",
    "retired":                 "low",
    "suspended":               "low",
}


def _sport_url_base(api_path: str, api_version: str) -> str:
    """Build the base URL prefix for a sport's Sportradar API."""
    return f"{SPORTRADAR_BASE_URL}/{api_path}/{SPORTRADAR_TIER}/{api_version}/en"


async def _fetch_active_sports(pool) -> list[dict]:
    """Load active sports from sports_metadata table."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT sport, series_slug, tag_ids
            FROM sports_metadata
            WHERE sport IS NOT NULL
            LIMIT 10
            """
        )
    return [dict(r) for r in rows]


def _map_injury_impact(status: str) -> str:
    return STATUS_IMPACT_MAP.get(status.lower(), "low")


def _map_transaction_impact(tx_type: str) -> str:
    return TRANSACTION_IMPACT_MAP.get(tx_type.lower(), "low")


def _parse_dt(raw: str | None) -> datetime:
    """Parse ISO timestamp from Sportradar; fall back to now."""
    if raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            pass
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Injury ingestion
# ---------------------------------------------------------------------------

async def _fetch_injuries(
    client: httpx.AsyncClient, api_path: str, api_version: str
) -> list[dict]:
    """
    Fetch injuries from Sportradar /league/injuries.json.
    Returns normalized injury dicts.
    """
    url = f"{_sport_url_base(api_path, api_version)}/league/injuries.json"
    resp = await client.get(url, params={"api_key": SPORTRADAR_API_KEY})
    resp.raise_for_status()
    data = resp.json()

    injuries = []
    for team in data.get("teams", []):
        team_name = team.get("name", "Unknown")
        for player in team.get("players", []):
            status = player.get("status", "").lower()
            if status in STATUS_IMPACT_MAP:
                injuries.append({
                    "player_name": player.get("full_name", "Unknown"),
                    "team_name": team_name,
                    "status": status,
                    "comment": player.get("comment", ""),
                    "update_date": player.get("updated", ""),
                    "position": player.get("position", ""),
                })
    return injuries


def _build_injury_item(injury: dict, sport: str) -> dict:
    player = injury["player_name"]
    team = injury["team_name"]
    status = injury["status"]
    title = f"{player} ({team}) — {status.upper()}"
    raw_text = (
        f"{player} of the {team} is listed as {status}. "
        f"Position: {injury['position']}. "
        f"{injury['comment']}"
    ).strip()

    return {
        "source_domain": "sports",
        "source_name": "sportradar",
        "title": title,
        "raw_text": raw_text,
        "url": None,
        "author": None,
        "published_at": _parse_dt(injury.get("update_date")),
        "metadata": {
            "source_api": "sportradar",
            "event_type": "injury",
            "sport": sport,
            "player": player,
            "team": team,
            "status": status,
            "position": injury["position"],
        },
        "impact_level": _map_injury_impact(status),
        "content_hash": content_hash("sports", f"{sport}:{player}", title),
    }


# ---------------------------------------------------------------------------
# Transaction ingestion
# ---------------------------------------------------------------------------

async def _fetch_transactions(
    client: httpx.AsyncClient, api_path: str, api_version: str
) -> list[dict]:
    """
    Fetch recent transactions from Sportradar /league/transactions.json.
    Returns normalized transaction dicts.

    Sportradar transaction response shape:
        { "transactions": [ { "id", "updated", "type", "desc",
                               "player": { "full_name", "position" },
                               "team": { "name" },
                               ...
                             } ] }

    Trades may reference multiple players — each is emitted as a separate item.
    """
    url = f"{_sport_url_base(api_path, api_version)}/league/transactions.json"
    resp = await client.get(url, params={"api_key": SPORTRADAR_API_KEY})
    resp.raise_for_status()
    data = resp.json()

    transactions = []
    for tx in data.get("transactions", []):
        tx_type = tx.get("type", "").lower()
        if tx_type not in TRANSACTION_IMPACT_MAP:
            continue  # skip low-signal administrative events we don't map

        desc = tx.get("desc", "")
        update_date = tx.get("updated", "") or tx.get("date", "")

        # Sportradar may nest player under "player" (single) or "players" (trade)
        players_raw = tx.get("players") or ([tx["player"]] if tx.get("player") else [])
        team_name = (tx.get("team") or {}).get("name", "")

        for player_raw in players_raw:
            player_name = player_raw.get("full_name", "Unknown")
            position = player_raw.get("position", "")
            # For trades the "to_team" is sometimes on the player node
            to_team = (player_raw.get("to_team") or tx.get("to_team") or {}).get("name", "")
            from_team = (player_raw.get("from_team") or tx.get("from_team") or {}).get("name", team_name)

            transactions.append({
                "tx_type": tx_type,
                "player_name": player_name,
                "position": position,
                "team_name": from_team,
                "to_team_name": to_team,
                "desc": desc,
                "update_date": update_date,
            })

    return transactions


def _build_transaction_item(tx: dict, sport: str) -> dict:
    player = tx["player_name"]
    tx_type = tx["tx_type"]
    from_team = tx["team_name"]
    to_team = tx["to_team_name"]

    # Build human-readable title
    if tx_type in ("trade", "traded") and to_team:
        title = f"{player} traded: {from_team} → {to_team}"
    elif tx_type in ("cut", "released", "waived", "waived_terminated"):
        title = f"{player} ({from_team}) — RELEASED"
    elif tx_type in ("injured_reserve", "ir", "placed_on_ir", "placed_on_il",
                     "il_10_day", "il_15_day", "il_60_day", "dl"):
        title = f"{player} ({from_team}) — PLACED ON IL/IR"
    elif tx_type in ("signed", "free_agent_signing"):
        action = f"signed by {to_team}" if to_team else "signed"
        title = f"{player} — {action.upper()}"
    elif tx_type == "designated_for_assignment":
        title = f"{player} ({from_team}) — DFA"
    elif tx_type == "activated":
        title = f"{player} ({from_team}) — ACTIVATED from IL/IR"
    else:
        title = f"{player} ({from_team}) — {tx_type.replace('_', ' ').upper()}"

    desc = tx.get("desc", "")
    raw_text = f"{title}. {desc}".strip(" .")

    return {
        "source_domain": "sports",
        "source_name": "sportradar",
        "title": title,
        "raw_text": raw_text,
        "url": None,
        "author": None,
        "published_at": _parse_dt(tx.get("update_date")),
        "metadata": {
            "source_api": "sportradar",
            "event_type": "transaction",
            "sport": sport,
            "transaction_type": tx_type,
            "player": player,
            "team": from_team,
            "to_team": to_team,
            "position": tx["position"],
        },
        "impact_level": _map_transaction_impact(tx_type),
        "content_hash": content_hash("sports", f"{sport}:tx:{player}:{tx_type}", title),
    }


# ---------------------------------------------------------------------------
# HTTP error handling (shared)
# ---------------------------------------------------------------------------

def _handle_sportradar_http_error(e: httpx.HTTPStatusError, sport: str, endpoint: str) -> None:
    if e.response.status_code == 429:
        logger.warning("[sports] Sportradar rate limited for %s/%s, skipping", sport, endpoint)
    elif e.response.status_code == 403:
        logger.warning("[sports] Sportradar access denied for %s/%s (check tier/key)", sport, endpoint)
    elif e.response.status_code == 404:
        logger.warning("[sports] Sportradar endpoint not found for %s/%s (check API version)", sport, endpoint)
    else:
        logger.error("[sports] Sportradar HTTP %s for %s/%s: %s",
                     e.response.status_code, sport, endpoint, e)


# ---------------------------------------------------------------------------
# Fast-path: immediate matching + notification for high-impact sports items
# ---------------------------------------------------------------------------

async def _fast_path_match_and_notify(
    pool, redis, arq_redis, high_impact_items: list[tuple]
) -> None:
    """
    Bypass the NLP queue for high-impact sports items. Uses structured
    metadata (sport, team, player) to match events directly via tag overlap,
    then dispatches notifications immediately.

    Still enqueues NLP for embedding generation — this only accelerates
    the notification path.
    """
    import json

    try:
        async with pool.acquire() as conn:
            for uid, item in high_impact_items:
                meta = item.get("metadata", {})
                sport = meta.get("sport", "")
                team = meta.get("team", "")
                player = meta.get("player", "")

                # Build candidate tag slugs from structured metadata
                from app.services.nlp_entities import _slugify
                candidates = set()
                if sport:
                    candidates.add(_slugify(sport))
                    candidates.add(sport.lower())
                if team:
                    candidates.add(_slugify(team))
                    for word in team.split():
                        slug = _slugify(word)
                        if len(slug) >= 3:
                            candidates.add(slug)
                if player:
                    candidates.add(_slugify(player))
                    parts = player.split()
                    if len(parts) > 1:
                        candidates.add(_slugify(parts[-1]))

                candidate_list = list(candidates)
                if not candidate_list:
                    continue

                # Find matching events via GIN tag overlap
                event_rows = await conn.fetch(
                    """
                    SELECT id FROM events
                    WHERE is_deleted = false
                      AND tags && $1::varchar[]
                    LIMIT 20
                    """,
                    candidate_list,
                )

                if not event_rows:
                    continue

                event_ids = [r["id"] for r in event_rows]
                sentiment = -0.7 if item["impact_level"] == "high" else 0.0

                # Get primary market prices from Redis for price_at_publish
                price_map = {}
                outcome_rows = await conn.fetch(
                    """
                    SELECT m.event_id, m.exchange, mo.execution_asset_id
                    FROM markets m
                    JOIN market_outcomes mo ON mo.market_id = m.id AND mo.side = 'yes'
                    WHERE m.event_id = ANY($1) AND m.is_deleted = false
                    ORDER BY m.event_id, m.volume DESC NULLS LAST
                    """,
                    event_ids,
                )
                if outcome_rows:
                    pipe = redis.pipeline(transaction=False)
                    lookup_eids = []
                    for orow in outcome_rows:
                        key = f"ticker:{orow['exchange']}:{orow['execution_asset_id']}"
                        pipe.hget(key, "price")
                        lookup_eids.append(orow["event_id"])
                    results = await pipe.execute()
                    for idx, val in enumerate(results):
                        if val is not None and lookup_eids[idx] not in price_map:
                            try:
                                price_map[lookup_eids[idx]] = float(val)
                            except (ValueError, TypeError):
                                pass

                # Insert mappings
                mapping_rows = [
                    (uid, eid, 0.7, sentiment, candidate_list, price_map.get(eid))
                    for eid in event_ids
                ]
                await conn.executemany(
                    """
                    INSERT INTO intelligence_market_mapping
                        (id, intelligence_id, event_id, confidence_score,
                         sentiment_polarity, matched_tags, price_at_publish)
                    VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6)
                    ON CONFLICT (intelligence_id, event_id, market_id) DO NOTHING
                    """,
                    mapping_rows,
                )

                # Dispatch notification immediately
                event_id_strs = [str(eid) for eid in event_ids]
                await arq_redis.enqueue_job(
                    "run_dispatch_intelligence_alerts",
                    str(uid),
                    "high",
                    event_id_strs,
                    [],  # no market-level IDs for fast-path
                )

                # Publish to SSE
                try:
                    payload = json.dumps({
                        "id": str(uid),
                        "title": item["title"],
                        "summary": item["raw_text"][:200],
                        "source_domain": "sports",
                        "impact_level": "high",
                        "matched_tags": candidate_list,
                    })
                    await redis.publish("intel:feed:global", payload)
                except Exception:
                    pass  # SSE publish is best-effort

                logger.info(
                    "[sports] Fast-path: %s matched %d events, notification dispatched",
                    item["title"][:60],
                    len(event_ids),
                )

    except Exception as e:
        logger.error("[sports] Fast-path matching failed: %s", e, exc_info=True)


# ---------------------------------------------------------------------------
# Main cron task
# ---------------------------------------------------------------------------

async def ingest_sports_data(ctx: dict) -> None:
    """
    arq cron task: poll Sportradar injuries + transactions for active sports.
    Dedup via Redis hash → batch INSERT → enqueue NLP batch processing.

    Active sports: NBA, NFL, MLB (see ACTIVE_SPORTS).
    """
    try:
        pool = ctx["asyncpg_pool"]
        redis = ctx["redis"]

        if not SPORTRADAR_API_KEY:
            logger.warning("[sports] SPORTRADAR_API_KEY not set, skipping")
            return

        items_to_insert = []

        async with httpx.AsyncClient(timeout=30.0) as client:
            for sport in ACTIVE_SPORTS:
                cfg = SPORT_API_CONFIG.get(sport)
                if not cfg:
                    logger.debug("[sports] No API config for sport=%s, skipping", sport)
                    continue

                api_path, api_version = cfg

                # --- Injuries ---
                try:
                    injuries = await _fetch_injuries(client, api_path, api_version)
                    for injury in injuries:
                        try:
                            item = _build_injury_item(injury, sport)
                            if not await check_redis_dedup(redis, item["content_hash"]):
                                items_to_insert.append(item)
                        except Exception as e:
                            logger.warning("[sports] Skipping injury record: %s", e)
                except httpx.HTTPStatusError as e:
                    _handle_sportradar_http_error(e, sport, "injuries")
                except Exception as e:
                    logger.warning("[sports] Failed to fetch injuries for %s: %s", sport, e)

                # --- Transactions ---
                try:
                    transactions = await _fetch_transactions(client, api_path, api_version)
                    for tx in transactions:
                        try:
                            item = _build_transaction_item(tx, sport)
                            if not await check_redis_dedup(redis, item["content_hash"]):
                                items_to_insert.append(item)
                        except Exception as e:
                            logger.warning("[sports] Skipping transaction record: %s", e)
                except httpx.HTTPStatusError as e:
                    _handle_sportradar_http_error(e, sport, "transactions")
                except Exception as e:
                    logger.warning("[sports] Failed to fetch transactions for %s: %s", sport, e)

        if not items_to_insert:
            logger.info("[sports] All sports data deduped, nothing to insert")
            return

        inserted_ids = await bulk_insert_intelligence(pool, items_to_insert)

        if inserted_ids:
            from arq import ArqRedis

            arq_redis = ArqRedis(pool_or_conn=redis.connection_pool)

            # --- Fast-path: high-impact items get immediate event matching ---
            # Skip the NLP queue for notifications — sports metadata is already
            # structured enough to find matching events directly.
            high_impact_items = [
                (uid, item) for uid, item in zip(inserted_ids, items_to_insert)
                if item["impact_level"] == "high"
            ]

            if high_impact_items:
                await _fast_path_match_and_notify(
                    pool, redis, arq_redis, high_impact_items
                )

            # Still enqueue ALL items for NLP (embedding generation + refinement)
            for i in range(0, len(inserted_ids), 10):
                batch = [str(uid) for uid in inserted_ids[i:i + 10]]
                await arq_redis.enqueue_job("run_process_intelligence_nlp_batch", batch)

            logger.info(
                "[sports] Inserted %d items (%d high-impact fast-path, %d NLP batches)",
                len(inserted_ids),
                len(high_impact_items),
                (len(inserted_ids) + 9) // 10,
            )

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            logger.warning("[sports] Sportradar rate limited, will retry next cycle")
        else:
            logger.error("[sports] Sportradar HTTP error: %s", e)
    except Exception as exc:
        logger.error("[sports] ingest_sports_data failed: %s", exc, exc_info=True)
