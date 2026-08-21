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

def env_flag(name: str, *, default: bool) -> bool:
    """
    Read a boolean env var so that a HALF-CONFIGURED environment fails in the
    direction the caller nominates.

    `default` is not just the value when unset — it decides which literal is
    load-bearing. With `default=False` only "true" enables; with `default=True`
    only "false" disables. Everything else — unset, empty, "0", "no", "off", a
    typo like "fasle" — leaves the flag at its default.

    That asymmetry is the point, and the two current callers want it pointed
    opposite ways for the same reason: each defaults toward the harmless
    outcome FOR ITS OWN FEATURE. A browser step that fails to start costs some
    email coverage; a demo gate that fails to start emails real creators.
    Making the safe direction an argument means the next flag has to state
    which way it fails instead of copying whichever neighbour it saw.
    """
    raw = (os.getenv(name) or "").strip().lower()
    if default:
        return raw != "false"
    return raw == "true"


# --- Pipeline behavior ---
# Seconds to sleep between individual API calls, to stay well under
# per-second rate limits and be a good API citizen.
API_SLEEP_SECONDS = float(os.getenv("API_SLEEP_SECONDS", 0.5))

# Airtable "Status" single-select default for newly discovered channels.
DEFAULT_STATUS = "New"
# The other two members of that same hand-maintained single-select, named here
# rather than spelled at each use site. Same reasoning that made
# outreach_ledger import scoring.QUALIFIED instead of re-typing "Qualified":
# two copies of an option name on a schema humans edit is how you end up with
# `Canada` AND `canada`.
#
# The QUERY side is the urgent half of this. A typo in the WRITE
# ("Contacted") sends typecast=False and 422s loudly. A typo in the filter
# ("Aproved") returns HTTP 200 with ZERO rows and no error at all — measured
# live 2026-08-14 — so the run reports an empty queue and silently sends
# nothing, forever.
STATUS_APPROVED = "Approved"
STATUS_CONTACTED = "Contacted"
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
# video descriptions for 2 quota units (playlistItems.list + videos.list).
#
# RAISED from 2 to 4 on 2026-08-20. This is the widest FREE step in the chain
# and the only one that can be widened without spending money: step 4 costs 0.2
# credits per hit and step 5 needs a browser that CI can barely run (datacenter
# IP reputation, see influencers.py). So when email coverage needs to improve,
# this is the lever that costs quota rather than cash.
#
# Worst case: 40 rows x 2 niches x 4 pages x 2 units = 640 units against a
# QUOTA_CEILING of 8000, and in practice far less — the step only runs for
# channels the two free steps missed, and 4 pages is a CEILING that
# scan_older_videos_for_email stops short of as soon as a repeated address is
# found. Measured coverage before the raise: 138 of 146 live rows already
# carried an address, so this is chasing the tail, which is exactly what a
# quota-only step should be spent on.
#
# Set to 0 to disable the step entirely.
EMAIL_DEEP_SCAN_PAGES = int(os.getenv("EMAIL_DEEP_SCAN_PAGES", 4))

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

# --- Credit ledger: the ceilings that survive the process ---
#
# The three limits above are all PER-RUN and live on an object, so they vanish
# with the process: two runs on the same day each got a full allowance, and
# nothing could report a monthly total. credit_tracker.py persists spend to
# CREDIT_LOG_FILE and enforces the two ceilings below across runs. See that
# module's docstring for why it fails CLOSED where quota_tracker fails open.
#
# Gitignored like quota_log.json and search_cache.json — it is machine-local
# state, and committing it would merge two machines' spend into one nonsense
# total.
CREDIT_LOG_FILE = os.getenv("CREDIT_LOG_FILE", "credit_log.json")

# --- Rejected-handle cache (server-side exclusion budget) ---
# Creators a niche's discovery query has already returned and our gates already
# rejected. See rejected_handles.py for why this exists; in short, the vendor
# bills 0.01 per creator RETURNED and sorts deterministically by relevancy, so
# without this the same rejects are re-bought every run.
REJECTED_HANDLES_FILE = os.getenv("REJECTED_HANDLES_FILE", "rejected_handles.json")

# How long a rejection is honoured before the creator is looked at again.
#
# This is a WINDOW, not a blacklist, and the length is the whole trade. Too short
# and we re-buy the same rejects on a fixed schedule, which is the leak. Too long
# and a channel that has since grown past the view floor stays invisible —
# growth is the entire reason a creator becomes a prospect, and the pipeline's
# own gates are the only thing that can notice it.
#
# 90 days: long enough that a daily run never re-buys the same reject (the leak
# was measured at 28% of a page), short enough that a channel gets four looks a
# year. A handle the query keeps returning is re-stamped on every rejection, so
# an actively-surfaced creator does not age out mid-window and get re-bought.
REJECTED_HANDLES_RETENTION_DAYS = int(os.getenv("REJECTED_HANDLES_RETENTION_DAYS", 90))

# A measured full two-niche day is ~7.3 credits (5.5 discovery + 1.8 email).
# 10 leaves room for that day plus a small manual top-up, while stopping a
# second full run from silently doubling it — which is the concrete waste this
# ledger was built to catch. Raise it deliberately for a backlog sweep.
INFLUENCERS_MAX_CREDITS_PER_DAY = float(
    os.getenv("INFLUENCERS_MAX_CREDITS_PER_DAY", 10)
)

# The brake in front of the vendor's FAIR-USE cap, which resets only at
# subscription renewal and which no amount of retrying clears (influencers.py
# trips a circuit breaker on that bodyless 429). 22 weekday runs at ~7.3 is
# ~161, so 200 is roughly 1.25x a full month.
#
# !! CHECK THIS AGAINST THE ACTUAL SUBSCRIPTION. !! The API does not expose the
# plan's credit allowance or its renewal date, so this default is derived from
# measured usage, NOT from the real entitlement. If the plan is smaller than
# 200/month this number is worse than useless — it would authorise spending
# past the real cap and the first symptom would be a bodyless 429 disabling
# email lookups mid-run.
INFLUENCERS_MAX_CREDITS_PER_MONTH = float(
    os.getenv("INFLUENCERS_MAX_CREDITS_PER_MONTH", 200)
)


# --- Gemini relevance verification (FREE TIER ONLY) -----------------------
#
# READ THIS FIRST. The cost guarantee for this integration is NOT in this file.
# Per Google's API terms, the Gemini API is a "Paid Service" *only* when reached
# through a Cloud project with an active billing account. The key in .env belongs
# to project 208204240231, which has NO billing account linked — so an
# over-quota request returns 429 RESOURCE_EXHAUSTED and CANNOT be billed. There
# is no API that reports a project's billing status, so nothing below can verify
# this; it is a property of which key is pasted in. Re-check after any rotation:
#   https://console.cloud.google.com/billing/linkedaccount?project=208204240231
#
# Everything below is the SECOND layer. See GEMINI_VERIFY_PLAN.md.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# The endpoint is `:generateContent`, which Google's docs now label LEGACY. That
# is deliberate and it is the whole reason this integration exists in this shape:
# the newer Interactions API (POST /v1beta/interactions) does NOT yet support
# `video_metadata`, the clipping field that lets us analyse 25 seconds instead of
# a whole 20-minute upload. Google states that limitation explicitly. Sending
# whole videos would burn the free tier's 8h/day YouTube allowance ~48x faster.
# TODOS.md carries the trigger to migrate once clipping lands there.
GEMINI_BASE_URL = os.getenv(
    "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"
)

# Both flags go through env_flag, which is DIRECTIONAL: with default=True only
# the literal "false" disables, so a typo ("ture", "0", "no") leaves the safe
# value in place. That asymmetry is the point here — the competing raw
# `os.getenv(...) == "true"` idiom used by USE_PLAYWRIGHT_STEALTH would let
# GEMINI_FREE_ONLY=ture silently switch off the model allowlist below.
#
# GEMINI_ENABLED defaults FALSE: this is a new unattended outbound call inside a
# scheduled CI job, so it is opt-in, the same way OUTREACH_DEMO_MODE defaults to
# the harmless direction.
GEMINI_ENABLED = env_flag("GEMINI_ENABLED", default=False)
GEMINI_FREE_ONLY = env_flag("GEMINI_FREE_ONLY", default=True)

# Hardcoded, never read from the environment: an operator-overridable allowlist
# is not an allowlist.
#
# WHAT THIS ACTUALLY PREVENTS is an operator TYPO, not a charge — see the header
# above for where the real guarantee lives. Do not read this constant as a cost
# control and relax the billing discipline that is doing the work.
#
# It also freezes a support snapshot dated 2026-08-21 with no expiry. Verified
# against Google's own `gemini-interactions-api` skill, which struck four models
# an earlier read of the pricing page had accepted:
#   gemini-2.5-flash, gemini-2.5-flash-lite -> "legacy and deprecated. Never use."
#   gemini-3.6-flash, gemini-3.5-flash      -> active legacy; use gemini-3.7-flash
# A pricing page proves a model is billed at zero, NOT that it is supported.
GEMINI_FREE_TIER_MODELS = frozenset({
    "gemini-3.7-flash",        # latest Flash
    "gemini-3.5-flash-lite",   # DEFAULT — latest Flash-Lite
    "gemini-3.1-flash-lite",   # prior Flash-Lite; upgrading to 3.5 is recommended
})
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

# Longer than any other timeout in this pipeline because Google fetches and
# decodes the YouTube segment server-side before the model sees a frame.
GEMINI_TIMEOUT = float(os.getenv("GEMINI_TIMEOUT", 60))

# One retry, and only for 5xx/network. NEVER for 429 (that is the free-tier wall,
# and retrying it is the one behaviour this integration must not have) and never
# for other 4xx (a stale request field would just fail identically).
GEMINI_MAX_RETRIES = int(os.getenv("GEMINI_MAX_RETRIES", 1))

# PROVISIONAL — the one threshold in this file with no measurement behind it,
# and it is labelled so rather than dressed up as tuned. Every other number here
# cites its provenance; this one cannot until there are verdicts to read.
#
# HOW TO TUNE IT: "Relevance Detail" records the confidence on every verdict.
# After ~2 weeks, set this just below the lowest confidence a human reviewer
# agreed with. Raising it only converts rescues into non-rescues, never into
# drops, so it is safe to move in either direction.
GEMINI_MIN_CONFIDENCE = float(os.getenv("GEMINI_MIN_CONFIDENCE", 0.6))

# Request ceilings. Sized on CANDIDATES EXAMINED, not rows written: a request is
# spent per candidate reaching the gate, while the daily row caps only advance on
# a successful push, so the two diverge. Both niches run in ONE process
# (main.run loops NICHES), so these are per-process totals across all niches.
#
# The VIDEO sub-caps are the only ones that touch the free tier's 8h/day YouTube
# allowance; the text tier is bounded by tokens alone.
#
# The brief's own spellings (MAX_GEMINI_REQUESTS_PER_RUN / _PER_DAY) are read as
# fallbacks so the operator's notes stay true, but the GEMINI_-prefixed names are
# canonical: every other vendor ceiling in this file is vendor-prefix-first
# (INFLUENCERS_MAX_*, OUTREACH_MAX_*) and there is no bare MAX_* var anywhere.
# MEASURED 2026-08-21, and this is the one number in this block that is not a
# guess. A backtest run issued 106 requests (103 text + 3 video) on
# gemini-3.5-flash-lite and Google answered the 107th with a PerDay 429. So the
# free-tier RPD for this model on this project is ~100/day — NOT the 600 an
# earlier revision defaulted to, which could never bind because Google's own
# limit hit first. Google no longer publishes per-model free RPD (it is
# per-project, visible only in AI Studio), so measurement is the only way to
# know, and the number may differ on another project.
#
# 80/day leaves headroom to stop BEFORE Google does, which matters: our own cap
# is a clean pause that marks candidates unavailable, whereas walking into
# Google's limit burns a request to discover it and latches the run.
GEMINI_MAX_REQUESTS_PER_RUN = int(
    os.getenv("GEMINI_MAX_REQUESTS_PER_RUN")
    or os.getenv("MAX_GEMINI_REQUESTS_PER_RUN", 70)
)
GEMINI_MAX_REQUESTS_PER_DAY = int(
    os.getenv("GEMINI_MAX_REQUESTS_PER_DAY")
    or os.getenv("MAX_GEMINI_REQUESTS_PER_DAY", 80)
)
# Video is a SUBSET of the totals above, so these only bite when video would
# otherwise crowd out the text tier. Also the only caps that touch the free
# tier's separate 8h/day YouTube allowance.
GEMINI_MAX_VIDEO_REQUESTS_PER_RUN = int(
    os.getenv("GEMINI_MAX_VIDEO_REQUESTS_PER_RUN", 30)
)
GEMINI_MAX_VIDEO_REQUESTS_PER_DAY = int(
    os.getenv("GEMINI_MAX_VIDEO_REQUESTS_PER_DAY", 40)
)

# Wall-clock brake. GEMINI_TIMEOUT x the run cap would exceed the workflow's own
# timeout-minutes, and a killed run prints NO run summary at all — the least
# legible failure this pipeline can produce.
GEMINI_MAX_SECONDS_PER_RUN = float(os.getenv("GEMINI_MAX_SECONDS_PER_RUN", 900))

# How many seconds of video to send, and where the window starts.
#
# NOT the first 25 seconds: the opening of a YouTube video is intro animation,
# channel branding and the sponsor read — the least representative footage on the
# timeline, and the segment most likely to show someone ELSE's product. The
# window starts at least GEMINI_CLIP_MIN_START_SECONDS in, or 25% through for
# longer uploads, whichever is later.
#
# Always reachable: every candidate video is drawn from a long-form set that
# requires a parseable duration > SHORTS_MAX_SECONDS (180), so a video shorter
# than the window is impossible by construction, not merely rare.
# Run the VIDEO tier on every candidate that reaches the relevance gate, not
# only on the ones the title gate flagged. Requested 2026-08-21: the operator
# wants a video-checked verdict on every row, not just on rescues.
#
# It costs one request per candidate, which against the MEASURED ~100/day free
# ceiling is the single biggest consumer of the budget — so it is a flag, and
# turning it off returns to rescue-path-only video.
GEMINI_VIDEO_ALWAYS = env_flag("GEMINI_VIDEO_ALWAYS", default=True)

# FALLBACK when a model's free daily quota runs out.
#
# READ THIS BEFORE ASSUMING IT VIOLATES THE NO-PAID-FALLBACK RULE — IT DOES NOT.
# Google's free RPD is per MODEL, not per project (measured 2026-08-21:
# gemini-3.5-flash-lite refused at ~106 requests while the other allowlisted
# models were untouched). So when one model is spent, the fallback moves to the
# next model ON THE HARDCODED FREE-TIER ALLOWLIST and keeps going. Every model in
# the chain is free-of-charge; there is no paid model anywhere in it, and none can
# be added, because the chain is built from GEMINI_FREE_TIER_MODELS and nothing
# else. A project with no billing account cannot be charged for any of them.
#
# The order is deliberate: cheapest-quota-first is meaningless when everything is
# free, so it runs lightest-model-first to keep latency and token use down, with
# the most capable model last as the final free option.
GEMINI_FALLBACK_ENABLED = env_flag("GEMINI_FALLBACK_ENABLED", default=True)
GEMINI_MODEL_CHAIN = (
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.7-flash",
)

# HOW STRICT the verdict is, WITHOUT touching the criteria text.
#
# The model returns an overall `matches` boolean plus a per-criterion breakdown.
# Trusting `matches` alone means EVERY criterion must satisfy the model before a
# candidate confirms. This ratio is the second, looser route: a candidate also
# confirms when at least this fraction of its individual criteria matched, even
# if the model's own aggregate said no.
#
# 0.5 with two criteria means one is enough. Raise it to 1.0 to require all of
# them again, which is the pre-2026-08-21 behaviour. This is the knob to move
# when the criteria are right but the bar is too high — it changes the judgement,
# never the question.
GEMINI_MIN_CRITERIA_RATIO = float(os.getenv("GEMINI_MIN_CRITERIA_RATIO", 0.5))

# The TEXT tier is OFF by default, and that is an evidence-based decision rather
# than a cost one. Measured 2026-08-21 across 96 reviewer-labelled rows
# (GEMINI_VERIFY_PLAN.md 2.16): its on_niche verdict is NOT predictive of the
# reviewer's Approved/Rejected — 27% against a 38% base rate, and 0 of 5 in Home
# Theater. Spending half of a ~100/day request budget on a signal measured as
# non-predictive is the wrong trade, so it is opt-in until the criteria are
# rewritten and re-measured with backtest_relevance.py.
#
# When on it is ADVISORY ONLY: it records a 0-100 relevance score for the
# reviewer and never gates a rescue. That changed on 2026-08-21 too — it used to
# gate the video tier, which made a non-predictive signal a precondition for
# every rescue.
GEMINI_TEXT_TIER = env_flag("GEMINI_TEXT_TIER", default=False)

GEMINI_CLIP_SECONDS = int(os.getenv("GEMINI_CLIP_SECONDS", 25))
GEMINI_CLIP_MIN_START_SECONDS = int(os.getenv("GEMINI_CLIP_MIN_START_SECONDS", 90))
GEMINI_CLIP_START_FRACTION = float(os.getenv("GEMINI_CLIP_START_FRACTION", 0.25))

# Local state. Env-overridable so the test fixture can redirect them to tmp_path
# — the credit ledger learned this the hard way by writing 10.14 real credits
# into the repo's own log. Matches CREDIT_LOG_FILE rather than the bare-constant
# QUOTA_LOG_FILE for exactly that reason.
GEMINI_LOG_FILE = os.getenv("GEMINI_LOG_FILE", "gemini_log.json")
GEMINI_CACHE_FILE = os.getenv("GEMINI_CACHE_FILE", "gemini_cache.json")

# Manual cache-invalidation lever for a prompt or threshold change that the
# criteria hash does not capture. Bump it and every stored verdict is a miss.
GEMINI_VERDICT_VERSION = int(os.getenv("GEMINI_VERDICT_VERSION", 1))

# Cached verdicts expire after this many days. NOT the 90 that
# REJECTED_HANDLES_RETENTION_DAYS uses: generateContent is not deterministic, and
# GEMINI_MODEL is a floating ALIAS that Google repoints at new snapshots, so a
# long window would serve verdicts from a model that no longer exists. The stored
# entry also records the response's own modelVersion and a mismatch is a miss.
GEMINI_CACHE_RETENTION_DAYS = int(os.getenv("GEMINI_CACHE_RETENTION_DAYS", 30))


# --- Outreach: review-to-send system ---
# Tables created 2026-08-14 in the same base as the niche tables. IDs are the
# defaults so a fresh clone works without a full .env; override per environment.
AIRTABLE_TABLE_OUTREACH_LOG = os.getenv("AIRTABLE_TABLE_OUTREACH_LOG", "tblcKnLKAbdjUCH68")
AIRTABLE_TABLE_AUDIT_TRAIL = os.getenv("AIRTABLE_TABLE_AUDIT_TRAIL", "tblTjGTRHCAEnq2qq")
AIRTABLE_TABLE_OUTREACH_LOCK = os.getenv("AIRTABLE_TABLE_OUTREACH_LOCK", "tbldWjtDW8EOCT0V2")
AIRTABLE_TABLE_HOME_THEATER_OUTREACH = os.getenv("AIRTABLE_TABLE_HOME_THEATER_OUTREACH", "tblOChqk6iVlRxwkp")
AIRTABLE_TABLE_LIFESTYLE_SOFA_OUTREACH = os.getenv("AIRTABLE_TABLE_LIFESTYLE_SOFA_OUTREACH", "tblk6Tml6PO90wLZz")

# DEMO MODE — the project is pre-launch and must not email real creators.
#
# Defaults to TRUE, and that direction is the whole point. Every other guard in
# this system (--dry-run being the default, the daily cap, the claim ledger)
# protects against a MISTAKE. This one protects against the system working
# exactly as designed at a time when nobody has agreed it should run: a cold
# email to a real creator cannot be recalled, apologised away, or deleted from
# their inbox.
#
# It is deliberately a separate switch from --dry-run rather than a stricter
# default on it. --dry-run lives on the command line, where it is one typo or
# one copied-from-the-README command away from being overridden; this lives in
# the environment, so leaving demo mode requires a deliberate edit to .env or a
# CI secret that someone has to justify. Two independent gates, and the send
# path must clear BOTH.
#
# Flipping this to "false" is the moment this project starts contacting real
# people. Do not do it to make a test pass, and do not do it before the
# blocking prerequisites in OUTREACH_PLAN.md are signed off: a dedicated warmed
# sending domain, the CAN-SPAM/PECR footer, and confirmed Gmail auth.
OUTREACH_DEMO_MODE = env_flag("OUTREACH_DEMO_MODE", default=True)

# In demo mode every message is REDIRECTED here regardless of the prospect's
# real address, so an end-to-end rehearsal exercises the true send path —
# rendering, MIME assembly, transport, the ledger write — without a creator
# ever receiving anything. Unset means demo mode cannot send at all, which is
# the safe failure: a missing test address must never fall back to the real one.
OUTREACH_DEMO_RECIPIENT = os.getenv("OUTREACH_DEMO_RECIPIENT", "")

# Per prospect day, counted from the Outreach Log's Claimed At, NOT per run.
# A per-run cap is not a cap: five runs before lunch at 50 each is 250 emails.
OUTREACH_DAILY_CAP = int(os.getenv("OUTREACH_DAILY_CAP", 10))
OUTREACH_MAX_PER_RUN = int(os.getenv("OUTREACH_MAX_PER_RUN", 10))
# Pacing between sends. 60 identical cold emails fired in seconds from one
# mailbox is a spam-filter signal in its own right.
OUTREACH_SLEEP_SECONDS = float(os.getenv("OUTREACH_SLEEP_SECONDS", 2))
# A claim still unsettled after this long MAY have been delivered. Surfaced by
# --reconcile for a human to settle; never auto-retried.
OUTREACH_STRANDED_AFTER_MINUTES = int(os.getenv("OUTREACH_STRANDED_AFTER_MINUTES", 60))
# Advisory lease take-over threshold. See the Outreach Lock table description:
# that row is NOT a mutex, and the real serialisation is the CI concurrency
# group. Without a take-over threshold one killed run locks outreach forever.
OUTREACH_LEASE_STALE_MINUTES = int(os.getenv("OUTREACH_LEASE_STALE_MINUTES", 60))

# Follow-up ("respam"): re-contact a NON-replier months later. Bounded by both
# a minimum age and a hard ceiling on total touches — prior Sent rows are the
# counter, so it cannot be reset by editing a field. A follow-up cadence with
# no ceiling is indistinguishable from spam.
OUTREACH_RESPAM_MIN_DAYS = int(os.getenv("OUTREACH_RESPAM_MIN_DAYS", 90))
OUTREACH_MAX_TOUCHES = int(os.getenv("OUTREACH_MAX_TOUCHES", 2))

# --- Outreach mail transport ---
# No defaults: --send refuses to start without these, which turns "we forgot
# the unsubscribe link" from a legal exposure into a startup failure.
GMAIL_SENDER_EMAIL = os.getenv("GMAIL_SENDER_EMAIL", "")
# Base64 rather than a path: CI cannot supply a file, and a service-account
# private key should not be written to a runner's disk where a later step
# can read it.
GMAIL_CREDENTIALS_B64 = os.getenv("GMAIL_CREDENTIALS_B64", "")
OUTREACH_FOOTER_TEXT = os.getenv("OUTREACH_FOOTER_TEXT", "")
OUTREACH_UNSUBSCRIBE_URL = os.getenv("OUTREACH_UNSUBSCRIBE_URL", "")
