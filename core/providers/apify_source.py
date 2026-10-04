"""Live Google Maps search through the Apify Actor `compass/crawler-google-places`.

Written for apify-client 3.x, where `actor().call()` returns a typed `Run`
object and timeouts are `timedelta`s (older tutorials use dicts and seconds).
Every failure is raised as ApifyUnavailable so discovery can fall back cleanly.
"""
import logging
import os
from datetime import timedelta

from django.conf import settings

logger = logging.getLogger(__name__)

ACTOR_ID = "compass/crawler-google-places"


class ApifyUnavailable(Exception):
    """No token, network error, failed run or timeout: use the fallback data."""


def build_run_input(search_term: str, location_query: str, max_places: int) -> dict:
    return {
        "searchStringsArray": [search_term],
        "locationQuery": location_query,
        "maxCrawledPlacesPerSearch": max_places,
        "language": "en",
    }


def search_places(search_term: str, location_query: str) -> list[dict]:
    """Run the Actor and return its raw dataset items (not yet normalized)."""
    token = os.environ.get("APIFY_API_TOKEN", "").strip()
    if not token or token == "your_apify_token_here":
        raise ApifyUnavailable("APIFY_API_TOKEN is not set")

    from apify_client import ApifyClient

    max_places = getattr(settings, "DISCOVERY_MAX_PLACES", 25)
    timeout = timedelta(seconds=getattr(settings, "DISCOVERY_APIFY_TIMEOUT_SECONDS", 150))
    client = ApifyClient(token)
    try:
        run = client.actor(ACTOR_ID).call(
            run_input=build_run_input(search_term, location_query, max_places),
            max_items=max_places,      # hard cap on results we pay for
            run_timeout=timeout,       # Apify stops the run itself after this
            wait_duration=timeout,     # and we stop waiting at the same point
            logger=None,               # don't stream Actor logs into the worker
        )
    except Exception as exc:  # network, auth or quota errors
        raise ApifyUnavailable(f"Apify call failed: {exc}") from exc

    if run is None:
        raise ApifyUnavailable("Apify returned no run")
    status = getattr(run.status, "value", run.status)
    if status != "SUCCEEDED":
        if status in {"READY", "RUNNING"}:
            # We stopped waiting; abort so the run stops spending credits.
            try:
                client.run(run.id).abort()
            except Exception:
                logger.warning("Could not abort Apify run %s", run.id)
        raise ApifyUnavailable(f"Apify run ended with status {status}")

    try:
        return client.dataset(run.default_dataset_id).list_items(limit=max_places).items
    except Exception as exc:
        raise ApifyUnavailable(f"Could not read Apify results: {exc}") from exc
