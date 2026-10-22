from datetime import date, datetime

import pytest

from buyble.config import load_catalog
from buyble.normalise import (has_price_signal, normalise_city, parse_price, per_unit,
                              purchased_at, scrub_pii, trim)
from buyble.validate import cohort_median, trust, validate

TODAY = date(2026, 10, 22)
CAT = load_catalog()
SPARK = CAT.items["spark-plug"]


@pytest.mark.parametrize("text, expected", [
    ("1.2k", 1200), ("Rs. 450/-", 450), ("₹2,400", 2400), ("rs 280", 280), ("INR 99.50", 99.5),
    ("₹1,20,000", 120000), ("2K", 2000), ("paid 550 at the garage", 550), ("1.5 lakh", 150000),
    (250, 250), ("no price here", None), (None, None), (0, None),
])
def test_parse_price(text, expected):
    assert parse_price(text) == expected


@pytest.mark.parametrize("raw, expected", [
    ("Bangalore", "Bengaluru"), ("bengaluru", "Bengaluru"), ("BOMBAY", "Mumbai"), ("Madras", "Chennai"),
    ("Gurgaon", "Gurugram"), ("  pune ", "Pune"), ("mysuru", "Mysuru"), ("", None), (None, None),
])
def test_city_aliases(raw, expected):
    assert normalise_city(raw) == expected


@pytest.mark.parametrize("price, unit, cat, canon, expected", [
    (180, "1 cup", "food_drink", "hot chocolate", (180, "cup")),
    (500, "250 g", "food_drink", "hot chocolate", (200, "100g")),
    (560, "2 pcs", "auto_parts", "spark plug", (280, "pc")),
    (240, "12 km", "service", "city cab ride", (20, "km")),
    (800, None, "service", "scooter general service", (800, "job")),
    (900, "trip", "service", "airport cab", (None, None)),
    (300, None, "food_drink", "hot chocolate", (None, None)),
    (300, "some", "apparel", "t-shirt", (None, None)),
])
def test_per_unit(price, unit, cat, canon, expected):
    assert per_unit(price, unit, cat, canon) == expected


def test_purchased_at():
    assert purchased_at("2026-09", date(2026, 10, 1)) == date(2026, 9, 1)
    assert purchased_at("", datetime(2026, 10, 1, 5)) == date(2026, 10, 1)
    assert purchased_at("2026-13", date(2026, 10, 1)) == date(2026, 10, 1)


@pytest.mark.parametrize("text, hit", [
    ("paid ₹280 for a spark plug", True), ("it was 450rs", True), ("Rs.300", True), ("1.2k for the clutch", True),
    ("garage charged me", True), ("750/- only", True), ("great coffee here", False), ("ran 5km today", False),
])
def test_price_signal(text, hit):
    assert has_price_signal(text) is hit


def test_scrub_pii():
    s = scrub_pii("Call 98765 43210 or +91-9876543210, mail a.b@x.com, bike KA 05 MX 1234, "
                  "invoice no: INV20261011, thanks u/some_user")
    for leak in ("98765", "9876543210", "a.b@x.com", "KA 05", "INV2026", "some_user"):
        assert leak not in s
    assert "₹280" in scrub_pii("spark plug ₹280")


def test_trim():
    assert len(trim("x" * 400, 300)) == 300
    assert trim("short", 300) == "short"


def point(**kw):
    base = dict(price=280, price_per_unit=280, unit_basis="pc", confidence=0.9, attrs={"origin": "genuine_oem"},
                source="reddit", kind="purchase", purchased_at=TODAY, city="Bengaluru", known=False,
                attr_source="buyer_stated")
    base.update(kw)
    return base


def test_validate_ok():
    assert validate(point(), SPARK, today=TODAY, post_date=TODAY) == ("staged", None)


@pytest.mark.parametrize("kw, ctx, note", [
    ({"confidence": 0.3}, {}, "low confidence"),
    ({}, {"post_date": date(2023, 1, 1)}, "older than 3 years"),
    ({"price": 20, "price_per_unit": 20}, {}, "outside bounds"),
    ({"price": 5000, "price_per_unit": 2500, "unit": "2 pc"}, {}, "outside bounds"),
    ({}, {"duplicate": True}, "duplicate"),
])
def test_validate_rejects(kw, ctx, note):
    ctx = {"post_date": TODAY, **ctx}
    status, why = validate(point(**kw), SPARK, today=TODAY, **ctx)
    assert status == "rejected" and note in why


def test_outlier_stays_staged():
    approved = [point(price=p, price_per_unit=p) for p in (250, 280, 300)]
    med = cohort_median(point(), approved)
    assert med == 280
    status, note = validate(point(price=1500, price_per_unit=1500), SPARK, today=TODAY, post_date=TODAY,
                            median_price=med)
    assert status == "staged" and note.startswith("outlier")


def test_cohort_needs_three_and_same_attrs():
    approved = [point(), point(), point(attrs={"origin": "local_unbranded"})]
    assert cohort_median(point(), approved) is None
    approved.append(point(attrs={"origin": "genuine_oem", "channel": "garage"}))
    assert cohort_median(point(), approved) == 280


def test_trust():
    assert trust(point(), today=TODAY, target_city="Bengaluru") == 0.8
    year_old = point(purchased_at=date(2025, 10, 22))
    assert trust(year_old, today=TODAY, target_city="Bengaluru") == 0.4
    rich = point(known=True, attr_source="seller_stated", city="Chennai")
    assert trust(rich, today=TODAY, target_city="Bengaluru") == round(0.8 * 1.2 * 1.1 * 0.8, 4)
    assert trust(point(kind="asked"), today=TODAY, target_city="Bengaluru") == 0.5
    assert trust(point(source="bill"), today=TODAY, target_city="Bengaluru") == 1.2
