"""Service Discovery Agent (Person 2).

Reads state["requirements"], finds providers and writes state["providers"] in
the shared provider format. Source order for one query (see DISCOVERY.md):

    fresh cache -> live Apify -> stale cache -> fixture JSON -> nothing

Providers in state["excluded_provider_ids"] (cancelled or already shown) are
never returned. If fewer than DISCOVERY_MIN_RESULTS remain, the search widens
from the locality to the whole city.
"""
from django.conf import settings

from ..logging import log_agent_event
from ..providers.apify_source import ApifyUnavailable, search_places
from ..providers.cache import read_cache, write_cache
from ..providers.cache_keys import make_cache_key
from ..providers.categories import ServiceCategory, resolve_category
from ..providers.fixtures import load_fixture
from ..providers.normalize import normalize_results

AGENT_NAME = "Service Discovery Agent"


def discovery_agent(state: dict) -> dict:
    request_id = state["request_id"]
    requirements = state.get("requirements") or {}
    excluded = set(state.get("excluded_provider_ids") or [])

    category, search_term = _category_and_search_term(requirements, state.get("raw_text", ""))
    if not search_term:
        log_agent_event(request_id, AGENT_NAME, "No service category in the request, so there is nothing to search for.")
        return {"providers": [], "status": "ranking"}

    locality, city = _location_parts(requirements)
    label = category.label if category else search_term
    where = f"{locality}, {city}" if locality else city
    log_agent_event(request_id, AGENT_NAME, f"Searching for {label} providers in {where}.")

    providers = _available(_discover(request_id, category, search_term, locality, city), excluded)

    min_results = getattr(settings, "DISCOVERY_MIN_RESULTS", 3)
    if len(providers) < min_results and locality:
        log_agent_event(request_id, AGENT_NAME,
                        f"Only {len(providers)} suitable providers in {locality}; widening the search to all of {city}.")
        wider = _available(_discover(request_id, category, search_term, None, city), excluded)
        providers = _merge(providers, wider)

    if excluded:
        log_agent_event(request_id, AGENT_NAME, f"Skipped {len(excluded)} provider(s) already shown or cancelled.")
    if providers:
        log_agent_event(request_id, AGENT_NAME, f"Found {len(providers)} providers to compare.")
    else:
        log_agent_event(request_id, AGENT_NAME, "No suitable providers found right now.")
    return {"providers": providers, "status": "ranking"}


def _discover(request_id: int, category: ServiceCategory | None, search_term: str,
              locality: str | None, city: str) -> list[dict]:
    """Providers for one query, following the fallback order. Exclusions not applied."""
    key = make_cache_key(category.slug if category else search_term, locality, city)
    cached = read_cache(key)
    if cached and cached.is_fresh and cached.providers:
        log_agent_event(request_id, AGENT_NAME, f"Used {len(cached.providers)} cached providers (no Apify credits spent).")
        return cached.providers

    location_query = ", ".join(part for part in (locality, city) if part)
    try:
        result = normalize_results(search_places(search_term, location_query), category, source="apify")
        log_agent_event(request_id, AGENT_NAME, f"Apify Google Maps search: {result.summary()}.")
        if result.providers:
            write_cache(key, result.providers)
            return result.providers
    except ApifyUnavailable as exc:
        log_agent_event(request_id, AGENT_NAME, f"Live search unavailable ({exc}).")

    if cached and cached.providers:
        log_agent_event(request_id, AGENT_NAME,
                        f"Using saved results from {cached.fetched_at:%d %b %Y} instead.")
        return cached.providers

    fixture = normalize_results(load_fixture(category), category, source="fixture")
    if fixture.providers:
        # Demo data is never written to the cache, so it can't pose as a real search.
        log_agent_event(request_id, AGENT_NAME, f"Using offline demo data for {category.label}.")
        return fixture.providers
    return []


def _category_and_search_term(requirements: dict, raw_text: str) -> tuple[ServiceCategory | None, str]:
    """Known category if one matches, plus the term to send to Apify."""
    requested = (requirements.get("category") or "").strip()
    # Only guess from the raw text when the Requirement Agent gave no category;
    # otherwise "pest control before AC season" would become an AC search.
    category = resolve_category(requested) if requested else resolve_category(raw_text)
    if category:
        return category, category.search_term
    return None, requested


def _location_parts(requirements: dict) -> tuple[str | None, str]:
    """(locality, city). Accepts separate keys or one "Kothrud, Pune" string."""
    default_city = getattr(settings, "DISCOVERY_DEFAULT_CITY", "Pune")
    locality = (requirements.get("locality") or "").strip() or None
    city = (requirements.get("city") or "").strip() or None
    location = (requirements.get("location") or "").strip()
    if location and not (locality or city):
        parts = [p.strip() for p in location.split(",") if p.strip()]
        if len(parts) >= 2:
            locality, city = parts[0], parts[-1]
        elif parts and parts[0].lower() != default_city.lower():
            locality = parts[0]
    city = city or default_city
    if locality and locality.lower() == city.lower():
        locality = None
    return locality, city


def _available(providers: list[dict], excluded: set[str]) -> list[dict]:
    return [p for p in providers if p["place_id"] not in excluded]


def _merge(first: list[dict], second: list[dict]) -> list[dict]:
    seen = {p["place_id"] for p in first}
    return first + [p for p in second if p["place_id"] not in seen]
