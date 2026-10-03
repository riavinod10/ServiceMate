from unittest.mock import patch
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from .logging import log_agent_event
from .models import Approval, Provider, ProviderSearchCache, ServiceRequest
from .orchestration import approval_gate, create_pending_approval, recovery_agent, route_after_approval, route_recovery
from .tasks import run_workflow

class PersonOneTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("sam", password="safe-password-123")
        self.request_obj = ServiceRequest.objects.create(user=self.user, raw_text="AC repair", workflow_thread_id="thread-1")

    def test_registration_login_and_protected_approval_page(self):
        self.assertRedirects(self.client.get(reverse("home")), reverse("approvals"), fetch_redirect_response=False)
        self.assertEqual(self.client.get(reverse("approvals")).status_code, 302)
        response = self.client.post(reverse("register"), {"username": "new", "password1": "strong-pass-123", "password2": "strong-pass-123"})
        self.assertRedirects(response, reverse("approvals"))
        self.client.logout()
        self.assertTrue(self.client.login(username="new", password="strong-pass-123"))

    def test_models_and_agent_log(self):
        provider = Provider.objects.create(place_id="place-1", name="Example", source="fixture")
        self.assertEqual(provider.price_is_estimate, True)
        cache = ProviderSearchCache.objects.create(query="ac repair kothrud")
        cache.providers.add(provider)
        self.assertEqual(cache.providers.get(), provider)
        log = log_agent_event(self.request_obj.id, "orchestrator", "queued")
        self.assertEqual(log.request, self.request_obj)

    @patch("core.views.run_workflow.delay")
    def test_approval_uses_same_thread_and_post_only(self, delay):
        approval = Approval.objects.create(request=self.request_obj, thread_id="thread-1", payload={"ranked_providers": [{"place_id": "place-1", "name": "Example"}]})
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("approval_action", args=[approval.id])).status_code, 405)
        response = self.client.post(reverse("approval_action", args=[approval.id]), {"action": "approve", "provider_id": "place-1"})
        self.assertRedirects(response, reverse("approvals"))
        delay.assert_called_once_with(self.request_obj.id, "thread-1", {"request_id": self.request_obj.id, "resume": {"action": "approve", "provider_id": "place-1"}})
        approval.refresh_from_db()
        self.assertEqual(approval.decision, Approval.Decision.APPROVED)

    @patch("core.orchestration.interrupt")
    def test_gate_creates_approval_and_sets_selected_provider(self, interrupt_mock):
        state = {"request_id": self.request_obj.id, "ranked_providers": [{"place_id": "place-1", "name": "Example"}]}
        create_pending_approval(state, {"configurable": {"thread_id": "thread-1"}})
        approval = Approval.objects.get(request=self.request_obj, thread_id="thread-1")
        self.assertEqual(approval.decision, Approval.Decision.PENDING)
        interrupt_mock.return_value = {"action": "approve", "provider_id": "place-1"}
        self.assertEqual(approval_gate(state), {"status": "approved", "selected_provider_id": "place-1"})

    @patch("core.views.run_workflow.delay")
    def test_reject_and_invalid_action_do_not_book(self, delay):
        approval = Approval.objects.create(request=self.request_obj, thread_id="thread-1")
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(reverse("approval_action", args=[approval.id]), {"action": "not-real"}).status_code, 400)
        self.client.post(reverse("approval_action", args=[approval.id]), {"action": "reject"})
        self.assertEqual(delay.call_args.args[2]["resume"], {"action": "reject"})
        self.assertEqual(route_after_approval({"status": "rejected"}), "end")

    def test_other_users_cannot_resume_a_workflow_thread(self):
        other = User.objects.create_user("other", password="safe-password-123")
        approval = Approval.objects.create(request=self.request_obj, thread_id="thread-1")
        self.client.force_login(other)
        self.assertEqual(self.client.post(reverse("approval_action", args=[approval.id]), {"action": "approve"}).status_code, 404)

    def test_search_again_and_bounded_cancellation_recovery(self):
        self.assertEqual(route_after_approval({"status": "search_again", "retry_count": 1}), "discover")
        self.assertEqual(route_after_approval({"status": "search_again", "retry_count": 3}), "end")
        self.assertEqual(route_recovery({"status": "cancelled", "retry_count": 0}), "recover")
        self.assertEqual(route_recovery({"status": "cancelled", "retry_count": 2}), "end")
        self.assertEqual(recovery_agent({"selected_provider_id": "place-1", "excluded_provider_ids": [], "retry_count": 0})["excluded_provider_ids"], ["place-1"])

    @patch("core.orchestration.interrupt", return_value={"action": "search_again"})
    def test_search_again_excludes_all_shown_providers(self, _interrupt):
        result = approval_gate({"request_id": self.request_obj.id, "ranked_providers": [{"place_id": "place-1"}, {"place_id": "place-2"}], "excluded_provider_ids": ["earlier"], "retry_count": 0})
        self.assertEqual(result, {"status": "search_again", "excluded_provider_ids": ["earlier", "place-1", "place-2"], "retry_count": 1})

    def test_task_rejects_missing_or_mismatched_workflow_identity(self):
        with self.assertRaisesMessage(ValueError, "Unknown service request"):
            run_workflow(self.request_obj.id + 999, "thread-1", {"request_id": self.request_obj.id + 999})
        with self.assertRaisesMessage(ValueError, "Workflow request and thread identity do not match"):
            run_workflow(self.request_obj.id, "wrong-thread", {"request_id": self.request_obj.id})
        with self.assertRaisesMessage(ValueError, "Workflow request and thread identity do not match"):
            run_workflow(self.request_obj.id, "thread-1", {"request_id": self.request_obj.id + 1})
