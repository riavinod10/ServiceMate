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
