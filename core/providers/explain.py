"""Ranking explanations written by Gemini (Person 2).

Gemini only writes the short "reason" text for providers that scoring.py has
already ranked. It never scores, reorders, selects or adds providers: the
deterministic ranking stays the source of truth.

One Gemini call per ranking. If anything goes wrong (no key, timeout, API
error, malformed or incomplete output), the template reasons from scoring.py
are kept and the workflow carries on.
"""
import json
import logging
import re

from pydantic import BaseModel, Field

from ..gemini import GeminiUnavailable, generate_structured

logger = logging.getLogger(__name__)

MAX_REASON_CHARS = 220

SYSTEM_INSTRUCTION = """You explain a provider ranking to a customer in India who asked for a local home service.
The providers are already ranked by a scoring system. Your only job is to write one short reason per provider.

Rules:
- Use ONLY the facts given for each provider. Do not invent prices, availability, experience, services or reviews.
- Do not change, question or comment on the order or the scores.
- Do not recommend a provider over another beyond what the given rank and facts show.
- One sentence per provider, at most 25 words, plain friendly English, no emojis.
- Mention the most useful facts for that provider, e.g. rating with review count, distance, or budget fit.
- If a price is marked as an estimate, call it an estimate or typical price, never a quote.
- If a provider's location is approximate, do not state a distance for it.
- Return exactly one item per provider, using the provider's place_id unchanged."""


class ProviderReason(BaseModel):
    place_id: str = Field(description="The provider's place_id, copied exactly")
    reason: str = Field(description="One short sentence explaining why this provider is a good match")


class RankingExplanations(BaseModel):
    reasons: list[ProviderReason]


def explain_ranking(ranked: list[dict], requirements: dict, raw_text: str = "") -> tuple[list[dict], bool]:
    """Return (ranked providers, used_llm). Order, scores and all other fields are untouched."""
    if not ranked:
        return ranked, False
    try:
        return _explain_with_gemini(ranked, requirements or {}, raw_text)
    except GeminiUnavailable as exc:
        logger.info("Ranking explanations fell back to templates: %s", exc)
    except Exception:  # a bug here must never stop the workflow
        logger.exception("Unexpected error while explaining the ranking; using template reasons")
    return ranked, False


def _explain_with_gemini(ranked: list[dict], requirements: dict, raw_text: str) -> tuple[list[dict], bool]:
    result = generate_structured(build_prompt(ranked, requirements, raw_text),
                                 RankingExplanations, SYSTEM_INSTRUCTION)
    reasons = {}
    for item in result.reasons:
        text = _clean(item.reason)
        if text:
            reasons[item.place_id.strip()] = text
    missing = [p["place_id"] for p in ranked if p["place_id"] not in reasons]
    if missing:
        logger.info("Gemini skipped %d provider(s); using template reasons", len(missing))
        return ranked, False

    return [{**p, "reason": reasons[p["place_id"]], "reason_source": "llm"} for p in ranked], True


def build_prompt(ranked: list[dict], requirements: dict, raw_text: str) -> str:
    """Only facts the scoring already used. No phone numbers, websites or coordinates."""
    request = {
        "customer_request": raw_text or None,
        "service": requirements.get("category"),
        "area": requirements.get("locality") or requirements.get("location") or requirements.get("city"),
        "budget": requirements.get("budget"),
    }
    providers = []
    for p in ranked:
        providers.append({
            "place_id": p["place_id"],
            "rank": p.get("rank"),
            "name": p.get("name"),
            "score_out_of_100": p.get("score"),
            "rating": p.get("rating"),
            "review_count": p.get("review_count"),
            "distance_km": p.get("distance_km"),
            "location_approximate": bool(p.get("location_approximate")),
            "price": p.get("price_info") or None,
            "price_is_estimate": bool(p.get("price_is_estimate")),
            "typical_price_above_budget": bool(p.get("over_budget")),
            "score_breakdown_out_of_100": p.get("score_breakdown"),
        })
    return ("Customer request:\n" + json.dumps({k: v for k, v in request.items() if v is not None}, ensure_ascii=False)
            + "\n\nRanked providers (best first):\n" + json.dumps(providers, ensure_ascii=False, indent=1)
            + "\n\nWrite one reason per provider.")


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip().strip('"').strip()
    if len(text) <= MAX_REASON_CHARS:
        return text
    cut = text[:MAX_REASON_CHARS].rsplit(" ", 1)[0].rstrip(",;:")
    return cut + "…"
