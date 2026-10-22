import httpx
import pytest

from buyble.config import Settings, load_catalog
from buyble.sources import reddit as rmod
from buyble.sources.base import SourceUnavailable
from buyble.sources.reddit import RedditSource, author_hash

CAT = load_catalog()


def settings(**kw):
    base = dict(database_url=None, anthropic_api_key=None, extract_model="m", reddit_client_id="id",
                reddit_client_secret="sec", reddit_username="u", reddit_password="p", reddit_refresh_token=None,
                reddit_user_agent="test-agent", author_hash_salt="salt", max_llm_calls_per_run=10,
                max_posts_per_item=40, default_city="Bengaluru")
    base.update(kw)
    return Settings(**base)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(rmod.time, "sleep", lambda s: None)
    monkeypatch.setattr(rmod, "MIN_INTERVAL", 0)


def post(name, title, selftext="", num_comments=0, author="alice", sub="IndianBikes"):
    return {"kind": "t3", "data": {"name": name, "id": name[3:], "title": title, "selftext": selftext,
                                   "num_comments": num_comments, "author": author, "subreddit": sub,
                                   "permalink": f"/r/{sub}/comments/{name[3:]}/x/", "created_utc": 1790000000}}


def comment(name, body, author="bob", replies=""):
    return {"kind": "t1", "data": {"name": name, "body": body, "author": author, "replies": replies,
                                   "permalink": f"/r/IndianBikes/comments/p1/x/{name[3:]}/",
                                   "created_utc": 1790000100}}


def make_handler(calls):
    def handler(req: httpx.Request):
        calls.append(req)
        assert req.headers["user-agent"] == "test-agent"
        if req.url.path == "/api/v1/access_token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if req.url.path.endswith("/about"):
            sub = req.url.path.split("/")[2]
            if sub == "indianbikes" or sub == "IndianBikes":
                return httpx.Response(200, json={"data": {"subreddit_type": "public", "subscribers": 50000}})
            return httpx.Response(404, json={})
        if req.url.path.endswith("/search"):
            return httpx.Response(200, json={"data": {"children": [
                post("t3_p1", "Spark plug for Jupiter?", "", num_comments=3),
                post("t3_p2", "Paid ₹280 for spark plug at garage", "genuine one"),
                post("t3_p3", "Cat pictures", "₹50 treats"),       # not relevant
            ]}})
        if req.url.path == "/comments/p1":
            reply = {"kind": "Listing", "data": {"children": [comment("t1_c2", "Garage charged me 350 rs, call 9876543210")]}}
            return httpx.Response(200, json=[{}, {"data": {"children": [
                comment("t1_c1", "Genuine TVS plug was Rs. 280/- at the service centre", replies=reply),
                comment("t1_c3", "Just ride it, it's fine"),
                comment("t1_c4", "₹100", author="AutoModerator"),
                {"kind": "more", "data": {}},
            ]}}])
        return httpx.Response(404)
    return handler


def test_fetch_prefilters_scrubs_and_dedupes():
    calls = []
    client = httpx.Client(transport=httpx.MockTransport(make_handler(calls)))
    src = RedditSource(settings(), CAT, client=client)
    out = src.fetch(CAT.items["spark-plug"], seen=lambda ids: {"reddit:t3_p2"} & set(ids))
    ids = {p.id for p in out}
    assert ids == {"reddit:t1_c1", "reddit:t1_c2"}
    c2 = next(p for p in out if p.id == "reddit:t1_c2")
    assert "9876543210" not in c2.body and "[phone]" in c2.body
    assert c2.author_hash == author_hash("bob", "salt") and "bob" not in repr(c2)
    assert c2.url.startswith("https://www.reddit.com/r/IndianBikes/")
    # only one live subreddit searched, case-deduped; dead ones dropped
    searches = [c for c in calls if c.url.path.endswith("/search")]
    assert {c.url.path for c in searches} == {"/r/IndianBikes/search"}
    assert all(c.url.params["restrict_sr"] == "1" and c.url.params["sort"] == "new" for c in searches)
    token_calls = [c for c in calls if c.url.path == "/api/v1/access_token"]
    assert len(token_calls) == 1


def test_missing_credentials_raise_source_unavailable():
    with pytest.raises(SourceUnavailable):
        RedditSource(settings(reddit_client_id=None), CAT)


def test_rejected_credentials_raise_source_unavailable():
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(401, json={"error": "invalid_grant"})))
    src = RedditSource(settings(), CAT, client=client)
    with pytest.raises(SourceUnavailable, match="rejected"):
        src.fetch(CAT.items["spark-plug"])


def test_retries_on_429():
    state = {"n": 0}

    def handler(req):
        if req.url.path == "/api/v1/access_token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        state["n"] += 1
        if state["n"] < 3:
            return httpx.Response(429)
        return httpx.Response(200, json={"ok": True})

    src = RedditSource(settings(), CAT, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert src.get("/x").json() == {"ok": True}
    assert state["n"] == 3
