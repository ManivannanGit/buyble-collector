"""Export approved points and attribute maps for the app (CLAUDE.md section 10).

    python -m buyble.export --out exports/points.json

Also writes exports/attr_maps.json next to it and upserts attr_maps in the DB.
The export holds no author hashes and no raw post bodies, so it is safe to share.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path

from psycopg.types.json import Jsonb

from . import db
from .config import Catalog, ConfigError, load_catalog

# Human-friendly labels and neutral one-line trade-offs for every attribute in items.yaml.
ATTR_COPY: dict[str, dict] = {
    "channel": {
        "label": "Where bought", "question": "Where did you buy it?",
        "options": {
            "dine_in": ("Dine-in", "Table service; price usually includes seating and service."),
            "takeaway": ("Takeaway", "Counter pickup; often a little cheaper than dine-in."),
            "delivery_app": ("Delivery app", "Menu prices on apps are often marked up; fees are separate."),
            "online": ("Online", "Wide choice and frequent sales; you can't check the item before buying."),
            "brand_store": ("Brand store", "Full price, easy returns and genuine stock."),
            "local_shop": ("Local shop", "Room to bargain; quality and returns vary."),
            "authorised": ("Authorised service centre", "Genuine parts and records; labour rates are higher."),
            "garage": ("Local garage", "Cheaper labour; part origin varies, so ask."),
            "app": ("App", "Upfront, metered fare; surge pricing at peak times."),
        },
    },
    "fat_source": {
        "label": "Fat source", "question": "What fat is in it?",
        "options": {
            "cocoa_butter": ("Cocoa butter", "Real chocolate fat; richer, costs more to make."),
            "vegetable_fat": ("Vegetable fat", "Compound chocolate; cheaper, less cocoa flavour."),
        },
    },
    "base": {
        "label": "Base", "question": "What is it made from?",
        "options": {
            "real_chocolate": ("Real chocolate", "Melted chocolate; thickest and most expensive base."),
            "cocoa_powder": ("Cocoa powder", "Lighter body; cheaper than melted chocolate."),
            "premix": ("Premix", "Ready-made powder; consistent and cheapest."),
            "espresso": ("Espresso", "Fresh coffee shots; stronger, needs a machine."),
            "instant": ("Instant coffee", "Quick and cheap; less depth of flavour."),
        },
    },
    "chicory": {
        "label": "Chicory", "question": "Is chicory blended in?",
        "options": {
            "pure_coffee": ("Pure coffee", "No chicory; costs more per cup."),
            "with_chicory": ("With chicory", "Traditional blend; thicker and cheaper."),
        },
    },
    "milk": {
        "label": "Milk", "question": "What milk is used?",
        "options": {
            "full_cream": ("Full cream", "Richest; costs a little more."),
            "toned": ("Toned", "Lighter and cheaper; the common default."),
            "plant": ("Plant milk", "Dairy-free; usually priced higher."),
        },
    },
    "fibre": {
        "label": "Fabric", "question": "What fabric is it?",
        "options": {
            "cotton_100": ("100% cotton", "Breathable and soft; may shrink or fade faster."),
            "cotton_poly_blend": ("Cotton-poly blend", "Holds shape and wrinkles less; slightly less breathable."),
            "polyester": ("Polyester", "Quick-drying and durable; can hold odour."),
            "rayon_viscose": ("Rayon / viscose", "Soft drape; weaker when wet."),
            "poly_spandex": ("Poly-spandex", "Stretchy and quick-drying; good for workouts."),
            "cotton_blend": ("Cotton blend", "Softer feel; dries slower than synthetics."),
        },
    },
    "weight": {
        "label": "Fabric weight", "question": "How heavy is the fabric (GSM)?",
        "options": {
            "light_under_160gsm": ("Light (under 160 GSM)", "Cooler and cheaper; can be see-through."),
            "mid_160_200gsm": ("Mid (160-200 GSM)", "Everyday weight; balances feel and durability."),
            "heavy_over_200gsm": ("Heavy (over 200 GSM)", "Structured and durable; warmer, costs more."),
        },
    },
    "origin": {
        "label": "Part origin", "question": "Is the part genuine, branded or local?",
        "options": {
            "genuine_oem": ("Genuine (OEM)", "Made for the vehicle maker; highest price, known fit."),
            "branded_aftermarket": ("Branded aftermarket", "Known brand like Bosch or NGK; usually cheaper than OEM."),
            "local_unbranded": ("Local / unbranded", "Cheapest; quality and lifespan vary widely."),
        },
    },
    "scope": {
        "label": "What's included", "question": "What did the job include?",
        "options": {
            "labour_only": ("Labour only", "You pay for work; parts and oil billed separately."),
            "labour_plus_oil": ("Labour + oil", "Common service bundle; other parts extra."),
            "full_package": ("Full package", "Fixed bundle of checks and parts; easy to compare, may include extras."),
            "oil_only": ("Oil change only", "Cheapest; doesn't fix leaking seals."),
            "seals_and_oil": ("Seals + oil", "Fixes leaks; costs more for parts and labour."),
            "alignment_and_oil": ("Alignment + oil", "Corrects bent or misaligned forks; most labour."),
        },
    },
    "vehicle": {
        "label": "Vehicle", "question": "What kind of vehicle?",
        "options": {
            "auto": ("Auto-rickshaw", "Cheapest per km; less comfort, limited luggage."),
            "hatchback": ("Hatchback", "Lowest car fare; less boot space."),
            "sedan": ("Sedan", "More room and boot space; higher fare."),
            "suv": ("SUV", "Most space; highest fare."),
        },
    },
    "booking": {
        "label": "Booking", "question": "How was it booked?",
        "options": {
            "app": ("App", "Upfront fare; surge pricing at peak times."),
            "prepaid_counter": ("Prepaid counter", "Fixed fare paid in advance; no surge."),
            "street": ("Street / hailed", "Negotiated or metered; price varies most."),
        },
    },
}


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    elif isinstance(value, date):
        dt = datetime.combine(value, time(12, 0), tzinfo=timezone.utc)
    else:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(value):
    if value is None:
        return None
    f = float(value)
    return int(f) if f.is_integer() else round(f, 2)


def app_point(p: dict) -> dict:
    """One approved points row in the app's shape. Deliberately omits author_hash and raw text."""
    return {
        "item": p["item"],
        "canonical": p["canonical"],
        "category": p["category"],
        "kind": p["kind"],
        "lineKind": p["line_kind"],
        "attrs": p["attrs"] or {},
        "attrSource": p["attr_source"],
        "source": p["source"],
        "price": _num(p["price"]),
        "unit": p["unit"],
        "city": p["city"],
        "quality": p["quality"],
        "worth": p["worth"],
        "known": bool(p["known"]),
        "note": p["quote"],
        "url": p["url"],
        "for": p["for_model"],
        "purchasedAt": _iso(p["purchased_at"]),
        "createdAt": _iso(p["created_at"]),
        "trust": round(float(p["trust"]), 2),
    }


def attr_maps(catalog: Catalog) -> list[dict]:
    out = []
    for item in catalog.items.values():
        attrs = []
        for key, values in catalog.allowed_attrs(item).items():
            copy = ATTR_COPY.get(key, {})
            opts = copy.get("options", {})
            attrs.append({
                "key": key,
                "label": copy.get("label", key.replace("_", " ").capitalize()),
                "question": copy.get("question", f"Which {key.replace('_', ' ')}?"),
                "options": [
                    {"value": v, "label": opts.get(v, (v.replace("_", " ").capitalize(), ""))[0],
                     "note": opts.get(v, ("", ""))[1]}
                    for v in values
                ],
            })
        out.append({"canonical": item.canonical, "category": item.category, "attrs": attrs, "approved": True})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m buyble.export")
    ap.add_argument("--out", default="exports/points.json")
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        catalog = load_catalog()
        maps = attr_maps(catalog)
        with db.connect() as conn:
            rows = list(conn.execute("select * from points where status = 'approved' order by canonical, id"))
            for m in maps:
                conn.execute(
                    """insert into attr_maps (canonical, category, attrs, approved, updated_at)
                       values (%s, %s, %s, true, now())
                       on conflict (canonical) do update set category = excluded.category,
                         attrs = excluded.attrs, approved = true, updated_at = now()""",
                    (m["canonical"], m["category"], Jsonb(m["attrs"])),
                )
            conn.commit()
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    points = [app_point(r) for r in rows]
    out.write_text(json.dumps(points, ensure_ascii=False, indent=1))
    maps_path = out.with_name("attr_maps.json")
    maps_path.write_text(json.dumps(maps, ensure_ascii=False, indent=1))
    print(f"wrote {len(points)} approved points to {out}")
    print(f"wrote {len(maps)} attribute maps to {maps_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
