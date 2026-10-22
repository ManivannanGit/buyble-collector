from datetime import date, datetime, timezone

from buyble.config import load_catalog
from buyble.export import ATTR_COPY, app_point, attr_maps
from buyble.golden import judge, resolve_canonical, verdict, weighted_percentile

CAT = load_catalog()


def test_weighted_percentile_and_verdicts():
    cohort = [(100, 1), (200, 1), (300, 1), (400, 1)]
    assert weighted_percentile(50, cohort) == 0
    assert weighted_percentile(200, cohort) == 37.5
    assert weighted_percentile(500, cohort) == 100
    assert weighted_percentile(250, [(100, 3), (400, 1)]) == 75
    assert [verdict(x) for x in (25, 26, 65, 66, 85, 86)] == \
        ["good_deal", "fair", "fair", "pricey", "pricey", "overpriced"]


def ap(price, attrs=None, trust=1.0, ppu=None, basis=None):
    return {"price": price, "price_per_unit": ppu, "unit_basis": basis, "attrs": attrs or {}, "trust": trust}


def test_judge_cohort_rules():
    approved = [ap(200, {"origin": "local_unbranded"}), ap(250, {"origin": "local_unbranded", "channel": "garage"}),
                ap(300, {"origin": "local_unbranded"}), ap(900, {"origin": "genuine_oem"})]
    row = {"price": 280, "attrs": {"origin": "local_unbranded"}}
    v, pct, n = judge(row, approved)
    assert n == 3 and v == "pricey" and round(pct) == 67
    assert judge({"price": 280, "attrs": {"origin": "genuine_oem"}}, approved)[0] is None   # too thin
    # per-unit price is used for countable units
    assert judge({"price": 280, "attrs": {}}, [ap(560, ppu=280, basis="pc")] * 3)[1] == 50


def test_resolve_canonical():
    assert resolve_canonical("Spark Plug", CAT) == "spark plug"
    assert resolve_canonical("spark-plug", CAT) == "spark plug"
    assert resolve_canonical("toaster", CAT) is None


def test_app_point_is_safe_to_share():
    row = {"item": "Spark plug", "canonical": "spark plug", "category": "auto_parts", "kind": "purchase",
           "line_kind": "part", "attrs": {"origin": "genuine_oem"}, "attr_source": "buyer_stated", "source": "reddit",
           "price": 280.0, "unit": "1 pc", "city": "Bengaluru", "quality": 4, "worth": "fair", "known": False,
           "quote": "q", "url": "https://reddit.com/x", "for_model": "TVS Jupiter", "purchased_at": date(2026, 9, 1),
           "created_at": datetime(2026, 10, 22, 6, tzinfo=timezone.utc), "trust": 0.7412,
           "author_hash": "SECRET", "raw_post_id": "reddit:t1_x", "id": 1}
    out = app_point(row)
    assert out["purchasedAt"] == "2026-09-01T12:00:00Z" and out["createdAt"] == "2026-10-22T06:00:00Z"
    assert out["price"] == 280 and out["trust"] == 0.74 and out["for"] == "TVS Jupiter"
    assert "SECRET" not in repr(out) and "author_hash" not in out and "body" not in out


def test_every_attr_value_has_copy():
    for m in attr_maps(CAT):
        assert m["approved"] is True
        for a in m["attrs"]:
            assert a["key"] in ATTR_COPY, a["key"]
            for o in a["options"]:
                assert o["note"], f"{a['key']}={o['value']} has no note"
