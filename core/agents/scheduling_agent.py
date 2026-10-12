"""Scheduling Agent (Person 3).

Reads the approved provider from state and creates a Booking record.
Because real providers have no booking API, this agent prepares a
*booking request* — not a confirmed booking — and gives the user a
pre-filled WhatsApp message and a PDF summary they can use to contact
the provider themselves.

Reads:
    state["request_id"]          int
    state["selected_provider_id"] str   place_id of the approved provider
    state["ranked_providers"]    list   ranked provider dicts from Analysis
    state["requirements"]        dict   structured requirements from Requirement Agent

Writes:
    state["booking"]   dict with booking details (also persisted to Booking model)
    state["status"]    "booking_requested"
"""
import io
import logging
import urllib.parse

from ..logging import log_agent_event
from ..models import Booking, Provider, ServiceRequest

logger = logging.getLogger(__name__)

AGENT_NAME = "Scheduling Agent"


# ---------------------------------------------------------------------------
# Main agent function
# ---------------------------------------------------------------------------

def scheduling_agent(state: dict) -> dict:
    request_id = state["request_id"]
    provider_id = state.get("selected_provider_id")
    requirements = state.get("requirements") or {}
    ranked = state.get("ranked_providers") or []

    # Find the approved provider in the ranked list (avoid a DB round-trip
    # for the fields analysis already fetched).
    provider_data = _find_provider(provider_id, ranked)

    if not provider_data:
        log_agent_event(
            request_id, AGENT_NAME,
            "No approved provider found in state — booking cannot be prepared.",
        )
        return {"booking": None, "status": "booking_requested"}

    provider_name = provider_data.get("name", "Provider")
    log_agent_event(
        request_id, AGENT_NAME,
        f"Preparing booking request for {provider_name}.",
    )

    # Build the booking details dict (stored in Booking.details).
    details = _build_details(provider_data, requirements, request_id)

    # Persist the Booking record.
    _save_booking(request_id, provider_id, details)

    log_agent_event(
        request_id, AGENT_NAME,
        f"Booking request saved. Contact {provider_name} on {details.get('phone', 'their number')} "
        f"to confirm the appointment.",
    )

    return {"booking": details, "status": "booking_requested"}


# ---------------------------------------------------------------------------
# Detail builder
# ---------------------------------------------------------------------------

def _build_details(provider: dict, requirements: dict, request_id: int) -> dict:
    """Assemble all booking-related information into one serialisable dict."""
    phone = provider.get("phone", "")
    provider_name = provider.get("name", "Provider")
    service_label = _service_label(requirements)
    problem = requirements.get("problem", "")
    location = requirements.get("location", requirements.get("city", ""))
    date_str = requirements.get("date", "")
    time_str = requirements.get("time", "")
    budget = requirements.get("budget")

    # Human-readable appointment string shown in the PDF and UI.
    appointment_parts = []
    if date_str:
        appointment_parts.append(date_str)
    if time_str:
        appointment_parts.append(time_str)
    appointment_str = " at ".join(appointment_parts) if appointment_parts else "time to be confirmed"

    # Pre-filled WhatsApp message.
    whatsapp_message = _build_whatsapp_message(
        provider_name=provider_name,
        service_label=service_label,
        problem=problem,
        appointment_str=appointment_str,
        location=location,
        budget=budget,
    )
    whatsapp_url = _whatsapp_url(phone, whatsapp_message)

    # Generate the PDF bytes and base64-encode them for JSON storage.
    pdf_bytes = generate_booking_pdf(
        provider=provider,
        requirements=requirements,
        service_label=service_label,
        appointment_str=appointment_str,
        whatsapp_message=whatsapp_message,
        request_id=request_id,
    )
    import base64
    pdf_b64 = base64.b64encode(pdf_bytes).decode("ascii") if pdf_bytes else ""

    return {
        "provider_name": provider_name,
        "provider_place_id": provider.get("place_id", ""),
        "phone": phone,
        "website": provider.get("website", ""),
        "service_label": service_label,
        "problem": problem,
        "location": location,
        "date": date_str,
        "time": time_str,
        "appointment_str": appointment_str,
        "budget_inr": budget,
        "rating": provider.get("rating"),
        "review_count": provider.get("review_count", 0),
        "price_info": provider.get("price_info", ""),
        "price_is_estimate": provider.get("price_is_estimate", True),
        "whatsapp_message": whatsapp_message,
        "whatsapp_url": whatsapp_url,
        "pdf_b64": pdf_b64,
    }


# ---------------------------------------------------------------------------
# WhatsApp message builder
# ---------------------------------------------------------------------------

def _build_whatsapp_message(
    provider_name: str,
    service_label: str,
    problem: str,
    appointment_str: str,
    location: str,
    budget: int | None,
) -> str:
    lines = [f"Hello {provider_name},"]
    lines.append("")
    if problem:
        lines.append(f"I need {service_label} help. {problem}.")
    else:
        lines.append(f"I need {service_label} service.")
    if location:
        lines.append(f"Location: {location}")
    lines.append(f"Preferred time: {appointment_str}")
    if budget:
        lines.append(f"Budget: ₹{budget:,}")
    lines.append("")
    lines.append("Could you confirm availability? Thank you.")
    return "\n".join(lines)


def _whatsapp_url(phone: str, message: str) -> str:
    """Build a wa.me deep-link. Returns empty string if no phone number."""
    if not phone:
        return ""
    # Strip everything except digits and leading +
    digits = "".join(c for c in phone if c.isdigit())
    if not digits:
        return ""
    # Add India country code if no country code present (10 digits = Indian mobile).
    if len(digits) == 10:
        digits = "91" + digits
    encoded = urllib.parse.quote(message)
    return f"https://wa.me/{digits}?text={encoded}"


# ---------------------------------------------------------------------------
# ReportLab PDF generator
# ---------------------------------------------------------------------------

def generate_booking_pdf(
    provider: dict,
    requirements: dict,
    service_label: str,
    appointment_str: str,
    whatsapp_message: str,
    request_id: int,
) -> bytes:
    """Build and return a booking-summary PDF as bytes.

    Returns empty bytes if ReportLab is not installed — the rest of the
    workflow never crashes because of a missing PDF library.
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
        )
    except ImportError:
        logger.warning("reportlab is not installed; PDF generation skipped.")
        return b""

    buffer = io.BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
        title="ServiceMate Booking Request",
        author="ServiceMate AI",
    )

    styles = getSampleStyleSheet()

    # Custom styles matching the ServiceMate brand palette.
    COBALT = colors.HexColor("#1B2CC1")
    CREAM = colors.HexColor("#FFF8F0")
    DARK = colors.HexColor("#1A1A2E")

    heading_style = ParagraphStyle(
        "SMHeading",
        parent=styles["Heading1"],
        textColor=COBALT,
        fontSize=18,
        spaceAfter=6,
    )
    subheading_style = ParagraphStyle(
        "SMSubheading",
        parent=styles["Heading2"],
        textColor=DARK,
        fontSize=13,
        spaceBefore=14,
        spaceAfter=4,
    )
    body_style = ParagraphStyle(
        "SMBody",
        parent=styles["Normal"],
        fontSize=10,
        leading=15,
        textColor=DARK,
    )
    note_style = ParagraphStyle(
        "SMNote",
        parent=styles["Normal"],
        fontSize=9,
        leading=13,
        textColor=colors.HexColor("#666666"),
        italics=1,
    )

    story = []

    # --- Header ---
    story.append(Paragraph("ServiceMate AI", heading_style))
    story.append(Paragraph("Booking Request", subheading_style))
    story.append(Spacer(1, 4 * mm))

    # --- Provider summary table ---
    provider_name = provider.get("name", "—")
    phone = provider.get("phone", "—")
    website = provider.get("website", "—")
    rating = provider.get("rating")
    review_count = provider.get("review_count", 0)
    price_info = provider.get("price_info", "")
    price_is_estimate = provider.get("price_is_estimate", True)

    rating_str = f"★ {rating:.1f} ({review_count} review{'s' if review_count != 1 else ''})" if rating else "No reviews yet"
    price_str = price_info if price_info else "Price on request"
    if price_info and price_is_estimate:
        price_str += " (estimate)"

    table_data = [
        ["Provider", provider_name],
        ["Phone", phone],
        ["Website", website if website else "—"],
        ["Rating", rating_str],
        ["Pricing", price_str],
    ]

    tbl = Table(table_data, colWidths=[45 * mm, 125 * mm])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), CREAM),
        ("TEXTCOLOR", (0, 0), (0, -1), COBALT),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, CREAM]),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#DDDDDD")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 6 * mm))

    # --- Service details ---
    story.append(Paragraph("Service Details", subheading_style))

    problem = requirements.get("problem", "")
    location = requirements.get("location", requirements.get("city", ""))
    budget = requirements.get("budget")

    service_rows = [
        ["Service", service_label],
    ]
    if problem:
        service_rows.append(["Problem", problem])
    service_rows.append(["Location", location if location else "—"])
    service_rows.append(["Preferred time", appointment_str])
    if budget:
        service_rows.append(["Budget", f"₹{budget:,}"])

    svc_tbl = Table(service_rows, colWidths=[45 * mm, 125 * mm])
    svc_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), CREAM),
        ("TEXTCOLOR", (0, 0), (0, -1), COBALT),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, CREAM]),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#DDDDDD")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(svc_tbl)
    story.append(Spacer(1, 6 * mm))

    # --- Pre-filled message ---
    story.append(Paragraph("Pre-filled Contact Message", subheading_style))
    story.append(Paragraph(
        "Copy and send this message to the provider by phone or WhatsApp:",
        note_style,
    ))
    story.append(Spacer(1, 2 * mm))

    # Wrap the raw message text in a box.
    escaped_msg = whatsapp_message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")
    msg_style = ParagraphStyle(
        "SMMsg",
        parent=body_style,
        backColor=CREAM,
        borderPadding=(6, 8, 6, 8),
        borderColor=colors.HexColor("#DDDDDD"),
        borderWidth=0.5,
        borderRadius=4,
        leading=16,
    )
    story.append(Paragraph(escaped_msg, msg_style))
    story.append(Spacer(1, 6 * mm))

    # --- Disclaimer ---
    story.append(Paragraph(
        "This is a booking request prepared by ServiceMate AI, not a confirmed appointment. "
        "Contact the provider directly to confirm availability, pricing and timing. "
        "Pricing shown is an estimate only.",
        note_style,
    ))

    doc.build(story)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Model persistence
# ---------------------------------------------------------------------------

def _save_booking(request_id: int, provider_place_id: str | None, details: dict) -> None:
    """Create or update the Booking record for this request."""
    provider = None
    if provider_place_id:
        try:
            provider = Provider.objects.get(place_id=provider_place_id)
        except Provider.DoesNotExist:
            logger.warning(
                "Scheduling agent: Provider with place_id=%s not found in DB.",
                provider_place_id,
            )
    try:
        # Exclude the large PDF blob from the DB details to keep the JSONField small.
        db_details = {k: v for k, v in details.items() if k != "pdf_b64"}
        Booking.objects.update_or_create(
            request_id=request_id,
            defaults={
                "provider": provider,
                "status": "requested",
                "details": db_details,
            },
        )
    except Exception as exc:
        logger.error("Failed to save booking for request %s: %s", request_id, exc)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _find_provider(place_id: str | None, ranked: list[dict]) -> dict | None:
    """Find the approved provider dict in the ranked list by place_id."""
    if not place_id:
        return None
    for p in ranked:
        if p.get("place_id") == place_id:
            return p
    return None


def _service_label(requirements: dict) -> str:
    """Human-readable service label from the requirements dict."""
    category = requirements.get("category", "")
    if not category:
        return "home service"
    # Try the known categories for a nice label.
    try:
        from ..providers.categories import CATEGORIES
        cat = CATEGORIES.get(category)
        if cat:
            return cat.label
    except Exception:
        pass
    # Fall back to title-casing the slug/free-text.
    return category.replace("_", " ").title()
