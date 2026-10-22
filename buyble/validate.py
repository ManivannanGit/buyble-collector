"""Sanity rules and trust scoring (CLAUDE.md section 8).

Pure functions: callers do the DB lookups and pass results in.
"""
from __future__ import annotations

from datetime import date
from statistics import median

from .config import Item

MIN_CONFIDENCE = 0.5
MAX_AGE_DAYS = 3 * 365
OUTLIER_HIGH, OUTLIER_LOW = 4.0, 0.25
MIN_COHORT = 3

SOURCE_TRUST = {"bill": 1.2, "buyer": 1.0, "reddit": 0.8}
ASKED_TRUST = 0.5
# Bases where bounds apply to the per-unit price; for others (100g, km, ...) bounds apply to the price paid.
PER_UNIT_BOUNDED = {"cup", "plate", "pc", "pair", "set", "job"}


def bounded_value(point: dict) -> float:
    if point.get("price_per_unit") and point.get("unit_basis") in PER_UNIT_BOUNDED:
        return float(point["price_per_unit"])
    return float(point["price"])


def comparable_value(point: dict) -> tuple[str | None, float]:
    if point.get("price_per_unit") is not None and point.get("unit_basis"):
        return point["unit_basis"], float(point["price_per_unit"])
    return None, float(point["price"])


def in_cohort(candidate_attrs: dict, point_attrs: dict) -> bool:
    """Same cohort when the candidate matches every fact the point states."""
    return all(candidate_attrs.get(k) == v for k, v in (point_attrs or {}).items())


def cohort_median(point: dict, approved: list[dict]) -> float | None:
    basis, _ = comparable_value(point)
    values = [
        comparable_value(r)[1]
        for r in approved
        if in_cohort(r.get("attrs") or {}, point.get("attrs") or {}) and comparable_value(r)[0] == basis
    ]
    return median(values) if len(values) >= MIN_COHORT else None


def validate(point: dict, item: Item, *, today: date, post_date: date | None,
             median_price: float | None = None, duplicate: bool = False) -> tuple[str, str | None]:
    """Return (status, review_note). Rejections always carry a reason."""
    conf = point.get("confidence")
    if conf is None or conf < MIN_CONFIDENCE:
        return "rejected", f"low confidence ({conf})"
    if post_date and (today - post_date).days > MAX_AGE_DAYS:
        return "rejected", f"post older than 3 years ({post_date.isoformat()})"
    lo, hi = item.bounds
    value = bounded_value(point)
    if not lo <= value <= hi:
        return "rejected", f"price {value:g} outside bounds {lo:g}-{hi:g}"
    if duplicate:
        return "rejected", "duplicate: same author, item and price within 30 days"
    if median_price:
        _, v = comparable_value(point)
        ratio = v / median_price
        if ratio > OUTLIER_HIGH or ratio < OUTLIER_LOW:
            return "staged", f"outlier: {ratio:.2f}x cohort median {median_price:g}"
    return "staged", None


def trust(point: dict, *, today: date, target_city: str) -> float:
    if point.get("kind") == "asked":
        score = ASKED_TRUST
    else:
        score = SOURCE_TRUST.get(point.get("source"), 1.0)
    when = point.get("purchased_at") or today
    age_days = max((today - when).days, 0)
    score *= 0.5 ** (age_days / 365)
    if point.get("known"):
        score *= 1.2
    if point.get("attr_source") in ("verified", "seller_stated"):
        score *= 1.1
    if point.get("city") != target_city:
        score *= 0.8
    return round(score, 4)
