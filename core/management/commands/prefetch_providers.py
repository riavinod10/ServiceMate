"""Pre-load the provider cache before a demo (Person 2).

    python manage.py prefetch_providers --locality Kothrud            # all 4 known categories
    python manage.py prefetch_providers ac_repair plumber --locality Baner
    python manage.py prefetch_providers "pest control" --locality Kothrud
    python manage.py prefetch_providers --list                        # what's cached, and is it fresh?

Run it the day before the viva for the localities you will demo. Cached
results stay fresh for 7 days, so the live demo then needs no Apify call.
Entries that are already fresh are skipped (no credits spent) unless --force.
"""
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import ProviderSearchCache
from core.providers.apify_source import ApifyUnavailable, search_places
from core.providers.cache import read_cache, write_cache
from core.providers.cache_keys import make_cache_key
from core.providers.categories import CATEGORIES, resolve_category
from core.providers.normalize import normalize_results


class Command(BaseCommand):
    help = "Run live Apify searches now and save them to the provider cache."

    def add_arguments(self, parser):
        parser.add_argument("categories", nargs="*", help="Category slugs or free text. Default: all known categories.")
        parser.add_argument("--locality", default=None, help='e.g. "Kothrud". Omit for a city-wide search.')
        parser.add_argument("--city", default="Pune")
        parser.add_argument("--force", action="store_true", help="Search again even if the cache is fresh.")
        parser.add_argument("--list", action="store_true", help="Show cache entries and exit.")

    def handle(self, *args, **opts):
        if opts["list"]:
            return self.list_cache()

        names = opts["categories"] or list(CATEGORIES)
        failures = 0
        for name in names:
            category = resolve_category(name)
            search_term = category.search_term if category else name.strip()
            if not search_term:
                raise CommandError("Empty category name")
            key = make_cache_key(category.slug if category else search_term, opts["locality"], opts["city"])

            cached = read_cache(key)
            if cached and cached.is_fresh and cached.providers and not opts["force"]:
                self.stdout.write(f"{key}: already fresh ({len(cached.providers)} providers), skipped")
                continue

            location = ", ".join(p for p in (opts["locality"], opts["city"]) if p)
            self.stdout.write(f"{key}: searching Apify for '{search_term}' in {location}...")
            try:
                result = normalize_results(search_places(search_term, location), category, source="apify")
            except ApifyUnavailable as exc:
                failures += 1
                self.stderr.write(self.style.ERROR(f"{key}: failed ({exc})"))
                continue
            if not result.providers:
                failures += 1
                self.stderr.write(self.style.WARNING(f"{key}: no usable providers ({result.summary()}), cache unchanged"))
                continue
            write_cache(key, result.providers)
            self.stdout.write(self.style.SUCCESS(f"{key}: cached, {result.summary()}"))

        if failures:
            raise CommandError(f"{failures} search(es) failed; see messages above")

    def list_cache(self):
        entries = ProviderSearchCache.objects.order_by("query")
        if not entries:
            self.stdout.write("Cache is empty.")
        for entry in entries:
            cached = read_cache(entry.query)
            age_days = (timezone.now() - entry.fetched_at).days
            state = "fresh" if cached.is_fresh else "STALE"
            self.stdout.write(f"{entry.query}: {len(cached.providers)} providers, {age_days} day(s) old, {state}")
