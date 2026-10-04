"""Cache key format for ProviderSearchCache.query (documented in docs/DISCOVERY.md).

Format: v1|<category>|<locality>|<city>
  - category: the canonical slug of a known category (so "AC mechanic" and
    "air conditioner service" share one entry), otherwise the slugified free text.
  - locality / city: slugified; empty segments are kept so the key shape is fixed.
Bump KEY_VERSION to invalidate every existing cache entry at once.
"""
from .categories import resolve_category
from .text import slugify

KEY_VERSION = "v1"


def make_cache_key(category: str, locality: str | None = None, city: str | None = None) -> str:
    known = resolve_category(category)
    category_part = known.slug if known else slugify(category)
    if not category_part:
        raise ValueError("A cache key needs a service category")
    return "|".join([KEY_VERSION, category_part, slugify(locality), slugify(city)])
