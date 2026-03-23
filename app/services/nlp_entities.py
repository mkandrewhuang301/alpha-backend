"""
NER + entity disambiguation via OpenAI function calling.

One-shot extraction: given raw text and a list of valid PlatformTag slugs,
returns matched tags with confidence, sentiment polarity, impact level, and summary.

Critical: output validation rejects any slug NOT in the provided available_tags list.
Never blindly trust LLM output for DB operations.
"""

import json
import logging

from app.services.nlp_client import _get_openai_client

logger = logging.getLogger(__name__)

# Function calling schema for entity extraction
EXTRACTION_FUNCTION = {
    "name": "extract_entities",
    "description": (
        "Extract named entities from text and match them to known category slugs. "
        "Return sentiment, impact assessment, and a brief summary."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "matched_tags": {
                "type": "array",
                "description": "Tags from the available list that are relevant to this text",
                "items": {
                    "type": "object",
                    "properties": {
                        "slug": {
                            "type": "string",
                            "description": "Exact slug from the available tags list",
                        },
                        "confidence": {
                            "type": "number",
                            "description": "Confidence 0.0-1.0 that this tag is relevant",
                        },
                    },
                    "required": ["slug", "confidence"],
                },
            },
            "sentiment_polarity": {
                "type": "number",
                "description": (
                    "Sentiment from -1.0 (very negative) to 1.0 (very positive). "
                    "0.0 is neutral."
                ),
            },
            "impact_level": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": "How impactful this information is for prediction markets",
            },
            "summary": {
                "type": "string",
                "description": "One-sentence summary of the key information",
            },
        },
        "required": ["matched_tags", "sentiment_polarity", "impact_level", "summary"],
    },
}


async def extract_entities_and_match(
    text: str, available_tags: list[str]
) -> dict:
    """
    Extract entities from text and match against known PlatformTag slugs.

    Returns:
        {
            "matched_tags": [{"slug": str, "confidence": float}, ...],
            "sentiment_polarity": float,
            "impact_level": str,
            "summary": str,
        }

    On failure, returns a safe default with empty matches.
    """
    client = _get_openai_client()

    tags_str = ", ".join(available_tags[:500])  # cap to avoid token overflow; indexed tags are first

    try:
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an entity extraction assistant for a prediction market platform. "
                        "Extract entities from the text and match them to the provided category slugs. "
                        "Only use slugs from the provided list — never invent new ones. "
                        "Assess sentiment and impact for prediction market traders."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Available category slugs: [{tags_str}]\n\n"
                        f"Text to analyze:\n{text[:4000]}"  # cap input length
                    ),
                },
            ],
            tools=[{"type": "function", "function": EXTRACTION_FUNCTION}],
            tool_choice={"type": "function", "function": {"name": "extract_entities"}},
            temperature=0.1,
        )

        msg = response.choices[0].message
        if not msg.tool_calls or not msg.tool_calls[0].function.arguments:
            logger.warning("[nlp_entities] No tool call in response")
            return _default_result()

        result = json.loads(msg.tool_calls[0].function.arguments)

        # Validate: reject any slug not in available_tags
        valid_tags = set(available_tags)
        result["matched_tags"] = [
            tag
            for tag in result.get("matched_tags", [])
            if tag.get("slug") in valid_tags and 0.0 <= tag.get("confidence", 0) <= 1.0
        ]

        # Clamp sentiment
        sentiment = result.get("sentiment_polarity", 0.0)
        result["sentiment_polarity"] = max(-1.0, min(1.0, float(sentiment)))

        # Validate impact_level
        if result.get("impact_level") not in ("high", "medium", "low"):
            result["impact_level"] = "low"

        # Ensure summary exists
        if not result.get("summary"):
            result["summary"] = text[:200]

        return result

    except Exception as e:
        logger.error("[nlp_entities] Entity extraction failed: %s", e)
        return _default_result()


def _default_result() -> dict:
    """Safe default when extraction fails."""
    return {
        "matched_tags": [],
        "sentiment_polarity": 0.0,
        "impact_level": "low",
        "summary": "",
    }
