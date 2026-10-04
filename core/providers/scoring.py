"""Weighted provider scoring for the Provider Analysis Agent (Person 2).

Each provider gets four sub-scores between 0 and 1, combined with fixed weights:

    quality  40%  rating, adjusted for how many reviews back it up
    reviews  20%  review count, log-scaled
    distance 30%  straight-line km from the user (haversine)
    budget   10%  user's budget against the provider's price range

Missing budget or price gets a neutral 0.5, and a missing location gets the
typical distance score for that search, so a provider is never punished or
rewarded for data Google didn't have.
"""
import math
import re
from dataclasses import dataclass
from statistics import median

from .categories import resolve_category
from .geo import haversine_km, locate_user, pin_key, shared_pins

WEIGHTS = {"quality": 0.40, "reviews": 0.20, "distance": 0.30, "budget": 0.10}
PRIOR_REVIEWS = 10          # how many "average" reviews every rating is blended with
DEFAULT_PRIOR_RATING = 4.0  # used only if no provider in the set has a rating
REVIEWS_FOR_FULL_SCORE = 500
DISTANCE_ZERO_KM = 10.0     # 0 km scores 1.0, 10 km or more scores 0
NEUTRAL = 0.5
TOP_N = 5


@dataclass
class RankingResult:
    ranked: list[dict]       # top N, best first, JSON-safe
    total: int               # providers considered
    location_note: str       # how the user's position was found


def rank_providers(providers: list[dict], requirements: dict | None = None, top_n: int = TOP_N) -> RankingResult:
    requirements = requirements or {}
    user_point, location_note = locate_user(requirements, providers)
    pins = shared_pins(providers)
    prior = _prior_rating(providers)
    budget = parse_budget(requirements.get("budget"))
    category = resolve_category(requirements.get("category"))
    fallback_range = (category.price_min, category.price_max) if category else None

    distances = [_distance(p, user_point, pins) for p in providers]
    # A provider with an unknown location is treated as a typical distance for
    # this search, so it is neither helped nor hurt by the missing pin.
    known = sorted(distance_score(km) for km, _ in distances if km is not None)
    typical_distance = median(known) if known else NEUTRAL

    scored = []
    for provider, (distance_km, approximate) in zip(providers, distances):
        price_range = parse_price_range(provider.get("price_info")) or fallback_range
        parts = {
            "quality": quality_score(provider.get("rating"), provider.get("review_count") or 0, prior),
            "reviews": review_score(provider.get("review_count") or 0),
            "distance": distance_score(distance_km) if distance_km is not None else typical_distance,
            "budget": budget_score(budget, price_range),
        }
        total = sum(WEIGHTS[name] * value for name, value in parts.items())
        scored.append({
            **provider,
            "score": round(total * 100, 1),
            "score_breakdown": {name: round(value * 100) for name, value in parts.items()},
            "distance_km": round(distance_km, 1) if distance_km is not None else None,
            "location_approximate": approximate,
            "over_budget": budget is not None and price_range is not None and budget < price_range[0],
        })

    scored.sort(key=lambda p: (-p["score"], -(p.get("review_count") or 0), p["name"]))
    ranked = scored[:top_n]
    for position, provider in enumerate(ranked, start=1):
        provider["rank"] = position
        provider["reason"] = template_reason(provider)
        provider["reason_source"] = "template"  # the LLM step sets "llm" when it rewrites it
    return RankingResult(ranked=ranked, total=len(providers), location_note=location_note)


def quality_score(rating: float | None, review_count: int, prior: float) -> float:
    """Bayesian average: blend the rating with PRIOR_REVIEWS average reviews.

    A 5.0 from 2 reviews is pulled most of the way to the average, while a 4.7
    from 385 reviews barely moves.
    The result is mapped from the 3.0–5.0 range (where real ratings sit) to 0–1.
    """
    if rating is None or review_count <= 0:
        adjusted = prior
    else:
        adjusted = (review_count * rating + PRIOR_REVIEWS * prior) / (review_count + PRIOR_REVIEWS)
    return _clamp((adjusted - 3.0) / 2.0)


def review_score(review_count: int) -> float:
    """log(1 + n) scaled so 500+ reviews scores 1.0; 10 reviews scores about 0.39."""
    return _clamp(math.log1p(max(review_count, 0)) / math.log1p(REVIEWS_FOR_FULL_SCORE))


def distance_score(distance_km: float | None) -> float:
    if distance_km is None:
        return NEUTRAL
    return _clamp(1 - distance_km / DISTANCE_ZERO_KM)


def budget_score(budget: int | None, price_range: tuple[int, int] | None) -> float:
    """1.0 if the budget covers the top of the range, falling to 0 as it drops below the bottom."""
    if budget is None or price_range is None:
        return NEUTRAL
    low, high = price_range
    if budget >= high:
        return 1.0
    if budget >= low:
        return NEUTRAL + NEUTRAL * (budget - low) / (high - low) if high > low else 1.0
    return _clamp(NEUTRAL * budget / low) if low else NEUTRAL


def parse_budget(value) -> int | None:
    """Accepts 1500, "1500", "₹1,500", "under ₹1,500" or {"max": 1500}."""
    if isinstance(value, dict):
        value = value.get("max")
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else None
    numbers = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", str(value))]
    return max(numbers) if numbers else None


def parse_price_range(price_info: str | None) -> tuple[int, int] | None:
    """Rupee amounts in a price string: "₹500–₹1,500 (estimate: ...)" -> (500, 1500)."""
    amounts = [int(n.replace(",", "")) for n in re.findall(r"₹\s?(\d[\d,]*)", price_info or "")]
    if not amounts:
        return None
    return min(amounts), max(amounts)


def template_reason(provider: dict) -> str:
    """One-line, non-LLM explanation. The LLM step may later replace this text."""
    parts = []
    if provider.get("rating") is not None and provider.get("review_count"):
        parts.append(f"{provider['rating']:.1f}★ from {provider['review_count']} reviews")
    else:
        parts.append("no reviews yet")
    if provider.get("distance_km") is not None:
        parts.append(describe_distance(provider["distance_km"]))
    elif provider.get("location_approximate"):
        parts.append("exact location not listed")
    if provider.get("over_budget"):
        parts.append("typical price may be above your budget")
    text = ", ".join(parts)
    return text[0].upper() + text[1:] + "."


def describe_distance(distance_km: float) -> str:
    if distance_km < 0.5:
        return "less than 0.5 km away"
    return f"about {distance_km} km away"


def _distance(provider: dict, user_point, pins) -> tuple[float | None, bool]:
    key = pin_key(provider)
    if key is None or user_point is None:
        return None, False
    if key in pins:
        return None, True
    return haversine_km(user_point[0], user_point[1], provider["lat"], provider["lng"]), False


def _prior_rating(providers: list[dict]) -> float:
    ratings = [p["rating"] for p in providers if p.get("rating") is not None and (p.get("review_count") or 0) > 0]
    return sum(ratings) / len(ratings) if ratings else DEFAULT_PRIOR_RATING


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
