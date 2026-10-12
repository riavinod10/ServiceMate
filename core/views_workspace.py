"""Dashboard, My Services, Analytics, and the agent-log panel (Person 4)."""
import uuid

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .agents.analytics import build_analytics, note_analytics
from .agents.followup import mark_booking_cancelled
from .logging import log_agent_event
from .models import AgentLog, Approval, Booking, ServiceRequest
from .providers.scoring import parse_budget
from .tasks import run_workflow
from .tasks_followup import reopen_workflow_after_cancellation


@login_required
def dashboard(request):
    summary = build_analytics(request.user, with_insight=False)
    pending = Approval.objects.filter(request__user=request.user, decision=Approval.Decision.PENDING).count()
    return render(request, "core/dashboard.html", {
        "summary": summary,
        "pending_count": pending,
        "logs": _logs(request.user),
        "open_request_form": _has_error_message(request),
    })


@login_required
def my_services(request):
    return render(request, "core/my_services.html", {
        "rows": _service_rows(request.user),
        "logs": _logs(request.user),
        "max_retries": settings.WORKFLOW_MAX_RECOVERY_RETRIES,
    })


@login_required
def analytics(request):
    summary = build_analytics(request.user)
    note_analytics(request.user, summary)
    return render(request, "core/analytics.html", {"summary": summary})


@login_required
@require_POST
def new_request(request):
    raw_text = (request.POST.get("raw_text") or "").strip()
    if not raw_text:
        messages.error(request, "Describe the job before starting a request.")
        return redirect("dashboard")
    if len(raw_text) > 500:
        messages.error(request, "Keep the request to 500 characters.")
        return redirect("dashboard")
    locality = (request.POST.get("locality") or "Kothrud").strip()[:80] or "Kothrud"
    city = (request.POST.get("city") or "Pune").strip()[:80] or "Pune"
    budget_raw = (request.POST.get("budget") or "").strip()
    requirements = {"locality": locality, "city": city}
    if budget_raw:
        budget = parse_budget(budget_raw)
        if budget is None:
            messages.error(request, "Enter the budget as a number of rupees, or leave it blank.")
            return redirect("dashboard")
        requirements["budget"] = budget
    thread_id = uuid.uuid4().hex
    service_request = ServiceRequest.objects.create(
        user=request.user,
        raw_text=raw_text,
        requirements=requirements,
        status="new",
        workflow_thread_id=thread_id,
    )
    run_workflow.delay(service_request.id, thread_id, {
        "request_id": service_request.id,
        "raw_text": raw_text,
        "requirements": requirements,
        "excluded_provider_ids": [],
        "retry_count": 0,
    })
    messages.success(request, "Request started. ServiceMate is finding providers; they'll appear here in a few seconds.")
    return redirect("approvals")


@login_required
def agent_log(request):
    logs = _logs(request.user, request.GET.get("request"))
    return render(request, "core/_agent_log.html", {"logs": logs})


@login_required
@require_POST
def simulate_cancellation(request, request_id):
    service_request = get_object_or_404(ServiceRequest, pk=request_id, user=request.user)
    if not _can_reopen(service_request):
        return HttpResponseBadRequest("This request cannot be cancelled right now")
    mark_booking_cancelled(service_request)
    try:
        reopen_workflow_after_cancellation.delay(service_request.id)
    except Exception as exc:
        log_agent_event(
            service_request.id,
            "Follow-Up Agent",
            f"Could not queue the cancellation task: {exc}",
        )
        messages.error(request, "The cancellation was saved, but the background task could not be queued. Is Redis running?")
        return redirect("my_services")
    messages.success(request, "Provider cancellation simulated. ServiceMate is looking for someone else — check Approvals.")
    return redirect("my_services")


def _has_error_message(request) -> bool:
    """True when a failed submit sent the user back. Leaves the messages for the banner."""
    storage = messages.get_messages(request)
    has_error = False
    for message in storage:
        if message.level >= messages.ERROR:
            has_error = True
    storage.used = False
    return has_error


def _row_tone(label: str) -> str:
    if label == "Confirmed":
        return "ok"
    if label == "Booking requested":
        return "info"
    if label == "Waiting for approval":
        return "wait"
    if label in {"Overdue", "Provider cancelled", "Recovery stopped"}:
        return "alert"
    return "neutral"


def _logs(user, request_id=None):
    rows = AgentLog.objects.filter(request__user=user).select_related("request")
    if request_id:
        rows = rows.filter(request_id=request_id)
    return list(rows.order_by("-created_at")[:40])


def _service_rows(user):
    requests = ServiceRequest.objects.filter(user=user).prefetch_related("approvals").order_by("-created_at")
    bookings = {
        booking.request_id: booking
        for booking in Booking.objects.filter(request__user=user).select_related("provider", "follow_up")
    }
    rows = []
    for item in requests:
        approvals = list(item.approvals.all())
        approval = max(approvals, key=lambda row: row.id) if approvals else None
        pending = any(row.decision == Approval.Decision.PENDING for row in approvals)
        booking = bookings.get(item.id)
        label = _state_label(item, approval, booking, pending)
        rows.append({
            "request": item,
            "approval": approval,
            "booking": booking,
            "pending": pending,
            "label": label,
            "tone": _row_tone(label),
            "provider_name": _provider_name(booking),
            "appointment": _appointment_label(booking),
            "can_reopen": _can_reopen(item, approval=approval, pending=pending, booking=booking),
        })
    return rows


def _can_reopen(service_request, approval=None, pending=None, booking=None) -> bool:
    if service_request.retry_count >= settings.WORKFLOW_MAX_RECOVERY_RETRIES:
        return False
    if booking is None:
        booking = Booking.objects.filter(request=service_request).first()
    if booking is not None and booking.status == "cancelled":
        return False
    if approval is None or pending is None:
        approvals = list(service_request.approvals.all())
        approval = max(approvals, key=lambda row: row.id) if approvals else None
        pending = any(row.decision == Approval.Decision.PENDING for row in approvals)
    if pending or approval is None:
        return False
    return approval.decision == Approval.Decision.APPROVED


def _state_label(item, approval, booking, pending) -> str:
    if pending:
        return "Waiting for approval"
    if item.retry_count >= settings.WORKFLOW_MAX_RECOVERY_RETRIES and (
        item.status == "cancelled" or (booking is not None and booking.status == "cancelled")
    ):
        return "Recovery stopped"
    if booking is not None and booking.status == "overdue":
        return "Overdue"
    if booking is not None and booking.status == "cancelled":
        return "Provider cancelled"
    if booking is not None and booking.status == "confirmed":
        return "Confirmed"
    if booking is not None:
        return "Booking requested"
    if approval is not None and approval.decision == Approval.Decision.APPROVED:
        return "Approved"
    if approval is not None and approval.decision == Approval.Decision.REJECTED:
        return "Rejected"
    return "In progress"


def _provider_name(booking) -> str:
    if booking is None:
        return ""
    if booking.provider_id and booking.provider.name:
        return booking.provider.name
    return (booking.details or {}).get("provider_name") or ""


def _appointment_label(booking) -> str:
    if booking is None:
        return ""
    details = booking.details or {}
    if details.get("appointment_str"):
        return details["appointment_str"]
    try:
        follow = booking.follow_up
    except ObjectDoesNotExist:
        follow = None
    if follow is not None and follow.appointment_at is not None:
        clock = timezone.localtime(follow.appointment_at).strftime("%d %b %Y, %H:%M")
        if follow.assumed_time:
            return f"{clock} (time assumed)"
        return clock
    date_str = details.get("date") or ""
    time_str = details.get("time") or ""
    return " at ".join(part for part in (date_str, time_str) if part)
