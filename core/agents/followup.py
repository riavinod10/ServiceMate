"""Follow-Up Agent (Person 4).

Celery Beat calls the reminder and overdue jobs. The cancellation button
marks the booking cancelled and a separate task reopens the workflow.
"""
from datetime import datetime, time, timedelta

from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime, parse_time

from ..logging import log_agent_event
from ..models import Booking, ServiceFollowUp, ServiceRequest

AGENT_NAME = "Follow-Up Agent"
REMINDER_HOURS = 24
OPEN_STATUSES = ("requested", "booking_requested")
_CLOCK_WORDS = {
    "morning": time(9, 0),
    "afternoon": time(14, 0),
    "evening": time(18, 0),
    "night": time(20, 0),
}


def resolve_appointment(booking: Booking) -> tuple[datetime | None, bool]:
    """Return (aware datetime, assumed_time).

    Date and time come from the booking details Person 3 stores, then from the
    request requirements. A date with no clock uses 09:00 and assumed_time=True.
    """
    details = booking.details or {}
    requirements = {}
    if booking.request_id:
        requirements = booking.request.requirements or {}

    raw = details.get("appointment_at") or requirements.get("appointment_at")
    if isinstance(raw, str) and raw.strip():
        parsed = parse_datetime(raw.strip())
        if parsed is not None:
            if timezone.is_naive(parsed):
                parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
            return parsed, False

    date_str = str(details.get("date") or requirements.get("date") or "").strip()
    day = parse_date(date_str) if date_str else None
    if day is None:
        return None, False

    time_str = str(details.get("time") or requirements.get("time") or "").strip().lower()
    clock = parse_time(time_str) if time_str else None
    assumed = False
    if clock is None and time_str in _CLOCK_WORDS:
        clock = _CLOCK_WORDS[time_str]
    if clock is None:
        clock = time(9, 0)
        assumed = True
    combined = datetime.combine(day, clock)
    return timezone.make_aware(combined, timezone.get_current_timezone()), assumed


def ensure_follow_up(booking: Booking) -> ServiceFollowUp:
    when, assumed = resolve_appointment(booking)
    follow, _created = ServiceFollowUp.objects.get_or_create(booking=booking)
    follow.appointment_at = when
    follow.assumed_time = assumed
    follow.save(update_fields=["appointment_at", "assumed_time"])
    return follow


def mark_booking_cancelled(service_request: ServiceRequest) -> Booking:
    """Record the provider cancellation on the booking row."""
    booking = Booking.objects.filter(request=service_request).first()
    if booking is None:
        booking = Booking(request=service_request, details={})
    details = dict(booking.details or {})
    details["cancelled_by"] = "follow_up_simulation"
    booking.details = details
    booking.status = "cancelled"
    booking.save()
    log_agent_event(
        service_request.id,
        AGENT_NAME,
        "Booking marked cancelled after a simulated provider cancellation.",
    )
    return booking


def send_due_reminders(now=None) -> int:
    """Remind users whose appointment is inside the next 24 hours. Returns how many were sent."""
    now = now or timezone.now()
    horizon = now + timedelta(hours=REMINDER_HOURS)
    sent = 0
    bookings = Booking.objects.select_related("request", "provider").filter(status__in=OPEN_STATUSES)
    for booking in bookings:
        follow = ensure_follow_up(booking)
        when = follow.appointment_at
        if when is None or follow.reminder_sent_at is not None:
            continue
        if not (now <= when <= horizon):
            continue
        follow.reminder_sent_at = now
        follow.save(update_fields=["reminder_sent_at"])
        who = _provider_name(booking)
        clock = timezone.localtime(when).strftime("%d %b %Y, %H:%M")
        note = " The clock was not on the booking, so 09:00 was used." if follow.assumed_time else ""
        log_agent_event(
            booking.request_id,
            AGENT_NAME,
            f"Reminder: {who} is booked for {clock}.{note}",
        )
        sent += 1
    return sent


def mark_overdue_bookings(now=None) -> int:
    """Mark open bookings overdue once the appointment time has passed."""
    now = now or timezone.now()
    marked = 0
    bookings = Booking.objects.select_related("request", "provider").filter(status__in=OPEN_STATUSES)
    for booking in bookings:
        follow = ensure_follow_up(booking)
        when = follow.appointment_at
        if when is None or when >= now or follow.overdue_marked_at is not None:
            continue
        booking.status = "overdue"
        booking.save(update_fields=["status"])
        follow.overdue_marked_at = now
        follow.save(update_fields=["overdue_marked_at"])
        who = _provider_name(booking)
        log_agent_event(
            booking.request_id,
            AGENT_NAME,
            f"Booking with {who} is overdue. The appointment time has passed and it was still open.",
        )
        marked += 1
    return marked


def _provider_name(booking: Booking) -> str:
    if booking.provider_id and booking.provider.name:
        return booking.provider.name
    details = booking.details or {}
    return details.get("provider_name") or "the provider"
