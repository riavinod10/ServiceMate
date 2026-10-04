"""Gemini explanation tests. Gemini is always faked here: no test makes a network call."""
import copy
import os
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from . import gemini
from .agents.analysis import analysis_agent
from .gemini import GeminiUnavailable, generate_structured, get_client
from .models import AgentLog, Approval, ServiceRequest
from .providers.categories import CATEGORIES
from .providers.explain import (
    MAX_REASON_CHARS, ProviderReason, RankingExplanations, build_prompt, explain_ranking,
)
from .providers.fixtures import load_fixture
from .providers.normalize import normalize_results
from .providers.scoring import rank_providers

KOTHRUD = {"category": "ac_repair", "locality": "Kothrud", "city": "Pune", "budget": 1500}
RAW_TEXT = "AC not cooling, need someone tomorrow evening under ₹1,500"
GENERATE = "core.providers.explain.generate_structured"


def ranked_ac():
    providers = normalize_results(load_fixture(CATEGORIES["ac_repair"]), CATEGORIES["ac_repair"]).providers
    return rank_providers(providers, KOTHRUD).ranked


def explanations_for(ranked, text="Good match, {name}."):
    return RankingExplanations(reasons=[ProviderReason(place_id=p["place_id"], reason=text.format(name=p["name"]))
                                        for p in ranked])


class FakeModels:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def fake_client(**kwargs):
    return SimpleNamespace(models=FakeModels(**kwargs))


class GeminiModuleTests(SimpleTestCase):
    @patch.dict(os.environ, {"GEMINI_API_KEY": ""})
    def test_missing_key_is_unavailable(self):
        with self.assertRaises(GeminiUnavailable):
            get_client()

    @patch.dict(os.environ, {"GEMINI_API_KEY": "your_key_here"})
    def test_placeholder_key_is_unavailable(self):
        with self.assertRaises(GeminiUnavailable):
            get_client()

    @patch.dict(os.environ, {"GEMINI_API_KEY": "real-key"})
    def test_client_has_short_timeout_and_few_retries(self):
        with patch("google.genai.Client") as client_cls:
            get_client(timeout_seconds=20)
        options = client_cls.call_args.kwargs["http_options"]
        self.assertEqual(client_cls.call_args.kwargs["api_key"], "real-key")
        self.assertEqual(options.timeout, 20000)           # milliseconds
        self.assertEqual(options.retry_options.attempts, 2)  # not the SDK default of 5

    def test_model_name_from_env_with_team_default(self):
        with patch.dict(os.environ, {"GEMINI_MODEL": ""}):
            self.assertEqual(gemini.gemini_model(), "gemini-3.6-flash")
        with patch.dict(os.environ, {"GEMINI_MODEL": "gemini-other"}):
            self.assertEqual(gemini.gemini_model(), "gemini-other")

    def test_structured_call_config(self):
        expected = RankingExplanations(reasons=[])
        client = fake_client(response=SimpleNamespace(parsed=expected, text=None))
        with patch("core.gemini.get_client", return_value=client), patch.dict(os.environ, {"GEMINI_MODEL": ""}):
            self.assertIs(generate_structured("prompt", RankingExplanations, "system"), expected)
        call = client.models.calls[0]
        self.assertEqual(call["model"], "gemini-3.6-flash")
        self.assertEqual(call["config"].response_mime_type, "application/json")
        self.assertIs(call["config"].response_schema, RankingExplanations)
        self.assertEqual(call["config"].system_instruction, "system")
        self.assertIsNone(call["config"].temperature)        # ignored by this model
        self.assertIsNone(call["config"].frequency_penalty)  # would raise on this model

    def test_text_json_is_parsed_when_parsed_is_missing(self):
        client = fake_client(response=SimpleNamespace(parsed=None, text='{"reasons": [{"place_id": "a", "reason": "Fine."}]}'))
        with patch("core.gemini.get_client", return_value=client):
            result = generate_structured("p", RankingExplanations, "s")
        self.assertEqual(result.reasons[0].place_id, "a")

    def test_bad_output_and_errors_become_unavailable(self):
        cases = [
            fake_client(response=SimpleNamespace(parsed=None, text="not json")),
            fake_client(response=SimpleNamespace(parsed=None, text="")),
            fake_client(error=TimeoutError("timed out")),
            fake_client(error=RuntimeError("404 model not found")),
        ]
        for client in cases:
            with self.subTest(client=client), patch("core.gemini.get_client", return_value=client):
                with self.assertRaises(GeminiUnavailable):
                    generate_structured("p", RankingExplanations, "s")


class ExplainRankingTests(SimpleTestCase):
    def test_success_only_changes_reasons(self):
        ranked = ranked_ac()
        before = copy.deepcopy(ranked)
        with patch(GENERATE, return_value=explanations_for(ranked)):
            result, used_llm = explain_ranking(ranked, KOTHRUD, RAW_TEXT)
        self.assertTrue(used_llm)
        self.assertEqual([p["place_id"] for p in result], [p["place_id"] for p in before])  # same order
        for new, old in zip(result, before):
            self.assertEqual(new["reason"], f"Good match, {old['name']}.")
            self.assertEqual(new["reason_source"], "llm")
            self.assertEqual({k: v for k, v in new.items() if k not in ("reason", "reason_source")},
                             {k: v for k, v in old.items() if k not in ("reason", "reason_source")})
        self.assertEqual(ranked, before)  # input list not mutated

    def test_out_of_order_and_extra_items_still_match_by_place_id(self):
        ranked = ranked_ac()
        reply = explanations_for(list(reversed(ranked)))
        reply.reasons.append(ProviderReason(place_id="not-in-ranking", reason="Ignore me."))
        with patch(GENERATE, return_value=reply):
            result, used_llm = explain_ranking(ranked, KOTHRUD, RAW_TEXT)
        self.assertTrue(used_llm)
        self.assertEqual([p["place_id"] for p in result], [p["place_id"] for p in ranked])
        self.assertEqual(result[0]["reason"], f"Good match, {ranked[0]['name']}.")

    def test_incomplete_or_empty_reasons_fall_back_to_templates(self):
        ranked = ranked_ac()
        short = explanations_for(ranked[:4])
        blank = explanations_for(ranked, text="   ")
        for reply in (short, blank):
            with self.subTest(reply=reply), patch(GENERATE, return_value=reply):
                result, used_llm = explain_ranking(ranked, KOTHRUD, RAW_TEXT)
            self.assertFalse(used_llm)
            self.assertEqual({p["reason_source"] for p in result}, {"template"})

    def test_failures_fall_back_and_never_raise(self):
        ranked = ranked_ac()
        with patch(GENERATE, side_effect=GeminiUnavailable("GEMINI_API_KEY is not set")):
            self.assertEqual(explain_ranking(ranked, KOTHRUD, RAW_TEXT), (ranked, False))
        with patch(GENERATE, side_effect=ValueError("unexpected bug")), \
                self.assertLogs("core.providers.explain", level="ERROR"):  # logged, not raised
            self.assertEqual(explain_ranking(ranked, KOTHRUD, RAW_TEXT), (ranked, False))

    @patch.dict(os.environ, {"GEMINI_API_KEY": ""})
    def test_no_key_falls_back_without_any_call(self):
        ranked = ranked_ac()
        with patch("google.genai.Client") as client_cls:
            result, used_llm = explain_ranking(ranked, KOTHRUD, RAW_TEXT)
        client_cls.assert_not_called()
        self.assertFalse(used_llm)

    def test_long_reasons_are_trimmed(self):
        ranked = ranked_ac()
        with patch(GENERATE, return_value=explanations_for(ranked, text="word " * 100)):
            result, _ = explain_ranking(ranked, KOTHRUD, RAW_TEXT)
        self.assertTrue(all(len(p["reason"]) <= MAX_REASON_CHARS + 1 for p in result))
        self.assertTrue(result[0]["reason"].endswith("…"))

    def test_empty_ranking_makes_no_call(self):
        with patch(GENERATE) as generate:
            self.assertEqual(explain_ranking([], KOTHRUD, RAW_TEXT), ([], False))
        generate.assert_not_called()

    def test_prompt_contains_ranking_facts_but_no_contact_details(self):
        ranked = ranked_ac()
        prompt = build_prompt(ranked, KOTHRUD, RAW_TEXT)
        self.assertIn(RAW_TEXT, prompt)
        for p in ranked:
            self.assertIn(p["place_id"], prompt)
            self.assertIn(p["name"], prompt)
            self.assertNotIn(p["phone"], prompt)
            if p["website"]:
                self.assertNotIn(p["website"], prompt)
        self.assertNotIn('"lat"', prompt)


class AnalysisAgentLlmTests(TestCase):
    def setUp(self):
        self.req = ServiceRequest.objects.create(raw_text=RAW_TEXT, workflow_thread_id="t-llm")
        providers = normalize_results(load_fixture(CATEGORIES["ac_repair"]), CATEGORIES["ac_repair"]).providers
        self.state = {"request_id": self.req.id, "raw_text": RAW_TEXT, "providers": providers, "requirements": KOTHRUD}

    def logs(self):
        return list(AgentLog.objects.filter(request=self.req).values_list("message", flat=True))

    def test_logs_llm_success(self):
        with patch(GENERATE, side_effect=lambda prompt, schema, system: explanations_for(ranked_ac())):
            out = analysis_agent(self.state)
        self.assertIn("Explanations written by the LLM", self.logs())
        self.assertEqual({p["reason_source"] for p in out["ranked_providers"]}, {"llm"})

    @patch.dict(os.environ, {"GEMINI_API_KEY": ""})
    def test_logs_fallback_and_keeps_ranking(self):
        out = analysis_agent(self.state)
        self.assertIn("LLM unavailable, used standard explanations", self.logs())
        self.assertEqual([p["place_id"] for p in out["ranked_providers"]], [p["place_id"] for p in ranked_ac()])

    def test_llm_reasons_reach_the_comparison_page(self):
        user = User.objects.create_user("ria", password="safe-password-123")
        self.req.user = user
        self.req.save()
        with patch(GENERATE, side_effect=lambda prompt, schema, system: explanations_for(ranked_ac(), "Why: {name}.")):
            out = analysis_agent(self.state)
        Approval.objects.create(request=self.req, thread_id="t-llm", payload={"ranked_providers": out["ranked_providers"]})
        self.client.force_login(user)
        html = self.client.get(reverse("compare_providers", args=[self.req.id])).content.decode()
        self.assertIn(f"Why: {out['ranked_providers'][0]['name']}.", html)
