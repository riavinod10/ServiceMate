"""Analytics Agent (Person 4).

Totals are computed in Django. Gemini only writes the short insights paragraph
and is skipped when it is unavailable. Spending uses the budget on the request
when one was given, otherwise the midpoint of that category's estimate range.
"""
import logging
import re

from pydantic import BaseModel, Field

from ..gemini import GeminiUnavailable, generate_structured
from ..models import Booking, ServiceRequest
from ..providers.categories import CATEGORIES, resolve_category

logger = logging.getLogger(__name__)

AGENT_NAME = "Analytics Agent"
MINUTES_SAVED_PER_REQUEST = 45

SYSTEM_INSTRUCTION = """You write a short insights paragraph for someone in India who uses ServiceMate for home services.
Use only the figures in the prompt. Do not invent providers, bookings, savings, or prices.
Mention that time saved assumes 45 minutes of manual searching per request.
Say that spending is an estimate from the user's budget or a typical category price, not an invoice.
Two or three sentences, plain English, no emoji, no markdown."""


class InsightParagraph(BaseModel):
    paragraph: str = Field(description="Two or three sentences summarising the user's service activity")


def build_analytics(user, *, with_insight: bool = True) -> dict:
    requests = list(ServiceRequest.objects.filter(user=user).order_by("-created_at"))
    bookings = {
        booking.request_id: booking
        for booking in Booking.objects.filter(request__user=user)
    }
    category_counts: dict[str, int] = {}
    spend = 0
    priced = 0
    completed = 0
    cancelled = 0
    for item in requests:
        booking = bookings.get(item.id)
        label = _category_label(item)
        category_counts[label] = category_counts.get(label, 0) + 1
        if booking is not None and booking.status == "cancelled":
            cancelled += 1
            continue
        if booking is not None:
            completed += 1
        amount = _estimate_rupees(item, booking)
        if amount:
            spend += amount
            priced += 1

    minutes = len(requests) * MINUTES_SAVED_PER_REQUEST
    categories = [
        {"label": label, "count": count}
        for label, count in sorted(category_counts.items(), key=lambda pair: (-pair[1], pair[0]))
    ]
    summary = {
        "request_count": len(requests),
        "completed_count": completed,
        "cancelled_count": cancelled,
        "estimated_spend_inr": spend,
        "priced_request_count": priced,
        "minutes_saved": minutes,
        "minutes_each": MINUTES_SAVED_PER_REQUEST,
        "time_saved_label": format_duration(minutes),
        "categories": categories,
        "spend_is_estimate": True,
    }
    if with_insight:
        paragraph, source = write_insight(summary)
        summary["insight"] = paragraph
        summary["insight_source"] = source
    return summary


def write_insight(summary: dict) -> tuple[str, str]:
    fallback = fallback_insight(summary)
    try:
        result = generate_structured(_prompt(summary), InsightParagraph, SYSTEM_INSTRUCTION)
    except GeminiUnavailable as exc:
        logger.info("Analytics insight used the template paragraph: %s", exc)
        return fallback, "template"
    except Exception:
        logger.exception("Analytics insight failed; using the template paragraph")
        return fallback, "template"
    text = " ".join((result.paragraph or "").split())
    if not text:
        return fallback, "template"
    return text, "llm"


def fallback_insight(summary: dict) -> str:
    requests = summary["request_count"]
    if requests == 0:
        return (
            "No services yet. Time saved assumes 45 minutes of manual searching per request, "
            "and spending will be an estimate from your budget or a typical category price."
        )
    top = summary["categories"][0]["label"] if summary["categories"] else "home services"
    return (
        f"You have made {requests} service request{'s' if requests != 1 else ''}, "
        f"most often for {top}. Estimated spend is ₹{summary['estimated_spend_inr']:,}, "
        f"taken from the budget you set or a typical category price, not an invoice. "
        f"Time saved assumes 45 minutes of manual searching per request, "
        f"about {summary['time_saved_label']} across these requests. "
        f"{summary['completed_count']} booking request{'s are' if summary['completed_count'] != 1 else ' is'} "
        f"still active and {summary['cancelled_count']} were cancelled."
    )


def format_duration(minutes: int) -> str:
    hours, mins = divmod(max(minutes, 0), 60)
    if hours and mins:
        return f"{hours} h {mins} min"
    if hours:
        return f"{hours} h"
    return f"{mins} min"


def _category_label(item: ServiceRequest) -> str:
    requirements = item.requirements or {}
    category = resolve_category(str(requirements.get("category") or "")) or resolve_category(item.raw_text)
    if category is not None:
        return category.label
    raw = str(requirements.get("category") or "").strip()
    if raw:
        known = CATEGORIES.get(raw)
        return known.label if known else raw.replace("_", " ").title()
    return "Unspecified"


def _estimate_rupees(item: ServiceRequest, booking: Booking | None) -> int:
    requirements = item.requirements or {}
    details = (booking.details if booking is not None else None) or {}
    for value in (details.get("budget_inr"), requirements.get("budget"), requirements.get("budget_inr")):
        amount = _as_rupees(value)
        if amount:
            return amount
    category = resolve_category(str(requirements.get("category") or "")) or resolve_category(item.raw_text)
    if category is None:
        return 0
    return (category.price_min + category.price_max) // 2


def _as_rupees(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    if isinstance(value, str):
        digits = re.sub(r"[^\d]", "", value)
        if digits:
            amount = int(digits)
            return amount or None
    return None


def _prompt(summary: dict) -> str:
    categories = ", ".join(f"{row['label']} ({row['count']})" for row in summary["categories"]) or "none"
    return (
        f"Requests: {summary['request_count']}. "
        f"Active booking requests: {summary['completed_count']}. "
        f"Cancelled: {summary['cancelled_count']}. "
        f"Estimated spend in INR: {summary['estimated_spend_inr']}. "
        f"Requests with a price figure: {summary['priced_request_count']}. "
        f"Minutes saved: {summary['minutes_saved']} "
        f"({summary['minutes_each']} minutes assumed per request). "
        f"Categories: {categories}."
    )
