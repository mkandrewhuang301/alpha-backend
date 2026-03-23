"""
Firebase Cloud Messaging (FCM) notifications + intelligence alert dispatch.

Features:
    - Firebase Admin SDK init (gated by FCM_ENABLED)
    - Single push notification sender
    - Intelligence alert dispatcher with sliding window (2-min, cap 5 per user)
    - Matches intelligence tags against UserOrder + TrackedEntity for exposure detection

Redis keys:
    notif_window:{user_id} — STRING (int), 120s TTL, cap at 5
"""

import json
import logging
from uuid import UUID

from app.core.config import FCM_ENABLED, FIREBASE_CREDENTIALS_JSON

logger = logging.getLogger(__name__)

_firebase_app = None

NOTIFICATION_WINDOW_TTL = 120  # 2 minutes
NOTIFICATION_WINDOW_CAP = 5


def init_firebase() -> None:
    """Initialize Firebase Admin SDK. Call once in lifespan, gated by FCM_ENABLED."""
    global _firebase_app
    if not FCM_ENABLED:
        logger.info("[notifications] FCM_ENABLED=false, skipping Firebase init")
        return

    if not FIREBASE_CREDENTIALS_JSON:
        logger.warning("[notifications] FIREBASE_CREDENTIALS_JSON not set, skipping")
        return

    try:
        import firebase_admin
        from firebase_admin import credentials

        cred_dict = json.loads(FIREBASE_CREDENTIALS_JSON)
        cred = credentials.Certificate(cred_dict)
        _firebase_app = firebase_admin.initialize_app(cred)
        logger.info("[notifications] Firebase Admin SDK initialized")
    except Exception as e:
        logger.error("[notifications] Firebase init failed: %s", e)


async def send_push_notification(
    user_id: str,
    title: str,
    body: str,
    data: dict | None = None,
    pool=None,
) -> bool:
    """
    Send a single FCM push notification to a user.

    Looks up the user's FCM token from the DB. Returns True on success.
    """
    if not FCM_ENABLED or _firebase_app is None:
        logger.debug("[notifications] FCM disabled, skipping push for user=%s", user_id)
        return False

    try:
        from firebase_admin import messaging

        # Look up user's FCM token
        if pool is None:
            logger.warning("[notifications] No pool provided, cannot look up FCM token")
            return False

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT fcm_token FROM users
                WHERE id = $1 AND fcm_token IS NOT NULL
                """,
                UUID(user_id),
            )

        if not row or not row["fcm_token"]:
            logger.debug("[notifications] No FCM token for user=%s", user_id)
            return False

        token = row["fcm_token"]

        message = messaging.Message(
            notification=messaging.Notification(title=title, body=body),
            data={k: str(v) for k, v in (data or {}).items()},
            token=token,
        )

        # Firebase send is synchronous — run in executor
        import asyncio

        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, messaging.send, message)
        logger.info("[notifications] Sent push to user=%s, response=%s", user_id, response)
        return True

    except Exception as e:
        error_str = str(e)
        if "NOT_FOUND" in error_str or "UNREGISTERED" in error_str:
            # Token is invalid — clean it up
            if pool:
                try:
                    async with pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE users SET fcm_token = NULL WHERE id = $1",
                            UUID(user_id),
                        )
                    logger.info("[notifications] Cleared invalid FCM token for user=%s", user_id)
                except Exception:
                    pass
        else:
            logger.error("[notifications] Push failed for user=%s: %s", user_id, e)
        return False


async def dispatch_intelligence_alerts(
    intelligence_id: str,
    impact_level: str,
    matched_event_ids: list[str],
    matched_market_ids: list[str],
    pool,
    redis,
) -> None:
    """
    Dispatch intelligence alerts to users with financial exposure.

    1. Query UserOrder WHERE status IN ('live','partial','matched_pending') on matched markets
    2. Query TrackedEntity linked to matched markets
    3. Check Redis sliding window per user (cap 5 per 2 min)
    4. Enqueue send_intelligence_notification arq task for each
    """
    try:
        affected_user_ids = set()

        async with pool.acquire() as conn:
            # Find users with active orders on matched markets
            if matched_market_ids:
                market_uuids = [UUID(mid) for mid in matched_market_ids]
                order_rows = await conn.fetch(
                    """
                    SELECT DISTINCT user_id FROM user_orders
                    WHERE market_id = ANY($1)
                      AND status IN ('live', 'partial', 'matched_pending')
                    """,
                    market_uuids,
                )
                for row in order_rows:
                    affected_user_ids.add(str(row["user_id"]))

            # Find users tracking matched events/markets
            entity_ids = []
            if matched_event_ids:
                entity_ids.extend([UUID(eid) for eid in matched_event_ids])
            if matched_market_ids:
                entity_ids.extend([UUID(mid) for mid in matched_market_ids])

            if entity_ids:
                tracked_rows = await conn.fetch(
                    """
                    SELECT DISTINCT user_id FROM tracked_entities
                    WHERE entity_id = ANY($1)
                    """,
                    entity_ids,
                )
                for row in tracked_rows:
                    affected_user_ids.add(str(row["user_id"]))

        if not affected_user_ids:
            logger.debug(
                "[notifications] No affected users for intelligence=%s", intelligence_id
            )
            return

        # Load intelligence title for notification
        async with pool.acquire() as conn:
            intel_row = await conn.fetchrow(
                "SELECT title, metadata FROM external_intelligence WHERE id = $1",
                UUID(intelligence_id),
            )

        if not intel_row:
            return

        title = intel_row["title"]
        metadata = intel_row["metadata"] or {}
        summary = metadata.get("summary", title)

        # Check sliding window + enqueue per user
        from arq import ArqRedis

        arq_redis = ArqRedis(pool_or_conn=redis.connection_pool)

        for user_id in affected_user_ids:
            # Atomic sliding window: INCR first, then check cap.
            # GET→check→INCR is racy: two concurrent dispatches can both pass
            # the cap check. INCR is atomic — returns new count in one operation.
            window_key = f"notif_window:{user_id}"
            new_count = await redis.incr(window_key)
            if new_count == 1:
                # First notification in this window — set TTL
                await redis.expire(window_key, NOTIFICATION_WINDOW_TTL)

            if new_count > NOTIFICATION_WINDOW_CAP:
                logger.debug(
                    "[notifications] User %s hit notification cap (%d/%d)",
                    user_id,
                    new_count,
                    NOTIFICATION_WINDOW_CAP,
                )
                continue

            # Enqueue notification task
            payload = {
                "intelligence_id": intelligence_id,
                "title": f"Alert: {title[:80]}",
                "body": summary[:200],
                "impact_level": impact_level,
                "market_ids": matched_market_ids,
                "event_ids": matched_event_ids,
            }

            await arq_redis.enqueue_job(
                "run_send_intelligence_notification",
                user_id,
                payload,
            )

        logger.info(
            "[notifications] Dispatched alerts to %d users for intelligence=%s",
            len(affected_user_ids),
            intelligence_id,
        )

    except Exception as exc:
        logger.error(
            "[notifications] dispatch_intelligence_alerts failed: %s",
            exc,
            exc_info=True,
        )
