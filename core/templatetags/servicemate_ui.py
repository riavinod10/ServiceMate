"""Display-only template filters for the ServiceMate pages (no business logic)."""
import re

from django import template

register = template.Library()


@register.filter
def price_short(price_info: str) -> str:
    """"₹500–₹1,500 (estimate: typical service visit...)" -> "₹500–₹1,500"."""
    if not price_info:
        return ""
    return re.sub(r"\s*\(.*\)\s*$", "", str(price_info)).strip()


@register.filter
def price_note(price_info: str) -> str:
    """The part in brackets, without the "estimate:" prefix."""
    match = re.search(r"\((?:estimate:\s*)?(.*)\)\s*$", str(price_info or ""))
    return match.group(1) if match else ""
