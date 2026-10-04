# Service Discovery: design decisions (Person 2)

This file records how provider discovery and caching work, so the other agents
can rely on it. Code lives in `core/providers/`; tests in `core/test_discovery.py`.

## Data source

Discovery calls the Apify Google Maps Scraper (`compass/crawler-google-places`)
**live** for every query that isn't cached. The files in `core/fixtures/apify/`
are not the data source. They are test data for the normalizer and offline
fallback for the demo.

Actor input used for the saved runs (reuse it for live runs):

| Field | Value |
|---|---|
| `searchStringsArray` | the category's `search_term`, e.g. `["AC repair"]` |
| `locationQuery` | `"<locality>, <city>"`, e.g. `"Kothrud, Pune"` |
| `maxCrawledPlacesPerSearch` | 25 |
| `language` | `en` |

## Categories

`core/providers/categories.py` defines the known categories: `ac_repair`,
`plumber`, `electrician`, `home_cleaning`. Each has aliases (to map user text to
the category), the Google categories it accepts, a price estimate and a fixture.

Any other category can still be searched. It uses the user's own words as the
search term and cache key, and it gets no relevance filter, no price estimate
and no offline fallback.

**Person 3:** please output `requirements["category"]` as one of the four slugs
when it applies, otherwise a short free-text service name ("pest control").

## Cache key format

```
v1|<category>|<locality>|<city>      e.g.  v1|ac_repair|kothrud|pune
```

- Category is the canonical slug for known categories, so "AC mechanic" and
  "air conditioner service" share one entry. Otherwise it is the slugified text.
- Locality and city are lowercased, with punctuation removed and spaces turned
  into `_`. An empty locality stays as an empty segment: `v1|plumber||pune`.
- Changing `KEY_VERSION` in `cache_keys.py` invalidates every entry at once.

Built by `make_cache_key(category, locality, city)`. Nothing else should build keys.

## Expiry and fallback order

Cache entries are fresh for **7 days** (local businesses rarely change faster).
Discovery tries these sources in order and stops at the first that returns providers:

1. Fresh cache entry (under 7 days old)
2. Live Apify run, which then refreshes the cache
3. Stale cache entry, if Apify fails or times out
4. Fixture JSON for the category, if one exists
5. Nothing: log it and set no providers, so the user sees "no providers found right now"

`ProviderSearchCache.fetched_at` uses `auto_now`, but `cache.providers.set(...)`
does not save the row. Always call `cache.save()` after a refresh, or the
timestamp never moves.

## Exclusions

The cache is shared by all users; `excluded_provider_ids` belongs to one request.
So the cache always stores the **full, unfiltered** result set, and exclusions are
removed when reading it, never when writing.

Analysis puts only the **top 5** into `ranked_providers`. "Search again" excludes
every shown provider, so showing 5 out of 10–18 leaves enough for another round.
If fewer than 3 remain after exclusions, discovery widens to a city-level search
(`v1|ac_repair||pune`), which is a different key and triggers a new run.

## Normalization rules

Output is exactly the shared provider dict (`PROVIDER_KEYS` in `normalize.py`).
A raw place is dropped when it:

- has no `placeId` or name;
- is permanently or temporarily closed;
- is **off-topic**: for a known category, none of its Google categories are in
  the accepted list. Google pads results with unrelated places (the home-cleaning
  run returned salons, an electronics store and a fried chicken takeaway);
- has **no phone number**. Scheduling builds a call/WhatsApp message, so a
  provider without a phone can't be booked;
- duplicates a `placeId` already seen.

Each run's rejections are counted, for the agent log:
`kept 18 of 25 (6 off_topic, 1 no_phone)`.

Other rules: unrated places are kept with `rating=None, review_count=0`.
Websites longer than the model's 200-character limit become `""`.

Results kept from the saved runs: AC repair 18/25, electrician 17/25,
plumber 12/22, home cleaning 11/25.

## Prices

None of the 97 saved places has a price. When Google lists none, `price_info`
is the category estimate (e.g. "₹500–₹1,500 (estimate: typical service visit;
gas refill and parts extra)") with `price_is_estimate=True`, and the UI must
label it as an estimate. Unknown categories get `price_info=""`; the UI shows
"Price on request". The estimate ranges are rough Pune figures and should be
reviewed by the team before the demo.

## Known data issue for Provider Analysis

Several unrelated businesses share the exact coordinates `18.5073514, 73.8076543`.
This looks like Google's default pin for Kothrud, used for service-area
businesses with no shop address. Taken at face value, these providers would all
look very close to a user in central Kothrud. Analysis should treat coordinates
shared by two or more different places as "distance unknown" and give a neutral
distance score.

## Discovery Agent (Week 2)

`core/agents/discovery.py` is the `discover` node in the graph. It replaces the
stub in `core/orchestration.py` with one import line.

**What it reads from `state["requirements"]` (Person 3):**

| Key | Example | If missing |
|---|---|---|
| `category` | `"ac_repair"` or `"pest control"` | guessed from `raw_text`; if still unknown, no search |
| `locality` + `city` | `"Kothrud"`, `"Pune"` | city defaults to Pune; no locality means a city-wide search |
| or `location` | `"Kothrud, Pune"` | used only when `locality`/`city` are absent |

**What it writes:** `providers` (all available, exclusions removed, not yet
ranked) and `status="ranking"`. Every step is logged to the agent log as
"Service Discovery Agent".

**Apify token:** set `APIFY_API_TOKEN` in your local `.env`. `docker-compose.yml`
passes it to the worker container. Without a token, discovery still works using
cache and fixtures, and the agent log says "Live search unavailable".

Settings (optional, read with defaults, not in `settings.py`):
`DISCOVERY_CACHE_TTL_DAYS=7`, `DISCOVERY_MAX_PLACES=25`,
`DISCOVERY_APIFY_TIMEOUT_SECONDS=150`, `DISCOVERY_MIN_RESULTS=3`,
`DISCOVERY_DEFAULT_CITY="Pune"`.

## Provider Analysis Agent (Week 3)

`core/agents/analysis.py` is the `analyze` node. It scores every provider from
Discovery and writes the **top 5** to `ranked_providers`. Person 1's
`create_approval` node copies them into `Approval.payload`.

**Score (0–100), from `core/providers/scoring.py`:**

| Part | Weight | How |
|---|---|---|
| Rating quality | 40% | Bayesian average: the rating is blended with 10 "average" reviews (average = mean rating of this search), so a 5.0 from 2 reviews counts for less than a 4.7 from 385. Mapped from 3.0–5.0 to 0–100. |
| Review count | 20% | `log(1 + reviews) / log(501)`, capped at 100. 10 reviews ≈ 39, 500+ = 100. |
| Distance | 30% | Haversine (straight-line) km from the user. 0 km = 100, 10 km or more = 0. |
| Budget fit | 10% | Budget covers the top of the price range = 100; falls to 50 at the bottom of the range and towards 0 below it. No budget or no price = 50. |

Budget fit is usually the same for every provider in a category, because prices
are category estimates. It rarely changes the order, but it flags "typical
price may be above your budget".

**User location** (for distance), in order: `requirements["lat"/"lng"]` if
present; the approximate centre of a known Pune locality (`PUNE_LOCALITIES` in
`geo.py`); the middle of the search results; the centre of Pune.

**Unknown provider locations** (no coordinates, Google's default area pin, or a
pin shared by 2+ businesses) get `distance_km=None` and the typical distance
score for that search, so they are neither helped nor hurt.

**Fields added to each ranked provider** (on top of the shared provider dict):
`rank`, `score`, `score_breakdown`, `distance_km`, `location_approximate`,
`over_budget`, `reason`, `reason_source`. All are plain JSON.

**Comparison page:** `/requests/<id>/compare/` (`core/views_comparison.py`). It
reads the latest `Approval.payload` and links to the Approvals page for the
decision. The Approvals page links back to it. The page is self-contained for
now; Person 4 can make it extend `base.html` and drop its `<style>` block.

**LLM explanation:** see "Ranking explanations (Gemini)" below.

## Re-discovery after a cancellation (Week 4)

Discovery already skips everything in `excluded_provider_ids`, and Person 1's
`recover` node adds the cancelled provider there, so no new Person 2 code is
needed for re-discovery. `core/test_recovery_demo.py` proves the full loop:
the cancelled provider never comes back, the other providers move up, and the
request returns to "pending" on the Approvals page.

**For Persons 1 and 4 (cancellation trigger):** the graph has already finished
after Scheduling, so there is no interrupt to resume. The trigger must record
the cancellation as if Scheduling had produced it, then continue the run, using
the same checkpointer and `thread_id`:

```python
config = {"configurable": {"thread_id": service_request.workflow_thread_id}}
graph.update_state(config, {"status": "cancelled"}, as_node="schedule")
graph.invoke(None, config=config)   # recover -> discover -> analyze -> approval
```

This should run in a Celery task (discovery may call Apify), like `run_workflow`.

**Note for Person 1:** "Search again" and cancellation recovery share
`retry_count`. After two "Search again" clicks (retry_count = 2), a later
cancellation ends the workflow instead of recovering. Consider a separate
counter if that matters for the demo.

## Where results came from (`source`)

| `source` | Meaning | Comparison page says |
|---|---|---|
| `apify` | live search for this request | Live results from Google Maps |
| `cache` | fresh cache (under 7 days) | Results from a search in the last 7 days |
| `stale_cache` | Apify failed, older cache used | Older saved results |
| `fixture` | Apify failed, no cache, demo JSON used | Saved demo results, not a live search |

## Demo day checklist

1. The day before, with `APIFY_API_TOKEN` set, pre-load the localities you will demo:
   `python manage.py prefetch_providers --locality Kothrud`
   (all 4 categories; about 4 Apify runs). Fresh entries are skipped, so running
   it twice costs nothing. Add `--force` to refresh anyway.
2. Check what's cached: `python manage.py prefetch_providers --list`. Every entry
   you need should say "fresh" (they last 7 days).
3. Use the same category and locality in the demo request, so it hits the cache:
   the agent log shows "Used N cached providers (no Apify credits spent)".
4. If the network fails on stage, discovery falls back to the demo JSON and the
   Comparison page says so. That fallback only covers the 4 known categories,
   with Kothrud providers.

## Ranking explanations (Gemini)

Team standard: Google Gemini via `google-genai`, configured by `GEMINI_API_KEY`
and `GEMINI_MODEL` (default `gemini-3.6-flash`). The shared setup is
`core/gemini.py`; Person 3's Requirement Agent should reuse
`generate_structured()` from there rather than creating its own client.

`core/providers/explain.py` makes **one** Gemini call per ranking, after scoring.
It sends the user's request and, for each of the top 5, only the facts scoring
already used (name, rank, score, breakdown, rating, reviews, distance, price,
budget flag). No phone numbers, websites or coordinates. Gemini returns
`{"reasons": [{"place_id", "reason"}]}` (Pydantic schema), matched back by
`place_id`.

Gemini can only change `reason` (and sets `reason_source="llm"`). Order, scores
and every other field stay exactly as scoring produced them. If Gemini skips
any provider, returns an empty or malformed reply, times out, errors, or there
is no API key, **all** reasons stay as templates and the workflow carries on.
The agent log says either "Explanations written by the LLM" or "LLM
unavailable, used standard explanations".

Limits: 20-second timeout and at most 2 attempts (the SDK default is 5).
Temperature and penalties are not set: `gemini-3.6-flash` ignores the former
and rejects the latter. Tests always fake Gemini; test classes that run the
agent blank `GEMINI_API_KEY` so a key in your shell can't cause real calls.

## Frontend (Login/Register, Approvals, Comparison)

All three pages extend `core/templates/core/brand_base.html`, a temporary
layout holding the fonts, `core/static/core/css/servicemate.css`, the top bar
(with a POST logout button) and flash messages. Every CSS class starts with
`sm-`. When Person 4's shared `base.html` exists: move the font/stylesheet links
and `_topbar.html` into it and change each page's `{% extends %}` line.

Logo files: `core/static/core/img/servicemate-logo.png` (cobalt) and
`servicemate-logo-white.png`, both with real transparency. The uploaded PNG had
the checkerboard painted in, so these were extracted from it.

Backend changes for the UI: only a confirmation message in `approval_action`.
The Comparison page's "Choose this provider" buttons post to the existing
`approval_action` endpoint, so ownership checks and the LangGraph resume are
exactly the same as on the Approvals page.
