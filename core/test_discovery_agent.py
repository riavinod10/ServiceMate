"""Person 2 Week 2 tests: Discovery Agent, cache and Apify wrapper.

Apify is always mocked here, so these tests spend no credits and need no network.
"""
import os
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from . import orchestration
from .agents.discovery import _location_parts, discovery_agent
from .models import AgentLog, ProviderSearchCache, ServiceRequest
from .providers.apify_source import ApifyUnavailable, search_places
from .providers.cache import read_cache, write_cache
from .providers.categories import CATEGORIES
from .providers.fixtures import load_fixture
from .providers.normalize import normalize_results

AC = CATEGORIES["ac_repair"]
AC_RAW = load_fixture(AC)
KOTHRUD_KEY = "v1|ac_repair|kothrud|pune"
PUNE_KEY = "v1|ac_repair||pune"
SEARCH = "core.agents.discovery.search_places"


def provider(place_id, name="P", rating=4.5):
    return {"name": name, "place_id": place_id, "rating": rating, "review_count": 10,
            "lat": 18.5, "lng": 73.8, "phone": "+919000000000", "website": "",
            "price_info": "", "price_is_estimate": True, "source": "apify"}


class DiscoveryAgentTests(TestCase):
    def setUp(self):
        user = User.objects.create_user("ria", password="safe-password-123")
        self.req = ServiceRequest.objects.create(user=user, raw_text="AC not cooling", workflow_thread_id="t-1")

    def state(self, **overrides):
        state = {"request_id": self.req.id, "raw_text": "AC not cooling",
                 "requirements": {"category": "ac_repair", "locality": "Kothrud", "city": "Pune"},
                 "excluded_provider_ids": []}
        state.update(overrides)
        return state

    def logs(self):
        return " | ".join(AgentLog.objects.filter(request=self.req).values_list("message", flat=True))

    def age_cache(self, key, days):
        ProviderSearchCache.objects.filter(query=key).update(fetched_at=timezone.now() - timedelta(days=days))

    @patch(SEARCH, return_value=AC_RAW)
    def test_live_search_normalizes_and_fills_cache(self, search):
        result = discovery_agent(self.state())
        search.assert_called_once_with("AC repair", "Kothrud, Pune")
        self.assertEqual(result["status"], "ranking")
        self.assertEqual(len(result["providers"]), 18)
        self.assertEqual({p["source"] for p in result["providers"]}, {"apify"})
        self.assertEqual(len(read_cache(KOTHRUD_KEY).providers), 18)
        self.assertIn("kept 18 of 25", self.logs())

    @patch(SEARCH)
    def test_fresh_cache_skips_apify(self, search):
        write_cache(KOTHRUD_KEY, [provider(f"p{i}") for i in range(5)])
        result = discovery_agent(self.state())
        search.assert_not_called()
        self.assertEqual({p["source"] for p in result["providers"]}, {"cache"})
        self.assertIn("no Apify credits", self.logs())

    @patch(SEARCH, return_value=AC_RAW)
    def test_stale_cache_is_refreshed_and_timestamp_moves(self, search):
        write_cache(KOTHRUD_KEY, [provider(f"old{i}") for i in range(5)])
        self.age_cache(KOTHRUD_KEY, 8)
        discovery_agent(self.state())
        search.assert_called_once()
        cached = read_cache(KOTHRUD_KEY)
        self.assertTrue(cached.is_fresh)
        self.assertEqual(len(cached.providers), 18)  # old set replaced, not appended

    @patch(SEARCH, side_effect=ApifyUnavailable("timed out"))
    def test_apify_failure_falls_back_to_stale_cache(self, search):
        write_cache(KOTHRUD_KEY, [provider(f"old{i}") for i in range(5)])
        self.age_cache(KOTHRUD_KEY, 30)
        result = discovery_agent(self.state())
        self.assertEqual(len(result["providers"]), 5)
        self.assertIn("timed out", self.logs())
        self.assertIn("saved results", self.logs())

    @patch(SEARCH, side_effect=ApifyUnavailable("APIFY_API_TOKEN is not set"))
    def test_apify_failure_without_cache_uses_fixture_but_does_not_cache_it(self, search):
        result = discovery_agent(self.state())
        self.assertEqual(len(result["providers"]), 18)
        self.assertEqual({p["source"] for p in result["providers"]}, {"fixture"})
        self.assertFalse(ProviderSearchCache.objects.exists())
        self.assertIn("offline demo data", self.logs())

    @patch(SEARCH, side_effect=ApifyUnavailable("down"))
    def test_unknown_category_with_no_fallback_returns_empty(self, search):
        result = discovery_agent(self.state(requirements={"category": "pest control", "locality": "Baner"}))
        self.assertEqual(result["providers"], [])
        search.assert_called_with("pest control", "Pune")  # widened to city after Baner failed
        self.assertIn("No suitable providers", self.logs())

    @patch(SEARCH, return_value=AC_RAW)
    def test_exclusions_are_removed_but_cache_keeps_full_set(self, search):
        all_ids = [p["place_id"] for p in normalize_results(AC_RAW, AC).providers]
        result = discovery_agent(self.state(excluded_provider_ids=all_ids[:5]))
        returned = {p["place_id"] for p in result["providers"]}
        self.assertFalse(returned & set(all_ids[:5]))
        self.assertEqual(len(result["providers"]), 13)
        self.assertEqual(len(read_cache(KOTHRUD_KEY).providers), 18)

    def test_widens_to_city_when_too_few_remain(self):
        write_cache(KOTHRUD_KEY, [provider("a"), provider("b")])
        write_cache(PUNE_KEY, [provider("b"), provider("c"), provider("d")])
        with patch(SEARCH) as search:
            result = discovery_agent(self.state(excluded_provider_ids=["a"]))
        search.assert_not_called()  # both levels served from fresh cache
        self.assertEqual([p["place_id"] for p in result["providers"]], ["b", "c", "d"])
        self.assertIn("widening the search to all of Pune", self.logs())

    @patch(SEARCH, return_value=AC_RAW)
    def test_category_guessed_from_raw_text_only_when_missing(self, search):
        discovery_agent(self.state(requirements={"locality": "Kothrud"}))
        self.assertEqual(search.call_args.args[0], "AC repair")
        search.reset_mock(return_value=True)
        search.return_value = []
        discovery_agent(self.state(raw_text="pest control before AC season",
                                   requirements={"category": "pest control", "locality": "Kothrud"}))
        self.assertEqual(search.call_args_list[0].args[0], "pest control")

    def test_missing_category_logs_and_returns_empty(self):
        result = discovery_agent(self.state(raw_text="help", requirements={}))
        self.assertEqual(result["providers"], [])
        self.assertIn("No service category", self.logs())

    def test_graph_uses_the_real_discovery_agent(self):
        self.assertIs(orchestration.discovery_agent, discovery_agent)


class LocationPartsTests(SimpleTestCase):
    def test_location_formats(self):
        self.assertEqual(_location_parts({"locality": "Kothrud", "city": "Pune"}), ("Kothrud", "Pune"))
        self.assertEqual(_location_parts({"location": "Kothrud, Pune"}), ("Kothrud", "Pune"))
        self.assertEqual(_location_parts({"location": "Baner"}), ("Baner", "Pune"))
        self.assertEqual(_location_parts({"location": "Pune"}), (None, "Pune"))
        self.assertEqual(_location_parts({}), (None, "Pune"))


class ApifySourceTests(SimpleTestCase):
    @patch.dict(os.environ, {"APIFY_API_TOKEN": ""})
    def test_missing_token_raises(self):
        with self.assertRaises(ApifyUnavailable):
            search_places("AC repair", "Kothrud, Pune")

    @patch.dict(os.environ, {"APIFY_API_TOKEN": "your_apify_token_here"})
    def test_placeholder_token_raises(self):
        with self.assertRaises(ApifyUnavailable):
            search_places("AC repair", "Kothrud, Pune")

    def fake_client(self, status):
        client = MagicMock()
        client.actor.return_value.call.return_value = SimpleNamespace(id="run-1", status=status, default_dataset_id="ds-1")
        client.dataset.return_value.list_items.return_value = SimpleNamespace(items=AC_RAW)
        return client

    @patch.dict(os.environ, {"APIFY_API_TOKEN": "real-token"})
    def test_successful_run_returns_items(self):
        client = self.fake_client("SUCCEEDED")
        with patch("apify_client.ApifyClient", return_value=client):
            self.assertEqual(search_places("AC repair", "Kothrud, Pune"), AC_RAW)
        run_input = client.actor.return_value.call.call_args.kwargs["run_input"]
        self.assertEqual(run_input["searchStringsArray"], ["AC repair"])
        self.assertEqual(run_input["locationQuery"], "Kothrud, Pune")
        client.actor.assert_called_with("compass/crawler-google-places")

    @patch.dict(os.environ, {"APIFY_API_TOKEN": "real-token"})
    def test_unfinished_run_is_aborted(self):
        client = self.fake_client("RUNNING")
        with patch("apify_client.ApifyClient", return_value=client), self.assertRaises(ApifyUnavailable):
            search_places("AC repair", "Kothrud, Pune")
        client.run.assert_called_with("run-1")
        client.run.return_value.abort.assert_called_once()

    @patch.dict(os.environ, {"APIFY_API_TOKEN": "real-token"})
    def test_network_error_becomes_apify_unavailable(self):
        client = MagicMock()
        client.actor.return_value.call.side_effect = ConnectionError("no network")
        with patch("apify_client.ApifyClient", return_value=client), self.assertRaises(ApifyUnavailable):
            search_places("AC repair", "Kothrud, Pune")
