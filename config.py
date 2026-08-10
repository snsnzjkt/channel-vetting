"""
Loads environment variables and defines pipeline-wide constants.

All secrets come from .env (never hardcoded). Copy .env.example to .env
and fill in real values before running the pipeline.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# --- Airtable ---
AIRTABLE_TOKEN = os.getenv("AIRTABLE_TOKEN")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")

# One table per niche (see NICHES in main.py) — each niche's discovered
# channels are pre-filtered, deduped, and pushed against its own table
# independently, so a channel can legitimately appear in both if it's
# relevant to both niches.
AIRTABLE_TABLE_HOME_THEATER = os.getenv("AIRTABLE_TABLE_HOME_THEATER")
AIRTABLE_TABLE_LIFESTYLE_SOFA = os.getenv("AIRTABLE_TABLE_LIFESTYLE_SOFA")

# --- YouTube ---
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
YOUTUBE_API_BASE_URL = "https://www.googleapis.com/youtube/v3"

# --- Quota management ---
# Free tier daily allotment is 10,000 units. We cap ourselves below that
# to leave headroom for enrichment calls (channels/playlistItems/videos.list)
# that follow every successful search.
DAILY_QUOTA_BUDGET = 10000
QUOTA_CEILING = int(os.getenv("QUOTA_CEILING", 8000))

# Known YouTube Data API v3 costs (units per call), per Google's published
# quota calculator. These are used by quota_tracker to log spend accurately.
QUOTA_COST_SEARCH_LIST = 100
QUOTA_COST_CHANNELS_LIST = 1
QUOTA_COST_PLAYLIST_ITEMS_LIST = 1
QUOTA_COST_VIDEOS_LIST = 1

# --- File paths (relative to project root) ---
SEARCH_CACHE_FILE = "search_cache.json"
QUOTA_LOG_FILE = "quota_log.json"

# --- Pipeline behavior ---
# Seconds to sleep between individual API calls, to stay well under
# per-second rate limits and be a good API citizen.
API_SLEEP_SECONDS = float(os.getenv("API_SLEEP_SECONDS", 0.5))

# Airtable "Status" single-select default for newly discovered channels.
DEFAULT_STATUS = "New"
SOURCE_LABEL = "YouTube Discovery Pipeline"

# --- Daily prospect caps ---
# "Prospect" = a record successfully pushed to Airtable. Counted per niche
# per day from Airtable's own "Date Added" field, so a second run on the
# same day tops up to the cap rather than doubling it.
#
# Each niche table produces at most 40 new rows per day, total. The two
# budgets are separate so a weak discovery day cannot fill the table with
# below-criteria channels and crowd out real prospects.
DAILY_QUALIFIED_CAP = int(os.getenv("DAILY_QUALIFIED_CAP", 30))
DAILY_FLAGGED_CAP = int(os.getenv("DAILY_FLAGGED_CAP", 10))

# Discovery banks this multiple of the remaining headroom in fresh
# candidates, covering the ones lost to enrichment failure and dedupe.
CANDIDATE_OVERSHOOT = float(os.getenv("CANDIDATE_OVERSHOOT", 1.5))

# How far back search.list looks for videos, in days.
#
# Deliberately a SHORT rolling window rather than a fixed 90 days. Search
# results are ranked by relevance and that ranking is stable, so a wide
# fixed window returns the same channels every day; once they are all
# tracked, the pipeline produces nothing while still spending ~100 units
# per keyword re-reading a consumed pool. A recent window is self-renewing
# because creators keep uploading.
#
# Use --days-back 90 for a one-off sweep of the backlog (e.g. the first
# run against an empty table).
DISCOVERY_DAYS_BACK = int(os.getenv("DISCOVERY_DAYS_BACK", 7))

# The zone that defines a "prospect day". Deliberately NOT the Pacific
# zone quota_tracker uses: quota tracks Google's reset schedule, this
# tracks review capacity on the reviewing team's working day.
#
# Pinned rather than host-local because three clocks are in play — the
# GitHub Actions runner (UTC), the dev machine (UTC+8), and head office
# (Toronto). Unpinned, a CI run and a local run would disagree about the
# date and each claim a separate daily cap.
PROSPECT_DAY_TZ = os.getenv("PROSPECT_DAY_TZ", "America/Toronto")

# --- Email deep scan (step 3 of the chain) ---
# Extra pages of OLDER uploads to scan for a contact email, and only for
# channels where the two free steps found nothing. Each page is 50 more
# video descriptions for 2 quota units (playlistItems.list + videos.list),
# so the worst case is bounded: 40 rows x 2 niches x 2 pages x 2 units =
# 320 units against a QUOTA_CEILING of 8000, and in practice far less
# because channels whose email is already known never trigger it.
# Set to 0 to disable the step entirely.
EMAIL_DEEP_SCAN_PAGES = int(os.getenv("EMAIL_DEEP_SCAN_PAGES", 2))

# --- Browser-based email fallback ---
# Playwright + stealth follows the channel's public external link list to
# the creator's own site (and its /contact page), plus a Facebook page's
# /about. It is not a CAPTCHA bypass and does not touch YouTube's gated
# "business inquiries" address.
USE_PLAYWRIGHT_STEALTH = os.getenv(
	"USE_PLAYWRIGHT_STEALTH",
	os.getenv("USE_CLOAKBROWSER", "false"),
).lower() == "true"

# Backward-compatible alias for existing env files and workflows.
USE_CLOAKBROWSER = USE_PLAYWRIGHT_STEALTH
