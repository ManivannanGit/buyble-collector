"""Reddit source via the official OAuth API (CLAUDE.md section 6)."""
from __future__ import annotations

import hashlib
import logging
import random
import re
import time
from datetime import datetime, timezone
from typing import Callable

import httpx

from ..config import Catalog, Item, Settings
from ..normalise import has_price_signal, scrub_pii, trim
from .base import BODY_LIMIT, RawPost, SourceUnavailable

log = logging.getLogger(__name__)

API = "https://oauth.reddit.com"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
MIN_INTERVAL = 1.0          # 60 requests/minute
MAX_RETRIES = 5
THREADS_PER_ITEM = 8        # comment trees fetched per item
SKIP_AUTHORS = {"[deleted]", "AutoModerator"}
STOPWORDS = {"price", "prices", "cost", "costs", "best", "quality", "india", "rs", "fare", "charges",
             "change", "replacement", "repair", "per", "km", "10", "brand", "cafe", "the"}


def author_hash(author: str | None, salt: str) -> str | None:
    if not author or author in SKIP_AUTHORS:
        return None
    return hashlib.sha256(f"{salt}:{author}".encode()).hexdigest()


def relevance_terms(item: Item) -> set[str]:
    words = set(item.canonical.replace("-", " ").split())
    for q in item.queries:
        words |= set(q.lower().replace("-", " ").split())
    words -= STOPWORDS
    # 't shirt' / 'tshirt' / 't-shirt' all count
    if {"t", "shirt"} & words or "tshirt" in words:
        words |= {"tshirt", "t-shirt", "tee", "shirt"}
    words.discard("t")
    return words


def is_relevant(text: str, terms: set[str]) -> bool:
    text = text.lower()
    return any(re.search(rf"\b{re.escape(t)}", text) for t in terms)


class RedditSource:
    name = "reddit"

    def __init__(self, settings: Settings, catalog: Catalog, client: httpx.Client | None = None):
        if not settings.reddit_ready:
            raise SourceUnavailable("Reddit credentials missing in .env; skipping Reddit")
        if not settings.author_hash_salt:
            raise SourceUnavailable("AUTHOR_HASH_SALT missing in .env; skipping Reddit")
        self.s = settings
        self.catalog = catalog
        self.http = client or httpx.Client(timeout=30, headers={"User-Agent": settings.reddit_user_agent})
        self._token: str | None = None
        self._token_expiry = 0.0
        self._last_request = 0.0
        self._live_subs: dict[str, bool] = {}
        self.requests = 0

    # --- HTTP plumbing ------------------------------------------------------

    def _auth(self) -> str:
        if self._token and time.time() < self._token_expiry - 300:
            return self._token
        if self.s.reddit_refresh_token:
            data = {"grant_type": "refresh_token", "refresh_token": self.s.reddit_refresh_token}
        else:
            data = {"grant_type": "password", "username": self.s.reddit_username,
                    "password": self.s.reddit_password}
        r = self.http.post(TOKEN_URL, data=data, auth=(self.s.reddit_client_id, self.s.reddit_client_secret),
                           headers={"User-Agent": self.s.reddit_user_agent})
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code in (401, 403) or "access_token" not in body:
            raise SourceUnavailable(
                f"Reddit rejected the credentials (HTTP {r.status_code}, {body.get('error', 'no token')}); "
                "skipping Reddit"
            )
        self._token = body["access_token"]
        self._token_expiry = time.time() + float(body.get("expires_in", 3600))
        return self._token

    def _throttle(self) -> None:
        wait = MIN_INTERVAL - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def get(self, path: str, params: dict | None = None, *, allow_status: tuple[int, ...] = ()) -> httpx.Response:
        params = {"raw_json": 1, **(params or {})}
        for attempt in range(MAX_RETRIES + 1):
            self._throttle()
            r = self.http.get(API + path, params=params, headers={
                "Authorization": f"bearer {self._auth()}", "User-Agent": self.s.reddit_user_agent})
            self.requests += 1
            self._respect_ratelimit(r)
            if r.status_code in allow_status:
                return r
            if r.status_code == 401 and attempt == 0:
                self._token = None          # expired early; retry once with a fresh token
                continue
            if r.status_code in (401, 403):
                raise SourceUnavailable(f"Reddit returned HTTP {r.status_code} for {path}; skipping Reddit")
            if r.status_code == 429 or r.status_code >= 500:
                delay = min(60, 2 ** attempt) + random.uniform(0, 1)
                log.warning("reddit %s on %s, retrying in %.1fs", r.status_code, path, delay)
                time.sleep(delay)
                continue
            r.raise_for_status()
            return r
        raise RuntimeError(f"reddit: gave up on {path} after {MAX_RETRIES} retries")

    @staticmethod
    def _respect_ratelimit(r: httpx.Response) -> None:
        try:
            remaining = float(r.headers.get("x-ratelimit-remaining", "100"))
            reset = float(r.headers.get("x-ratelimit-reset", "0"))
        except ValueError:
            return
        if remaining < 5 and reset > 0:
            log.info("reddit rate limit low (%.0f left), sleeping %.0fs", remaining, reset)
            time.sleep(reset + 1)

    # --- subreddits -----------------------------------------------------------

    def subreddit_live(self, sub: str) -> bool:
        if sub.lower() not in self._live_subs:
            r = self.get(f"/r/{sub}/about", allow_status=(403, 404))
            ok = False
            if r.status_code == 200:
                d = r.json().get("data", {})
                ok = (d.get("subreddit_type") in ("public", "restricted") and not d.get("quarantine")
                      and (d.get("subscribers") or 0) >= 1000)
            if not ok:
                log.warning("r/%s is missing, private or tiny; dropping it", sub)
            self._live_subs[sub.lower()] = ok
        return self._live_subs[sub.lower()]

    # --- fetch ----------------------------------------------------------------

    def fetch(self, item: Item, *, seen: Callable[[list[str]], set[str]] | None = None) -> list[RawPost]:
        terms = relevance_terms(item)
        threads: dict[str, dict] = {}
        for sub in self.catalog.subreddits_for(item):
            if not self.subreddit_live(sub):
                continue
            for q in item.queries:
                r = self.get(f"/r/{sub}/search", {"q": q, "restrict_sr": 1, "sort": "new", "t": "year",
                                                  "limit": 25})
                for child in r.json().get("data", {}).get("children", []):
                    d = child.get("data", {})
                    if d.get("name") and is_relevant(f"{d.get('title', '')} {d.get('selftext', '')}", terms):
                        threads.setdefault(d["name"], d)

        posts: list[RawPost] = []
        for d in threads.values():
            if has_price_signal(f"{d.get('title', '')} {d.get('selftext', '')}"):
                posts.append(self._post(item, d, d.get("title"), d.get("selftext") or ""))

        # Price talk lives mostly in comments: read the busiest relevant threads.
        busiest = sorted(threads.values(), key=lambda d: d.get("num_comments", 0), reverse=True)
        for d in busiest[:THREADS_PER_ITEM]:
            if not d.get("num_comments"):
                continue
            r = self.get(f"/comments/{d['id']}", {"limit": 100, "depth": 3, "sort": "top"})
            listing = r.json()
            if not isinstance(listing, list) or len(listing) < 2:
                continue
            for c in _walk(listing[1]):
                if has_price_signal(c.get("body", "")):
                    posts.append(self._post(item, c, d.get("title"), c.get("body", ""), sub=d.get("subreddit")))

        unique = {p.id: p for p in posts}
        already = seen(list(unique)) if seen else set()
        fresh = [p for pid, p in unique.items() if pid not in already]
        fresh.sort(key=lambda p: p.created_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return fresh[: self.s.max_posts_per_item]

    def _post(self, item: Item, d: dict, title: str | None, body: str, sub: str | None = None) -> RawPost:
        created = d.get("created_utc")
        return RawPost(
            id=f"reddit:{d['name']}",
            source="reddit",
            url="https://www.reddit.com" + d.get("permalink", ""),
            item_key=item.key,
            title=trim(scrub_pii(title), 300),
            body=trim(scrub_pii(body), BODY_LIMIT),
            created_at=datetime.fromtimestamp(created, tz=timezone.utc) if created else None,
            subreddit=sub or d.get("subreddit"),
            author_hash=author_hash(d.get("author"), self.s.author_hash_salt),
        )


def _walk(listing: dict):
    """Yield comment dicts from a listing, depth-first; skips 'more' stubs."""
    for child in (listing or {}).get("data", {}).get("children", []):
        if child.get("kind") != "t1":
            continue
        d = child["data"]
        if d.get("author") not in SKIP_AUTHORS and d.get("body") not in (None, "[deleted]", "[removed]"):
            yield d
        if isinstance(d.get("replies"), dict):
            yield from _walk(d["replies"])
