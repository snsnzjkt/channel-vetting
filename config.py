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

# The FLOOR cost of examining one candidate: channels.list to resolve it, then
# playlistItems + videos.list for the performance window. Optional extras on top
# (long-form paging, the email deep scan) are not counted here — see
# quota_tracker.can_afford_enrichment() for why the floor is the right figure to
# gate on.
QUOTA_COST_ENRICHMENT = (
    QUOTA_COST_CHANNELS_LIST + QUOTA_COST_PLAYLIST_ITEMS_LIST + QUOTA_COST_VIDEOS_LIST
)

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
#
# This is now a BATCH-SIZING hint only, not the thing that decides whether
# the day's cap can be reached. It used to be both, and that was a bug: the
# 2026-08 criteria change made the view floor, the video-count floor and the
# search zone HARD discards (see pre_push_drop_reason), which dropped the
# share of fresh candidates that can become a row to ~15% — measured 18
# survivors from 122 fresh candidates on 2026-08-11. At 1.5x, a 40-row
# budget was fed 60 candidates and could only ever yield ~9 rows, and
# discovery then stopped for good with most keywords never searched. The
# caps were unreachable by construction.
#
# run_niche() now REFILLS: it discovers a batch, pushes what survives, and
# goes back for more keywords while budget and keywords both remain. So this
# value only sets how much is searched per round trip — too low costs an
# extra round trip, too high costs quota on candidates the day didn't need.
CANDIDATE_OVERSHOOT = float(os.getenv("CANDIDATE_OVERSHOOT", 1.5))

# Unique channels one keyword yields at max_results=50 over a 7-day window.
# Measured at ~42 (127 unique channels across 3 Home Theater keywords,
# 2026-08-11). Used only to convert a row shortfall into a keyword count for
# the next discovery batch; the refill loop is what actually guarantees the
# budget fills, so drift here costs a round trip, never a short day.
EXPECTED_CANDIDATES_PER_KEYWORD = int(os.getenv("EXPECTED_CANDIDATES_PER_KEYWORD", 40))

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

# --- Long-form confirmation (the "30+ videos that aren't Shorts" gate) ---
# Extra pages of OLDER uploads to page through when confirming a channel has
# main.MIN_LONGFORM_VIDEO_COUNT non-Shorts videos, and only for channels the
# newest-50 window left short of that bar. 2 quota units per page.
#
# 3 pages means up to ~200 videos examined, so a channel needs roughly a 15%
# long-form rate to reach 30 — which is the point of the cap. It admits a
# genuine mixed-format channel (measured 20-26 long-form per 50 on real
# candidates) and rejects a Shorts factory (2-7 per 50) without paging
# through its whole catalogue. Raising it buys back only channels that post
# long-form very rarely, which is the opposite of what a 30-video floor asks.
# Set to 0 to judge on the newest-50 window alone.
LONGFORM_SCAN_MAX_PAGES = int(os.getenv("LONGFORM_SCAN_MAX_PAGES", 3))

# --- Browser-based email fallback ---
# Playwright + stealth follows the channel's public external link list to
# the creator's own site (and its /contact page), plus a Facebook page's
# /about. It is not a CAPTCHA bypass and does not touch YouTube's gated
# "business inquiries" address.
USE_PLAYWRIGHT_STEALTH = os.getenv(
	"USE_PLAYWRIGHT_STEALTH",
	# The old name, still honoured so an existing .env keeps working. This
	# fallback IS the whole back-compat surface — nothing imports a
	# module-level USE_CLOAKBROWSER alias, so don't add one back.
	os.getenv("USE_CLOAKBROWSER", "false"),
).lower() == "true"

# --- influencers.club (email chain step 4) ---
# A creator-data platform whose enrich-by-handle endpoint resolves a
# YouTube channel ID to a validated contact address. Runs BEFORE the
# browser step because it is one HTTP call against up to four page loads,
# and because `must_have` (below) makes a miss free.
INFLUENCERS_API_KEY = os.getenv("INFLUENCERS_API_KEY")
INFLUENCERS_BASE_URL = os.getenv(
    "INFLUENCERS_BASE_URL",
    "https://api-dashboard.influencers.club",
)

# The "profile" variant, deliberately, not "full". Both return the email;
# full additionally returns growth curves, medians, income estimates and
# audience demographics for 5x the price (1 credit vs 0.2), and this
# pipeline scores channels off the YouTube Data API instead. Don't "upgrade"
# this to full without a reason that isn't the word "full".
INFLUENCERS_ENRICH_PATH = "/public/v1/creators/enrich/handle/profile/"

# Bounds the credit spend of a single run the way EMAIL_DEEP_SCAN_PAGES
# bounds the deep scan's quota spend. Only channels that reached step 4 —
# i.e. the free steps missed — consume one, and only a returned address is
# billed (see EMAIL_REQUIRED below), so this caps a runaway, not normal use.
INFLUENCERS_MAX_LOOKUPS_PER_RUN = int(
    os.getenv("INFLUENCERS_MAX_LOOKUPS_PER_RUN", 100)
)

# "must_have" over the "preferred" default: the docs state no credits are
# charged for an empty result or a failed validation under must_have, which
# turns a miss into a free call. "preferred" would bill 0.2 credits to be
# told the address it found is unvalidated — and an unvalidated address is
# worth nothing to an outreach table a human works from.
INFLUENCERS_EMAIL_REQUIRED = "must_have"

# --- influencers.club discovery (creator search — replaces search.list) ---
# The same account/key, a different endpoint: POST /public/v1/discovery/
# filters creators server-side on the criteria the pipeline already gates on
# (English content, subscriber floor, an allowed country, a niche topic), so
# a far larger fraction of what it returns can become a row than the ~15% of
# raw YouTube search results that survive. Verified live 2026-08-13: the
# response is {total, accounts:[{user_id, profile:{username, full_name,
# followers, engagement_percent}}], credits_left, credits_cost}.
INFLUENCERS_DISCOVERY_PATH = "/public/v1/discovery/"

# UNLIKE search.list, discovery costs real money, not free YouTube quota:
# 0.01 credits per creator RETURNED (measured). This ceiling bounds a run the
# way INFLUENCERS_MAX_LOOKUPS_PER_RUN bounds the enrich step — a runaway
# guard, not a normal-use limit (30 rows/table needs on the order of 1-3
# credits of discovery once exclude_handles is filtering out the known base).
#
# LOWERED from 50 to 6 (2026-08-14). 50 credits is 5,000 creators, and the run
# was structurally able to reach it: 2 niches x DISCOVERY_MAX_ROUNDS (50) x the
# 0.5 credits a 50-result page costs = exactly 50. A live run spent 16 credits
# for ONE qualified row before anyone noticed, because the ceiling was set so
# far above normal use (1-3 credits) that it could only ever catch a runaway
# after the money was gone. 6 leaves ~2x headroom over a full two-niche day and
# turns a silent overspend into a loud warning. Raise it deliberately for a
# backlog sweep; do not raise it to make a low-yield day fill its cap — that is
# the pre-2026-08-14 mistake, and the yield problem is upstream (see the
# per-video view floor in main.py).
INFLUENCERS_MAX_DISCOVERY_CREDITS_PER_RUN = float(
    os.getenv("INFLUENCERS_MAX_DISCOVERY_CREDITS_PER_RUN", 6)
)

# --test is a smoke test, so give it its own much tighter discovery ceiling.
# The row caps alone do NOT bound discovery spend — they make it WORSE. A small
# cap shrinks `target`, and `target` never drops below the vendor's 50-result
# minimum billable page, so each round buys 50 creators to examine 3 and the
# waste-per-round grows as the cap shrinks. One page per niche is all a smoke
# test needs.
INFLUENCERS_TEST_DISCOVERY_CREDITS = float(
    os.getenv("INFLUENCERS_TEST_DISCOVERY_CREDITS", 1)
)

# exclude_handles caps at 10,000 entries per request (vendor limit, verified
# accepted at HTTP 200). This is the credit-safety mechanism: a creator the
# query would re-return that is already in our base is 0.01 spent on a row we
# already have, so excluding server-side means the vendor never returns — and
# never bills — them. A larger base than 10k needs the exclusion split or the
# persistent server-side exclusion list (not yet wired — see the discovery
# module).
INFLUENCERS_MAX_EXCLUDE_HANDLES = 10_000
