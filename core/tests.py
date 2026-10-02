from unittest.mock import patch
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from .logging import log_agent_event
from .models import Approval, Provider, ServiceRequest
from .orchestration import recovery_agent, route_after_approval, route_recovery

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
        log = log_agent_event(self.request_obj.id, "orchestrator", "queued")
        self.assertEqual(log.request, self.request_obj)

    @patch("core.views.run_workflow.delay")
    def test_approval_uses_same_thread_and_post_only(self, delay):
        approval = Approval.objects.create(request=self.request_obj, thread_id="thread-1")
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("approval_action", args=[approval.id])).status_code, 405)
        response = self.client.post(reverse("approval_action", args=[approval.id]), {"action": "approve"})
        self.assertRedirects(response, reverse("approvals"))
        delay.assert_called_once_with(self.request_obj.id, "thread-1", {"request_id": self.request_obj.id, "status": "approve"})
        approval.refresh_from_db()
        self.assertEqual(approval.decision, Approval.Decision.APPROVED)

    @patch("core.views.run_workflow.delay")
    def test_reject_and_invalid_action_do_not_book(self, delay):
        approval = Approval.objects.create(request=self.request_obj, thread_id="thread-1")
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(reverse("approval_action", args=[approval.id]), {"action": "not-real"}).status_code, 400)
        self.client.post(reverse("approval_action", args=[approval.id]), {"action": "reject"})
        self.assertEqual(delay.call_args.args[2]["status"], "reject")
        self.assertEqual(route_after_approval({"status": "rejected"}), "end")

    def test_other_users_cannot_resume_a_workflow_thread(self):
        other = User.objects.create_user("other", password="safe-password-123")
        approval = Approval.objects.create(request=self.request_obj, thread_id="thread-1")
        self.client.force_login(other)
        self.assertEqual(self.client.post(reverse("approval_action", args=[approval.id]), {"action": "approve"}).status_code, 404)

    def test_search_again_and_bounded_cancellation_recovery(self):
        self.assertEqual(route_after_approval({"status": "search_again"}), "discover")
        self.assertEqual(route_recovery({"status": "cancelled", "retry_count": 0}), "recover")
        self.assertEqual(route_recovery({"status": "cancelled", "retry_count": 2}), "end")
        self.assertEqual(recovery_agent({"selected_provider_id": "place-1", "excluded_provider_ids": [], "retry_count": 0})["excluded_provider_ids"], ["place-1"])
