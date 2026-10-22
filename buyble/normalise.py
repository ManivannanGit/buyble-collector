"""Price parsing, units, cities, PII scrubbing and the cheap price prefilter."""
from __future__ import annotations

import re
from datetime import date, datetime

CITY_ALIASES = {
    "bangalore": "Bengaluru", "bengaluru": "Bengaluru", "blr": "Bengaluru", "bengalooru": "Bengaluru",
    "bombay": "Mumbai", "mumbai": "Mumbai",
    "madras": "Chennai", "chennai": "Chennai",
    "gurgaon": "Gurugram", "gurugram": "Gurugram",
    "calcutta": "Kolkata", "kolkata": "Kolkata",
    "poona": "Pune", "pune": "Pune",
    "new delhi": "Delhi", "delhi": "Delhi",
    "hyderabad": "Hyderabad", "secunderabad": "Hyderabad",
}

_PRICE_RE = re.compile(
    r"""(?:₹|\brs\.?|\binr)?\s*          # optional currency before
        (\d{1,3}(?:,\d{2,3})+|\d+)(?:\.(\d+))?   # 2,400 / 1,20,000 / 450 / 1.2
        \s*(k|lakh|lac)?\b               # multiplier
        (?:\s*/-)?""",
    re.I | re.X,
)


def parse_price(text: str | int | float | None) -> float | None:
    """'1.2k' -> 1200, 'Rs. 450/-' -> 450, '₹2,400' -> 2400. None if no number."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text) if text > 0 else None
    m = _PRICE_RE.search(text.strip())
    if not m:
        return None
    whole, frac, mult = m.group(1).replace(",", ""), m.group(2), (m.group(3) or "").lower()
    value = float(f"{whole}.{frac}" if frac else whole)
    value *= {"k": 1_000, "lakh": 100_000, "lac": 100_000}.get(mult, 1)
    return round(value, 2) if value > 0 else None


def normalise_city(city: str | None) -> str | None:
    if not city or not city.strip():
        return None
    key = city.strip().lower()
    return CITY_ALIASES.get(key, city.strip().title())


# --- units ------------------------------------------------------------------

_UNIT_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)?\s*([a-z]+)", re.I)
_UNIT_WORDS = {
    "cup": "cup", "cups": "cup", "glass": "cup", "glasses": "cup", "mug": "cup", "mugs": "cup",
    "plate": "plate", "plates": "plate",
    "g": "g", "gm": "g", "gms": "g", "gram": "g", "grams": "g",
    "kg": "kg", "kgs": "kg", "ml": "ml", "l": "l", "litre": "l", "liter": "l",
    "pc": "pc", "pcs": "pc", "piece": "pc", "pieces": "pc", "unit": "pc", "units": "pc",
    "pair": "pair", "pairs": "pair", "set": "set", "sets": "set",
    "km": "km", "kms": "km", "kilometre": "km", "kilometer": "km",
    "job": "job", "service": "job", "visit": "job",
    "trip": "trip", "ride": "trip",
}
CAB_CANONICALS = {"airport cab", "city cab ride"}


def unit_basis(unit: str | None, category: str, canonical: str = "") -> tuple[float | None, str | None]:
    """Return (divisor, basis): price_per_unit = price / divisor.

    food per cup/plate/100g, apparel and parts per piece, labour per job, cabs per km.
    (None, None) when the unit is unclear.
    """
    if not unit:
        if category == "service" and canonical not in CAB_CANONICALS:
            return 1.0, "job"
        return None, None
    m = _UNIT_RE.match(unit.lower())
    if not m:
        return None, None
    qty = float(m.group(1)) if m.group(1) else 1.0
    word = _UNIT_WORDS.get(m.group(2))
    if qty <= 0 or word is None:
        return None, None
    if word == "g":
        return qty / 100, "100g"
    if word == "kg":
        return qty * 10, "100g"
    if word == "ml" and category == "food_drink":
        return qty / 100, "100ml"
    if word == "l" and category == "food_drink":
        return qty * 10, "100ml"
    if word in ("cup", "plate", "pc", "pair", "set", "job", "km"):
        return qty, word
    if word == "trip" and category == "service" and canonical not in CAB_CANONICALS:
        return qty, "job"
    return None, None


def per_unit(price: float, unit: str | None, category: str, canonical: str = "") -> tuple[float | None, str | None]:
    divisor, basis = unit_basis(unit, category, canonical)
    if divisor is None:
        return None, None
    return round(price / divisor, 2), basis


def purchased_at(year_month: str | None, post_date: date | datetime | None) -> date | None:
    if year_month and re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", year_month):
        y, m = year_month.split("-")
        return date(int(y), int(m), 1)
    if isinstance(post_date, datetime):
        return post_date.date()
    return post_date


# --- prefilter and PII ------------------------------------------------------

PRICE_SIGNAL_RE = re.compile(
    r"₹|\brs\.?\s*\d|\d\s*rs\b|\binr\b|\d\s*/-|\b\d+(?:\.\d+)?\s?k\b|"
    r"\bpaid\b|\bcharged\b|\bcost me\b|\bbill(?:ed)?\b",
    re.I,
)


def has_price_signal(text: str) -> bool:
    return bool(text) and bool(PRICE_SIGNAL_RE.search(text))


_PII_PATTERNS = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
    (re.compile(r"(?:\+91[\s-]?|\b0)?\b[6-9]\d{4}[\s-]?\d{5}\b"), "[phone]"),
    # Indian vehicle registration: KA 01 AB 1234, KA-05-MX-1234, KA01AB1234
    (re.compile(r"\b[A-Z]{2}[\s-]?\d{1,2}[\s-]?[A-Z]{1,3}[\s-]?\d{4}\b"), "[reg]"),
    (re.compile(r"\b(?:order|invoice|bill|booking|txn|transaction)\s*(?:no\.?|number|id|#)\s*[:#]?\s*[A-Z0-9-]{4,}\b",
                re.I), "[order-no]"),
    (re.compile(r"(?<![\w/])/?u/[A-Za-z0-9_-]{3,20}"), "[user]"),
]


def scrub_pii(text: str | None) -> str | None:
    if not text:
        return text
    for pattern, repl in _PII_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def trim(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
