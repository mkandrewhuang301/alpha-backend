"""
Generic streaming ingestion framework for intelligence sources.

Reuses the micro-batch flusher pattern from polymarket/stream.py:
    WebSocket/webhook → asyncio.Queue(100K) → 100ms/500-item flush → batch INSERT

Architecturally ready for social/crypto feeds — not connected to live
sources yet. Provides the base classes and flush loop that future
streaming workers will inherit.

Usage:
    class CryptoStream(IntelligenceStream):
        SOURCE_DOMAIN = "crypto"
        SOURCE_NAME = "coingecko_ws"

        async def connect(self):
            # establish WS connection
            ...

        async def parse_message(self, raw: dict) -> dict | None:
            # return an item dict or None to skip
            ...

    stream = CryptoStream(pool, redis)
    await stream.run()
"""

import asyncio
import logging
from abc import ABC, abstractmethod

from app.workers.intelligence.base import (
    bulk_insert_intelligence,
    check_redis_dedup,
)

logger = logging.getLogger(__name__)

# Queue and batch sizing (matching polymarket/stream.py pattern)
QUEUE_MAX_SIZE = 100_000
FLUSH_INTERVAL_MS = 100
FLUSH_BATCH_SIZE = 500


class IntelligenceStream(ABC):
    """
    Base class for streaming intelligence ingestion.

    Subclasses implement connect() and parse_message() — this base class
    handles the queue, flush loop, dedup, and batch insert.
    """

    SOURCE_DOMAIN: str = ""
    SOURCE_NAME: str = ""

    def __init__(self, pool, redis):
        self.pool = pool
        self.redis = redis
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)
        self._running = False

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection and start pushing raw dicts into self._queue."""
        ...

    @abstractmethod
    async def parse_message(self, raw: dict) -> dict | None:
        """
        Parse a raw message into an intelligence item dict.
        Return None to skip the message.

        Expected dict keys:
            source_domain, source_name, title, raw_text, url, author,
            published_at, metadata, impact_level, content_hash
        """
        ...

    async def run(self) -> None:
        """Start the stream: connect + flush loop in parallel."""
        self._running = True
        try:
            await asyncio.gather(
                self.connect(),
                self._flush_loop(),
            )
        except asyncio.CancelledError:
            logger.info("[stream:%s] Cancelled, draining queue", self.SOURCE_NAME)
            self._running = False
            await self._flush_batch()
        except Exception as exc:
            logger.error(
                "[stream:%s] Stream failed: %s",
                self.SOURCE_NAME,
                exc,
                exc_info=True,
            )
            self._running = False

    async def stop(self) -> None:
        """Signal the stream to stop after current flush."""
        self._running = False

    async def enqueue(self, raw: dict) -> None:
        """Put a raw message onto the queue (non-blocking, drops on full)."""
        try:
            self._queue.put_nowait(raw)
        except asyncio.QueueFull:
            logger.warning(
                "[stream:%s] Queue full (%d), dropping message",
                self.SOURCE_NAME,
                QUEUE_MAX_SIZE,
            )

    async def _flush_loop(self) -> None:
        """Drain queue in timed micro-batches → dedup → batch INSERT."""
        while self._running:
            await asyncio.sleep(FLUSH_INTERVAL_MS / 1000.0)
            await self._flush_batch()

    async def _flush_batch(self) -> None:
        """Drain up to FLUSH_BATCH_SIZE items, dedup, and insert."""
        items_raw = []
        for _ in range(FLUSH_BATCH_SIZE):
            try:
                items_raw.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        if not items_raw:
            return

        # Parse and dedup
        items_to_insert = []
        for raw in items_raw:
            try:
                parsed = await self.parse_message(raw)
                if parsed is None:
                    continue

                c_hash = parsed.get("content_hash", "")
                if c_hash and await check_redis_dedup(self.redis, c_hash):
                    continue

                items_to_insert.append(parsed)
            except Exception as e:
                logger.warning(
                    "[stream:%s] Skipping message: %s", self.SOURCE_NAME, e
                )

        if not items_to_insert:
            return

        # Batch insert
        inserted_ids = await bulk_insert_intelligence(self.pool, items_to_insert)

        # Enqueue NLP batch processing
        if inserted_ids:
            try:
                from arq import ArqRedis

                arq_redis = ArqRedis(pool_or_conn=self.redis.connection_pool)
                for i in range(0, len(inserted_ids), 10):
                    batch = [str(uid) for uid in inserted_ids[i : i + 10]]
                    await arq_redis.enqueue_job(
                        "run_process_intelligence_nlp_batch",
                        batch,
                    )
            except Exception as e:
                logger.error(
                    "[stream:%s] Failed to enqueue NLP batch: %s",
                    self.SOURCE_NAME,
                    e,
                )

        logger.debug(
            "[stream:%s] Flushed %d/%d items (inserted %d)",
            self.SOURCE_NAME,
            len(items_to_insert),
            len(items_raw),
            len(inserted_ids),
        )
