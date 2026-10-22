"""Source interface. Every source (Reddit now; Maps, YouTube, Instagram, app bills later)
turns one items.yaml item into prefiltered, PII-scrubbed RawPosts."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Protocol

from ..config import Item

BODY_LIMIT = 4000


@dataclass
class RawPost:
    id: str                     # "{source}:{native_id}", e.g. "reddit:t1_abc123"
    source: str                 # 'reddit' | later 'maps', 'youtube', 'instagram', 'bill'
    url: str
    item_key: str
    title: str | None
    body: str | None            # scrubbed, max BODY_LIMIT chars
    created_at: datetime | None
    subreddit: str | None = None
    author_hash: str | None = None


class SourceUnavailable(RuntimeError):
    """Credentials missing or rejected: skip this source for the run, don't crash."""


class Source(Protocol):
    name: str

    def fetch(self, item: Item, *, seen: Callable[[list[str]], set[str]] | None = None) -> list[RawPost]:
        """Return new RawPosts for the item. `seen(ids)` returns ids already stored."""
        ...
