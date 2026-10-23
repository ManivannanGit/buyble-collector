# Buyble collector

A nightly job that turns real buying experiences on Reddit into clean price data points for Buyble.
Flow: **Reddit → price prefilter → Claude Haiku extraction → normalise / validate / trust score → Supabase (`staged`) → your review → export for the app.**

Every number keeps its source link. Facts (composition, channel) and feelings (quality, worth) are stored separately. Delivery fees, taxes and tips are never part of an item's price.

---

## 1. Setup

### On your Mac (development)

```bash
cd ~/buyble
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env      # then: nano .env
.venv/bin/python -m buyble.config            # shows set/missing for each variable, never the values
.venv/bin/python -m buyble.db --apply-schema # creates the tables (safe to re-run)
.venv/bin/python -m pytest -q
```

To fill the Supabase password without typing it anywhere visible, leave `YOUR-PASSWORD` in `DATABASE_URL` and run
`.venv/bin/python -m buyble.config --set-db-password`.

### `.env` variables

| Variable | What |
|---|---|
| `DATABASE_URL` | Supabase **pooler** string (`…pooler.supabase.com:6543`). The direct `db.…supabase.co` host is IPv6-only and won't work from EC2. |
| `ANTHROPIC_API_KEY` | From console.anthropic.com (separate from a Claude Pro plan). |
| `ANTHROPIC_WORKSPACE_ID` | Only if the key isn't scoped to a workspace (`wrkspc_…`). |
| `EXTRACT_MODEL` | `claude-haiku-5-5`, the cheapest Claude model. |
| `REDDIT_*` | Script-app id/secret, plus username/password or a refresh token. |
| `REDDIT_USER_AGENT` | `linux:buyble-collector:0.1 (by /u/yourname)` |
| `AUTHOR_HASH_SALT` | Long random string. Keep it the same forever, or dedupe breaks. |
| `MAX_LLM_CALLS_PER_RUN` | Hard cap on Claude calls per run (default 100). |
| `MAX_LLM_USD_PER_RUN` | Hard cap on estimated Claude spend per run (default $0.10). |
| `MAX_POSTS_PER_ITEM` | New snippets kept per item per run (default 40). |

### On EC2 (production)

1. Push this repo to a **private** GitHub repo.
2. SSH in: `ssh -i <your-key>.pem ec2-user@<your-ec2-public-dns>`
3. Copy `deploy/install.sh` over (or `curl` it from GitHub), then run `sudo bash install.sh https://github.com/OWNER/REPO.git` (public repo) or `…git@github.com:OWNER/REPO.git` (private).
   For a private repo, the first run prints a **deploy key**. Add it in GitHub → repo → Settings → Deploy keys (read-only), then re-run the same command.
4. Fill in secrets on the box: `sudo -u buyble nano /opt/buyble-collector/.env`
5. Test: `sudo systemctl start buyble-collect && journalctl -u buyble-collect -n 100 --no-pager`

`install.sh` is idempotent: re-run it any time to update packages, the venv and the systemd units.

---

## 2. Daily operations (automatic)

The timer runs every night at **02:30 IST** (21:00 UTC, plus up to 10 minutes of random delay). It runs `git pull` first, so pushed changes go live the next night.

| Task | Command (on EC2) |
|---|---|
| Run now | `sudo systemctl start buyble-collect` |
| Logs | `journalctl -u buyble-collect -n 200 --no-pager` |
| Next run | `systemctl list-timers \| grep buyble` |
| Pause | `sudo systemctl disable --now buyble-collect.timer` |
| Resume | `sudo systemctl enable --now buyble-collect.timer` |

Manual runs (Mac or EC2, from the repo folder):

```bash
.venv/bin/python -m buyble.run --item spark-plug --dry-run   # nothing written; prints snippets and points
.venv/bin/python -m buyble.run --item spark-plug --item hot-chocolate
.venv/bin/python -m buyble.run --max-llm 20
```

Each run prints a summary (snippets, Claude calls, estimated cost, staged and rejected points, errors) and records it in the `runs` table.

---

## 3. Weekly routine (~20 minutes)

1. **Review**: `python -m buyble.review` (`a` approve, `r` reject with a reason, `e` edit, `s` skip, `q` quit). Only approved points reach the app.
   - `--item "spark plug"` reviews one item.
   - `--auto-approve-above 0.85` approves confident, clean points automatically. Off by default.
     If you turn it on, **spot-check at least 10% every week**: `python -m buyble.review --spot-check`.
2. **Golden set**: add your own bills, then check accuracy:
   ```bash
   python -m buyble.golden add --item "spark plug" --price 280 --expected fair \
       --attrs origin=local_unbranded channel=garage --note "my scooter service bill"
   python -m buyble.golden list
   python -m buyble.golden          # accuracy on judged rows + table of misses
   ```
   A row is judged only when ≥3 approved points share its item and stated attributes. The target is ≥80% accuracy over ≥20 rows.
3. **Export**: `python -m buyble.export --out exports/points.json`. This writes `exports/points.json` and `exports/attr_maps.json`.
   Both are safe to share: they hold no author hashes and no raw post text. Share them with Claude to load into the prototype.

---

## 4. Costs

- **Claude**: Haiku 5.5 at $0.10 / $0.50 per million input/output tokens, low effort. One call handles up to 8 snippets.
  Each run is capped at 100 calls **and** $0.10. Expect a few cents per night, with a hard ceiling of about ₹270/month.
  Also set a monthly spend limit in console.anthropic.com → Billing.
- **Supabase**: free tier.
- **EC2**: whatever the instance already costs. The job runs once a night for a few minutes.
- **Reddit**: free for non-commercial use (see below).

---

## 5. Reddit terms

New Reddit API apps need manual approval, which takes about 2–4 weeks. Free access is for **non-commercial** use only. **Before Buyble earns money, contact Reddit for commercial terms.**
The collector uses only the official OAuth API, stays under 60 requests/minute, stores at most 300-character quotes plus the link, and never stores usernames (only a salted hash).

---

## 6. Add an item

Add an entry to `items.yaml` under `items:`:

```yaml
  masala-dosa:
    category: food_drink            # must be listed under categories
    canonical: masala dosa          # lowercase, 1-3 words, unique
    queries: ["masala dosa price", "dosa rs"]
    attrs: { batter: [fermented_fresh, ready_mix] }   # composition facts only; channel comes from the category
    bounds: [20, 600]               # INR per unit; points outside are rejected
```

Then add labels and one-line notes for any new attribute values to `ATTR_COPY` in `buyble/export.py`. `pytest` fails until every value has a note.
Check it with `python -m buyble.run --item masala-dosa --dry-run`.

## 7. Add a source

Create `buyble/sources/<name>.py` with a class that has `name` and `fetch(item, seen=...) -> list[RawPost]` (see `sources/base.py`).
It must prefilter for price signals, scrub personal data with `normalise.scrub_pii`, set `RawPost.id` to `"<source>:<native id>"`, and raise `SourceUnavailable` when its credentials are missing or rejected.
Then call it in `run.py` next to `RedditSource`. Planned sources: Google Maps reviews, YouTube comments, Instagram (after Meta App Review; always `kind: "asked"`), and bills from the app.

---

## 8. Safety rules

- Secrets live only in `.env` (`chmod 600`, git-ignored). Tools print `set` / `missing`, never values.
- Row-level security is on for every table, so the Supabase public API can't read them; the collector uses the owner role.
- No scraping of Amazon, Flipkart, Instagram, Zomato or Swiggy. Official APIs only.
- Raw post bodies are deleted after 30 days. Rejected points always carry a reason.
