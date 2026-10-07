"""Views for Person 3: Create Service Request and Booking detail (Person 3)."""
import base64
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .forms import ServiceRequestForm
from .models import Booking, ServiceRequest
from .tasks import run_workflow


@login_required
def service_request_create(request):
    """GET: show the new-request form.  POST: create the request and kick off the workflow."""
    form = ServiceRequestForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        raw_text = form.cleaned_data["request_text"].strip()

        # Merge optional form fields into the raw_text context so the
        # Requirement Agent has them available.  We also pass them as
        # pre-populated hints via the initial_state so the agent can
        # short-circuit some LLM work.
        location = form.cleaned_data.get("location", "").strip()
        preferred_date = form.cleaned_data.get("preferred_date")
        budget_inr = form.cleaned_data.get("budget_inr")

        # Build a unique thread ID for LangGraph checkpointing.
        thread_id = str(uuid.uuid4())

        service_request = ServiceRequest.objects.create(
            user=request.user,
            raw_text=raw_text,
            status="new",
            workflow_thread_id=thread_id,
        )

        # Seed the initial state with any hints the user gave in the form
        # so the Requirement Agent can use them without an LLM call.
        hint_requirements: dict = {}
        if location:
            # Split "Kothrud, Pune" style inputs into locality + city.
            parts = [p.strip() for p in location.split(",", 1)]
            hint_requirements["locality"] = parts[0]
            hint_requirements["city"] = parts[1] if len(parts) > 1 else "Pune"
            hint_requirements["location"] = location
        if preferred_date:
            hint_requirements["date"] = preferred_date.isoformat()
        if budget_inr:
            hint_requirements["budget"] = budget_inr

        initial_state = {
            "request_id": service_request.pk,
            "raw_text": raw_text,
            "status": "new",
            "retry_count": 0,
            "excluded_provider_ids": [],
        }
        if hint_requirements:
            initial_state["requirements"] = hint_requirements

        # Dispatch the LangGraph workflow to a Celery worker.
        run_workflow.delay(service_request.pk, thread_id, initial_state)

        messages.success(
            request,
            "ServiceMate is finding providers for you. "
            "They'll appear on the Approvals page shortly.",
        )
        return redirect("approvals")

    return render(request, "core/service_request_create.html", {"form": form})


@login_required
def booking_detail(request, request_id):
    """Show the booking summary for a completed service request."""
    service_request = get_object_or_404(
        ServiceRequest, pk=request_id, user=request.user
    )

    try:
        booking = service_request.booking_record
    except Booking.DoesNotExist:
        raise Http404("No booking found for this request.")

    details = booking.details or {}

    # Reconstruct the PDF bytes from the booking agent's stored state so we
    # can offer a download without re-generating it each time.
    # The PDF is not stored in Booking.details (too large for JSONField),
    # so we regenerate it on demand using the details dict.
    context = {
        "service_request": service_request,
        "booking": booking,
        "details": details,
        "whatsapp_url": details.get("whatsapp_url", ""),
        "whatsapp_message": details.get("whatsapp_message", ""),
    }
    return render(request, "core/booking.html", context)


@login_required
def booking_pdf(request, request_id):
    """Serve the booking summary as a downloadable PDF."""
    service_request = get_object_or_404(
        ServiceRequest, pk=request_id, user=request.user
    )

    try:
        booking = service_request.booking_record
    except Booking.DoesNotExist:
        raise Http404("No booking found for this request.")

    details = booking.details or {}

    # Re-generate the PDF from the stored details.
    from .agents.scheduling_agent import generate_booking_pdf

    # Reconstruct the minimal dicts the PDF generator needs.
    provider_dict = {
        "name": details.get("provider_name", ""),
        "phone": details.get("phone", ""),
        "website": details.get("website", ""),
        "rating": details.get("rating"),
        "review_count": details.get("review_count", 0),
        "price_info": details.get("price_info", ""),
        "price_is_estimate": details.get("price_is_estimate", True),
        "place_id": details.get("provider_place_id", ""),
    }
    requirements_dict = {
        "problem": details.get("problem", ""),
        "location": details.get("location", ""),
        "date": details.get("date", ""),
        "time": details.get("time", ""),
        "budget": details.get("budget_inr"),
        "category": "",
    }
    service_label = details.get("service_label", "Home Service")
    appointment_str = details.get("appointment_str", "time to be confirmed")
    whatsapp_message = details.get("whatsapp_message", "")

    pdf_bytes = generate_booking_pdf(
        provider=provider_dict,
        requirements=requirements_dict,
        service_label=service_label,
        appointment_str=appointment_str,
        whatsapp_message=whatsapp_message,
        request_id=request_id,
    )

    if not pdf_bytes:
        raise Http404("PDF generation is unavailable.")

    provider_name = details.get("provider_name", "booking")
    filename = f"servicemate-booking-{provider_name.replace(' ', '-').lower()}.pdf"

    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
