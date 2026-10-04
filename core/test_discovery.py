"""Person 2 tests: categories, cache keys and the Apify normalizer.

Kept in their own file so they never conflict with core/tests.py (Person 1).
Run: python manage.py test core
"""
from django.test import SimpleTestCase, TestCase

from .models import Provider
from .providers.cache_keys import make_cache_key
from .providers.categories import CATEGORIES, resolve_category
from .providers.fixtures import load_fixture
from .providers.normalize import (
    CLOSED, DUPLICATE, NO_PHONE, OFF_TOPIC, PROVIDER_KEYS, normalize_results,
)

AC = CATEGORIES["ac_repair"]


def raw_place(**overrides):
    place = {
        "title": "Test AC Works", "placeId": "place-1", "totalScore": 4.5, "reviewsCount": 12,
        "location": {"lat": 18.5, "lng": 73.8}, "phone": "+91 90000 00000",
        "phoneUnformatted": "+919000000000", "website": None, "price": None,
        "categoryName": "Air conditioning repair service",
        "categories": ["Air conditioning repair service"],
        "permanentlyClosed": False, "temporarilyClosed": False,
    }
    place.update(overrides)
    return place


class CategoryTests(SimpleTestCase):
    def test_free_text_maps_to_known_category(self):
        self.assertEqual(resolve_category("AC not cooling, need someone tomorrow").slug, "ac_repair")
        self.assertEqual(resolve_category("Air Conditioner service").slug, "ac_repair")
        self.assertEqual(resolve_category("leaking tap in kitchen").slug, "plumber")
        self.assertEqual(resolve_category("home_cleaning").slug, "home_cleaning")

    def test_short_alias_matches_whole_words_only(self):
        self.assertIsNone(resolve_category("vacuum repair"))  # "ac" inside "vacuum"

    def test_unknown_category_returns_none(self):
        self.assertIsNone(resolve_category("pest control"))
        self.assertIsNone(resolve_category(""))


class CacheKeyTests(SimpleTestCase):
    def test_synonyms_share_one_key(self):
        self.assertEqual(make_cache_key("AC mechanic", "Kothrud", "Pune"), "v1|ac_repair|kothrud|pune")
        self.assertEqual(make_cache_key("air conditioner service", " KOTHRUD ", "pune."), "v1|ac_repair|kothrud|pune")

    def test_unknown_category_uses_slugified_text(self):
        self.assertEqual(make_cache_key("Pest Control", "Right Bhusari Colony", "Pune"),
                         "v1|pest_control|right_bhusari_colony|pune")

    def test_missing_locality_keeps_key_shape(self):
        self.assertEqual(make_cache_key("plumber", None, "Pune"), "v1|plumber||pune")

    def test_empty_category_is_rejected(self):
        with self.assertRaises(ValueError):
            make_cache_key("  ")


class NormalizerTests(SimpleTestCase):
    def test_output_matches_shared_contract_exactly(self):
        provider = normalize_results([raw_place()], AC).providers[0]
        self.assertEqual(tuple(provider), PROVIDER_KEYS)
        self.assertEqual(provider["phone"], "+919000000000")
        self.assertEqual(provider["website"], "")
        self.assertEqual(provider["source"], "apify")

    def test_missing_price_uses_labelled_category_estimate(self):
        provider = normalize_results([raw_place()], AC).providers[0]
        self.assertTrue(provider["price_is_estimate"])
        self.assertIn("estimate", provider["price_info"])

    def test_listed_price_is_not_an_estimate(self):
        provider = normalize_results([raw_place(price="₹₹")], AC).providers[0]
        self.assertEqual(provider["price_info"], "₹₹")
        self.assertFalse(provider["price_is_estimate"])

    def test_unrated_place_is_kept_with_safe_defaults(self):
        provider = normalize_results([raw_place(totalScore=None, reviewsCount=None)], AC).providers[0]
        self.assertIsNone(provider["rating"])
        self.assertEqual(provider["review_count"], 0)

    def test_filters_and_counts_rejections(self):
        result = normalize_results([
            raw_place(),
            raw_place(placeId="dup"), raw_place(placeId="dup"),
            raw_place(placeId="car", categoryName="Auto repair shop", categories=["Auto repair shop"]),
            raw_place(placeId="closed", permanentlyClosed=True),
            raw_place(placeId="nophone", phone="", phoneUnformatted=""),
        ], AC)
        self.assertEqual([p["place_id"] for p in result.providers], ["place-1", "dup"])
        self.assertEqual(result.rejected, {DUPLICATE: 1, OFF_TOPIC: 1, CLOSED: 1, NO_PHONE: 1})
        self.assertEqual(result.summary(), "kept 2 of 6 (1 duplicate, 1 off_topic, 1 closed, 1 no_phone)")

    def test_unknown_category_skips_relevance_filter(self):
        place = raw_place(categoryName="Pest control service", categories=["Pest control service"])
        provider = normalize_results([place], None).providers[0]
        self.assertEqual(provider["price_info"], "")
        self.assertTrue(provider["price_is_estimate"])


class FixtureTests(SimpleTestCase):
    """Run the normalizer over the real saved Apify runs."""

    def test_every_known_category_has_usable_fallback_data(self):
        for slug, category in CATEGORIES.items():
            with self.subTest(category=slug):
                result = normalize_results(load_fixture(category), category, source="fixture")
                self.assertGreaterEqual(len(result.providers), 10)
                for provider in result.providers:
                    self.assertTrue(provider["phone"])
                    self.assertIsNotNone(provider["lat"])
                    self.assertEqual(provider["source"], "fixture")

    def test_off_topic_places_from_real_data_are_removed(self):
        names = {p["name"] for p in normalize_results(load_fixture(AC), AC).providers}
        self.assertNotIn("Shiv Car Ac", names)            # auto repair shop
        self.assertNotIn("Sadgurn Refrigeration sale & service.", names)
        cleaning = CATEGORIES["home_cleaning"]
        names = {p["name"] for p in normalize_results(load_fixture(cleaning), cleaning).providers}
        self.assertNotIn("Croma - Kothrud", names)
        self.assertIn("Cleaner Masters", names)

    def test_unknown_category_has_no_fixture(self):
        self.assertEqual(load_fixture(None), [])


class ProviderModelTests(TestCase):
    def test_normalized_providers_save_to_provider_model(self):
        for category in CATEGORIES.values():
            for provider in normalize_results(load_fixture(category), category, "fixture").providers:
                Provider.objects.update_or_create(
                    place_id=provider["place_id"],
                    defaults={k: v for k, v in provider.items() if k != "place_id"},
                )
        self.assertGreater(Provider.objects.count(), 50)
