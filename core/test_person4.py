"""Person 4: dashboard, follow-up, cancellation reopen, and analytics."""
import os
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from langgraph.types import Command

from .agents.analytics import MINUTES_SAVED_PER_REQUEST, build_analytics
from .agents.followup import mark_overdue_bookings, send_due_reminders
from .agents.scheduling_agent import scheduling_agent
from .gemini import GeminiUnavailable
from .models import AgentLog, Approval, Booking, ServiceRequest
from .orchestration import build_workflow
from .tasks_followup import reopen_workflow_after_cancellation

try:
    from langgraph.checkpoint.memory import InMemorySaver as MemorySaver
except ImportError:
    from langgraph.checkpoint.memory import MemorySaver

KOTHRUD = {"category": "ac_repair", "locality": "Kothrud", "city": "Pune", "budget": 1500}


class _Saver:
    def __init__(self, saver):
        self.saver = saver

    def __enter__(self):
        return self.saver

    def __exit__(self, exc_type, exc, tb):
        return False


class PageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("shubh", password="safe-password-123")
        self.client.force_login(self.user)

    def test_home_and_login_land_on_the_dashboard(self):
        self.client.logout()
        self.assertRedirects(self.client.get(reverse("home")), reverse("dashboard"), fetch_redirect_response=False)
        self.assertRedirects(self.client.get(reverse("dashboard")), f"{reverse('login')}?next={reverse('dashboard')}")
        self.client.post(reverse("login"), {"username": "shubh", "password": "safe-password-123"})
        self.assertRedirects(
            self.client.post(reverse("login"), {"username": "shubh", "password": "safe-password-123"}),
            reverse("dashboard"),
        )

    def test_navigation_marks_the_current_page_and_keeps_the_profile_menu(self):
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn('<details class="sm-profile" data-sm-menu>', html)
        self.assertIn("Signed in as<strong>shubh</strong>", html)
        self.assertIn('<form method="post" action="/logout/">', html)
        self.assertIn('aria-disabled="true">Settings', html)
        self.assertIn('class="sm-footer"', html)
        self.assertIn('class="sm-profile__nav"', html)
        self.assertIn('href="/dashboard/" aria-current="page"', html)
        self.assertIn('href="/my-services/"', html)
        self.assertIn('href="/analytics/"', html)
        self.assertIn('hx-trigger="load, every 5s"', html)
        approvals = self.client.get(reverse("approvals")).content.decode()
        self.assertIn('href="/approvals/" aria-current="page"', approvals)
        self.assertIn('href="/dashboard/"', approvals)
        self.assertIn("Start request", html)
        self.assertIn('action="/requests/new/"', html)

    @patch("core.views_workspace.run_workflow.delay")
    def test_new_request_starts_the_workflow(self, delay):
        response = self.client.post(reverse("new_request"), {
            "raw_text": "AC not cooling, need someone tomorrow evening",
            "locality": "Kothrud",
            "city": "Pune",
            "budget": "1500",
        })
        self.assertRedirects(response, reverse("dashboard"))
        service_request = ServiceRequest.objects.get(user=self.user)
        self.assertEqual(service_request.requirements["locality"], "Kothrud")
        self.assertEqual(service_request.requirements["city"], "Pune")
        self.assertEqual(service_request.requirements["budget"], 1500)
        delay.assert_called_once()
        request_id, thread_id, state = delay.call_args.args
        self.assertEqual(request_id, service_request.id)
        self.assertEqual(thread_id, service_request.workflow_thread_id)
        self.assertEqual(state["raw_text"], service_request.raw_text)
        self.assertNotIn("resume", state)

    def test_blank_request_does_not_start_a_workflow(self):
        response = self.client.post(reverse("new_request"), {"raw_text": "   "})
        self.assertRedirects(response, reverse("dashboard"))
        self.assertFalse(ServiceRequest.objects.filter(user=self.user).exists())

    def test_comparison_does_not_grow_an_agent_log_panel(self):
        req = ServiceRequest.objects.create(user=self.user, raw_text="AC not cooling", workflow_thread_id="cmp-p4")
        Approval.objects.create(request=req, thread_id="cmp-p4", payload={"ranked_providers": []})
        html = self.client.get(reverse("compare_providers", args=[req.id])).content.decode()
        self.assertNotIn("hx-trigger", html)
        self.assertNotIn("Agent activity", html)
        self.assertIn('class="sm-back"', html)

    def test_agent_log_fragment_is_only_this_users_events(self):
        mine = ServiceRequest.objects.create(user=self.user, raw_text="Fix the AC", workflow_thread_id="log-1")
        other = User.objects.create_user("other", password="safe-password-123")
        theirs = ServiceRequest.objects.create(user=other, raw_text="Secret tap", workflow_thread_id="log-2")
        AgentLog.objects.create(request=mine, agent_name="Follow-Up Agent", message="Reminder for you")
        AgentLog.objects.create(request=theirs, agent_name="Follow-Up Agent", message="Hidden from you")
        html = self.client.get(reverse("agent_log")).content.decode()
        self.assertIn("Reminder for you", html)
        self.assertNotIn("Hidden from you", html)
        self.assertNotIn("sm-nav", html)


@override_settings(CELERY_TASK_ALWAYS_EAGER=False)
class CancellationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("shubh", password="safe-password-123")
        self.req = ServiceRequest.objects.create(
            user=self.user, raw_text="AC not cooling", workflow_thread_id="cancel-ui",
            requirements=KOTHRUD,
        )
        self.approval = Approval.objects.create(
            request=self.req, thread_id="cancel-ui", decision=Approval.Decision.APPROVED,
            payload={"ranked_providers": [{"place_id": "p-1", "name": "Cool Air"}]},
        )
        self.client.force_login(self.user)

    @patch("core.views_workspace.reopen_workflow_after_cancellation.delay")
    def test_button_marks_the_booking_cancelled_and_queues_reopen(self, delay):
        response = self.client.post(reverse("simulate_cancellation", args=[self.req.id]))
        self.assertRedirects(response, reverse("my_services"))
        booking = Booking.objects.get(request=self.req)
        self.assertEqual(booking.status, "cancelled")
        delay.assert_called_once_with(self.req.id)
        html = self.client.get(reverse("my_services")).content.decode()
        self.assertNotIn("Simulate provider cancellation", html)
        self.assertIn("Provider cancelled", html)

    def test_pending_or_exhausted_requests_cannot_be_cancelled(self):
        self.approval.decision = Approval.Decision.PENDING
        self.approval.save(update_fields=["decision"])
        self.assertEqual(self.client.post(reverse("simulate_cancellation", args=[self.req.id])).status_code, 400)
        self.approval.decision = Approval.Decision.APPROVED
        self.approval.save(update_fields=["decision"])
        self.req.retry_count = 2
        self.req.save(update_fields=["retry_count"])
        self.assertEqual(self.client.post(reverse("simulate_cancellation", args=[self.req.id])).status_code, 400)

    def test_another_user_cannot_cancel(self):
        other = User.objects.create_user("other", password="safe-password-123")
        self.client.force_login(other)
        self.assertEqual(self.client.post(reverse("simulate_cancellation", args=[self.req.id])).status_code, 404)

    @patch.dict(os.environ, {"APIFY_API_TOKEN": "", "GEMINI_API_KEY": ""})
    def test_task_reopens_discovery_and_drops_the_cancelled_provider(self):
        memory = MemorySaver()
        graph = build_workflow(memory)
        config = {"configurable": {"thread_id": self.req.workflow_thread_id}}
        graph.invoke({
            "request_id": self.req.id, "raw_text": self.req.raw_text, "requirements": KOTHRUD,
            "excluded_provider_ids": [], "retry_count": 0,
        }, config=config)
        chosen = Approval.objects.get(request=self.req).payload["ranked_providers"][0]["place_id"]
        graph.invoke(Command(resume={"action": "approve", "provider_id": chosen}), config=config)
        Approval.objects.filter(request=self.req).update(decision=Approval.Decision.APPROVED)
        self.assertEqual(graph.get_state(config).next, ())

        with patch("core.tasks_followup.get_checkpointer", return_value=_Saver(memory)):
            result = reopen_workflow_after_cancellation(self.req.id)

        self.req.refresh_from_db()
        state = build_workflow(memory).get_state(config)
        self.assertEqual(result["reopened"], True)
        self.assertEqual(state.next, ("approval",))
        self.assertEqual(state.values["excluded_provider_ids"], [chosen])
        self.assertEqual(self.req.retry_count, 1)
        self.assertNotIn(chosen, [p["place_id"] for p in Approval.objects.get(request=self.req).payload["ranked_providers"]])
        self.assertEqual(Approval.objects.get(request=self.req).decision, Approval.Decision.PENDING)


class FollowUpTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("shubh", password="safe-password-123")
        self.req = ServiceRequest.objects.create(user=self.user, raw_text="AC not cooling", workflow_thread_id="fu-1")

    def _booking(self, when, status="requested"):
        return Booking.objects.create(
            request=self.req, status=status,
            details={"date": when.date().isoformat(), "time": when.strftime("%H:%M"), "provider_name": "Cool Air"},
        )

    def test_reminder_is_sent_once_inside_the_day_window(self):
        soon = timezone.now() + timedelta(hours=2)
        self._booking(soon)
        self.assertEqual(send_due_reminders(), 1)
        self.assertEqual(send_due_reminders(), 0)
        self.assertEqual(AgentLog.objects.filter(request=self.req, message__startswith="Reminder:").count(), 1)

    def test_far_appointments_are_not_reminded_yet(self):
        self._booking(timezone.now() + timedelta(days=3))
        self.assertEqual(send_due_reminders(), 0)
        self.assertFalse(AgentLog.objects.exists())

    def test_overdue_bookings_are_marked_and_cancelled_ones_are_left_alone(self):
        self._booking(timezone.now() - timedelta(days=1))
        self.assertEqual(mark_overdue_bookings(), 1)
        self.assertEqual(Booking.objects.get(request=self.req).status, "overdue")
        self.assertEqual(mark_overdue_bookings(), 0)
        Booking.objects.filter(request=self.req).update(status="cancelled")
        self.req.retry_count = 0
        later = ServiceRequest.objects.create(user=self.user, raw_text="Tap", workflow_thread_id="fu-2")
        Booking.objects.create(
            request=later, status="cancelled",
            details={"date": (timezone.now() - timedelta(days=2)).date().isoformat(), "time": "09:00"},
        )
        self.assertEqual(mark_overdue_bookings(), 0)


class AnalyticsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("shubh", password="safe-password-123")
        self.ac = ServiceRequest.objects.create(
            user=self.user, raw_text="AC not cooling", workflow_thread_id="an-1",
            requirements={"category": "ac_repair", "budget": 1500},
        )
        self.plumber = ServiceRequest.objects.create(
            user=self.user, raw_text="Leaking tap", workflow_thread_id="an-2",
            requirements={"category": "plumber"},
        )
        Booking.objects.create(request=self.plumber, status="requested", details={"provider_name": "Pipe Co"})
        cancelled = ServiceRequest.objects.create(
            user=self.user, raw_text="Deep clean", workflow_thread_id="an-3",
            requirements={"category": "home_cleaning", "budget": 4000},
        )
        Booking.objects.create(request=cancelled, status="cancelled")

    def test_totals_use_budget_then_category_midpoint_and_skip_cancelled_spend(self):
        summary = build_analytics(self.user, with_insight=False)
        self.assertEqual(summary["request_count"], 3)
        self.assertEqual(summary["completed_count"], 1)
        self.assertEqual(summary["cancelled_count"], 1)
        self.assertEqual(summary["estimated_spend_inr"], 1500 + (300 + 1000) // 2)
        self.assertEqual(summary["minutes_saved"], 3 * MINUTES_SAVED_PER_REQUEST)
        self.assertEqual(summary["categories"][0]["count"], 1)
        self.assertEqual({row["label"] for row in summary["categories"]}, {"AC repair", "Plumber", "Home cleaning"})

    @patch("core.agents.analytics.generate_structured")
    def test_page_shows_the_model_paragraph_and_the_time_assumption(self, generate):
        generate.return_value.paragraph = "You mostly book cooling help and the estimate stays modest."
        self.client.force_login(self.user)
        html = self.client.get(reverse("analytics")).content.decode()
        self.assertIn("You mostly book cooling help", html)
        self.assertTrue(AgentLog.objects.filter(request__user=self.user, agent_name="Analytics Agent").exists())
        self.assertIn("45 minutes", html)
        self.assertIn("AC repair", html)
        self.assertIn("₹2150", html)

    @patch("core.agents.analytics.generate_structured", side_effect=GeminiUnavailable("no key"))
    def test_page_still_explains_the_numbers_without_gemini(self, _generate):
        self.client.force_login(self.user)
        html = self.client.get(reverse("analytics")).content.decode()
        self.assertIn("45 minutes", html)
        self.assertIn("Estimated spend is", html)


class BookingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("shubh", password="safe-password-123")
        self.req = ServiceRequest.objects.create(
            user=self.user, raw_text="AC not cooling", workflow_thread_id="book-1", requirements=KOTHRUD,
        )
        self.client.force_login(self.user)

    def test_scheduling_builds_a_whatsapp_link_and_my_services_opens_it(self):
        result = scheduling_agent({
            "request_id": self.req.id,
            "selected_provider_id": "p-1",
            "ranked_providers": [{
                "place_id": "p-1",
                "name": "Shiv Krupa AC repair and service centre",
                "phone": "9876543210",
                "rating": 4.8,
                "review_count": 20,
            }],
            "requirements": {
                **KOTHRUD,
                "problem": "AC not cooling",
                "location": "Kothrud, Pune",
                "date": "2026-10-13",
                "time": "18:00",
            },
        })
        self.assertEqual(result["status"], "booking_requested")
        self.assertTrue(result["booking"]["whatsapp_url"].startswith("https://wa.me/919876543210?text="))
        self.assertIn("AC not cooling", result["booking"]["whatsapp_message"])
        self.assertIn("Kothrud, Pune", result["booking"]["whatsapp_message"])
        html = self.client.get(reverse("my_services")).content.decode()
        self.assertIn(reverse("booking_detail", args=[self.req.id]), html)
        page = self.client.get(reverse("booking_detail", args=[self.req.id]))
        self.assertContains(page, "https://wa.me/919876543210?text=")
        self.assertContains(page, "Open in WhatsApp")
        self.assertContains(page, "Mark as confirmed")

    def test_mark_confirmed_updates_the_booking(self):
        scheduling_agent({
            "request_id": self.req.id,
            "selected_provider_id": "p-1",
            "ranked_providers": [{"place_id": "p-1", "name": "Cool Air", "phone": "9876543210"}],
            "requirements": KOTHRUD,
        })
        response = self.client.post(reverse("mark_booking_confirmed", args=[self.req.id]))
        self.assertRedirects(response, reverse("booking_detail", args=[self.req.id]))
        self.assertEqual(Booking.objects.get(request=self.req).status, "confirmed")
        self.assertContains(self.client.get(reverse("my_services")), "Confirmed")

    def test_cancelled_booking_shows_the_approved_provider_without_a_form(self):
        Approval.objects.create(
            request=self.req, thread_id=self.req.workflow_thread_id, decision=Approval.Decision.APPROVED,
            payload={
                "selected_provider_id": "p-9",
                "ranked_providers": [{
                    "place_id": "p-9",
                    "name": "Kothrud Plumbing Works",
                    "phone": "9876543210",
                }],
            },
        )
        Booking.objects.create(request=self.req, status="cancelled", details={})
        page = self.client.get(reverse("booking_detail", args=[self.req.id]))
        self.assertContains(page, "Kothrud Plumbing Works")
        self.assertContains(page, "AC repair")
        self.assertContains(page, "https://wa.me/919876543210?text=")
        self.assertContains(page, "Open in WhatsApp")
