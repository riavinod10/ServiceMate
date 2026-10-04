"""Redesigned pages: Login/Register, Approvals, Comparison. Behaviour must be unchanged."""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Approval, ServiceRequest
from .providers.categories import CATEGORIES
from .providers.fixtures import load_fixture
from .providers.normalize import normalize_results
from .providers.scoring import rank_providers

KOTHRUD = {"category": "ac_repair", "locality": "Kothrud", "budget": 1500}


def ranked_ac():
    providers = normalize_results(load_fixture(CATEGORIES["ac_repair"]), CATEGORIES["ac_repair"]).providers
    return rank_providers(providers, KOTHRUD).ranked


class AuthPageTests(TestCase):
    def test_login_page_is_branded_and_keeps_django_fields(self):
        html = self.client.get(reverse("login")).content.decode()
        self.assertIn("servicemate-logo-white.png", html)
        self.assertIn('name="username"', html)
        self.assertIn('name="password"', html)
        self.assertIn(reverse("register"), html)

    def test_login_keeps_next_redirect(self):
        User.objects.create_user("ria", password="safe-password-123")
        url = reverse("compare_providers", args=[1])
        html = self.client.get(reverse("login") + f"?next={url}").content.decode()
        self.assertIn(f'name="next" value="{url}"', html)
        response = self.client.post(reverse("login"), {"username": "ria", "password": "safe-password-123", "next": url})
        self.assertRedirects(response, url, fetch_redirect_response=False)

    def test_login_error_is_shown(self):
        html = self.client.post(reverse("login"), {"username": "x", "password": "y"}).content.decode()
        self.assertIn("sm-alert", html)

    def test_register_page_and_errors(self):
        html = self.client.get(reverse("register")).content.decode()
        for name in ("username", "password1", "password2"):
            self.assertIn(f'name="{name}"', html)
        self.assertIn(reverse("login"), html)
        html = self.client.post(reverse("register"), {"username": "a", "password1": "x1", "password2": "x2"}).content.decode()
        self.assertIn("sm-error", html)

    def test_logout_button_is_a_post_form(self):
        User.objects.create_user("ria", password="safe-password-123")
        self.client.login(username="ria", password="safe-password-123")
        html = self.client.get(reverse("approvals")).content.decode()
        self.assertIn(f'<form method="post" action="{reverse("logout")}">', html)


class ApprovalsPageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("ria", password="safe-password-123")
        self.req = ServiceRequest.objects.create(user=self.user, raw_text="AC not cooling", workflow_thread_id="t-ui")
        self.ranked = ranked_ac()
        self.approval = Approval.objects.create(request=self.req, thread_id="t-ui", payload={"ranked_providers": self.ranked})
        self.client.force_login(self.user)

    def test_lists_providers_with_scores_and_actions(self):
        html = self.client.get(reverse("approvals")).content.decode()
        for p in self.ranked:
            self.assertIn(f'value="{p["place_id"]}"', html)
        self.assertIn("Top pick", html)
        self.assertIn('value="approve"', html)

    def test_reject_and_search_again_do_not_require_a_selection(self):
        html = self.client.get(reverse("approvals")).content.decode()
        self.assertIn('value="search_again" formnovalidate', html)
        self.assertIn('value="reject" formnovalidate', html)

    @patch("core.views.run_workflow.delay")
    def test_actions_show_a_confirmation(self, delay):
        url = reverse("approval_action", args=[self.approval.id])
        response = self.client.post(url, {"action": "approve", "provider_id": self.ranked[0]["place_id"]}, follow=True)
        self.assertContains(response, f"Approved {self.ranked[0]['name']}")
        delay.assert_called_once()  # workflow resume unchanged

    def test_empty_state(self):
        Approval.objects.all().delete()
        self.assertContains(self.client.get(reverse("approvals")), "Nothing waiting for you")


class ComparisonPageUiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("ria", password="safe-password-123")
        self.req = ServiceRequest.objects.create(user=self.user, raw_text="AC not cooling", workflow_thread_id="t-cmp")
        self.ranked = ranked_ac()
        self.approval = Approval.objects.create(request=self.req, thread_id="t-cmp", payload={"ranked_providers": self.ranked})
        self.client.force_login(self.user)
        self.url = reverse("compare_providers", args=[self.req.id])

    def test_top_pick_and_choose_buttons_use_existing_endpoint(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn("Top pick for you", html)
        self.assertEqual(html.count("Choose this provider"), 5)
        self.assertIn(reverse("approval_action", args=[self.approval.id]), html)
        for p in self.ranked:
            self.assertIn(f'name="provider_id" value="{p["place_id"]}"', html)

    @patch("core.views.run_workflow.delay")
    def test_choosing_from_comparison_resumes_with_that_provider(self, delay):
        chosen = self.ranked[2]["place_id"]
        self.client.post(reverse("approval_action", args=[self.approval.id]), {"action": "approve", "provider_id": chosen})
        resume = delay.call_args.args[2]["resume"]
        self.assertEqual(resume, {"action": "approve", "provider_id": chosen})

    def test_no_choose_buttons_after_a_decision(self):
        self.approval.decision = Approval.Decision.REJECTED
        self.approval.save()
        html = self.client.get(self.url).content.decode()
        self.assertNotIn("Choose this provider", html)
        self.assertIn("You already decided on this request: Rejected", html)

    def test_short_distances_read_naturally(self):
        self.ranked[0]["distance_km"] = 0.0
        self.approval.payload = {"ranked_providers": self.ranked}
        self.approval.save()
        html = self.client.get(self.url).content.decode()
        self.assertIn("Under 0.5 km in a straight line", html)
        self.assertNotIn("0.0 km", html)
