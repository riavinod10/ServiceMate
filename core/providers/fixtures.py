"""Offline demo data: saved Apify runs used when the live Actor fails or times out."""
import json
from pathlib import Path

from .categories import ServiceCategory

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "apify"


def load_fixture(category: ServiceCategory | None) -> list[dict]:
    """Raw Apify items for a known category, or [] if there is no saved run."""
    if category is None or not category.fixture:
        return []
    path = FIXTURE_DIR / category.fixture
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return json.load(f)
