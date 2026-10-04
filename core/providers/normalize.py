"""Turn raw Apify Google Maps output into the shared provider format.

Shared provider dict (team contract, do not add or rename keys without telling
Persons 1, 3 and 4):
    name, place_id, rating, review_count, lat, lng, phone, website,
    price_info, price_is_estimate, source
"""
from collections import Counter
from dataclasses import dataclass, field

from .categories import ServiceCategory

PROVIDER_KEYS = (
    "name", "place_id", "rating", "review_count", "lat", "lng",
    "phone", "website", "price_info", "price_is_estimate", "source",
)

# Limits from core.models.Provider, so a normalized dict always saves cleanly.
MAX_NAME, MAX_PHONE, MAX_URL = 255, 64, 200

# Rejection reasons, used in the agent log ("kept 14 of 25: 6 off_topic, ...").
NO_ID, CLOSED, OFF_TOPIC, NO_PHONE, DUPLICATE = "no_id", "closed", "off_topic", "no_phone", "duplicate"


@dataclass
class NormalizeResult:
    providers: list[dict]
    rejected: Counter = field(default_factory=Counter)

    def summary(self) -> str:
        total = len(self.providers) + sum(self.rejected.values())
        if not self.rejected:
            return f"kept {len(self.providers)} of {total}"
        reasons = ", ".join(f"{n} {reason}" for reason, n in self.rejected.most_common())
        return f"kept {len(self.providers)} of {total} ({reasons})"


def rejection_reason(raw: dict, category: ServiceCategory | None) -> str | None:
    """Why a raw place should be dropped, or None to keep it."""
    if not raw.get("placeId") or not (raw.get("title") or "").strip():
        return NO_ID
    if raw.get("permanentlyClosed") or raw.get("temporarilyClosed"):
        return CLOSED
    if category is not None:
        # Google pads results with unrelated places (salons, electronics stores,
        # car AC shops), so a known category must match one of the place's
        # Google categories. Unknown categories trust Google's ordering.
        place_categories = {c.strip().lower() for c in (raw.get("categories") or []) if c}
        if raw.get("categoryName"):
            place_categories.add(raw["categoryName"].strip().lower())
        if not place_categories & category.accepted_google_categories:
            return OFF_TOPIC
    if not _phone(raw):
        # Scheduling builds a call/WhatsApp message, so a provider with no
        # phone number cannot be booked.
        return NO_PHONE
    return None


def to_provider_dict(raw: dict, category: ServiceCategory | None, source: str) -> dict:
    location = raw.get("location") or {}
    listed_price = (raw.get("price") or "").strip()
    if listed_price:
        price_info, price_is_estimate = listed_price, False
    elif category is not None:
        price_info, price_is_estimate = category.price_info, True
    else:
        price_info, price_is_estimate = "", True  # UI shows "Price on request"
    website = (raw.get("website") or "").strip()
    return {
        "name": raw["title"].strip()[:MAX_NAME],
        "place_id": raw["placeId"],
        "rating": _float_or_none(raw.get("totalScore")),
        "review_count": _int_or_zero(raw.get("reviewsCount")),
        "lat": _float_or_none(location.get("lat")),
        "lng": _float_or_none(location.get("lng")),
        "phone": _phone(raw)[:MAX_PHONE],
        "website": website if len(website) <= MAX_URL else "",
        "price_info": price_info,
        "price_is_estimate": price_is_estimate,
        "source": source,
    }


def normalize_results(raw_items: list[dict], category: ServiceCategory | None,
                      source: str = "apify") -> NormalizeResult:
    """Filter and normalize one Apify dataset, keeping Google's ranking order."""
    result = NormalizeResult(providers=[])
    seen: set[str] = set()
    for raw in raw_items:
        reason = rejection_reason(raw, category)
        if reason is None and raw["placeId"] in seen:
            reason = DUPLICATE
        if reason:
            result.rejected[reason] += 1
            continue
        seen.add(raw["placeId"])
        result.providers.append(to_provider_dict(raw, category, source))
    return result


def _phone(raw: dict) -> str:
    phone = raw.get("phoneUnformatted") or raw.get("phone") or ""
    return "".join(phone.split())


def _float_or_none(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _int_or_zero(value) -> int:
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0
