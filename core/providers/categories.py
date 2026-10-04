"""Known service categories for discovery (Person 2).

The category list makes cache keys consistent and filters out off-topic Google
Maps results. It does NOT limit what users can search: an unknown category is
still searched live on Apify, it just has no relevance filter, no price
estimate range and no offline fallback fixture.
"""
from dataclasses import dataclass

from .text import normalize_text


@dataclass(frozen=True)
class ServiceCategory:
    slug: str
    label: str
    search_term: str                 # what we send to the Apify Actor
    aliases: tuple[str, ...]         # phrases that map user text to this category
    accepted_google_categories: frozenset[str]
    price_min: int                   # INR, category-level estimate only
    price_max: int
    price_note: str
    fixture: str | None = None       # file in core/fixtures/apify/

    @property
    def price_info(self) -> str:
        return f"₹{self.price_min:,}–₹{self.price_max:,} (estimate: {self.price_note})"


CATEGORIES: dict[str, ServiceCategory] = {
    c.slug: c
    for c in [
        ServiceCategory(
            slug="ac_repair",
            label="AC repair",
            search_term="AC repair",
            aliases=("ac repair", "ac service", "ac servicing", "ac mechanic", "ac installation",
                     "ac not cooling", "air conditioner", "air conditioning", "aircon", "ac"),
            accepted_google_categories=frozenset({
                "air conditioning repair service", "air conditioning contractor",
                "hvac contractor", "air conditioning system supplier",
            }),
            price_min=500, price_max=1500,
            price_note="typical service visit; gas refill and parts extra",
            fixture="ac_repair_pune.json",
        ),
        ServiceCategory(
            slug="plumber",
            label="Plumber",
            search_term="Plumber",
            aliases=("plumber", "plumbing", "pipe leak", "leaking tap", "tap leak", "water leak",
                     "drain", "blocked drain", "clogged", "flush", "geyser installation"),
            accepted_google_categories=frozenset({"plumber", "plumbing service"}),
            price_min=300, price_max=1000,
            price_note="visit charge plus minor repair; materials extra",
            fixture="plumber_pune.json",
        ),
        ServiceCategory(
            slug="electrician",
            label="Electrician",
            search_term="Electrician",
            aliases=("electrician", "electrical", "wiring", "short circuit", "switchboard",
                     "fan installation", "light fitting", "mcb", "power socket"),
            accepted_google_categories=frozenset({
                "electrician", "electrical installation service", "electrical repair shop",
                "electrical engineer",
            }),
            price_min=200, price_max=800,
            price_note="visit charge plus small job; parts extra",
            fixture="electrician_pune.json",
        ),
        ServiceCategory(
            slug="home_cleaning",
            label="Home cleaning",
            search_term="Home cleaning",
            aliases=("home cleaning", "house cleaning", "deep cleaning", "deep clean", "cleaning",
                     "cleaner", "bathroom cleaning", "kitchen cleaning", "sofa cleaning"),
            accepted_google_categories=frozenset({
                "house cleaning service", "cleaners", "home help", "home help service agency",
                "carpet cleaning service", "window cleaning service",
            }),
            price_min=1500, price_max=6000,
            price_note="1–3 BHK deep clean; varies with home size",
            fixture="home_cleaning_pune.json",
        ),
    ]
}


def resolve_category(text: str | None) -> ServiceCategory | None:
    """Map free text (or an existing slug) to a known category, else None.

    Longer aliases are checked first so "ac not cooling" wins over "ac", and
    matching is on whole words so "ac" doesn't match inside "vacuum".
    """
    if not text:
        return None
    norm = normalize_text(text)
    if norm.replace(" ", "_") in CATEGORIES:
        return CATEGORIES[norm.replace(" ", "_")]
    padded = f" {norm} "
    candidates = sorted(
        ((alias, cat) for cat in CATEGORIES.values() for alias in cat.aliases),
        key=lambda pair: -len(pair[0]),
    )
    for alias, cat in candidates:
        if f" {normalize_text(alias)} " in padded:
            return cat
    return None
