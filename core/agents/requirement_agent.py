"""Requirement Agent (Person 3).

Converts the user's natural-language request into a structured requirements dict
and writes it to state["requirements"]. The downstream agents (Discovery and
Analysis) read from this dict, so the keys and their meaning are a team contract
documented in DISCOVERY.md.

Output keys written to state["requirements"]:
    category    str        Known slug (ac_repair / plumber / electrician /
                           home_cleaning) or a short free-text service name.
                           Empty string if the LLM cannot determine it.
    problem     str        Description of the problem ("AC not cooling").
    locality    str | ""   Neighbourhood, e.g. "Kothrud".
    city        str        City, defaults to "Pune" if absent.
    location    str        Combined "locality, city" fallback for Discovery.
    lat         float|None User latitude (None if not in request).
    lng         float|None User longitude (None if not in request).
    date        str        ISO date string, e.g. "2026-10-08" or "" if absent.
    time        str        Time string, e.g. "18:00" or "" if absent.
    budget      int|None   Budget in INR (None if not stated).
    constraints list[str]  Any other constraints the user mentioned.
    missing     list[str]  Fields the user did not provide (flagged, not guessed).
"""
import logging
from datetime import date, timedelta

from pydantic import BaseModel, Field

from ..gemini import GeminiUnavailable, generate_structured
from ..logging import log_agent_event

logger = logging.getLogger(__name__)

AGENT_NAME = "Requirement Agent"

# Known category slugs — must match core/providers/categories.py CATEGORIES keys.
KNOWN_SLUGS = {"ac_repair", "plumber", "electrician", "home_cleaning"}

# Fields considered "required" for a useful service request.
REQUIRED_FIELDS = ("category", "locality")

# ---------------------------------------------------------------------------
# Pydantic schema for structured LLM output
# ---------------------------------------------------------------------------

class RequirementSchema(BaseModel):
    """Structured extraction of a service request from natural language."""

    category: str = Field(
        default="",
        description=(
            "The service category. Use one of the known slugs when it matches: "
            "ac_repair, plumber, electrician, home_cleaning. "
            "For other services use a short English label like 'pest control' or "
            "'laptop repair'. Leave empty if you cannot determine it."
        ),
    )
    problem: str = Field(
        default="",
        description="The specific problem the user described, e.g. 'AC not cooling'.",
    )
    locality: str = Field(
        default="",
        description=(
            "Neighbourhood or area within the city, e.g. 'Kothrud'. "
            "Leave empty if the user did not mention one."
        ),
    )
    city: str = Field(
        default="Pune",
        description="City name. Default to 'Pune' if not mentioned.",
    )
    date_raw: str = Field(
        default="",
        description=(
            "The date or day the user wants the service. Preserve the user's "
            "exact wording, e.g. 'tomorrow', 'this Saturday', '10 October'. "
            "Leave empty if not mentioned."
        ),
    )
    time_raw: str = Field(
        default="",
        description=(
            "The time or time period the user wants, e.g. 'evening', '6 pm', "
            "'morning'. Leave empty if not mentioned."
        ),
    )
    budget_inr: int | None = Field(
        default=None,
        description=(
            "Budget in Indian Rupees as an integer, e.g. 1500. "
            "Extract the number from phrases like 'under ₹1,500' or 'budget of 2000'. "
            "Null if the user did not mention a budget."
        ),
    )
    constraints: list[str] = Field(
        default_factory=list,
        description=(
            "Any other constraints the user mentioned, such as 'reliable', "
            "'female technician', 'no chemicals'. Keep each item short."
        ),
    )


_SYSTEM_INSTRUCTION = (
    "You are a structured data extractor for ServiceMate AI, an Indian home-services "
    "platform. Extract service requirements from the user's message. "
    "Do not invent information the user did not provide. "
    "For dates, preserve the user's wording exactly — do NOT resolve relative dates "
    "yourself; the application will handle that. "
    "Output valid JSON matching the schema."
)


# ---------------------------------------------------------------------------
# Date / time resolution helpers
# ---------------------------------------------------------------------------

_WEEKDAY_NAMES = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

_TIME_SLOTS = {
    "morning": "09:00",
    "afternoon": "14:00",
    "evening": "18:00",
    "night": "20:00",
}

_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _resolve_date(raw: str, today: date) -> str:
    """Turn a natural-language date string into an ISO date (YYYY-MM-DD).

    Returns the original raw string unchanged when it cannot be resolved, so
    the caller can decide whether to surface it as "missing".
    """
    if not raw:
        return ""
    text = raw.strip().lower()

    if text in ("today", "now"):
        return today.isoformat()
    if text == "tomorrow":
        return (today + timedelta(days=1)).isoformat()
    if text in ("day after tomorrow", "day after"):
        return (today + timedelta(days=2)).isoformat()

    # "this <weekday>" or "next <weekday>"
    for prefix, extra_weeks in (("this ", 0), ("next ", 1)):
        if text.startswith(prefix):
            day_name = text[len(prefix):]
            if day_name in _WEEKDAY_NAMES:
                target = _WEEKDAY_NAMES[day_name]
                days_ahead = (target - today.weekday()) % 7
                if days_ahead == 0 and extra_weeks == 0:
                    days_ahead = 7  # "this Monday" when today is Monday → next occurrence
                days_ahead += extra_weeks * 7
                return (today + timedelta(days=days_ahead)).isoformat()

    # bare weekday name
    if text in _WEEKDAY_NAMES:
        target = _WEEKDAY_NAMES[text]
        days_ahead = (target - today.weekday()) % 7 or 7
        return (today + timedelta(days=days_ahead)).isoformat()

    # "<day> <month>" e.g. "10 october" or "october 10"
    parts = text.split()
    if len(parts) == 2:
        a, b = parts
        try:
            day_num = int(a)
            month_num = _MONTH_NAMES.get(b)
        except ValueError:
            day_num = None
            month_num = None
        if day_num is None:
            try:
                day_num = int(b)
                month_num = _MONTH_NAMES.get(a)
            except ValueError:
                pass
        if day_num and month_num:
            year = today.year
            try:
                resolved = date(year, month_num, day_num)
                if resolved < today:
                    resolved = date(year + 1, month_num, day_num)
                return resolved.isoformat()
            except ValueError:
                pass  # invalid day/month combo, fall through

    # Could not resolve — return the raw text so the caller knows it's unresolved.
    return raw


def _resolve_time(raw: str) -> str:
    """Map a natural-language time phrase to HH:MM (24-hour).

    Returns the raw string unchanged when unresolvable.
    """
    if not raw:
        return ""
    text = raw.strip().lower()
    if text in _TIME_SLOTS:
        return _TIME_SLOTS[text]
    # Already looks like HH:MM or similar digit pattern — keep as-is.
    return raw


# ---------------------------------------------------------------------------
# Main agent function
# ---------------------------------------------------------------------------

def requirement_agent(state: dict) -> dict:
    """Extract structured requirements from the user's free-text request.

    Reads:  state["raw_text"], state["request_id"]
    Writes: state["requirements"], state["status"]
    """
    request_id = state["request_id"]
    raw_text = (state.get("raw_text") or "").strip()

    if not raw_text:
        log_agent_event(request_id, AGENT_NAME, "No request text provided.")
        return {
            "requirements": _empty_requirements(["category", "locality", "city"]),
            "status": "discovering",
        }

    log_agent_event(request_id, AGENT_NAME, f"Extracting requirements from: \"{raw_text[:120]}\"")

    # --- LLM extraction ---
    extracted: RequirementSchema | None = None
    try:
        extracted = generate_structured(
            prompt=raw_text,
            schema=RequirementSchema,
            system_instruction=_SYSTEM_INSTRUCTION,
            timeout_seconds=20,
        )
        log_agent_event(request_id, AGENT_NAME, "Requirements extracted by LLM.")
    except GeminiUnavailable as exc:
        log_agent_event(
            request_id, AGENT_NAME,
            f"LLM unavailable ({exc}). Falling back to keyword-based extraction.",
        )
        extracted = _keyword_fallback(raw_text)

    # --- Resolve relative dates against today (UTC) ---
    from django.utils import timezone as dj_timezone
    today = dj_timezone.now().date()

    resolved_date = _resolve_date(extracted.date_raw, today)
    resolved_time = _resolve_time(extracted.time_raw)

    # --- Normalise category to known slug where possible ---
    category = extracted.category.strip()
    if category and category not in KNOWN_SLUGS:
        # try matching against category aliases via the providers module
        try:
            from ..providers.categories import resolve_category
            cat = resolve_category(category)
            if cat:
                category = cat.slug
        except Exception:
            pass  # resolution is best-effort

    # --- Build the requirements dict ---
    locality = extracted.locality.strip()
    city = (extracted.city.strip() or "Pune")
    location = ", ".join(part for part in (locality, city) if part)

    requirements = {
        "category": category,
        "problem": extracted.problem.strip(),
        "locality": locality,
        "city": city,
        "location": location,
        "lat": None,
        "lng": None,
        "date": resolved_date,
        "time": resolved_time,
        "budget": extracted.budget_inr,
        "constraints": extracted.constraints,
        "missing": [],
    }

    # --- Flag missing required fields (do NOT guess) ---
    missing = []
    if not requirements["category"]:
        missing.append("category")
    if not requirements["locality"]:
        missing.append("locality")
    requirements["missing"] = missing

    # Log what was extracted
    parts = []
    if category:
        parts.append(f"category={category}")
    if locality:
        parts.append(f"locality={locality}")
    if city:
        parts.append(f"city={city}")
    if extracted.budget_inr:
        parts.append(f"budget=₹{extracted.budget_inr:,}")
    if resolved_date:
        parts.append(f"date={resolved_date}")
    if resolved_time:
        parts.append(f"time={resolved_time}")
    if missing:
        parts.append(f"missing={missing}")

    log_agent_event(
        request_id, AGENT_NAME,
        "Structured: " + (", ".join(parts) if parts else "no fields extracted"),
    )

    return {"requirements": requirements, "status": "discovering"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _empty_requirements(missing: list[str]) -> dict:
    return {
        "category": "", "problem": "", "locality": "", "city": "Pune",
        "location": "Pune", "lat": None, "lng": None,
        "date": "", "time": "", "budget": None,
        "constraints": [], "missing": missing,
    }


def _keyword_fallback(raw_text: str) -> RequirementSchema:
    """Minimal keyword-based extraction used when the LLM is unavailable.

    Tries to identify category from known alias lists and a budget from
    common ₹ patterns. Everything else is left blank so downstream agents
    handle the gaps gracefully.
    """
    text_lower = raw_text.lower()

    # Category from alias lists
    category = ""
    try:
        from ..providers.categories import CATEGORIES
        for cat in CATEGORIES.values():
            for alias in cat.aliases:
                if alias in text_lower:
                    category = cat.slug
                    break
            if category:
                break
    except Exception:
        pass

    # Budget: look for digits near ₹ or "rupees" / "rs"
    import re
    budget = None
    match = re.search(r"[₹rR][sS]?\.?\s*(\d[\d,]*)", raw_text)
    if match:
        try:
            budget = int(match.group(1).replace(",", ""))
        except ValueError:
            pass
    if budget is None:
        match = re.search(r"(\d[\d,]*)\s*(?:rupees|inr)", text_lower)
        if match:
            try:
                budget = int(match.group(1).replace(",", ""))
            except ValueError:
                pass

    return RequirementSchema(category=category, budget_inr=budget)
