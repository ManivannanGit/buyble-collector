"""Golden-set accuracy check (CLAUDE.md section 11).

    python -m buyble.golden                      # judge every golden row, print accuracy and misses
    python -m buyble.golden add --item "spark plug" --price 280 --expected fair \\
        --attrs origin=local_unbranded channel=garage --note "my scooter service bill"
    python -m buyble.golden list
"""
from __future__ import annotations

import argparse
import sys

from psycopg.types.json import Jsonb

from . import db
from .config import Catalog, ConfigError, load_catalog
from .normalise import normalise_city, parse_price
from .validate import PER_UNIT_BOUNDED, in_cohort

VERDICTS = ("good_deal", "fair", "pricey", "overpriced")
MIN_POINTS = 3


def weighted_percentile(price: float, cohort: list[tuple[float, float]]) -> float:
    """Percent of trust weight priced below `price` (ties count half)."""
    total = sum(w for _, w in cohort)
    if total <= 0:
        return 50.0
    below = sum(w for p, w in cohort if p < price)
    equal = sum(w for p, w in cohort if p == price)
    return 100 * (below + equal / 2) / total


def verdict(percentile: float) -> str:
    if percentile <= 25:
        return "good_deal"
    if percentile <= 65:
        return "fair"
    if percentile <= 85:
        return "pricey"
    return "overpriced"


def point_value(p: dict) -> float:
    """Compare a single-unit golden price against per-unit prices where the unit is countable."""
    if p.get("price_per_unit") is not None and p.get("unit_basis") in PER_UNIT_BOUNDED:
        return float(p["price_per_unit"])
    return float(p["price"])


def judge(row: dict, approved: list[dict]) -> tuple[str | None, float | None, int]:
    """Return (verdict or None if too thin, percentile, cohort size)."""
    cohort = [(point_value(p), float(p["trust"])) for p in approved if in_cohort(p["attrs"] or {}, row["attrs"] or {})]
    if len(cohort) < MIN_POINTS:
        return None, None, len(cohort)
    pct = weighted_percentile(float(row["price"]), cohort)
    return verdict(pct), pct, len(cohort)


def resolve_canonical(name: str, catalog: Catalog) -> str | None:
    name = name.strip().lower()
    if catalog.by_canonical(name):
        return name
    if name in catalog.items:
        return catalog.items[name].canonical
    return None


def evaluate(conn, catalog: Catalog) -> dict:
    rows = list(conn.execute("select * from golden order by id"))
    approved_by: dict[str, list[dict]] = {}
    results = []
    for row in rows:
        canonical = resolve_canonical(row["item"], catalog)
        if canonical is None:
            results.append((row, None, None, 0, "unknown item"))
            continue
        if canonical not in approved_by:
            approved_by[canonical] = db.approved_prices(conn, canonical)
        v, pct, n = judge(row, approved_by[canonical])
        results.append((row, v, pct, n, None if v else "too thin"))
    judged = [r for r in results if r[1]]
    hits = sum(1 for r in judged if r[1] == r[0]["expected"])
    return {"rows": len(rows), "judged": len(judged), "hits": hits,
            "accuracy": hits / len(judged) if judged else None, "results": results}


def print_report(report: dict) -> None:
    acc = report["accuracy"]
    print(f"golden rows: {report['rows']}   judged: {report['judged']}   "
          f"accuracy: {'n/a' if acc is None else f'{acc:.0%}'} ({report['hits']}/{report['judged']})")
    misses = [r for r in report["results"] if r[1] != r[0]["expected"]]
    if not misses:
        return
    print(f"\n{'id':>4}  {'item':<24} {'price':>8}  {'expected':<11} {'got':<11} {'pct':>5} {'n':>3}  attrs")
    for row, v, pct, n, why in misses:
        attrs = ",".join(f"{k}={x}" for k, x in (row["attrs"] or {}).items()) or "-"
        print(f"{row['id']:>4}  {row['item'][:24]:<24} {float(row['price']):>8g}  {row['expected']:<11} "
              f"{(v or why):<11} {'' if pct is None else f'{pct:.0f}':>5} {n:>3}  {attrs}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m buyble.golden")
    sub = ap.add_subparsers(dest="cmd")
    add = sub.add_parser("add", help="add a golden row from your own bill")
    add.add_argument("--item", required=True, help="canonical name or items.yaml key")
    add.add_argument("--price", required=True)
    add.add_argument("--expected", required=True, choices=VERDICTS)
    add.add_argument("--city", default="Bengaluru")
    add.add_argument("--attrs", nargs="*", default=[], metavar="KEY=VALUE")
    add.add_argument("--note")
    sub.add_parser("list")
    args = ap.parse_args(argv)
    try:
        catalog = load_catalog()
        with db.connect() as conn:
            if args.cmd == "add":
                canonical = resolve_canonical(args.item, catalog)
                if canonical is None:
                    print(f"unknown item {args.item!r}", file=sys.stderr)
                    return 2
                allowed = catalog.allowed_attrs(catalog.by_canonical(canonical))
                attrs = {}
                for kv in args.attrs:
                    k, _, v = kv.partition("=")
                    if k not in allowed or v not in allowed[k]:
                        print(f"bad attr {kv!r}; allowed: "
                              + "; ".join(f"{a}={'|'.join(vs)}" for a, vs in allowed.items()), file=sys.stderr)
                        return 2
                    attrs[k] = v
                price = parse_price(args.price)
                if not price:
                    print("price must be a positive number", file=sys.stderr)
                    return 2
                gid = conn.execute(
                    "insert into golden (item, price, city, attrs, expected, note) values (%s,%s,%s,%s,%s,%s) returning id",
                    (canonical, price, normalise_city(args.city), Jsonb(attrs), args.expected, args.note),
                ).fetchone()["id"]
                conn.commit()
                print(f"added golden row #{gid}")
                return 0
            if args.cmd == "list":
                for r in conn.execute("select * from golden order by id"):
                    print(f"#{r['id']:<4} {r['item']:<24} ₹{float(r['price']):g}  {r['expected']:<11} "
                          f"{r['attrs'] or {}}  {r['note'] or ''}")
                return 0
            report = evaluate(conn, catalog)
            print_report(report)
            run_id = db.start_run(conn, kind="golden")
            db.finish_run(conn, run_id, golden_accuracy=report["accuracy"])
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
