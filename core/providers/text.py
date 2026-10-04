import re
import unicodedata


def normalize_text(value: str | None) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace.

    "  Kothrud,  Pune! " -> "kothrud pune"
    """
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-z0-9]+", " ", value.lower())
    return " ".join(value.split())


def slugify(value: str | None) -> str:
    """"Right Bhusari Colony" -> "right_bhusari_colony"."""
    return normalize_text(value).replace(" ", "_")
