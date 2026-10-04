"""Provider Comparison page (Person 2).

Read-only view of the ranking stored in Approval.payload. The final decision is
made on Person 1's Approvals page, which this page links to.
"""
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render

from .models import Approval, ServiceRequest
from .providers.scoring import WEIGHTS

BREAKDOWN_LABELS = {"quality": "Rating quality", "reviews": "Review count",
                    "distance": "Distance", "budget": "Budget fit"}


@login_required
def compare_providers(request, request_id):
    service_request = get_object_or_404(ServiceRequest, pk=request_id, user=request.user)
    approval = service_request.approvals.order_by("-id").first()
    ranked = approval.payload.get("ranked_providers", []) if approval else []
    for provider in ranked:
        breakdown = provider.get("score_breakdown") or {}
        provider["breakdown_rows"] = [
            {"label": BREAKDOWN_LABELS[name], "value": breakdown.get(name, 0), "weight": round(weight * 100)}
            for name, weight in WEIGHTS.items()
        ]
    return render(request, "core/comparison.html", {
        "service_request": service_request,
        "approval": approval,
        "ranked": ranked,
        "is_pending": approval is not None and approval.decision == Approval.Decision.PENDING,
        "still_searching": approval is None,
    })
