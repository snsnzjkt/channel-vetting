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
1. **Discovery** (`discovery.py`) — searches YouTube for videos matching your
   niche keywords (`search.list`, type=video) and extracts the unique
   channels behind the results. Results are cached per keyword per day so
   re-running the same keyword twice in one day costs no extra quota. The
   search window defaults to the last `DISCOVERY_DAYS_BACK` (7) days —
   deliberately short and self-renewing, rather than a wide fixed window
   that would return the same already-tracked channels every day. Stops
   searching further keywords once enough fresh (not-yet-tracked)
   candidates are banked to fill the day's remaining headroom.
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
   > comes up empty and `USE_PLAYWRIGHT_STEALTH=true`, a last-resort
   > lookup follows the channel's public external link list in Playwright
   > with stealth enabled — each link that isn't a social/platform domain,
   > then that site's `/contact` page — and applies the same pattern.
   > There is no paid email-finder fallback (Hunter.io and Modash have
   > been removed). Often still blank; treat as a bonus signal, not a
   > guarantee.

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

### 3b. Optional: add Playwright + stealth for site testing

If you want to test a site with a stealth Chromium wrapper, this repo
now includes a small smoke-test script:

```bash
python cloakbrowser_test.py --url https://example.com
```

For protected sites, the smoke test supports headed mode and proxies:

```bash
python cloakbrowser_test.py --url https://target-site.example --headed --proxy http://user:pass@host:port
```

If Playwright has not yet downloaded Chromium, run `python -m playwright
install chromium` once before using the script.

To try the browser-backed email backfill path on already-tracked
records, run:

```bash
python backfill_missing_emails.py --use-playwright-stealth
```

That adds a public-page browser check (following the channel's external
link list to the creator's own site) on top of the free text-based steps —
there is no paid fallback to disable.

### 3c. Optional: `cloakbrowser-mcp` for interactive agent use

This repo's `.mcp.json` registers `cloakbrowser-mcp` (an npm package,
pinned to `1.10.0`) as an MCP server named `cloakbrowser`. It lets Claude
Code (or any other MCP client opened in this directory) drive a real
CloakBrowser session interactively — useful for poking at a channel's
actual page to prototype extraction logic or debug selectors before that
logic lands in `enrichment.py`.

This is **entirely separate from the pipeline**: `main.py` and
`browser_email.py` drive Playwright directly and never touch `.mcp.json`
or the MCP server — no `cloakbrowser` Python package is installed at all
any more. The MCP server
requires an LLM client to drive it, which the scheduled
`python main.py` run has no reason to grow. It is **not** used by, and
not required for, the GitHub Actions workflow — the pipeline runs fine
with `.mcp.json` absent entirely.

No secret is stored in `.mcp.json` — it references
`CLOAKBROWSER_LICENSE_KEY` from your shell/session environment via
`${CLOAKBROWSER_LICENSE_KEY:-}` and runs unlicensed if that variable
isn't set. Requires Node.js (tested with Node 24.12.0 / npm 11.6.2) since
`cloakbrowser-mcp` is fetched via `npx` on first use. After adding or
editing `.mcp.json`, restart your MCP client and check `/mcp` (Claude
Code) to confirm `cloakbrowser` is listed.

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
| `CANDIDATE_OVERSHOOT` | 1.5 | Multiple of remaining daily headroom that discovery banks in fresh candidates, to cover losses to enrichment failures and dedupe |
| `DISCOVERY_DAYS_BACK` | 7 | How many days back `search.list` looks for videos (short and self-renewing by design — see below; `--days-back` overrides per run) |
| `PROSPECT_DAY_TZ` | `America/Toronto` | Timezone defining a "prospect day" for the daily caps above — deliberately separate from `quota_tracker.py`'s Pacific-Time YouTube quota clock |
| `EMAIL_DEEP_SCAN_PAGES` | 2 | Extra pages of older uploads scanned for a contact email when the free steps find nothing (2 quota units per page, per channel; 0 disables) |
| `USE_PLAYWRIGHT_STEALTH` | `false` | Enables the Playwright link-list email fallback (see "Browser path" below). The search-zone filter does not depend on it. `USE_CLOAKBROWSER` is still accepted as an alias |

### 5. Edit your keywords / niches

`main.py`'s `NICHES` dict holds one entry per niche: its search keywords
(real terms pulled from the Types of Content Posting > Primary sections of
the "Lifestyle Sofa" and "Home Theater" Influencer Profiling briefs,
Cynthia Lim, 15 April 2024), which Airtable table it pushes to, and its
qualification thresholds — `min_avg_views` and `min_channel_age_months`
(`None` if the niche has no age requirement, as with Lifestyle Sofa).

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

This runs with 1 keyword, `max_results=5`, on the first niche only, so it
costs at most ~100 units of search quota plus a handful of enrichment
units — enough to confirm YouTube and Airtable are both wired up
correctly without burning a meaningful chunk of your daily budget. Add
`--daily-cap N` to also cap both the qualified and flagged daily budgets
at `N` for that run — useful for testing the capping behavior cheaply
against your production Airtable base.

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
| `discovery.py` | `search.list`-based channel discovery + per-day search cache |
| `enrichment.py` | `channels.list` + `playlistItems.list` + `videos.list` stats |
| `scoring.py` | Fake-follower risk heuristic + weighted overall score + `qualify()` (channel age) |
| `search_zones.py` | Allowed-country tables (US/CA/UK/EU/AU, minus Ireland) + `zone_verdict()` |
| `do_not_contact.py` | DO NOT CONTACT suppression list — fetched fresh every run, fails closed |
| `browser_email.py` | Playwright link-list email fallback (last step of the email chain) |
| `prospect_day.py` | Single source of truth for "what day is it" for the daily caps (`PROSPECT_DAY_TZ`) |
| `airtable_client.py` | Dedupe check, create/update records, `count_added_today()` (per-table, one table per niche) |
| `quota_tracker.py` | Daily quota spend log (resets at midnight Pacific Time) |
| `audit_blocklist.py` | One-off: check rows already in the niche tables against DO NOT CONTACT |
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
   repository secret** and add five secrets: `AIRTABLE_TOKEN`,
   `AIRTABLE_BASE_ID`, `AIRTABLE_TABLE_HOME_THEATER`,
   `AIRTABLE_TABLE_LIFESTYLE_SOFA`, `YOUTUBE_API_KEY` — same values as
   your local `.env`. That's the full list; no browser license secret is
   needed any more.
3. The workflow pins `PROSPECT_DAY_TZ` to `America/Toronto` in its env, and
   runs with `USE_PLAYWRIGHT_STEALTH` **off** by default even though the
   runner env var exists — GitHub-hosted runners sit on Azure datacenter
   IPs that YouTube challenges hard, and stealth patches browser
   fingerprints, not IP reputation. To test whether it works from a runner
   anyway, trigger the workflow manually (**Actions > Channel Vetting
   Pipeline > Run workflow**) with its `use_playwright_stealth` input
   checked — that also triggers the `playwright install --with-deps
   chromium` step, since pip installs the driver but not the browser
   binary. Only make it the schedule's default once a manual run has
   proven it out.
4. The workflow will run automatically on schedule; to run it immediately
   without the browser step, use the same **Run workflow** button.
5. To change the schedule, edit the `cron` line in the workflow file
   (cron is UTC; see https://crontab.guru to build a new expression).

## Running the tests

```bash
python -m pytest
```

Runs the full suite in `tests/` (257 tests at time of writing) covering
discovery windowing/early-stop, the pre-push gate (view floor, video-count
floor, dead and Shorts-only channels), the search-zone tables and their
three-state verdict, qualification, the DO NOT
CONTACT fail-closed paths, `count_added_today()`/daily-cap behavior,
`prospect_day.py`, the candidate pre-filter, every step of the email
chain (including the older-uploads scan's quota arithmetic and which
step gets credited for a hit), the browser About-panel reader, and a
regression check that the removed paid email-finder integrations
(Hunter.io, Modash) stay removed. No network calls or real credentials
are needed — everything is mocked.

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
