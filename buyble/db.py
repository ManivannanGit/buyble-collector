"""Postgres access (psycopg 3).

The Supabase transaction pooler (port 6543) does not support prepared
statements, so every connection sets prepare_threshold=None.

    python -m buyble.db --apply-schema   # create tables (idempotent)
    python -m buyble.db --check          # list tables and row counts
"""
from __future__ import annotations

import argparse
import sys
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .config import ROOT, ConfigError, load_settings

TABLES = ("raw_posts", "points", "attr_maps", "golden", "runs")

POINT_COLUMNS = (
    "status", "source", "raw_post_id", "url", "kind", "line_kind", "item", "canonical", "category",
    "attrs", "attr_source", "price", "currency", "unit", "price_per_unit", "unit_basis", "city",
    "quality", "worth", "known", "quote", "for_model", "author_hash", "purchased_at", "trust",
    "confidence", "review_note",
)


@contextmanager
def connect(url: str | None = None) -> Iterator[psycopg.Connection]:
    if url is None:
        settings = load_settings()
        settings.require("DATABASE_URL")
        url = settings.database_url
    with psycopg.connect(url, prepare_threshold=None, row_factory=dict_row, connect_timeout=15) as conn:
        yield conn


def apply_schema(conn: psycopg.Connection) -> None:
    conn.execute((ROOT / "schema.sql").read_text())
    conn.commit()


def table_counts(conn: psycopg.Connection) -> dict[str, int | None]:
    present = {
        r["table_name"]
        for r in conn.execute(
            "select table_name from information_schema.tables where table_schema = 'public'"
        )
    }
    return {
        t: conn.execute(f"select count(*) as n from {t}").fetchone()["n"] if t in present else None
        for t in TABLES
    }


# --- raw_posts -------------------------------------------------------------

def existing_raw_ids(conn: psycopg.Connection, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    rows = conn.execute("select id from raw_posts where id = any(%s)", (ids,))
    return {r["id"] for r in rows}


def insert_raw_posts(conn: psycopg.Connection, posts: list) -> int:
    n = 0
    for p in posts:
        cur = conn.execute(
            """insert into raw_posts (id, source, url, subreddit, author_hash, created_at, title, body, item_key)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict (id) do nothing""",
            (p.id, p.source, p.url, p.subreddit, p.author_hash, p.created_at, p.title, p.body, p.item_key),
        )
        n += cur.rowcount
    conn.commit()
    return n


def unextracted_posts(conn: psycopg.Connection, item_keys: list[str] | None = None) -> list[dict]:
    sql = "select * from raw_posts where not extracted and body is not null"
    args: tuple = ()
    if item_keys:
        sql += " and item_key = any(%s)"
        args = (item_keys,)
    return list(conn.execute(sql + " order by fetched_at, id", args))


def mark_extracted(conn: psycopg.Connection, ids: list[str]) -> None:
    if ids:
        conn.execute("update raw_posts set extracted = true where id = any(%s)", (ids,))
        conn.commit()


def purge_old_bodies(conn: psycopg.Connection, days: int = 30) -> int:
    cur = conn.execute(
        "update raw_posts set body = null where body is not null and fetched_at < now() - make_interval(days => %s)",
        (days,),
    )
    conn.commit()
    return cur.rowcount


# --- points ----------------------------------------------------------------

def insert_point(conn: psycopg.Connection, point: dict) -> bool:
    """Insert one point; returns False if it hit the dedupe index."""
    values = [Jsonb(point.get(c) or {}) if c == "attrs" else point.get(c) for c in POINT_COLUMNS]
    cur = conn.execute(
        f"insert into points ({', '.join(POINT_COLUMNS)}) values ({', '.join(['%s'] * len(POINT_COLUMNS))})"
        " on conflict (raw_post_id, canonical, price) do nothing",
        values,
    )
    return cur.rowcount == 1


def recent_author_point(conn: psycopg.Connection, author_hash: str, canonical: str, price: float,
                        when, days: int = 30) -> bool:
    row = conn.execute(
        """select 1 from points where author_hash = %s and canonical = %s and price = %s
           and status <> 'rejected'
           and abs(purchased_at - %s::date) <= %s limit 1""",
        (author_hash, canonical, price, when, days),
    ).fetchone()
    return row is not None


def approved_prices(conn: psycopg.Connection, canonical: str) -> list[dict]:
    return list(conn.execute(
        "select price, price_per_unit, unit_basis, attrs, trust from points where canonical = %s and status = 'approved'",
        (canonical,),
    ))


# --- runs ------------------------------------------------------------------

def start_run(conn: psycopg.Connection, kind: str = "collect") -> int:
    rid = conn.execute("insert into runs (kind) values (%s) returning id", (kind,)).fetchone()["id"]
    conn.commit()
    return rid


def finish_run(conn: psycopg.Connection, run_id: int, *, posts_fetched=0, llm_calls=0, points_staged=0,
               errors=(), golden_accuracy=None, llm_cost_usd=None) -> None:
    conn.execute(
        """update runs set finished_at = now(), posts_fetched = %s, llm_calls = %s, points_staged = %s,
           errors = %s, golden_accuracy = %s, llm_cost_usd = %s where id = %s""",
        (posts_fetched, llm_calls, points_staged, Jsonb(list(errors)), golden_accuracy, llm_cost_usd, run_id),
    )
    conn.commit()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m buyble.db")
    ap.add_argument("--apply-schema", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    try:
        with connect() as conn:
            if args.apply_schema:
                apply_schema(conn)
                print("schema applied")
            for table, n in table_counts(conn).items():
                print(f"  {table:<10} {'MISSING' if n is None else f'{n} rows'}")
    except ConfigError as e:
        print(f"error: {e}. Fill it in with: nano .env", file=sys.stderr)
        return 2
    except psycopg.OperationalError as e:
        # The message can include the host but never the password.
        print(f"error: could not connect to the database ({type(e).__name__}). "
              "Check DATABASE_URL uses the Transaction pooler string (port 6543).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
