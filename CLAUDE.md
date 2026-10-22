# Buyble Collector — build and run instructions

You are Claude Code running on an AWS EC2 instance owned by the Buyble founder. Your job is to build, test and schedule the **Buyble data collector**: a background job that turns real human buying experiences from public sources into clean, unbiased price data points.

Read this whole file before writing code. Work in small steps, run what you build, and stop to ask the founder only where this file says **ASK**.

---

## 1. What Buyble is (context)

Buyble is a buyer-first app for India that answers "is this price fair?" for any purchase: a hot chocolate, a t-shirt, a scooter spare part, a cab ride. Its rules:

- **Human experience is the evidence. The LLM only reasons.** Every number shown must trace back to real data points with a source link.
- **Facts and feelings are kept apart.** Composition (what it's made of, how it's made, genuine vs aftermarket) is objective. Taste, comfort and satisfaction are subjective. Never merge the two.
- **Like-for-like comparison.** Prices are compared only within the same composition and the same channel (e.g. dine-in vs delivery app; authorised service centre vs local garage).
- **Labour, parts and service packages are separate items.** Delivery fees, platform fees, packaging, taxes, tips and discounts are never part of an item's price.
- **"Paid" and "asked" never mix.** What a buyer paid is different from what a seller lists.
- **Neutral.** No brand, shop or advertiser can influence data. No affiliate logic anywhere.

The app already exists as a prototype. This collector feeds it.

---

## 2. Your deliverables

Build a Python 3.11+ project in this repo:

```
buyble-collector/
  CLAUDE.md               # this file
  README.md               # setup + operations guide for the founder
  .env.example            # every variable, no real values
  requirements.txt
  items.yaml              # what to track (section 5)
  schema.sql              # Supabase/Postgres schema (section 4)
  buyble/
    config.py             # loads .env and items.yaml, validates them
    db.py                 # Postgres access (psycopg 3)
    sources/
      base.py             # Source interface: fetch(item) -> list[RawPost]
      reddit.py           # section 6
    extract.py            # Claude extraction (section 7)
    normalise.py          # units, cities, canonical names, dedupe (section 8)
    validate.py           # sanity rules (section 8)
    review.py             # CLI to approve/reject staged points (section 9)
    export.py             # approved points -> JSON for the app (section 10)
    golden.py             # golden-set accuracy check (section 11)
    run.py                # one full collection run
  deploy/
    buyble-collect.service
    buyble-collect.timer
    install.sh            # idempotent setup on this EC2 box
  tests/
```

Keep dependencies few: `httpx`, `psycopg[binary]`, `pyyaml`, `python-dotenv`, `anthropic`, `pytest`.

---

## 3. Secrets and safety (non-negotiable)

- All secrets live in `/opt/buyble-collector/.env` (owner-only, `chmod 600`). Never commit `.env`. Add it to `.gitignore` first.
- **Never print, log or echo a secret**, not even partially. When checking a variable exists, print only `set` / `missing`.
- **ASK** the founder to type secrets into `.env` themselves. Do not ask them to paste secrets into the chat.
- Do not open new inbound ports. The collector only makes outbound HTTPS calls.
- Do not scrape Amazon, Flipkart, Instagram, Zomato or Swiggy. Use official APIs only.
- Respect every API's rate limits and terms. Back off on 429/5xx with exponential delay and jitter.
- Store only short excerpts (max 300 chars) plus the source link, never whole posts.
- Strip personal data before storing: usernames, real names, phone numbers, emails, addresses, vehicle registration numbers, order or invoice numbers. Reddit author names are **not** stored; store a salted SHA-256 hash only (for rater calibration and dedupe).

`.env.example`:

```
DATABASE_URL=postgresql://...            # Supabase connection string (pooler, port 6543)
ANTHROPIC_API_KEY=
EXTRACT_MODEL=claude-haiku-5-5
REDDIT_CLIENT_ID=
REDDIT_CLIENT_SECRET=
REDDIT_USERNAME=
REDDIT_PASSWORD=                         # or use REDDIT_REFRESH_TOKEN instead
REDDIT_REFRESH_TOKEN=
REDDIT_USER_AGENT=linux:buyble-collector:0.1 (by /u/YOUR_USERNAME)
AUTHOR_HASH_SALT=                        # any long random string
MAX_LLM_CALLS_PER_RUN=400                # hard cost cap
MAX_POSTS_PER_ITEM=40
DEFAULT_CITY=Bengaluru
```

---

## 4. Database (Supabase Postgres)

**ASK** the founder to create a free Supabase project and put the pooled connection string in `.env` as `DATABASE_URL`. Then apply `schema.sql`.

```sql
create table if not exists raw_posts (
  id text primary key,                 -- source:native_id
  source text not null,                -- 'reddit'
  url text not null,
  subreddit text,
  author_hash text,
  created_at timestamptz,
  title text, body text,               -- trimmed, max 4000 chars, deleted after 30 days
  item_key text not null,              -- items.yaml key that found it
  fetched_at timestamptz default now(),
  extracted boolean default false
);

create table if not exists points (
  id bigserial primary key,
  status text not null default 'staged' check (status in ('staged','approved','rejected')),
  source text not null,                -- 'reddit' | 'bill' | 'buyer' | later 'maps','instagram'
  raw_post_id text references raw_posts(id),
  url text,
  kind text not null check (kind in ('purchase','asked','component','service')),
  line_kind text check (line_kind in ('part','labour','service_package','food','product')),
  item text not null,                  -- as written, max 80 chars
  canonical text not null,             -- generic name, lowercase, 1-3 words
  category text not null,              -- food_drink | apparel | auto_parts | electronics | handmade | service | other
  attrs jsonb not null default '{}',   -- composition facts + channel, e.g. {"fat_source":"cocoa_butter","channel":"dine_in"}
  attr_source text not null default 'unknown',  -- verified | seller_stated | buyer_stated | inferred | unknown
  price numeric not null check (price > 0),
  currency text not null default 'INR',
  unit text,                           -- '1 cup', '250 g', '1 pc', '8 km'
  price_per_unit numeric,              -- normalised, see section 8
  unit_basis text,                     -- 'cup','100g','pc','km','job'
  city text,
  quality smallint check (quality between 1 and 5),     -- null if the author gave no quality signal
  worth text check (worth in ('good_deal','fair','pricey','overpriced')),
  known boolean default false,         -- author shows expertise
  quote text,                          -- <= 300 chars, verbatim from source
  for_model text,                      -- e.g. 'TVS Jupiter' for parts
  author_hash text,
  purchased_at date,                   -- best estimate; else the post date
  trust numeric not null default 1.0,
  confidence numeric,                  -- extractor's 0-1 confidence
  created_at timestamptz default now(),
  reviewed_at timestamptz,
  review_note text
);
create index if not exists points_canonical on points(canonical, status);
create unique index if not exists points_dedupe on points(raw_post_id, canonical, price);

create table if not exists attr_maps (
  canonical text primary key,
  category text not null,
  attrs jsonb not null,                -- [{key,label,question,options:[{value,label,note}]}]
  approved boolean default false,
  updated_at timestamptz default now()
);

create table if not exists golden (
  id bigserial primary key,
  item text not null, price numeric not null, city text,
  attrs jsonb default '{}',
  expected text not null check (expected in ('good_deal','fair','pricey','overpriced')),
  note text, created_at timestamptz default now()
);

create table if not exists runs (
  id bigserial primary key,
  started_at timestamptz default now(), finished_at timestamptz,
  posts_fetched int default 0, llm_calls int default 0,
  points_staged int default 0, errors jsonb default '[]'
);
```

---

## 5. What to track: `items.yaml`

Start with one city and four categories. Each item lists search terms, where to look, and the composition facts that make comparisons fair. Generate the file from this seed; keep the structure.

```yaml
city: Bengaluru
nearby_cities: [Chennai, Hyderabad, Mumbai, Pune, Delhi]   # data from these is kept but weighted lower

categories:
  food_drink:
    channels: [dine_in, takeaway, delivery_app]
  apparel:
    channels: [online, brand_store, local_shop]
  auto_parts:
    channels: [authorised, garage, online]
  service:
    channels: [authorised, garage, app]

items:
  hot-chocolate:
    category: food_drink
    canonical: hot chocolate
    queries: ["hot chocolate price", "hot chocolate cafe", "best hot chocolate"]
    attrs:
      fat_source: [cocoa_butter, vegetable_fat]
      base: [real_chocolate, cocoa_powder, premix]
  filter-coffee:
    category: food_drink
    canonical: filter coffee
    queries: ["filter coffee price", "filter coffee rs"]
    attrs: { chicory: [pure_coffee, with_chicory] }
  cold-coffee:
    category: food_drink
    canonical: cold coffee
    queries: ["cold coffee price"]
    attrs: { base: [espresso, instant, premix] }
  masala-chai:
    category: food_drink
    canonical: masala chai
    queries: ["chai price", "masala chai rs"]
    attrs: { milk: [full_cream, toned, plant] }

  round-neck-tshirt:
    category: apparel
    canonical: t-shirt
    queries: ["t shirt quality price", "best t shirt brand india", "tshirt gsm"]
    attrs:
      fibre: [cotton_100, cotton_poly_blend, polyester, rayon_viscose]
      weight: [light_under_160gsm, mid_160_200gsm, heavy_over_200gsm]
  polo-tshirt:
    category: apparel
    canonical: polo t-shirt
    queries: ["polo t shirt price india"]
    attrs: { fibre: [cotton_100, cotton_poly_blend, polyester] }
  gym-tshirt:
    category: apparel
    canonical: gym t-shirt
    queries: ["dri fit t shirt price", "gym tshirt technosport", "sports tshirt quality"]
    attrs: { fibre: [polyester, poly_spandex, cotton_blend] }

  spark-plug:
    category: auto_parts
    canonical: spark plug
    queries: ["spark plug price scooter", "spark plug jupiter", "spark plug activa"]
    attrs: { origin: [genuine_oem, branded_aftermarket, local_unbranded] }
    models: [TVS Jupiter, Honda Activa, Suzuki Access, TVS Ntorq]
  air-filter:
    category: auto_parts
    canonical: air filter
    queries: ["air filter price scooter", "air filter change cost"]
    attrs: { origin: [genuine_oem, branded_aftermarket, local_unbranded] }
    models: [TVS Jupiter, Honda Activa, Suzuki Access]
  brake-shoes:
    category: auto_parts
    canonical: brake shoes
    queries: ["brake shoe price scooter", "brake shoes replacement cost"]
    attrs: { origin: [genuine_oem, branded_aftermarket, local_unbranded] }
    models: [TVS Jupiter, Honda Activa, Suzuki Access]
  clutch-assembly:
    category: auto_parts
    canonical: clutch assembly
    queries: ["scooter clutch replacement cost", "clutch assembly price jupiter"]
    attrs: { origin: [genuine_oem, branded_aftermarket, local_unbranded] }
  accelerator-cable:
    category: auto_parts
    canonical: accelerator cable
    queries: ["accelerator cable price scooter"]
    attrs: { origin: [genuine_oem, branded_aftermarket, local_unbranded] }
  scooter-general-service:
    category: service
    canonical: scooter general service
    queries: ["scooter service cost", "jupiter service cost", "activa service charges"]
    attrs: { scope: [labour_only, labour_plus_oil, full_package] }
  fork-oil-service:
    category: service
    canonical: front fork service
    queries: ["fork oil change cost scooter", "front fork repair cost"]
    attrs: { scope: [oil_only, seals_and_oil, alignment_and_oil] }

  airport-cab:
    category: service
    canonical: airport cab
    queries: ["airport cab fare bangalore", "kia airport taxi price", "uber airport fare"]
    attrs:
      vehicle: [hatchback, sedan, suv]
      booking: [app, prepaid_counter, street]
  city-cab-10km:
    category: service
    canonical: city cab ride
    queries: ["cab fare per km bangalore", "ola uber fare 10 km"]
    attrs: { vehicle: [auto, hatchback, sedan], booking: [app, street] }

subreddits:                      # verify each exists and is active before use; drop dead ones
  general: [bangalore, india, chennai, hyderabad, mumbai, pune, delhi]
  food_drink: [IndianFood, bangalore]
  apparel: [IndianFashionAddicts, IndianMenFashion]
  auto_parts: [IndianBikes, indianbikes]
  service: [bangalore, IndianBikes]
```

---

## 6. Reddit source

- Use the official OAuth API at `https://oauth.reddit.com`. Unauthenticated `.json` endpoints return 403; do not use them.
- Auth: if `REDDIT_REFRESH_TOKEN` is set, use the refresh-token grant; else the script-app password grant. Tokens last 1 hour: cache the token and its expiry, refresh 5 minutes early.
- Send the exact `REDDIT_USER_AGENT` on every request.
- Stay under **60 requests/minute** (Reddit allows 100 QPM averaged over 10 minutes; leave headroom). Read the `X-Ratelimit-Remaining` and `X-Ratelimit-Reset` headers and sleep when remaining < 5.
- Per item: for each query × relevant subreddit, call `/r/{sub}/search` with `restrict_sr=1&sort=new&t=year&limit=25`. Then fetch comments for the most relevant threads via `/comments/{id}?limit=100&depth=3&sort=top`. Price talk lives mostly in comments.
- Keep a post or comment only if it contains a price signal: `₹`, `rs`, `rs.`, `inr`, `/-`, `k` after a number, or words like "paid", "charged", "cost me", "bill". Cheap regex prefilter **before** any LLM call.
- Skip anything already in `raw_posts` (dedupe by `reddit:{fullname}`).
- Store the post date, subreddit, permalink and a salted hash of the author.
- If credentials are missing or rejected (401/403), log a clear message and skip Reddit for this run. Do not crash the whole run.

Note for the founder (put it in README): new Reddit API apps now need manual approval, which takes about 2–4 weeks, and free access is for non-commercial use. Before Buyble earns money, contact Reddit for commercial terms.

---

## 7. Extraction with Claude

Use the Anthropic Python SDK with `EXTRACT_MODEL` (default `claude-haiku-5-5`). Batch up to 8 prefiltered snippets per call. Enforce `MAX_LLM_CALLS_PER_RUN`; when reached, stop extracting and leave the rest for the next run.

System prompt (adapt, keep every rule):

```
You extract price data points for Buyble, an Indian price-fairness app. Human experience is the evidence; you only structure it.

For each snippet, output zero or more data points. Output a point ONLY if the author states a real price for a specific item or service.

Rules
- kind: "purchase" (the author or someone they know paid it), "asked" (a seller's listed or quoted price), "component" (a raw ingredient/material/part cost used to make something). If unclear, skip.
- line_kind: part | labour | service_package | food | product.
- One point per item. Split bundled bills into separate items when the snippet lists separate prices. Never include delivery fees, platform fees, packaging, taxes, tips or discounts in an item's price.
- price: the price for the stated unit, as a number in INR. Convert "1.2k" to 1200. If a range is given, skip unless one price was actually paid.
- unit: what the price was for ("1 cup", "250 g", "1 pc", "12 km", "job").
- canonical: must be one of the CANONICAL ITEMS given, or "" if none fits (then skip).
- attrs: only facts explicitly stated or clearly implied (e.g. "Callebaut" implies real chocolate; "local shop part" implies local_unbranded). Use the allowed values only. Include "channel" when the snippet says where (dine-in, takeaway, Zomato/Swiggy = delivery_app, authorised service centre, local garage, online).
- attr_source: "seller_stated" if quoting a seller/label, "buyer_stated" if the author says it, "inferred" if implied. Never guess: leave a fact out if not stated.
- quality: 1-5 only if the author judges quality; else null. worth: good_deal | fair | pricey | overpriced only if the author judges value; else null. Rate quality and value separately.
- known: true only if the author shows expertise (mechanic, barista, works in garments, owns many of these).
- city: only if stated or obvious from context (subreddit r/bangalore is a hint, not proof: use it only if nothing contradicts it).
- quote: a verbatim excerpt of max 300 chars that contains the price. Remove any personal names, phone numbers, addresses, registration or order numbers.
- for_model: product or vehicle model if relevant ("TVS Jupiter"), else "".
- purchased_year_month: "YYYY-MM" if stated or implied ("last month"), else "".
- confidence: 0-1.

Reply with ONLY a JSON array: [{"snippet_id", "kind", "line_kind", "item", "canonical", "category", "attrs", "attr_source", "price", "unit", "city", "quality", "worth", "known", "quote", "for_model", "purchased_year_month", "confidence"}]
```

Pass in the user message: today's date, the canonical items with their allowed attribute values from `items.yaml`, and the snippets with ids.

Parse strictly. Drop any point that fails JSON-schema validation. Never let the model invent a field value that isn't in the allowed lists.

---

## 8. Normalise, validate, score

**Normalise**
- `price_per_unit` + `unit_basis`: food per cup/plate/100 g; apparel per piece; parts per piece; labour per job; cabs per km **and** keep the total fare. If the unit is unclear, leave `price_per_unit` null (the point still counts, only within the same unit).
- City aliases: Bangalore → Bengaluru, Bombay → Mumbai, Madras → Chennai, Gurgaon → Gurugram.
- `purchased_at`: from `purchased_year_month`, else the post date.

**Validate** (reject into status `rejected` with a `review_note`, never silently drop)
- Per-category price bounds, configurable in `items.yaml` (e.g. hot chocolate ₹30–₹1,500; spark plug ₹50–₹2,000; t-shirt ₹100–₹10,000; airport cab ₹200–₹5,000).
- Over 4× or under 0.25× the current approved median for the same canonical + attrs cohort: keep as `staged` but flag `outlier` in `review_note`.
- Posts older than 3 years: reject.
- Same author_hash + canonical + price within 30 days: duplicate, reject.
- Confidence < 0.5: reject.

**Trust score** (stored in `trust`, used by the app as a weight)
- source: bill 1.2, buyer 1.0, reddit 0.8, asked/seller 0.5
- × recency: 0.5 ^ (age_days / 365)
- × 1.2 if `known`; × 1.1 if `attr_source` is verified/seller_stated
- × 0.8 if city is not the target city

---

## 9. Review queue (human in the loop)

`python -m buyble.review` opens a simple terminal flow:

- Shows staged points grouped by canonical, oldest first: item, price, unit, attrs, channel, city, quote and source link.
- Keys: `a` approve, `r` reject (asks for a short reason), `e` edit a field, `s` skip, `q` quit.
- `--auto-approve-above 0.85` approves points with confidence ≥ 0.85, a non-null unit, no outlier flag and a known canonical. Default: off. **ASK** the founder before turning it on.
- Spot-check rule for README: review at least 10% of auto-approved points every week.

---

## 10. Export to the app

`python -m buyble.export --out exports/points.json` writes approved points in the app's data shape:

```json
{"item":"Spark plug","canonical":"spark plug","category":"auto_parts","kind":"purchase","lineKind":"part",
 "attrs":{"origin":"genuine_oem","channel":"garage"},"attrSource":"buyer_stated","source":"reddit",
 "price":280,"unit":"1 pc","city":"Bengaluru","quality":4,"worth":"fair","known":false,
 "note":"<quote>","url":"https://reddit.com/...","for":"TVS Jupiter",
 "purchasedAt":"2026-09-01T12:00:00Z","createdAt":"2026-10-22T06:00:00Z","trust":0.74}
```

The current prototype can't read from Supabase. Until Buyble is hosted as its own app, the founder shares `exports/points.json` with Claude, which loads it into the prototype. Write the export so it's safe to share: no author hashes, no raw bodies.

Also export `attr_maps` from `items.yaml` (one entry per canonical, `approved: true`) in the app's shape: `{canonical, category, attrs:[{key,label,question,options:[{value,label,note}]}]}`. Write human-friendly labels and neutral one-line trade-off notes for each option.

---

## 11. Golden set

`python -m buyble.golden` computes, for each row in `golden`, the verdict from approved points using the same rules as the app:

- Cohort = same canonical, and same attrs where the golden row specifies them; need ≥ 3 points, else "too thin".
- Weighted percentile of the price using `trust` as weight: ≤25 good_deal, ≤65 fair, ≤85 pricey, else overpriced.

Print accuracy (judged rows only) and a table of misses. Store each run's accuracy in `runs.errors` or a small extra column. **ASK** the founder to add at least 20 golden rows from their own bills (a recent scooter service invoice is a good start).

---

## 12. One run (`python -m buyble.run`)

1. Create a `runs` row.
2. For each item in `items.yaml`: fetch from Reddit → prefilter → insert new `raw_posts`.
3. Extract un-extracted posts in batches, respecting the LLM cap.
4. Normalise → validate → score → insert into `points` as `staged` (or `rejected` with a reason).
5. Delete `raw_posts.body` older than 30 days.
6. Finish the `runs` row with counts and errors. Print a one-screen summary.

Flags: `--item <key>` (single item), `--dry-run` (no DB writes, print what would be stored), `--max-llm <n>`.

---

## 13. Schedule on this EC2 box

`deploy/install.sh` (idempotent, run with sudo):

- Install Python 3.11+, `git`, create user `buyble`, clone or pull the repo into `/opt/buyble-collector`, create a venv, install requirements.
- Create `/opt/buyble-collector/.env` from `.env.example` if missing, `chmod 600`, owner `buyble`. Do not fill in secrets.
- Install the systemd units and enable the timer.

`buyble-collect.service`:
```
[Unit]
Description=Buyble data collection run
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=buyble
WorkingDirectory=/opt/buyble-collector
ExecStartPre=/usr/bin/git -C /opt/buyble-collector pull --ff-only
ExecStart=/opt/buyble-collector/.venv/bin/python -m buyble.run
TimeoutStartSec=3600
Nice=10
```

`buyble-collect.timer`:
```
[Unit]
Description=Run Buyble collector daily at 02:30 IST

[Timer]
OnCalendar=*-*-* 21:00:00 UTC
Persistent=true
RandomizedDelaySec=600

[Install]
WantedBy=timers.target
```

Logs: `journalctl -u buyble-collect -n 200`. Document in README: run now (`sudo systemctl start buyble-collect`), check the timer (`systemctl list-timers | grep buyble`), pause (`sudo systemctl disable --now buyble-collect.timer`).

---

## 14. Build order

1. `.gitignore`, `.env.example`, `requirements.txt`, `items.yaml`, `config.py` with validation. Tests for config.
2. `schema.sql` + `db.py`. **ASK** for `DATABASE_URL`, then apply the schema and confirm the tables exist.
3. `normalise.py`, `validate.py` with unit tests (price parsing like "1.2k", "Rs. 450/-", "₹2,400"; city aliases; bounds).
4. `reddit.py`. **ASK** the founder to fill Reddit keys in `.env`. Test with `--item spark-plug --dry-run` and show 5 sample snippets.
5. `extract.py`. Test on those snippets with `--dry-run`; show the extracted points and confirm the founder is happy with quality before writing to the DB.
6. `run.py` end to end on 2 items, writing to the DB. Show the run summary.
7. `review.py`, `export.py`, `golden.py`.
8. `deploy/` files, run `install.sh`, start one run through systemd, show the logs and the timer.
9. `README.md`: setup, daily operations, costs, how to add an item, how to add a source.

After each step, commit with a clear message. Push to GitHub only if the founder has set up the remote.

---

## 15. Later sources (design for them now, don't build yet)

The `Source` interface must make these drop-in:
- **Google Maps reviews** (Places API) for cafés, garages and service centres: prices mentioned in reviews, plus channel = dine_in/garage.
- **YouTube Data API** comments on review and long-term-ownership videos.
- **Instagram** hashtag search after Meta App Review (Business account, Public Content Access, 30 unique hashtags per rolling 7 days). These are always `kind: "asked"`.
- **Buyble buyers and bills** from the app, once it's hosted with direct database access.

---

## 16. Definition of done

- One systemd-timed run per day completes without errors and logs a summary.
- At least 200 approved points across the four categories in Bengaluru.
- At least 50% of approved points have their key composition fact or channel set.
- Golden set (≥ 20 rows) accuracy ≥ 80% on judged rows.
- `exports/points.json` and the attr maps export are generated and safe to share.
- README lets the founder operate everything without you.
