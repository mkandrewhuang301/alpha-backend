"""
Group message endpoints.

GET    /api/v1/groups/{group_id}/messages?cursor=&limit=50
POST   /api/v1/groups/{group_id}/messages
DELETE /api/v1/groups/{group_id}/messages/{msg_id}
"""

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.limiter import _get_user_key, limiter
from app.models.db import GroupMessage, User
from app.services.auth import get_current_user
from app.services.groups import (
    assert_min_role,
    create_message,
    get_group_or_404_authorized,
    list_messages,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/groups")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class SendMessageRequest(BaseModel):
    type: str = "text"
    content: Optional[str] = None
    metadata: dict = {}
    reply_to_id: Optional[UUID] = None

    class Config:
        json_schema_extra = {
            "example": {
                "type": "text",
                "content": "LeBron is gonna drop 40 tonight",
                "metadata": {},
            }
        }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

_VALID_MSG_TYPES = {"text", "image", "trade", "market", "article", "profile"}


@router.get("/{group_id}/messages")
async def get_messages(
    group_id: UUID,
    cursor: Optional[str] = Query(None, description="ISO timestamp — return messages older than this"),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Paginated message history for a group.
    cursor = ISO timestamp from previous response (for fetching older messages).
    Returns {messages: [...], next_cursor: str | null}
    """
    _, membership = await get_group_or_404_authorized(group_id, current_user.id, db)
    if not membership:
        raise HTTPException(status_code=403, detail="Join this group to read messages")

    messages = await list_messages(group_id, db, cursor=cursor, limit=limit)

    next_cursor = None
    if len(messages) == limit and messages:
        next_cursor = messages[-1]["created_at"]

    return {"messages": messages, "next_cursor": next_cursor}


@router.post("/{group_id}/messages", status_code=201)
@limiter.limit("60/minute", key_func=_get_user_key)
async def send_message(
    request: Request,
    group_id: UUID,
    body: SendMessageRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Send a message to a group.

    Supported types:
    - text: content field required
    - image: metadata.{url, width?, height?, mime_type?}
    - trade: metadata.{source, record_id, snapshot}
    - market: metadata.{exchange, market_id, shared_yes_price, snapshot}
    - article: metadata.{url, title, source, image_url?}
    - profile: metadata.{user_id, username, display_name?, avatar_url?}
    """
    # Must be a member to send (public groups require joining first)
    _, membership = await get_group_or_404_authorized(group_id, current_user.id, db)
    if not membership:
        raise HTTPException(status_code=403, detail="Join this group to send messages")

    if body.type not in _VALID_MSG_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid message type. Allowed: {', '.join(_VALID_MSG_TYPES)}",
        )

    from app.core.redis import get_redis
    redis = await get_redis()

    message = await create_message(
        group_id=group_id,
        sender_id=current_user.id,
        msg_type=body.type,
        content=body.content,
        metadata=body.metadata,
        db=db,
        redis=redis,
        reply_to_id=body.reply_to_id,
    )

    return {
        "id": str(message.id),
        "group_id": str(message.group_id),
        "sender_id": str(message.sender_id),
        "type": message.type,
        "content": message.content,
        "metadata": message.msg_metadata or {},
        "reply_to_id": str(message.reply_to_id) if message.reply_to_id else None,
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }


@router.delete("/{group_id}/messages/{msg_id}", status_code=200)
async def delete_message(
    group_id: UUID,
    msg_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Soft-delete a message.
    Sender can delete their own messages. Admins/owners can delete any message.
    """
    await get_group_or_404_authorized(group_id, current_user.id, db)  # validates access

    message = await db.get(GroupMessage, msg_id)
    if not message or message.group_id != group_id or message.is_deleted:
        raise HTTPException(status_code=404, detail="Message not found")

    # Permission check: sender can delete own, admin+ can delete any
    is_sender = message.sender_id == current_user.id
    if not is_sender:
        from app.services.groups import get_membership, ROLE_ORDER
        m = await get_membership(group_id, current_user.id, db)
        if not m or ROLE_ORDER.get(m.role, 0) < ROLE_ORDER.get("admin", 0):
            raise HTTPException(status_code=403, detail="Cannot delete this message")

    message.is_deleted = True
    await db.commit()
    return {"status": "deleted"}
