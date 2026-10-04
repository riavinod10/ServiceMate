"""ProviderSearchCache read/write (rules in DISCOVERY.md).

The cache stores the full, unfiltered result set for a query. Per-request
exclusions are applied by the caller after reading, never before writing.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..models import Provider, ProviderSearchCache
from .normalize import PROVIDER_KEYS


@dataclass
class CachedResult:
    providers: list[dict]
    fetched_at: datetime

    @property
    def is_fresh(self) -> bool:
        ttl = timedelta(days=getattr(settings, "DISCOVERY_CACHE_TTL_DAYS", 7))
        return timezone.now() - self.fetched_at < ttl


def provider_to_dict(provider: Provider, source: str) -> dict:
    data = {key: getattr(provider, key) for key in PROVIDER_KEYS}
    data["source"] = source
    return data


def read_cache(key: str) -> CachedResult | None:
    entry = ProviderSearchCache.objects.filter(query=key).first()
    if entry is None:
        return None
    # Highest rated first so reads are deterministic; Analysis re-ranks anyway.
    providers = entry.providers.order_by("-rating", "-review_count", "name")
    return CachedResult([provider_to_dict(p, "cache") for p in providers], entry.fetched_at)


@transaction.atomic
def write_cache(key: str, providers: list[dict]) -> ProviderSearchCache:
    """Upsert providers by place_id and point the cache entry at exactly this set."""
    rows = []
    for data in providers:
        fields = {k: v for k, v in data.items() if k in PROVIDER_KEYS and k != "place_id"}
        row, _ = Provider.objects.update_or_create(place_id=data["place_id"], defaults=fields)
        rows.append(row)
    entry, _ = ProviderSearchCache.objects.get_or_create(query=key)
    entry.providers.set(rows)
    entry.save()  # providers.set() doesn't save the row, so fetched_at (auto_now) needs this
    return entry
