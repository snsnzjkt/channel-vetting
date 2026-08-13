# Channel Vetting Pipeline

Discovers, enriches, scores, and pushes potential brand-partnership YouTube
channels into Airtable for human review.

## How it works

The pipeline runs once per niche (see `NICHES` in `main.py`), each niche
writing to its own Airtable table, and is bounded by two daily budgets so a
weak day can't flood a table with below-criteria channels:

0. **Daily cap check** — each niche's table is capped at `DAILY_QUALIFIED_CAP`
   (default 30) qualified rows and `DAILY_FLAGGED_CAP` (default 10) flagged
   rows per day (40 rows max), counted from Airtable's own "Date Added"
   field rather than a local file — so a second run the same day tops up
   the day's count instead of doubling it. A niche already at both caps is
   skipped before any quota is spent.
1. **Discovery** — finds candidate channels for the niche. With
   `INFLUENCERS_API_KEY` set, it uses **influencers.club creator search**
   (`influencer_discovery.py`), filtering server-side on the niche's own
   criteria — content language, a subscriber floor, creator gender, and an
   `ai_search` description of the niche — so far more of what it returns
   survives the hard requirements below than raw keyword search does. Your
   DO NOT CONTACT handles are excluded server-side (never dropped under the
   vendor's per-request cap), so no credit is spent surfacing a creator you
   are already suppressing. With no key set, discovery falls back to
   **YouTube `search.list`** (`discovery.py`): keyword search (type=video),
   cached per keyword per day, over a deliberately short and self-renewing
   `DISCOVERY_DAYS_BACK` (7) day window rather than a wide one that returns
   the same already-tracked channels every day. Either way, discovery keeps
   going until the day's qualified budget is filled or candidates run out.
2. **Pre-filter** — candidates already present in your Airtable base are
   dropped before any enrichment quota is spent on them.
3. **DO NOT CONTACT screening** (`do_not_contact.py`) — every candidate is
   checked against a suppression list (by handle, email, and name) at three
   points in the pipeline. The list is fetched fresh at the start of every
   run (never cached) and the whole run **aborts** if it can't be fetched
   with confidence — proceeding with a partial or empty blocklist risks
   contacting someone who asked not to be.
4. **Enrichment** (`enrichment.py`) — pulls subscriber/view counts and the
   last 10 videos' performance for each remaining candidate. It also reads
   the last 50 videos' descriptions looking for a contact email — a wider
   window that costs no extra quota, since the underlying calls are billed
   per-call rather than per-video.
5. **Hard requirements** (`main.pre_push_drop_reason`, `search_zones.py`) —
   a candidate is **discarded**, with no row written, unless it clears all
   of: 10,000+ average views (both niches), 30+ public videos, and a
   location inside the allowed search zones — **US, Canada, UK, Europe,
   Australia; Ireland excluded**. Dead channels and Shorts-only channels
   are dropped here too. Location comes from the channel's own `country`
   setting (85% of channels in the live tables set it), falling back to the
   region subtag of its content language (`en-GB` → GB) for the rest. A
   channel that declares neither is *kept*, not dropped — absent data
   isn't evidence against it.
6. **Qualification** (`scoring.py`) — the one soft criterion left: whether
   the channel meets that niche's minimum age. A channel that doesn't is
   **flagged for review, not discarded** — a human makes the final call.
7. **Scoring** (`scoring.py`) — computes a fake-follower risk score and a
   weighted overall score.
8. **Airtable push** (`airtable_client.py`) — creates or updates a row per
   channel in that niche's table (never duplicates), until both the
   qualified and flagged daily budgets are full or candidates run out.

Quota spend is tracked in `quota_log.json` and capped by `QUOTA_CEILING`
(default 8000/10000 daily units) so a run never blows your daily YouTube API
budget.

## Setup

### 1. Create an Airtable Personal Access Token (PAT)

1. Go to https://airtable.com/create/tokens.
2. Click **Create new token**.
3. Name it (e.g. "channel-vetting-pipeline").
4. Add scopes: `data.records:read` and `data.records:write`.
5. Add access to the specific base you'll use for this project.
6. Click **Create token** and copy the value — it's shown only once.
7. In your base, create **one table per niche** (currently: Home Theater
   and Lifestyle Sofa — see `NICHES` in `main.py`). Easiest way: build one
   table with the schema below, then right-click its tab → **Duplicate
   table → Duplicate table structure only** for each additional niche, so
   every table has an identical field set. Each table needs: Channel
   Name, Channel URL, Channel ID, Subscriber Count, Avg Views (last 10
   videos), Engagement Rate, Upload Frequency (Single line text — see
   note below), Content Language, Email (**Email** field type — not
   Single line text), Fake Follower Risk Score, Overall Score,
   Qualification (single select: Qualified / New Channel — see note
   below), Status (single
   select: New/Reviewing/Approved/Rejected/Contacted), Source, Notes,
   Date Added.

   Grab each table's ID (open the table → **Help → API documentation**,
   or read it from the URL — the `tbl...` segment) for step 4.

   > `Qualification` now records one thing: whether the channel is old
   > enough for its niche. `New Channel` means it isn't, and the row is
   > written anyway (`Status = New`) so a human reviewer can decide.
   >
   > A third option, `Below View Minimum`, existed before the 2026-08
   > criteria change. The view floor is now a hard requirement — channels
   > under it are dropped rather than written — so the pipeline never
   > produces that value again. **Keep the option on an existing table**:
   > it holds the rows written under the old rules, and deleting it would
   > blank their Qualification cell. A brand-new table doesn't need it.

   > This pipeline also requires a **DO NOT CONTACT** suppression table to
   > already exist in the same base — it's referenced by a hardcoded
   > table ID and field IDs (`DO_NOT_CONTACT_TABLE_ID`, `FIELD_NAME`,
   > `FIELD_URL`, `FIELD_EMAIL`) at the top of `do_not_contact.py`, rather
   > than an env var, since it's shared infrastructure rather than a
   > per-niche table. Pointing this at a different base means updating
   > those constants to match. Every candidate is checked against it by
   > handle, email, and name; if it can't be read, the whole run aborts
   > rather than risk contacting someone who opted out.

   > `Upload Frequency` is written as a formatted string (e.g. `"2.5
   > videos/month"`), not a raw number — if you make it a Number field
   > instead, update the `f"{upload_freq} videos/month"` line in
   > `main.py` to send `upload_freq` directly.

   > `Email`: YouTube's API does not expose a channel's gated
   > "business inquiries" email (it's behind a CAPTCHA-protected reveal
   > button specifically to block scraping, and this pipeline does not
   > attempt to bypass that). Instead, it does a best-effort regex scan
   > for a plain-text email, preferring one that recurs across at least
   > `EMAIL_MIN_VIDEO_REPEATS` (default 3) of the channel's last
   > `EMAIL_SCAN_SAMPLE_SIZE` (default 50) video descriptions — a much
   > more reliable "this is their real contact"
   > signal than a single mention — and falling back to the channel's
   > About description if no repeated one is found. If both come up
   > empty, it pages back through `EMAIL_DEEP_SCAN_PAGES` (default 2)
   > pages of *older* uploads and applies the same repeat test across
   > everything scanned so far, at 2 quota units per page. If that also
   > comes up empty and `INFLUENCERS_API_KEY` is set, it asks
   > influencers.club to resolve the channel ID to a validated address —
   > one HTTP call, and nothing is billed when no address is found. If
   > that misses too and `USE_PLAYWRIGHT_STEALTH=true`, a last-resort
   > lookup follows the channel's public external link list in Playwright
   > with stealth enabled — each link that isn't a social/platform domain,
   > then that site's `/contact` page — and applies the same pattern.
   > Hunter.io and Modash have been removed and are not coming back.
   > Often still blank; treat as a bonus signal, not a guarantee.

   > **Optional readable counts**: `Subscriber Count` and `Avg Views` stay
   > as Number fields (so you can still sort/filter numerically) but you
   > can add two Formula fields per table for a human-readable version —
   > `Subscribers (Display)` and `Avg Views (Display)`, formatted like
   > `"121K Subscribers"` / `"3.5M Subscribers"`. Formulas:
   > ```
   > IF(
   >     {Subscriber Count} >= 1000000,
   >     ROUND({Subscriber Count} / 1000000, 1) & "M Subscribers",
   >     IF(
   >         {Subscriber Count} >= 1000,
   >         ROUND({Subscriber Count} / 1000, 0) & "K Subscribers",
   >         {Subscriber Count} & " Subscribers"
   >     )
   > )
   > ```
   > Same pattern for `Avg Views (last 10 videos)`, suffixed `" Avg Views"`.
   > Pipeline code needs no changes for this — it's purely an Airtable-side
   > computed field.
8. Grab your Base ID from the base's API docs page
   (https://airtable.com/api, select your base — the ID starts with `app`).

### 2. Create a Google Cloud project + YouTube Data API key

1. Go to https://console.cloud.google.com/ and create a new project (or
   reuse one).
2. Go to **APIs & Services > Library**, search for **YouTube Data API v3**,
   and enable it.
3. Go to **APIs & Services > Credentials**, click **Create credentials >
   API key**.
4. (Recommended) Restrict the key to the YouTube Data API v3 to limit
   blast radius if it ever leaks.
5. Copy the key.

Note: the free tier gives you 10,000 quota units/day. A `search.list` call
costs 100 units; `channels.list`, `playlistItems.list`, and `videos.list`
each cost ~1 unit. This is why discovery (search) quota is capped
separately and more conservatively than enrichment quota.

### 3. Install dependencies

```bash
cd channel-vetting
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS/Linux
pip install -r requirements.txt
```

### 3b. Optional: Playwright + stealth for the browser email step

The last step of the email fallback chain (`browser_email.py`) uses
Playwright with stealth to follow a channel's public external link list to
the creator's own site. It is off by default; enable it with
`USE_PLAYWRIGHT_STEALTH=true`. If Playwright has not yet downloaded
Chromium, run `python -m playwright install chromium` once first.

To try that browser-backed path against already-tracked, email-less rows
without re-running discovery:

```bash
python backfill_missing_emails.py --use-playwright-stealth
```

That adds a public-page browser check on top of the free text-based email
steps — there is no paid fallback to disable.

### 4. Configure environment variables

```bash
copy .env.example .env        # Windows
# cp .env.example .env        # macOS/Linux
```

Fill in `.env` with your `AIRTABLE_TOKEN`, `AIRTABLE_BASE_ID`,
`AIRTABLE_TABLE_HOME_THEATER`, `AIRTABLE_TABLE_LIFESTYLE_SOFA` (the two
table IDs from step 1.7), and `YOUTUBE_API_KEY`. Everything else in
`.env.example` is optional and defaults sensibly:

| Variable | Default | Purpose |
|---|---|---|
| `QUOTA_CEILING` | 8000 | YouTube quota ceiling per day (of the 10,000 free-tier budget) |
| `API_SLEEP_SECONDS` | 0.5 | Delay between individual API calls |
| `DAILY_QUALIFIED_CAP` | 30 | Max qualified rows pushed per niche table per day |
| `DAILY_FLAGGED_CAP` | 10 | Max flagged (below-criteria) rows pushed per niche table per day |
| `CANDIDATE_OVERSHOOT` | 1.5 | Multiple of the remaining row shortfall that one discovery round banks in fresh candidates. Sizes a round only — `run_niche()` keeps discovering until the qualified cap is met or the keywords run out, so this does not limit the day's yield |
| `EXPECTED_CANDIDATES_PER_KEYWORD` | 40 | Unique channels one keyword is expected to yield (measured ~42 at `max_results=50` over a 7-day window). Converts a row shortfall into a keyword count for the next discovery round |
| `DISCOVERY_DAYS_BACK` | 7 | How many days back `search.list` looks for videos (short and self-renewing by design — see below; `--days-back` overrides per run) |
| `PROSPECT_DAY_TZ` | `America/Toronto` | Timezone defining a "prospect day" for the daily caps above — deliberately separate from `quota_tracker.py`'s Pacific-Time YouTube quota clock |
| `EMAIL_DEEP_SCAN_PAGES` | 2 | Extra pages of older uploads scanned for a contact email when the free steps find nothing (2 quota units per page, per channel; 0 disables) |
| `LONGFORM_SCAN_MAX_PAGES` | 3 | Extra pages of older uploads paged through to confirm 30+ non-Shorts videos, and only for channels the newest 50 left short of that bar (2 quota units per page; 0 judges on the newest 50 alone) |
| `USE_PLAYWRIGHT_STEALTH` | `false` | Enables the Playwright link-list email fallback (see §3b). The search-zone filter does not depend on it. `USE_CLOAKBROWSER` is still accepted as an alias |
| `INFLUENCERS_API_KEY` | _(unset)_ | Enables influencers.club **discovery** (replacing `search.list`) and email chain **step 4** (enrich-by-handle). Unset means both are skipped and discovery falls back to `search.list` — the pipeline runs fine without it |
| `INFLUENCERS_BASE_URL` | `https://api-dashboard.influencers.club` | API host override |
| `INFLUENCERS_MAX_LOOKUPS_PER_RUN` | 100 | Hard cap on step-4 email lookups per run, bounding credit spend. Only channels the free steps missed consume one, and a lookup that finds no address is not billed |
| `INFLUENCERS_MAX_DISCOVERY_CREDITS_PER_RUN` | 50 | Per-run credit ceiling for discovery (0.01 credits per creator returned). A runaway guard, not a normal-use limit |

### 5. Edit your keywords / niches

`main.py`'s `NICHES` dict holds one entry per niche: its search keywords
(real terms pulled from the Types of Content Posting > Primary sections of
the "Lifestyle Sofa" and "Home Theater" Influencer Profiling briefs,
Cynthia Lim, 15 April 2024), which Airtable table it pushes to, and its
qualification thresholds — `min_avg_views` and `min_channel_age_months`
(`None` if the niche has no age requirement, as with Lifestyle Sofa).

Each entry also carries a `discovery_filters` dict — the server-side
filters influencers.club discovery uses: content language, creator
`gender`, a subscriber floor, and an `ai_search` description of the niche
(or `topics` codes where the taxonomy fits). When `INFLUENCERS_API_KEY` is
set these drive discovery; the `keywords` are only the `search.list`
fallback used when no key is configured. Reword `ai_search` to steer which
creators surface.

Note that `min_avg_views` is **10,000 for both niches** as of the 2026-08
criteria change. Lifestyle Sofa's brief says 2,000; that was deliberately
overridden to put the two niches on the same bar. The other two shared
requirements — 30+ public videos and the allowed search zones — aren't
per-niche knobs: they live in `MIN_VIDEO_COUNT` (`main.py`) and
`search_zones.py`.

Add/replace keywords as new niche briefs come in — pull from a brief's
actual content-type list, not its demographic/psychographic sections
(those describe the audience, not searchable video topics). To add a
whole new niche, add a new `NICHES` entry with all four keys, plus a
matching env var and Airtable table — a niche entry missing either
threshold key is skipped (with a logged error) rather than crashing the
run.

### 6. Run the test flow first

```bash
python main.py --test
```

This runs on the first niche only and, unless you pass `--daily-cap`,
bounds the run to **2 qualified / 1 flagged** rows so it stays cheap. That
bound matters: when influencers.club discovery is active it fills the daily
cap rather than honouring `max_results`, so without it a "test" would
discover toward a full day of real credits and quota. It's enough to
confirm YouTube, Airtable (and influencers.club, if the key is set) are all
wired up correctly. Pass `--daily-cap N` to set a different bound for that
run — useful for testing the capping behavior against production Airtable.

### 7. Run the full pipeline

```bash
python main.py
```

> **First run against an empty (or recently emptied) table:** the default
> discovery window (`DISCOVERY_DAYS_BACK`, 7 days) is deliberately short —
> see "Discovery window" in `CLAUDE.md` — so a plain `python main.py` on a
> table with no existing rows will skip anything published more than a
> week ago and likely come back mostly empty. For that first sweep, run
> `python main.py --days-back 90` instead to pull in the backlog; switch
> back to the plain 7-day default for every run after that.

## Files

| File | Purpose |
|---|---|
| `config.py` | Loads `.env`, defines constants (quota ceiling, daily caps, weights inputs, etc.) |
| `http_client.py` | Shared retrying HTTP sessions (Airtable / YouTube / influencers.club); API keys travel as headers, never query params |
| `influencer_discovery.py` | influencers.club creator-search discovery source (replaces `search.list` when a key is set) |
| `discovery.py` | `search.list`-based channel discovery + per-day search cache (the discovery fallback) |
| `enrichment.py` | `channels.list` + `playlistItems.list` + `videos.list` stats |
| `scoring.py` | Fake-follower risk heuristic + weighted overall score + `qualify()` (channel age) |
| `search_zones.py` | Allowed-country tables (US/CA/UK/EU/AU, minus Ireland) + `zone_verdict()` |
| `do_not_contact.py` | DO NOT CONTACT suppression list — fetched fresh every run, fails closed |
| `external_dedupe.py` | 24h-cached @handle index over the base's other YouTube tables, to skip channels already tracked elsewhere |
| `influencers.py` | influencers.club enrich-by-handle lookup (step 4 of the email chain) |
| `browser_email.py` | Playwright link-list email fallback (last step of the email chain) |
| `prospect_day.py` | Single source of truth for "what day is it" for the daily caps (`PROSPECT_DAY_TZ`) |
| `airtable_client.py` | Dedupe check, create/update records, `count_added_today()` (per-table, one table per niche) |
| `quota_tracker.py` | Daily quota spend log (resets at midnight Pacific Time) |
| `audit_blocklist.py` | One-off: check rows already in the niche tables against DO NOT CONTACT |
| `backfill_missing_emails.py` | One-off: re-run the email chain over rows that have no email yet |
| `cleanup_external_duplicates.py` | One-off: delete niche-table rows already tracked elsewhere in the base (guarded by `--confirm`) |
| `main.py` | Orchestrates the full pipeline; `--test` and `--daily-cap` flags |
| `tests/` | pytest suite (see "Running the tests" below) |

## Running on a schedule (GitHub Actions)

`.github/workflows/channel-vetting.yml` runs the full pipeline daily at
09:00 UTC (safely after the YouTube quota resets at midnight Pacific) and
can also be triggered manually from the Actions tab, with an option to run
in `--test` mode.

Setup:
1. Push this repo to GitHub.
2. In the repo, go to **Settings > Secrets and variables > Actions > New
   repository secret** and add the five required secrets: `AIRTABLE_TOKEN`,
   `AIRTABLE_BASE_ID`, `AIRTABLE_TABLE_HOME_THEATER`,
   `AIRTABLE_TABLE_LIFESTYLE_SOFA`, `YOUTUBE_API_KEY` — same values as your
   local `.env`. Optionally add `INFLUENCERS_API_KEY` to turn on
   influencers.club discovery and email step 4; without it the scheduled run
   falls back to `search.list`. No browser license secret is needed.
3. The workflow pins `PROSPECT_DAY_TZ` to `America/Toronto` in its env and
   turns `USE_PLAYWRIGHT_STEALTH` **on for scheduled runs** — which also runs
   the `playwright install --with-deps chromium` step, since pip installs the
   Playwright driver but not the browser binary. Caveat: turning it on makes
   the browser email step *run*, but GitHub-hosted runners sit on Azure
   datacenter IPs that YouTube challenges hard, and stealth patches browser
   fingerprints, not IP reputation — so "running" is not "working". `main.py`
   logs a warning when the step was requested but the browser could not
   start; watch the run log for it.
4. To run immediately, use **Actions > Channel Vetting Pipeline > Run
   workflow**. That manual dispatch exposes a `use_playwright_stealth` toggle
   (on by default) — uncheck it to run without the browser step.
5. To change the schedule, edit the `cron` line in the workflow file
   (cron is UTC; see https://crontab.guru to build a new expression).

## Running the tests

```bash
python -m pytest
```

Runs the full suite in `tests/` (~570 tests; for the exact count run
`python -m pytest --collect-only -q | tail -1`) covering
discovery windowing/early-stop, the influencers.club discovery source
(pagination, credit budget, handle→channel-ID bridging, and its DO NOT
CONTACT / already-tracked exclusion), the pre-push gate (view floor,
video-count floor, dead and Shorts-only channels), the search-zone tables
and their three-state verdict, qualification, the DO NOT CONTACT
fail-closed paths, `count_added_today()`/daily-cap behavior,
`prospect_day.py`, the candidate pre-filter, every step of the email chain
(including the older-uploads scan's quota arithmetic and which step gets
credited for a hit), and a regression check that the removed paid
email-finder integrations (Hunter.io, Modash) stay removed. No network
calls or real credentials are needed — everything is mocked.

## Tuning

- Adjust scoring weights and thresholds at the top of `scoring.py`.
- Adjust `QUOTA_CEILING` in `.env` if you want more/less headroom for
  enrichment calls after discovery.
- Adjust `DAILY_QUALIFIED_CAP` / `DAILY_FLAGGED_CAP` in `.env` if 30/10 per
  niche per day doesn't match your review team's actual capacity. Note
  that with only the channel-age criterion left able to flag a row,
  Lifestyle Sofa (no age requirement) never produces a flagged row, so its
  flagged budget goes unused.
- Per-niche thresholds (`min_avg_views`, `min_channel_age_months`) live on
  each `NICHES` entry in `main.py`, not in `.env`.
- The two shared hard requirements are elsewhere: `MIN_VIDEO_COUNT` (30) at
  the top of `main.py`, and the allowed countries in `search_zones.py`
  (`ALLOWED_COUNTRY_CODES`, plus the name tables the About-panel lookup
  uses). Widening "Europe" to include Russia, Belarus or Turkey is a
  one-line edit there — they're excluded by default and flagged in a
  comment.
- `DEFAULT_NICHE_MATCH` in `main.py` is a fixed placeholder fed into every
  Overall Score, since automated topical/niche matching isn't implemented —
  reviewers can factor niche fit in manually via the Airtable
  "Notes"/"Status" fields.
