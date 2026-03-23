"""
FCM push notification sender — arq task.

Sends a single push notification for an intelligence alert.
Handles token refresh and invalid token cleanup.
"""

import logging

from app.services.notifications import send_push_notification

logger = logging.getLogger(__name__)


async def send_intelligence_notification(
    ctx: dict, user_id: str, payload: dict
) -> None:
    """
    arq task: send a single FCM push notification for an intelligence alert.
    """
    try:
        pool = ctx["asyncpg_pool"]

        title = payload.get("title", "Intelligence Alert")
        body = payload.get("body", "")
        data = {
            "type": "intelligence_alert",
            "intelligence_id": payload.get("intelligence_id", ""),
            "impact_level": payload.get("impact_level", "low"),
        }

        success = await send_push_notification(
            user_id=user_id,
            title=title,
            body=body,
            data=data,
            pool=pool,
        )

        if success:
            logger.info(
                "[notification_dispatch] Sent alert to user=%s, intel=%s",
                user_id,
                payload.get("intelligence_id"),
            )
        else:
            logger.debug(
                "[notification_dispatch] Push skipped/failed for user=%s",
                user_id,
            )

    except Exception as exc:
        logger.error(
            "[notification_dispatch] send_intelligence_notification failed: %s",
            exc,
            exc_info=True,
        )
