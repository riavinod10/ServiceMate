"""Distance helpers for Provider Analysis (Person 2).

The Requirement Agent gives a locality name ("Kothrud"), not coordinates, so the
user's position comes from (in order): explicit lat/lng in requirements, the
approximate centre of a known Pune locality, or the middle of the search
results (Apify centres its search on the locality, so this is a fair proxy).
"""
import math
from collections import Counter
from statistics import median

from .text import normalize_text

EARTH_RADIUS_KM = 6371.0

# Approximate centre points (within about 1 km), good enough for ranking by
# straight-line distance. Add localities as the team needs them.
PUNE_LOCALITIES: dict[str, tuple[float, float]] = {
    "aundh": (18.5580, 73.8075),
    "balewadi": (18.5770, 73.7780),
    "baner": (18.5590, 73.7868),
    "bavdhan": (18.5108, 73.7713),
    "camp": (18.5158, 73.8780),
    "deccan": (18.5167, 73.8414),
    "erandwane": (18.5100, 73.8300),
    "hadapsar": (18.5089, 73.9260),
    "hinjewadi": (18.5913, 73.7389),
    "kalyani nagar": (18.5486, 73.9020),
    "karve nagar": (18.4889, 73.8197),
    "katraj": (18.4575, 73.8677),
    "kharadi": (18.5515, 73.9348),
    "kondhwa": (18.4710, 73.8890),
    "koregaon park": (18.5362, 73.8940),
    "kothrud": (18.5074, 73.8077),
    "magarpatta": (18.5146, 73.9300),
    "pashan": (18.5386, 73.7932),
    "pimple saudagar": (18.5980, 73.8020),
    "shivajinagar": (18.5308, 73.8475),
    "sinhagad road": (18.4800, 73.8250),
    "swargate": (18.5018, 73.8636),
    "viman nagar": (18.5679, 73.9143),
    "wagholi": (18.5803, 73.9787),
    "wakad": (18.5987, 73.7650),
    "warje": (18.4818, 73.7975),
}
PUNE_CENTRE = (18.5204, 73.8567)

# Default pins Google gives to businesses with no shop address, seen in our
# saved Apify runs. Checked in addition to shared_pins(), because the
# normalizer may already have dropped the other businesses on the same pin.
KNOWN_DEFAULT_PINS: set[tuple[float, float]] = {(18.507351, 73.807654)}


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Straight-line (great-circle) distance between two points, in km."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi, dlmb = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def locate_user(requirements: dict, providers: list[dict]) -> tuple[tuple[float, float] | None, str]:
    """(lat, lng) for the user, plus a short note on how it was found."""
    lat, lng = requirements.get("lat"), requirements.get("lng")
    if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
        return (float(lat), float(lng)), "exact location"

    locality = requirements.get("locality")
    if not locality and requirements.get("location"):
        locality = requirements["location"].split(",")[0]
    if locality:
        point = PUNE_LOCALITIES.get(normalize_text(locality))
        if point:
            return point, f"centre of {locality.strip().title()}"

    points = [(p["lat"], p["lng"]) for p in providers if p.get("lat") is not None and p.get("lng") is not None]
    if points:
        return (median(lat for lat, _ in points), median(lng for _, lng in points)), "middle of the search results"
    city = (requirements.get("city") or "").strip().lower()
    if city in {"", "pune"}:
        return PUNE_CENTRE, "centre of Pune"
    return None, "unknown"


def shared_pins(providers: list[dict]) -> set[tuple[float, float]]:
    """Coordinates used by 2+ different providers.

    Google gives service-area businesses with no shop address a default pin for
    the area (we saw 18.5073514, 73.8076543 for unrelated Kothrud businesses).
    Their real distance is unknown, so they shouldn't look closest.
    """
    counts = Counter(key for key in map(pin_key, providers) if key is not None)
    return {point for point, n in counts.items() if n >= 2} | KNOWN_DEFAULT_PINS


def pin_key(provider: dict) -> tuple[float, float] | None:
    if provider.get("lat") is None or provider.get("lng") is None:
        return None
    return (round(provider["lat"], 6), round(provider["lng"], 6))
