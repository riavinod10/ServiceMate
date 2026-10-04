"""Provider Comparison page (Person 2).

Read-only view of the ranking stored in Approval.payload. The final decision is
made on Person 1's Approvals page, which this page links to.
"""
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render

from .models import Approval, ServiceRequest
from .providers.scoring import WEIGHTS

# Most reliable first. If a ranking mixes sources (e.g. after widening the
# search), the page describes the least reliable one present.
SOURCE_NOTES = {
    "apify": "Live results from Google Maps, searched for this request.",
    "cache": "Results from a Google Maps search made in the last 7 days.",
    "stale_cache": "Live search was unavailable, so these are older saved results from Google Maps.",
    "fixture": "Live search was unavailable, so these are saved demo results, not a live search.",
}

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
        "source_note": source_note(ranked),
    })


def source_note(ranked: list[dict]) -> str:
    present = {p.get("source") for p in ranked}
    for source in reversed(list(SOURCE_NOTES)):
        if source in present:
            return SOURCE_NOTES[source]
    return ""
