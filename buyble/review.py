"""Review staged points in the terminal (CLAUDE.md section 9).

    python -m buyble.review                         # a approve, r reject, e edit, s skip, q quit
    python -m buyble.review --item "spark plug"     # one canonical only
    python -m buyble.review --spot-check            # re-check 10% of last week's auto-approved points
    python -m buyble.review --auto-approve-above 0.85   # off by default; ask the founder first
"""
from __future__ import annotations

import argparse
import random
import sys

from psycopg.types.json import Jsonb

from . import db
from .config import ConfigError, load_catalog
from .extract import KINDS, LINE_KINDS, WORTH
from .normalise import normalise_city, parse_price, per_unit

AUTO_NOTE = "auto-approved"
EDITABLE = ("price", "unit", "city", "attrs", "kind", "line_kind", "quality", "worth", "known", "for_model", "item")


def staged(conn, canonical: str | None = None) -> list[dict]:
    sql = "select * from points where status = 'staged'"
    args: tuple = ()
    if canonical:
        sql += " and canonical = %s"
        args = (canonical,)
    return list(conn.execute(sql + " order by canonical, created_at, id", args))


def show(p: dict, i: int, n: int) -> None:
    attrs = ", ".join(f"{k}={v}" for k, v in (p["attrs"] or {}).items()) or "-"
    ppu = f"  ({p['price_per_unit']:g}/{p['unit_basis']})" if p["price_per_unit"] is not None else ""
    print(f"\n[{i}/{n}] #{p['id']}  {p['canonical']}  ({p['category']})")
    print(f"  item     : {p['item']}")
    print(f"  price    : ₹{p['price']:g} for {p['unit'] or '?'}{ppu}   {p['kind']}/{p['line_kind'] or '-'}")
    print(f"  attrs    : {attrs}  [{p['attr_source']}]")
    print(f"  channel  : {(p['attrs'] or {}).get('channel', '-')}   city: {p['city'] or '-'}"
          f"   model: {p['for_model'] or '-'}")
    print(f"  judged   : quality {p['quality'] or '-'}  worth {p['worth'] or '-'}  known {p['known']}")
    print(f"  conf     : {p['confidence']}   trust {p['trust']}"
          + (f"   NOTE: {p['review_note']}" if p["review_note"] else ""))
    print(f"  quote    : \"{p['quote'] or ''}\"")
    print(f"  source   : {p['url']}")


def set_status(conn, pid: int, status: str, note: str | None) -> None:
    conn.execute("update points set status = %s, review_note = %s, reviewed_at = now() where id = %s",
                 (status, note, pid))
    conn.commit()


def edit(conn, p: dict, catalog) -> dict:
    field = input(f"  field ({', '.join(EDITABLE)}): ").strip()
    if field not in EDITABLE:
        print("  unknown field")
        return p
    item = catalog.by_canonical(p["canonical"])
    raw = input(f"  new {field}" + (" (key=value, blank value removes)" if field == "attrs" else "") + ": ").strip()
    updates: dict = {}
    if field == "price":
        price = parse_price(raw)
        if not price:
            print("  not a price")
            return p
        updates["price"] = price
    elif field == "attrs":
        allowed = catalog.allowed_attrs(item) if item else {}
        key, _, value = raw.partition("=")
        key, value = key.strip(), value.strip()
        attrs = dict(p["attrs"] or {})
        if key not in allowed:
            print(f"  allowed keys: {', '.join(allowed)}")
            return p
        if value and value not in allowed[key]:
            print(f"  allowed values: {', '.join(allowed[key])}")
            return p
        if value:
            attrs[key] = value
        else:
            attrs.pop(key, None)
        updates["attrs"] = attrs
        updates["attr_source"] = "verified"
    elif field in ("kind", "line_kind", "worth"):
        allowed = {"kind": KINDS, "line_kind": LINE_KINDS, "worth": WORTH}[field]
        if raw and raw not in allowed:
            print(f"  allowed: {', '.join(allowed)}")
            return p
        updates[field] = raw or None
    elif field == "quality":
        if raw and raw not in "12345":
            print("  1-5 or blank")
            return p
        updates["quality"] = int(raw) if raw else None
    elif field == "known":
        updates["known"] = raw.lower() in ("y", "yes", "true", "1")
    elif field == "city":
        updates["city"] = normalise_city(raw)
    else:
        updates[field] = raw or None
    merged = {**p, **updates}
    if field in ("price", "unit") and item:
        merged["price_per_unit"], merged["unit_basis"] = per_unit(merged["price"], merged["unit"], item.category,
                                                                  item.canonical)
        updates["price_per_unit"], updates["unit_basis"] = merged["price_per_unit"], merged["unit_basis"]
    sets = ", ".join(f"{k} = %s" for k in updates)
    values = [Jsonb(v) if k == "attrs" else v for k, v in updates.items()]
    conn.execute(f"update points set {sets} where id = %s", (*values, p["id"]))
    conn.commit()
    return merged


def interactive(conn, points: list[dict], catalog) -> dict:
    counts = {"approved": 0, "rejected": 0, "skipped": 0, "edited": 0}
    i = 0
    while i < len(points):
        p = points[i]
        show(p, i + 1, len(points))
        key = input("  [a]pprove [r]eject [e]dit [s]kip [q]uit > ").strip().lower()
        if key == "a":
            note = p["review_note"] if (p["review_note"] or "").startswith("outlier") else None
            set_status(conn, p["id"], "approved", note)
            counts["approved"] += 1
        elif key == "r":
            reason = ""
            while not reason:
                reason = input("  reason: ").strip()
            set_status(conn, p["id"], "rejected", reason)
            counts["rejected"] += 1
        elif key == "e":
            points[i] = edit(conn, p, catalog)
            counts["edited"] += 1
            continue
        elif key == "s":
            counts["skipped"] += 1
        elif key == "q":
            break
        else:
            continue
        i += 1
    return counts


def auto_approve(conn, threshold: float, catalog) -> int:
    known = [i.canonical for i in catalog.items.values()]
    cur = conn.execute(
        """update points set status = 'approved', review_note = %s, reviewed_at = now()
           where status = 'staged' and confidence >= %s and unit is not null
             and coalesce(review_note, '') not like 'outlier%%' and canonical = any(%s)""",
        (AUTO_NOTE, threshold, known),
    )
    conn.commit()
    return cur.rowcount


def spot_check(conn, catalog, share: float = 0.10) -> dict:
    rows = list(conn.execute(
        "select * from points where status = 'approved' and review_note = %s and reviewed_at > now() - interval '7 days'",
        (AUTO_NOTE,)))
    sample = random.sample(rows, max(1, round(len(rows) * share))) if rows else []
    print(f"spot-checking {len(sample)} of {len(rows)} auto-approved points from the last 7 days")
    return interactive(conn, sample, catalog)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m buyble.review")
    ap.add_argument("--item", help="canonical name, e.g. 'spark plug'")
    ap.add_argument("--auto-approve-above", type=float, metavar="CONF")
    ap.add_argument("--spot-check", action="store_true")
    args = ap.parse_args(argv)
    try:
        catalog = load_catalog()
        with db.connect() as conn:
            if args.auto_approve_above is not None:
                n = auto_approve(conn, args.auto_approve_above, catalog)
                print(f"auto-approved {n} point(s) with confidence >= {args.auto_approve_above}")
            if args.spot_check:
                counts = spot_check(conn, catalog)
            else:
                points = staged(conn, args.item)
                print(f"{len(points)} staged point(s)")
                counts = interactive(conn, points, catalog) if points else {}
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("\nstopped; decisions so far are saved")
        return 0
    if counts:
        print("\n" + ", ".join(f"{k} {v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
