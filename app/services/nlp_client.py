"""
Shared OpenAI async client singleton for all NLP modules.

Used by: nlp_entities.py, nlp_embeddings.py, nlp_rag.py
"""

import logging

from openai import AsyncOpenAI

from app.core.config import OPENAI_API_KEY

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None


def _get_openai_client() -> AsyncOpenAI:
    """Lazy singleton — one client per process."""
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    return _client
