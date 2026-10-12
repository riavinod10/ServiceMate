from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.html import format_html
from django.utils import timezone
from .forms import RegistrationForm
from .models import Approval, Booking, ServiceRequest
from .tasks import run_workflow

def register(request):
    form = RegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        login(request, form.save())
        return redirect("approvals")
    return render(request, "core/register.html", {"form": form})

def home(request):
    return redirect("dashboard")

@login_required
def approvals(request):
    rows = Approval.objects.filter(request__user=request.user, decision=Approval.Decision.PENDING).select_related("request")
    return render(request, "core/approvals.html", {"approvals": rows, "finding": _finding_requests(request.user)})

@login_required
def approval_action(request, approval_id):
    if request.method != "POST": return HttpResponseNotAllowed(["POST"])
    approval = get_object_or_404(Approval.objects.select_related("request"), pk=approval_id, request__user=request.user)
    choices = {"approve": Approval.Decision.APPROVED, "reject": Approval.Decision.REJECTED, "search_again": Approval.Decision.SEARCH_AGAIN}
    action = request.POST.get("action")
    if action not in choices or approval.decision != Approval.Decision.PENDING: return HttpResponseBadRequest("Invalid approval action")
    provider_id = request.POST.get("provider_id")
    valid_ids = {provider.get("place_id") for provider in approval.payload.get("ranked_providers", [])}
    if action == "approve" and provider_id not in valid_ids:
        return HttpResponseBadRequest("Choose one of the ranked providers before approving")
    approval.decision, approval.decided_at = choices[action], timezone.now()
    update_fields = ["decision", "decided_at"]
    if action == "approve":
        payload = dict(approval.payload or {})
        payload["selected_provider_id"] = provider_id
        approval.payload = payload
        update_fields.append("payload")
    approval.save(update_fields=update_fields)
    resume = {"action": action}
    if action == "approve":
        resume["provider_id"] = provider_id
    run_workflow.delay(approval.request_id, approval.thread_id, {"request_id": approval.request_id, "resume": resume})
    if action == "approve":
        messages.success(request, _decision_message(action, provider_id, approval), extra_tags="safe")
    else:
        messages.success(request, _decision_message(action, provider_id, approval))
    return redirect("dashboard")

def _finding_requests(user):
    """Requests still being searched: no pending approval, and not rejected, booked, or cancelled."""
    booked_ids = set(Booking.objects.filter(request__user=user).values_list("request_id", flat=True))
    finding = []
    requests = ServiceRequest.objects.filter(user=user).prefetch_related("approvals").order_by("-created_at")
    for item in requests:
        if item.id in booked_ids or item.status in {"cancelled", "rejected"}:
            continue
        approvals = list(item.approvals.all())
        if any(row.decision == Approval.Decision.PENDING for row in approvals):
            continue
        latest = max(approvals, key=lambda row: row.id) if approvals else None
        if latest and latest.decision in {Approval.Decision.REJECTED, Approval.Decision.APPROVED}:
            continue
        finding.append(item)
    return finding

def _decision_message(action, provider_id, approval):
    """Confirmation shown on the next page (display only; the workflow is unchanged)."""
    if action == "approve":
        names = {p.get("place_id"): p.get("name") for p in approval.payload.get("ranked_providers", [])}
        provider_name = names.get(provider_id) or "your provider"
        return format_html(
            'Approved {}. ServiceMate is preparing the booking request. <a href="{}">Open My Services</a>',
            provider_name,
            reverse("my_services"),
        )
    if action == "search_again":
        return "Searching again. New providers will appear on Approvals shortly."
    return "Request rejected. Nothing was booked."
