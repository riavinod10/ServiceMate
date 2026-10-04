"""Person 2 Week 3 tests: scoring, Provider Analysis Agent, Comparison page, graph flow.

Apify is never called here (the token is blanked), so discovery uses fixtures.
"""
import json
import os
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from . import orchestration
from .agents.analysis import analysis_agent
from .models import AgentLog, Approval, ServiceRequest
from .providers.categories import CATEGORIES
from .providers.fixtures import load_fixture
from .providers.geo import haversine_km, locate_user, shared_pins
from .providers.normalize import PROVIDER_KEYS, normalize_results
from .providers.scoring import (
    budget_score, parse_budget, parse_price_range, quality_score, rank_providers, review_score,
)

try:
    from langgraph.checkpoint.memory import InMemorySaver as MemorySaver
except ImportError:  # older langgraph
    from langgraph.checkpoint.memory import MemorySaver

KOTHRUD = {"category": "ac_repair", "locality": "Kothrud", "city": "Pune", "budget": 1500}


def ac_providers():
    return normalize_results(load_fixture(CATEGORIES["ac_repair"]), CATEGORIES["ac_repair"]).providers


def provider(place_id, rating=4.5, reviews=20, lat=18.51, lng=73.81, **extra):
    return {"name": place_id.title(), "place_id": place_id, "rating": rating, "review_count": reviews,
            "lat": lat, "lng": lng, "phone": "+919000000000", "website": "",
            "price_info": "₹500–₹1,500 (estimate)", "price_is_estimate": True, "source": "fixture", **extra}


class GeoTests(SimpleTestCase):
    def test_haversine_matches_known_distance(self):
        self.assertAlmostEqual(haversine_km(18.5204, 73.8567, 19.0760, 72.8777), 120.2, delta=0.5)  # Pune–Mumbai
        self.assertEqual(haversine_km(18.5, 73.8, 18.5, 73.8), 0)

    def test_user_location_sources_in_order(self):
        self.assertEqual(locate_user({"lat": 18.6, "lng": 73.7}, [])[1], "exact location")
        self.assertEqual(locate_user({"locality": "Baner"}, [])[1], "centre of Baner")
        self.assertEqual(locate_user({"location": "kothrud, Pune"}, [])[1], "centre of Kothrud")
        point, note = locate_user({"locality": "Nowhere"}, [provider("a", lat=18.0, lng=73.0), provider("b", lat=18.2, lng=73.2)])
        self.assertEqual((note, point), ("middle of the search results", (18.1, 73.1)))
        self.assertEqual(locate_user({}, [])[1], "centre of Pune")
        self.assertEqual(locate_user({"city": "Mumbai"}, []), (None, "unknown"))

    def test_shared_and_known_default_pins(self):
        pins = shared_pins([provider("a", lat=18.1, lng=73.1), provider("b", lat=18.1, lng=73.1), provider("c")])
        self.assertIn((18.1, 73.1), pins)
        self.assertNotIn((18.51, 73.81), pins)
        self.assertIn((18.507351, 73.807654), pins)  # Kothrud default pin, even when seen once


class ScoreTests(SimpleTestCase):
    def test_few_reviews_count_for_less_than_many(self):
        self.assertLess(quality_score(5.0, 2, prior=4.5), quality_score(4.8, 300, prior=4.5))
        self.assertEqual(quality_score(None, 0, prior=4.5), quality_score(4.5, 50, prior=4.5))

    def test_review_count_is_log_scaled(self):
        self.assertAlmostEqual(review_score(10), 0.386, places=2)
        self.assertEqual(review_score(500), review_score(50000))  # capped, can't dominate
        self.assertEqual(review_score(0), 0)

    def test_budget_parsing_and_fit(self):
        for value in (1500, "1500", "₹1,500", "under ₹1,500", {"max": 1500}):
            self.assertEqual(parse_budget(value), 1500)
        self.assertIsNone(parse_budget(None))
        self.assertEqual(parse_price_range("₹1,500–₹6,000 (estimate: 1–3 BHK deep clean)"), (1500, 6000))
        self.assertEqual(budget_score(2000, (500, 1500)), 1.0)
        self.assertEqual(budget_score(1000, (500, 1500)), 0.75)
        self.assertEqual(budget_score(250, (500, 1500)), 0.25)
        self.assertEqual(budget_score(None, (500, 1500)), 0.5)

    def test_unknown_location_gets_typical_distance_not_best(self):
        near = provider("near", lat=18.5074, lng=73.8077)
        pinned = provider("pinned", lat=18.5073514, lng=73.8076543)  # Kothrud default pin
        far = provider("far", lat=18.56, lng=73.79)
        result = rank_providers([near, pinned, far], {"locality": "Kothrud"}, top_n=3)
        by_id = {p["place_id"]: p for p in result.ranked}
        self.assertIsNone(by_id["pinned"]["distance_km"])
        self.assertTrue(by_id["pinned"]["location_approximate"])
        self.assertLess(by_id["pinned"]["score_breakdown"]["distance"], by_id["near"]["score_breakdown"]["distance"])
        self.assertGreater(by_id["pinned"]["score_breakdown"]["distance"], by_id["far"]["score_breakdown"]["distance"])
        self.assertEqual(by_id["pinned"]["reason"].split(", ")[1], "exact location not listed.")


class RankingTests(SimpleTestCase):
    def test_top_five_best_first_with_contract_fields_and_json_safe(self):
        result = rank_providers(ac_providers(), KOTHRUD)
        self.assertEqual(result.total, 18)
        self.assertEqual([p["rank"] for p in result.ranked], [1, 2, 3, 4, 5])
        scores = [p["score"] for p in result.ranked]
        self.assertEqual(scores, sorted(scores, reverse=True))
        for p in result.ranked:
            self.assertTrue(set(PROVIDER_KEYS) <= set(p))
            self.assertTrue(0 <= p["score"] <= 100)
            self.assertEqual(set(p["score_breakdown"]), {"quality", "reviews", "distance", "budget"})
        json.dumps(result.ranked)  # must survive Approval.payload and the checkpointer

    def test_does_not_mutate_discovery_output(self):
        providers = ac_providers()
        rank_providers(providers, KOTHRUD)
        self.assertNotIn("score", providers[0])

    def test_low_budget_is_flagged(self):
        ranked = rank_providers(ac_providers(), {**KOTHRUD, "budget": 300}).ranked
        self.assertTrue(all(p["over_budget"] for p in ranked))
        self.assertIn("above your budget", ranked[0]["reason"])


@patch.dict(os.environ, {"GEMINI_API_KEY": ""})  # never call Gemini from tests
class AnalysisAgentTests(TestCase):
    def setUp(self):
        self.req = ServiceRequest.objects.create(raw_text="AC not cooling", workflow_thread_id="t-analysis")

    def test_ranks_and_logs(self):
        out = analysis_agent({"request_id": self.req.id, "providers": ac_providers(), "requirements": KOTHRUD})
        self.assertEqual(out["status"], "awaiting_approval")
        self.assertEqual(len(out["ranked_providers"]), 5)
        logs = " | ".join(AgentLog.objects.filter(request=self.req).values_list("message", flat=True))
        self.assertIn("Scored 18 providers", logs)
        self.assertIn("centre of Kothrud", logs)
        self.assertIn(out["ranked_providers"][0]["name"], logs)

    def test_empty_providers(self):
        out = analysis_agent({"request_id": self.req.id, "providers": []})
        self.assertEqual(out, {"ranked_providers": [], "status": "awaiting_approval"})

    def test_graph_uses_the_real_analysis_agent(self):
        self.assertIs(orchestration.analysis_agent, analysis_agent)


@patch.dict(os.environ, {"APIFY_API_TOKEN": "", "GEMINI_API_KEY": ""})
class GraphFlowTests(TestCase):
    """Person 1's graph with Person 2's real nodes: discover, rank, pause, resume."""

    def setUp(self):
        self.req = ServiceRequest.objects.create(raw_text="AC not cooling in Kothrud", workflow_thread_id="flow-1")
        self.graph = orchestration.build_workflow(MemorySaver())
        self.config = {"configurable": {"thread_id": "flow-1"}}
        self.start = {"request_id": self.req.id, "raw_text": self.req.raw_text, "requirements": KOTHRUD,
                      "excluded_provider_ids": [], "retry_count": 0}

    def test_pauses_with_ranked_providers_then_approve_selects_one(self):
        out = self.graph.invoke(self.start, config=self.config)
        self.assertIn("__interrupt__", out)
        approval = Approval.objects.get(request=self.req)
        ranked = approval.payload["ranked_providers"]
        self.assertEqual(len(ranked), 5)
        self.assertIn("score", ranked[0])

        from langgraph.types import Command
        out = self.graph.invoke(Command(resume={"action": "approve", "provider_id": ranked[1]["place_id"]}), config=self.config)
        self.assertEqual(out["selected_provider_id"], ranked[1]["place_id"])

    def test_search_again_shows_five_new_providers(self):
        self.graph.invoke(self.start, config=self.config)
        first = {p["place_id"] for p in Approval.objects.get(request=self.req).payload["ranked_providers"]}

        from langgraph.types import Command
        self.graph.invoke(Command(resume={"action": "search_again"}), config=self.config)
        second = {p["place_id"] for p in Approval.objects.get(request=self.req).payload["ranked_providers"]}
        self.assertEqual(len(second), 5)
        self.assertFalse(first & second)


class ComparisonPageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("ria", password="safe-password-123")
        self.req = ServiceRequest.objects.create(user=self.user, raw_text="AC not cooling", workflow_thread_id="t-page")
        self.url = reverse("compare_providers", args=[self.req.id])

    def add_approval(self, decision=Approval.Decision.PENDING):
        ranked = rank_providers(ac_providers(), KOTHRUD).ranked
        return Approval.objects.create(request=self.req, thread_id="t-page", decision=decision,
                                       payload={"ranked_providers": ranked}), ranked

    def test_requires_login(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_other_users_get_404(self):
        User.objects.create_user("other", password="safe-password-123")
        self.client.login(username="other", password="safe-password-123")
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_shows_ranking_estimates_and_link_to_approvals(self):
        _, ranked = self.add_approval()
        self.client.force_login(self.user)
        html = self.client.get(self.url).content.decode()
        for p in ranked:
            self.assertIn(p["name"], html)
        self.assertIn("Estimate", html)
        self.assertIn(reverse("approvals"), html)
        self.assertIn("Rating quality", html)

    def test_still_searching_refreshes(self):
        self.client.force_login(self.user)
        html = self.client.get(self.url).content.decode()
        self.assertIn("Still finding", html)
        self.assertIn('http-equiv="refresh"', html)

    def test_decided_request_shows_decision(self):
        self.add_approval(Approval.Decision.APPROVED)
        self.client.force_login(self.user)
        self.assertIn("You already decided on this request: Approved", self.client.get(self.url).content.decode())

    def test_approvals_page_links_here(self):
        self.add_approval()
        self.client.force_login(self.user)
        self.assertIn(self.url, self.client.get(reverse("approvals")).content.decode())

    def test_reason_shown_only_when_llm_written(self):
        approval, ranked = self.add_approval()
        self.client.force_login(self.user)
        self.assertNotIn(ranked[0]["reason"], self.client.get(self.url).content.decode())
        ranked[0].update(reason="Best balance of rating and distance.", reason_source="llm")
        approval.payload = {"ranked_providers": ranked}
        approval.save()
        self.assertIn("Best balance of rating and distance.", self.client.get(self.url).content.decode())
