"""Person 2 Week 4 tests: re-discovery after cancellation, demo cache tools, source labels.

Apify is never called for real here.
"""
import os
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from langgraph.types import Command

from . import orchestration
from .agents.discovery import discovery_agent
from .models import Approval, ProviderSearchCache, ServiceRequest
from .providers.cache import read_cache, write_cache
from .providers.categories import CATEGORIES
from .providers.fixtures import load_fixture
from .providers.scoring import rank_providers
from .views_comparison import SOURCE_NOTES, source_note

try:
    from langgraph.checkpoint.memory import InMemorySaver as MemorySaver
except ImportError:
    from langgraph.checkpoint.memory import MemorySaver

AC_RAW = load_fixture(CATEGORIES["ac_repair"])
KOTHRUD = {"category": "ac_repair", "locality": "Kothrud", "city": "Pune", "budget": 1500}
COMMAND_SEARCH = "core.management.commands.prefetch_providers.search_places"


def cancel_booking(graph, config):
    """What the cancellation trigger (Persons 1 and 4) must do once the graph has finished.

    The graph ends after Scheduling, so there is no interrupt to resume. Instead,
    record the cancellation as if Scheduling had produced it, then continue the
    run: route_recovery sends it to recover -> discover -> analyze -> approval.
    """
    graph.update_state(config, {"status": "cancelled"}, as_node="schedule")
    return graph.invoke(None, config=config)


@patch.dict(os.environ, {"APIFY_API_TOKEN": ""})
class CancellationRediscoveryTests(TestCase):
    def setUp(self):
        self.req = ServiceRequest.objects.create(raw_text="AC not cooling", workflow_thread_id="cancel-1")
        self.graph = orchestration.build_workflow(MemorySaver())
        self.config = {"configurable": {"thread_id": "cancel-1"}}
        self.graph.invoke({"request_id": self.req.id, "raw_text": "AC not cooling", "requirements": KOTHRUD,
                           "excluded_provider_ids": [], "retry_count": 0}, config=self.config)

    def ranked_ids(self):
        return [p["place_id"] for p in Approval.objects.get(request=self.req).payload["ranked_providers"]]

    def approve_top(self):
        chosen = self.ranked_ids()[0]
        self.graph.invoke(Command(resume={"action": "approve", "provider_id": chosen}), config=self.config)
        Approval.objects.filter(request=self.req).update(decision=Approval.Decision.APPROVED)
        return chosen

    def test_cancelled_provider_is_never_suggested_again(self):
        before = self.ranked_ids()
        cancelled = self.approve_top()
        self.assertEqual(self.graph.get_state(self.config).next, ())  # finished after scheduling

        cancel_booking(self.graph, self.config)
        state = self.graph.get_state(self.config)
        self.assertEqual(state.next, ("approval",))           # paused for a new approval
        self.assertEqual(state.values["excluded_provider_ids"], [cancelled])
        self.assertEqual(state.values["retry_count"], 1)
        after = self.ranked_ids()
        self.assertNotIn(cancelled, after)
        self.assertEqual(after[:4], before[1:5])               # the others move up
        self.assertEqual(Approval.objects.get(request=self.req).decision, Approval.Decision.PENDING)

    def test_recovery_stops_at_the_retry_cap(self):
        for _ in range(2):  # WORKFLOW_MAX_RECOVERY_RETRIES defaults to 2
            self.approve_top()
            cancel_booking(self.graph, self.config)
        self.assertEqual(len(self.graph.get_state(self.config).values["excluded_provider_ids"]), 2)

        self.approve_top()
        cancel_booking(self.graph, self.config)
        self.assertEqual(self.graph.get_state(self.config).next, ())  # no third recovery
        self.assertEqual(self.graph.get_state(self.config).values["status"], "cancelled")


class StaleSourceTests(TestCase):
    def test_stale_cache_results_are_labelled(self):
        req = ServiceRequest.objects.create(raw_text="AC", workflow_thread_id="stale-1")
        write_cache("v1|ac_repair|kothrud|pune", [{
            "name": f"Old {i}", "place_id": f"old-{i}", "rating": 4.5, "review_count": 10, "lat": 18.5, "lng": 73.8,
            "phone": "+919000000000", "website": "", "price_info": "", "price_is_estimate": True, "source": "apify"}
            for i in range(3)])  # 3 = DISCOVERY_MIN_RESULTS, so the search doesn't widen
        ProviderSearchCache.objects.update(fetched_at=timezone.now() - timedelta(days=30))
        from .providers.apify_source import ApifyUnavailable
        with patch("core.agents.discovery.search_places", side_effect=ApifyUnavailable("down")):
            out = discovery_agent({"request_id": req.id, "requirements": KOTHRUD})
        self.assertEqual({p["source"] for p in out["providers"]}, {"stale_cache"})


class PrefetchCommandTests(TestCase):
    def run_command(self, *args):
        out, err = StringIO(), StringIO()
        call_command("prefetch_providers", *args, stdout=out, stderr=err)
        return out.getvalue() + err.getvalue()

    @patch(COMMAND_SEARCH, return_value=AC_RAW)
    def test_fills_cache_then_skips_when_fresh(self, search):
        output = self.run_command("ac_repair", "--locality", "Kothrud")
        search.assert_called_once_with("AC repair", "Kothrud, Pune")
        self.assertIn("kept 18 of 25", output)
        self.assertEqual(len(read_cache("v1|ac_repair|kothrud|pune").providers), 18)

        output = self.run_command("ac_repair", "--locality", "Kothrud")
        self.assertEqual(search.call_count, 1)  # no credits spent the second time
        self.assertIn("already fresh", output)

        self.run_command("ac_repair", "--locality", "Kothrud", "--force")
        self.assertEqual(search.call_count, 2)

    @patch(COMMAND_SEARCH, return_value=[])
    def test_default_is_all_known_categories(self, search):
        with self.assertRaises(CommandError):  # every search returned nothing
            self.run_command("--locality", "Baner")
        self.assertEqual([c.args[0] for c in search.call_args_list], [c.search_term for c in CATEGORIES.values()])

    @patch(COMMAND_SEARCH, return_value=AC_RAW)
    def test_free_text_category_and_list(self, search):
        self.run_command("pest control", "--locality", "Kothrud")
        search.assert_called_once_with("pest control", "Kothrud, Pune")
        output = self.run_command("--list")
        self.assertIn("v1|pest_control|kothrud|pune", output)
        self.assertIn("fresh", output)

    def test_apify_failure_is_reported_and_cache_untouched(self):
        from .providers.apify_source import ApifyUnavailable
        with patch(COMMAND_SEARCH, side_effect=ApifyUnavailable("APIFY_API_TOKEN is not set")):
            with self.assertRaises(CommandError):
                self.run_command("plumber")
        self.assertFalse(ProviderSearchCache.objects.exists())


class SourceNoteTests(TestCase):
    def test_least_reliable_source_wins(self):
        self.assertEqual(source_note([{"source": "apify"}]), SOURCE_NOTES["apify"])
        self.assertEqual(source_note([{"source": "apify"}, {"source": "fixture"}]), SOURCE_NOTES["fixture"])
        self.assertEqual(source_note([{"source": "cache"}, {"source": "stale_cache"}]), SOURCE_NOTES["stale_cache"])
        self.assertEqual(source_note([]), "")

    def test_comparison_page_shows_demo_label(self):
        user = User.objects.create_user("ria", password="safe-password-123")
        req = ServiceRequest.objects.create(user=user, raw_text="AC", workflow_thread_id="src-1")
        from .providers.normalize import normalize_results
        providers = normalize_results(AC_RAW, CATEGORIES["ac_repair"], source="fixture").providers
        Approval.objects.create(request=req, thread_id="src-1",
                                payload={"ranked_providers": rank_providers(providers, KOTHRUD).ranked})
        self.client.force_login(user)
        html = self.client.get(reverse("compare_providers", args=[req.id])).content.decode()
        self.assertIn("saved demo results", html)
