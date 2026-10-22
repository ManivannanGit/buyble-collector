import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

from buyble.config import Settings, load_catalog
from buyble.extract import Extractor, clean_point, output_schema
from buyble.run import build_point

CAT = load_catalog()
TODAY = date(2026, 10, 22)
SETTINGS = Settings(None, "k", "claude-haiku-5-5", None, None, None, None, None, None, "salt", 3, 40, "Bengaluru")


def raw_point(**kw):
    p = {"snippet_id": "reddit:t1_a", "kind": "purchase", "line_kind": "part", "item": "TVS spark plug",
         "canonical": "spark plug", "attrs": [{"key": "origin", "value": "genuine_oem"},
                                              {"key": "channel", "value": "garage"},
                                              {"key": "origin", "value": "made_up"}],
         "attr_source": "buyer_stated", "price": 280, "unit": "1 pc", "city": "Bangalore", "quality": None,
         "worth": "fair", "known": False, "quote": "paid 280 for the plug, call 9876543210", "for_model": "TVS Jupiter",
         "purchased_year_month": "2026-09", "confidence": 0.9}
    p.update(kw)
    return p


class FakeClient:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kw):
        self.calls.append(kw)
        stop, payload = self.replies.pop(0)
        return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=json.dumps(payload))])


def snippets(n):
    return [{"id": f"reddit:t1_{i}", "title": "t", "body": "₹280", "subreddit": "IndianBikes",
             "created_at": datetime(2026, 10, 1, tzinfo=timezone.utc)} for i in range(n)]


def test_schema_has_no_open_objects():
    def walk(s):
        if isinstance(s, dict):
            if s.get("type") == "object":
                assert s["additionalProperties"] is False and set(s["required"]) == set(s["properties"])
            for v in s.values():
                walk(v)
        elif isinstance(s, list):
            for v in s:
                walk(v)
    walk(output_schema(CAT))


def test_clean_point_drops_invented_values():
    p = clean_point(raw_point(), CAT, {"reddit:t1_a"})
    assert p["attrs"] == {"origin": "genuine_oem", "channel": "garage"}
    assert p["category"] == "auto_parts" and "9876543210" not in p["quote"]
    assert clean_point(raw_point(snippet_id="other"), CAT, {"reddit:t1_a"}) is None
    assert clean_point(raw_point(canonical=""), CAT, {"reddit:t1_a"}) is None
    assert clean_point(raw_point(price=-5), CAT, {"reddit:t1_a"}) is None


def test_extractor_batches_and_respects_cap():
    replies = [("end_turn", {"points": [raw_point(snippet_id="reddit:t1_0")]})] + [("end_turn", {"points": []})] * 5
    client = FakeClient(replies)
    ex = Extractor(SETTINGS, CAT, max_calls=2, client=client)
    points, done = ex.extract(snippets(20), TODAY)
    assert ex.calls == 2 and len(done) == 16          # 3rd batch left for the next run
    assert len(points) == 1
    assert "Today's date: 2026-10-22" in client.calls[0]["messages"][0]["content"]
    assert client.calls[0]["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_refusal_batch_marked_done_without_points():
    ex = Extractor(SETTINGS, CAT, max_calls=5, client=FakeClient([("refusal", {"points": []})]))
    points, done = ex.extract(snippets(3), TODAY)
    assert points == [] and len(done) == 3 and ex.errors


def test_build_point_end_to_end():
    p = clean_point(raw_point(), CAT, {"reddit:t1_a"})
    raw = {"id": "reddit:t1_a", "source": "reddit", "url": "https://www.reddit.com/x", "author_hash": "h",
           "created_at": datetime(2026, 10, 1, tzinfo=timezone.utc)}
    pt = build_point(p, raw, CAT.items["spark-plug"], CAT, TODAY)
    assert pt["status"] == "staged" and pt["city"] == "Bengaluru"
    assert pt["price_per_unit"] == 280 and pt["unit_basis"] == "pc"
    assert pt["purchased_at"] == date(2026, 9, 1)
    assert 0.7 < pt["trust"] < 0.8
