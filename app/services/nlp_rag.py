"""
RAG narrative synthesis via GPT-4o.

Takes a user query + context items (markets, intelligence articles) and
generates a cited narrative answer. Opt-in via ?synthesize=true on the
search endpoint.
"""

import json
import logging

from app.services.nlp_client import _get_openai_client

logger = logging.getLogger(__name__)


async def synthesize_rag_answer(
    query: str,
    context_items: list[dict],
) -> dict:
    """
    Generate a narrative answer from search results with citations.

    Args:
        query: User's natural language search query.
        context_items: List of dicts with keys: id, type (market/event/intelligence),
                       title, text, source_name (optional).

    Returns:
        {"narrative": str, "citations": [{"id": str, "title": str, "type": str}]}

    On failure, returns {"narrative": None, "citations": []}.
    """
    if not context_items:
        return {"narrative": None, "citations": []}

    client = _get_openai_client()

    # Build context block
    context_parts = []
    for i, item in enumerate(context_items[:15], 1):  # cap at 15 items
        source = item.get("source_name", item.get("type", "unknown"))
        context_parts.append(
            f"[{i}] ({item['type']}) {item['title']}\n"
            f"Source: {source}\n"
            f"{item.get('text', '')[:500]}\n"
        )
    context_block = "\n---\n".join(context_parts)

    try:
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a prediction market intelligence analyst. "
                        "Given search results, synthesize a concise narrative answer "
                        "to the user's query. Cite sources using [N] notation. "
                        "Focus on actionable insights for traders. "
                        "Be factual — never speculate or fabricate information."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Query: {query}\n\n"
                        f"Search results:\n{context_block}\n\n"
                        "Provide a concise answer with citations."
                    ),
                },
            ],
            temperature=0.3,
            max_tokens=1000,
        )

        narrative = response.choices[0].message.content

        # Build citation list from referenced items
        citations = [
            {
                "id": item.get("id", ""),
                "title": item["title"],
                "type": item["type"],
            }
            for item in context_items[:15]
        ]

        return {"narrative": narrative, "citations": citations}

    except Exception as e:
        logger.error("[nlp_rag] RAG synthesis failed: %s", e)
        return {"narrative": None, "citations": []}
