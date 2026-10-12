"""Booking page for the Scheduling Agent (Person 3), linked from My Services."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .agents.scheduling_agent import generate_booking_pdf
from .logging import log_agent_event
from .models import Booking, ServiceRequest


@login_required
def booking_detail(request, request_id):
    service_request = get_object_or_404(ServiceRequest, pk=request_id, user=request.user)
    booking = _booking_or_404(service_request)
    details = booking.details or {}
    return render(request, "core/booking.html", {
        "service_request": service_request,
        "booking": booking,
        "details": details,
        "whatsapp_url": details.get("whatsapp_url", ""),
        "whatsapp_message": details.get("whatsapp_message", ""),
        "can_confirm": booking.status in {"requested", "booking_requested", "overdue"},
    })


@login_required
def booking_pdf(request, request_id):
    service_request = get_object_or_404(ServiceRequest, pk=request_id, user=request.user)
    booking = _booking_or_404(service_request)
    details = booking.details or {}
    pdf_bytes = generate_booking_pdf(
        provider={
            "name": details.get("provider_name", ""),
            "phone": details.get("phone", ""),
            "website": details.get("website", ""),
            "rating": details.get("rating"),
            "review_count": details.get("review_count", 0),
            "price_info": details.get("price_info", ""),
            "price_is_estimate": details.get("price_is_estimate", True),
            "place_id": details.get("provider_place_id", ""),
        },
        requirements={
            "problem": details.get("problem", ""),
            "location": details.get("location", ""),
            "date": details.get("date", ""),
            "time": details.get("time", ""),
            "budget": details.get("budget_inr"),
            "category": "",
        },
        service_label=details.get("service_label", "Home service"),
        appointment_str=details.get("appointment_str", "time to be confirmed"),
        whatsapp_message=details.get("whatsapp_message", ""),
        request_id=request_id,
    )
    if not pdf_bytes:
        raise Http404("PDF generation is unavailable.")
    provider_name = details.get("provider_name", "booking")
    filename = f"servicemate-booking-{provider_name.replace(' ', '-').lower()}.pdf"
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@login_required
@require_POST
def mark_booking_confirmed(request, request_id):
    """The provider replies on WhatsApp, outside ServiceMate. The user records that here."""
    service_request = get_object_or_404(ServiceRequest, pk=request_id, user=request.user)
    booking = _booking_or_404(service_request)
    if booking.status == "cancelled":
        messages.error(request, "This booking was cancelled, so it cannot be marked confirmed.")
        return redirect("booking_detail", request_id=request_id)
    booking.status = "confirmed"
    booking.save(update_fields=["status"])
    log_agent_event(
        service_request.id,
        "Scheduling Agent",
        "You marked this booking confirmed after the provider replied on WhatsApp.",
    )
    messages.success(request, "Booking marked confirmed. Reminders still run before the appointment.")
    return redirect("booking_detail", request_id=request_id)


def _booking_or_404(service_request):
    try:
        return service_request.booking_record
    except Booking.DoesNotExist:
        raise Http404("No booking found for this request.")
