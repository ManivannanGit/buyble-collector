"""One full collection run (CLAUDE.md section 12).

    python -m buyble.run                      # all items, writes to the DB
    python -m buyble.run --item spark-plug --dry-run
    python -m buyble.run --max-llm 20
"""
from __future__ import annotations

import argparse
import logging
import sys
from contextlib import ExitStack
from datetime import date, datetime, timezone

from . import db
from .config import Catalog, ConfigError, Item, Settings, load_catalog, load_settings
from .extract import Extractor
from .normalise import normalise_city, per_unit, purchased_at
from .sources.base import RawPost, SourceUnavailable
from .sources.reddit import RedditSource
from .validate import cohort_median, trust, validate

log = logging.getLogger("buyble.run")


def build_point(p: dict, raw: dict, item: Item, catalog: Catalog, today: date, conn=None) -> dict:
    """Normalise, validate and score one extracted point into a `points` row."""
    city = normalise_city(p["city"])
    post_date = raw["created_at"].date() if raw.get("created_at") else None
    ppu, basis = per_unit(p["price"], p["unit"], item.category, item.canonical)
    point = {
        **{k: p[k] for k in ("kind", "line_kind", "item", "canonical", "category", "attrs", "attr_source", "price",
                             "unit", "quality", "worth", "known", "quote", "for_model", "confidence")},
        "source": raw["source"],
        "raw_post_id": raw["id"],
        "url": raw["url"],
        "currency": "INR",
        "price_per_unit": ppu,
        "unit_basis": basis,
        "city": city,
        "author_hash": raw.get("author_hash"),
        "purchased_at": purchased_at(p["purchased_year_month"], post_date) or today,
    }
    median_price, duplicate = None, False
    if conn is not None:
        median_price = cohort_median(point, db.approved_prices(conn, item.canonical))
        if point["author_hash"]:
            duplicate = db.recent_author_point(conn, point["author_hash"], item.canonical, point["price"],
                                               point["purchased_at"])
    point["status"], point["review_note"] = validate(point, item, today=today, post_date=post_date,
                                                     median_price=median_price, duplicate=duplicate)
    point["trust"] = trust(point, today=today, target_city=catalog.city)
    return point


def _raw_dict(p: RawPost) -> dict:
    return {"id": p.id, "source": p.source, "url": p.url, "subreddit": p.subreddit, "author_hash": p.author_hash,
            "created_at": p.created_at, "title": p.title, "body": p.body, "item_key": p.item_key}


def print_snippets(posts: list[RawPost], n: int = 5) -> None:
    print(f"\n--- {len(posts)} snippets with a price signal; first {min(n, len(posts))} ---")
    for p in posts[:n]:
        when = p.created_at.date().isoformat() if p.created_at else "?"
        body = (p.body or "").replace("\n", " ")
        print(f"\n[{p.id}] r/{p.subreddit} {when}\n  thread: {p.title}\n  text:   {body[:280]}"
              f"{'…' if len(body) > 280 else ''}\n  link:   {p.url}")


def print_points(points: list[dict]) -> None:
    print(f"\n--- {len(points)} extracted points ---")
    for p in points:
        attrs = ", ".join(f"{k}={v}" for k, v in p["attrs"].items()) or "-"
        ppu = f" ({p['price_per_unit']:g}/{p['unit_basis']})" if p.get("price_per_unit") else ""
        note = f"  [{p['review_note']}]" if p.get("review_note") else ""
        print(f"\n{p['status'].upper():8} {p['canonical']}: ₹{p['price']:g} for {p['unit'] or '?'}{ppu}"
              f"  {p['kind']}/{p['line_kind']}  conf {p['confidence']}  trust {p['trust']}{note}\n"
              f"  attrs: {attrs} ({p['attr_source']})  city: {p['city'] or '-'}  model: {p['for_model'] or '-'}"
              f"  quality: {p['quality'] or '-'}  worth: {p['worth'] or '-'}  known: {p['known']}\n"
              f"  quote: \"{p['quote']}\"\n  {p['url']}")


def run(settings: Settings, catalog: Catalog, *, item_keys: list[str], dry_run: bool, max_llm: int,
        show: int = 5) -> dict:
    today = datetime.now(timezone.utc).date()
    stats = {"posts_fetched": 0, "llm_calls": 0, "llm_cost_usd": 0.0, "points_staged": 0, "points_rejected": 0, "errors": []}
    with ExitStack() as stack:
        conn = stack.enter_context(db.connect(settings.database_url)) if settings.database_url else None
        run_id = db.start_run(conn) if conn and not dry_run else None
        _run(settings, catalog, conn, run_id, stats, item_keys=item_keys, dry_run=dry_run, max_llm=max_llm,
             show=show, today=today)
    stats["run_id"] = run_id
    return stats


def _run(settings, catalog, conn, run_id, stats, *, item_keys, dry_run, max_llm, show, today) -> None:
    try:
        # 1-2. fetch -> prefilter -> raw_posts
        fetched: list[RawPost] = []
        try:
            reddit = RedditSource(settings, catalog)
            seen = (lambda ids: db.existing_raw_ids(conn, ids)) if conn else None
            for key in item_keys:
                posts = reddit.fetch(catalog.items[key], seen=seen)
                log.info("%s: %d new snippets", key, len(posts))
                fetched.extend(posts)
                if conn and not dry_run:
                    db.insert_raw_posts(conn, posts)
            log.info("reddit: %d requests", reddit.requests)
        except SourceUnavailable as e:
            log.error("%s", e)
            stats["errors"].append(str(e))
        stats["posts_fetched"] = len(fetched)
        if dry_run:
            print_snippets(fetched, show)

        # 3. extract
        if dry_run or not conn:
            pending = [_raw_dict(p) for p in fetched]
        else:
            pending = db.unextracted_posts(conn, item_keys)
        if not pending:
            log.info("nothing to extract")
        elif not settings.anthropic_api_key:
            msg = "ANTHROPIC_API_KEY missing in .env; skipping extraction"
            log.error(msg)
            stats["errors"].append(msg)
        else:
            extractor = Extractor(settings, catalog, max_calls=max_llm, max_usd=settings.max_llm_usd_per_run)
            extracted, done_ids = extractor.extract(pending, today)
            stats["llm_calls"] = extractor.calls
            stats["llm_cost_usd"] = round(extractor.cost_usd, 4)
            stats["llm_tokens"] = (extractor.tokens_in, extractor.tokens_out)
            stats["errors"].extend(extractor.errors)

            # 4. normalise -> validate -> score -> points
            by_id = {r["id"]: r for r in pending}
            built = []
            for p in extracted:
                raw = by_id[p["snippet_id"]]
                item = catalog.by_canonical(p["canonical"])
                built.append(build_point(p, raw, item, catalog, today, conn))
            if dry_run:
                print_points(built)
            else:
                for point in built:
                    if db.insert_point(conn, point):
                        stats["points_staged" if point["status"] == "staged" else "points_rejected"] += 1
                db.mark_extracted(conn, done_ids)
                conn.commit()
            if dry_run:
                stats["points_staged"] = sum(p["status"] == "staged" for p in built)
                stats["points_rejected"] = sum(p["status"] == "rejected" for p in built)

        # 5. purge old bodies
        if conn and not dry_run:
            stats["bodies_purged"] = db.purge_old_bodies(conn)
    except Exception as e:
        stats["errors"].append(f"{type(e).__name__}: {e}")
        raise
    finally:
        # 6. finish the run row
        if run_id is not None:
            try:
                conn.rollback()          # no-op unless something failed mid-transaction
                db.finish_run(conn, run_id, posts_fetched=stats["posts_fetched"], llm_calls=stats["llm_calls"],
                              points_staged=stats["points_staged"], errors=stats["errors"],
                              llm_cost_usd=stats["llm_cost_usd"])
            except Exception:
                log.exception("could not record the run summary")


def print_summary(stats: dict, dry_run: bool) -> None:
    print("\n=== Buyble run summary" + (" (dry run, nothing written)" if dry_run else f" #{stats['run_id']}") + " ===")
    print(f"  snippets fetched : {stats['posts_fetched']}")
    print(f"  LLM calls        : {stats['llm_calls']}")
    tin, tout = stats.get("llm_tokens", (0, 0))
    print(f"  LLM cost (est.)  : ${stats['llm_cost_usd']:.4f} (~₹{stats['llm_cost_usd'] * 88:.2f}); "
          f"{tin} in / {tout} out tokens")
    print(f"  points staged    : {stats['points_staged']}")
    print(f"  points rejected  : {stats['points_rejected']}")
    if "bodies_purged" in stats:
        print(f"  old bodies purged: {stats['bodies_purged']}")
    print(f"  errors           : {len(stats['errors'])}")
    for e in stats["errors"][:10]:
        print(f"    - {e}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m buyble.run")
    ap.add_argument("--item", action="append", help="items.yaml key (repeatable); default: all")
    ap.add_argument("--dry-run", action="store_true", help="no DB writes; print what would be stored")
    ap.add_argument("--max-llm", type=int, help="override MAX_LLM_CALLS_PER_RUN")
    ap.add_argument("--show", type=int, default=5, help="snippets to print in a dry run")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings, catalog = load_settings(), load_catalog()
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    keys = args.item or list(catalog.items)
    unknown = [k for k in keys if k not in catalog.items]
    if unknown:
        print(f"unknown item(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    if not args.dry_run and not settings.database_url:
        print("DATABASE_URL missing in .env (use --dry-run to test without a database)", file=sys.stderr)
        return 2
    max_llm = args.max_llm or settings.max_llm_calls_per_run
    stats = run(settings, catalog, item_keys=keys, dry_run=args.dry_run, max_llm=max_llm, show=args.show)
    print_summary(stats, args.dry_run)
    # Skipped sources are reported in the summary and the runs table; only crashes exit non-zero.
    return 0


if __name__ == "__main__":
    sys.exit(main())
