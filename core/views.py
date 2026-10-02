from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from .forms import RegistrationForm
from .models import Approval
from .tasks import run_workflow

def register(request):
    form = RegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        login(request, form.save())
        return redirect("approvals")
    return render(request, "core/register.html", {"form": form})

def home(request):
    return redirect("approvals")

@login_required
def approvals(request):
    rows = Approval.objects.filter(request__user=request.user, decision=Approval.Decision.PENDING).select_related("request")
    return render(request, "core/approvals.html", {"approvals": rows})

@login_required
def approval_action(request, approval_id):
    if request.method != "POST": return HttpResponseNotAllowed(["POST"])
    approval = get_object_or_404(Approval.objects.select_related("request"), pk=approval_id, request__user=request.user)
    choices = {"approve": Approval.Decision.APPROVED, "reject": Approval.Decision.REJECTED, "search_again": Approval.Decision.SEARCH_AGAIN}
    action = request.POST.get("action")
    if action not in choices or approval.decision != Approval.Decision.PENDING: return HttpResponseBadRequest("Invalid approval action")
    approval.decision, approval.decided_at = choices[action], timezone.now()
    approval.save(update_fields=["decision", "decided_at"])
    run_workflow.delay(approval.request_id, approval.thread_id, {"request_id": approval.request_id, "status": action})
    return redirect("approvals")
