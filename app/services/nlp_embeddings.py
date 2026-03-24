"""
Vector embedding generation via OpenAI text-embedding-3-large.

Truncated to 768 dimensions via the API's `dimensions` parameter.
768 dims retains 95%+ retrieval quality at half the cost and storage.
Used by NLP worker (batch) and search endpoint (single query).
"""

import logging

from app.services.nlp_client import _get_openai_client

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "text-embedding-3-large"
EMBEDDING_DIMENSIONS = 768


async def generate_embedding(text: str) -> list[float]:
    """
    Generate a single embedding vector for the given text.
    Returns a 1536-dimensional float list.
    Raises on failure — caller should handle.
    """
    client = _get_openai_client()
    response = await client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=text[:8000],  # cap to stay within token limits
        dimensions=EMBEDDING_DIMENSIONS,
    )
    return response.data[0].embedding


async def generate_embeddings_batch(texts: list[str]) -> list[list[float]]:
    """
    Generate embeddings for a batch of texts in a single API call.
    Returns list of 1536-dimensional float lists, one per input text.
    Raises on failure — caller should handle.
    """
    if not texts:
        return []

    client = _get_openai_client()
    # Cap each text and batch size
    capped = [t[:8000] for t in texts]

    response = await client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=capped,
        dimensions=EMBEDDING_DIMENSIONS,
    )

    # Sort by index to maintain input order
    sorted_data = sorted(response.data, key=lambda x: x.index)
    return [item.embedding for item in sorted_data]
