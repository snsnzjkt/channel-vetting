# Channel Vetting Pipeline

Discovers, enriches, scores, and pushes potential brand-partnership YouTube
channels into Airtable for human review.

## How it works

1. **Discovery** (`discovery.py`) — searches YouTube for videos matching your
   niche keywords (`search.list`, type=video) and extracts the unique
   channels behind the results. Results are cached per keyword per day so
   re-running the same keyword twice in one day costs no extra quota.
2. **Pre-filter** — candidates already present in your Airtable base are
   dropped before any enrichment quota is spent on them.
3. **Enrichment** (`enrichment.py`) — pulls subscriber/view counts and the
   last 10 videos' performance for each remaining candidate.
4. **Scoring** (`scoring.py`) — computes a fake-follower risk score and a
   weighted overall score.
5. **Airtable push** (`airtable_client.py`) — creates or updates a row per
   channel in the "Channel Prospects" table (never duplicates).

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
   note below), Content Language, Email (Single line text), Fake Follower
   Risk Score, Overall Score, Status (single select:
   New/Reviewing/Approved/Rejected/Contacted), Source, Notes, Date Added.

   Grab each table's ID (open the table → **Help → API documentation**,
   or read it from the URL — the `tbl...` segment) for step 4.

   > `Upload Frequency` is written as a formatted string (e.g. `"2.5
   > videos/month"`), not a raw number — if you make it a Number field
   > instead, update the `f"{upload_freq} videos/month"` line in
   > `main.py` to send `upload_freq` directly.

   > `Email`: YouTube's API does not expose a channel's gated
   > "business inquiries" email (it's behind a CAPTCHA-protected reveal
   > button specifically to block scraping, and this pipeline does not
   > attempt to bypass that). Instead, it does a best-effort regex scan
   > for a plain-text email, preferring one that recurs across at least
   > `EMAIL_MIN_VIDEO_REPEATS` (default 3) of the channel's sampled video
   > descriptions — a much more reliable "this is their real contact"
   > signal than a single mention — and falling back to the channel's
   > About description if no repeated one is found. Often still blank;
   > treat as a bonus signal, not a guarantee.
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

### 4. Configure environment variables

```bash
copy .env.example .env        # Windows
# cp .env.example .env        # macOS/Linux
```

Fill in `.env` with your `AIRTABLE_TOKEN`, `AIRTABLE_BASE_ID`,
`AIRTABLE_TABLE_HOME_THEATER`, `AIRTABLE_TABLE_LIFESTYLE_SOFA` (the two
table IDs from step 1.7), and `YOUTUBE_API_KEY`.

### 5. Edit your keywords / niches

`main.py`'s `NICHES` dict holds one entry per niche — its search keywords
(real terms pulled from the Types of Content Posting > Primary sections of
the "Lifestyle Sofa" and "Home Theater" Influencer Profiling briefs,
Cynthia Lim, 15 April 2024) and which Airtable table it pushes to.
Add/replace keywords as new niche briefs come in — pull from a brief's
actual content-type list, not its demographic/psychographic sections
(those describe the audience, not searchable video topics). To add a
whole new niche, add a new `NICHES` entry plus a matching env var and
Airtable table.

### 6. Run the test flow first

```bash
python main.py --test
```

This runs with 1 keyword, `max_results=5`, on the first niche only, so it
costs at most ~100 units of search quota plus a handful of enrichment
units — enough to confirm YouTube and Airtable are both wired up
correctly without burning a meaningful chunk of your daily budget.

### 7. Run the full pipeline

```bash
python main.py
```

## Files

| File | Purpose |
|---|---|
| `config.py` | Loads `.env`, defines constants (quota ceiling, weights inputs, etc.) |
| `discovery.py` | `search.list`-based channel discovery + per-day search cache |
| `enrichment.py` | `channels.list` + `playlistItems.list` + `videos.list` stats |
| `scoring.py` | Fake-follower risk heuristic + weighted overall score |
| `airtable_client.py` | Dedupe check, create/update records (per-table, one table per niche) |
| `quota_tracker.py` | Daily quota spend log (resets at midnight Pacific Time) |
| `main.py` | Orchestrates the full pipeline; `--test` flag for smoke testing |

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
   your local `.env`.
3. The workflow will run automatically on schedule; to run it immediately,
   go to **Actions > Channel Vetting Pipeline > Run workflow**.
4. To change the schedule, edit the `cron` line in the workflow file
   (cron is UTC; see https://crontab.guru to build a new expression).

## Tuning

- Adjust scoring weights and thresholds at the top of `scoring.py`.
- Adjust `QUOTA_CEILING` in `.env` if you want more/less headroom for
  enrichment calls after discovery.
- `DEFAULT_NICHE_MATCH` in `main.py` is a neutral placeholder (50/100)
  since automated topical/niche matching isn't implemented — reviewers can
  factor niche fit in manually via the Airtable "Notes"/"Status" fields.
