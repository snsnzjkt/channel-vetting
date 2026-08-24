"""
Orchestrates the full channel vetting pipeline, per niche:

  run_discovery() -> pre-filter against that niche's existing Airtable
  channel IDs -> for each remaining candidate: enrich -> score -> push
  to that niche's Airtable table

Run with --test to sanity-check the whole pipeline cheaply (1 keyword,
5 results, first niche only) before spending real quota on a full run.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone

import config as _config
import video_topics
import run_metrics
import inspect
import logging
import math
import re
import sys
import time

# Channel titles can contain characters outside Windows' default console
# codepage (cp1252) — without this, printing one crashes the whole run
# partway through with UnicodeEncodeError.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from discovery import run_discovery
from enrichment import (
    get_channel_stats,
    get_recent_video_performance,
    calc_upload_frequency,
    calc_uploads_per_year,
    channel_age_months,
    days_since_last_upload,
    scan_older_videos_for_email,
    count_longform_in_older_videos,
)
from scoring import calc_fake_follower_risk, calc_overall_score, QUALIFIED, qualify
from search_zones import (
    description_location_outside_zone,
    flag_country_outside_zone,
    title_country_outside_zone,
    zone_verdict,
)
from airtable_client import (
    get_existing_channel_ids,
    get_tracked_handles,
    push_record,
    table_has_field,
    AirtableReadError,
    count_added_today,
)
from external_dedupe import fetch_external_handles, ExternalIndex, match_external
import rejected_handles
from prospect_day import today_iso
from quota_tracker import can_afford_enrichment, get_today_spend
from browser_email import BrowserEmailScraper, null_scraper
from credit_tracker import (
    CreditLedgerUnavailable,
    assert_readable as assert_credit_ledger_readable,
    spend_summary as credit_spend_summary,
)
from gemini_verify import GeminiVerifier
from influencers import InfluencersClient, null_client
from influencer_discovery import InfluencerDiscovery
from do_not_contact import BlocklistUnavailable, fetch_blocklist
from config import (
    MIN_VIEWS_PER_VIDEO_RATIO as CONFIG_MIN_VIEWS_PER_VIDEO_RATIO,
    API_SLEEP_SECONDS,
    DEFAULT_STATUS,
    SOURCE_LABEL,
    DAILY_QUOTA_BUDGET,
    CANDIDATE_OVERSHOOT,
    DAILY_FLAGGED_CAP,
    DAILY_QUALIFIED_CAP,
    DISCOVERY_DAYS_BACK,
    EXPECTED_CANDIDATES_PER_KEYWORD,
    INFLUENCERS_MAX_EXCLUDE_HANDLES,
    INFLUENCERS_TEST_DISCOVERY_CREDITS,
    USE_PLAYWRIGHT_STEALTH,
    VIDEO_TOPIC_GATE,
    VIDEO_TOPIC_MIN_SHARE,
    VIDEO_TOPIC_CATEGORIES,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# EXCLUDED_TOPIC_TERMS, EXCLUDED_TOPIC_KEYWORDS, DISCOVERY_SUBSCRIBER_FLOOR_RATIO
# and wire_discovery_filters() moved to niches.py (2026-08-14). They had to go
# WITH the registry: wire_discovery_filters() mutates NICHES in place at import,
# so leaving it here made `import niches` on its own yield a registry missing
# keywords_not_in_description and number_of_subscribers — i.e. correctness that
# depended on whether main happened to be imported first.
from niches import (  # noqa: E402
    BIO_OFF_SIGNALS,
    BROADCAST_TV_NAME_TERMS,
    BROADCAST_TV_PHRASE_TERMS,
    EXCLUDED_TOPIC_TERMS,
    OFF_TARGET_TERMS,
)

# NICHES lives in niches.py (extracted 2026-08-14) so outreach.py can read the
# niche -> table mapping without importing this module and, with it, Playwright.
# Re-exported because this module and several tests refer to `main.NICHES`.
from niches import NICHES  # noqa: E402

# Outer refill-round cap for the influencers.club discovery loop. discovery
# paginates internally and stops when supply or its credit ceiling runs out;
# this only backstops a pathological run where a very low gate-survival rate
# would otherwise keep asking for more candidates round after round.
DISCOVERY_MAX_ROUNDS = 50

# How many candidates push_until_full will still enrich after the QUALIFIED
# budget is full, hunting for a flagged row, before giving up on the batch.
#
# The asymmetry is the point. Qualification now turns on channel age alone, so a
# flagged row requires an under-12-month channel that also cleared every hard
# gate — uncommon for Home Theater and IMPOSSIBLE for Lifestyle Sofa, whose
# min_channel_age_months is None (qualify() can only ever return "Qualified"
# there). Enrichment costs 3-13 YouTube units per candidate and is charged
# BEFORE qualification is known, so an unbounded hunt spends real quota on rows
# that cannot exist. 10 is generous next to how rarely a flagged row appears,
# and the counter resets whenever one does.
FLAGGED_ONLY_PATIENCE = 10

# Automated topical matching isn't implemented, so every channel is scored
# with the same niche-match value. 70.0 is a deliberate mild-positive prior,
# NOT a neutral midpoint — it asserts that a candidate which came out of a
# niche-targeted discovery query and survived the pre-push gate is somewhat
# more likely than not to fit the brief. (It was 50.0 until 85e9537, which
# raised it without updating this comment; the rows in the live tables were
# all written under 70.0.)
#
# Two consequences before touching this:
#
#   - Because the value is constant, the "Overall Score" carries ZERO
#     brand-fit signal. It ranks channels on size, views, engagement,
#     consistency and trust only. Do not use it to order a human review
#     queue by how on-brand a channel is; it cannot express that, and
#     reviewers have to judge niche fit themselves during Airtable review.
#
#   - Changing the value re-bases every future score. It feeds
#     calc_overall_score() at WEIGHT_NICHE_MATCH = 0.10 (scoring.py), so
#     each point here moves every Overall Score by 0.1 — dropping back to
#     50.0 would put new rows 2 points below the rows already in the live
#     tables and make the two sets incomparable.
#
# Wire in a real niche classifier here if/when one becomes available; that
# is the fix, rather than retuning this constant.
DEFAULT_NICHE_MATCH = 70.0


# One label per step of the chain below. backfill_missing_emails.py
# aggregates these to report which step actually moved email coverage,
# so they must stay distinct.
EMAIL_SOURCE_REPEATED = "repeated across recent videos"
EMAIL_SOURCE_ABOUT = "About description"
EMAIL_SOURCE_OLDER = "repeated across older videos"
EMAIL_SOURCE_INFLUENCERS = "influencers.club enrichment"
EMAIL_SOURCE_BROWSER = "linked site or its /contact page (Playwright)"


# --- Pre-push gate -------------------------------------------------------
# The ONE place this pipeline discards a candidate outright instead of
# flagging it for review. Everything else follows the flag-never-discard
# rule; these cases are exceptions because a human reviewing them is pure
# cost.
#
# The 2026-08 criteria change moved the view floor in here. It used to
# produce a "Below View Minimum" row for a reviewer to dismiss; it is now
# a hard requirement, so an under-view channel is discarded and that
# Qualification value no longer exists (see scoring.py). Two more hard
# requirements landed with it: a minimum video count, and the search-zone
# check below.
#
# Dead channels: both measures have to be dead, not either. In the live
# Home Theater table five rows were burning flagged budget at 0-281 subs
# and 0-38 avg views, while the lowest legitimate Qualified channel sat at
# 2,400 subs / 16,160 views — so 100/100 clears the junk with two orders of
# magnitude to spare. Requiring BOTH keeps a small-but-growing channel
# (few subs, real views) and a fading big one (many subs, few views) in the
# table where a reviewer can see them.
#
# That gate is now mostly redundant against a 10,000-view floor, and is
# kept anyway: it is the floor that holds if a niche's own min_avg_views is
# ever lowered, and it is the only one of the two that reads subscribers.
JUNK_MIN_SUBSCRIBERS = 100
JUNK_MIN_AVG_VIEWS = 100

# A published track record, applied to BOTH niches. Read from
# channels.list statistics.videoCount, i.e. the channel's whole public
# catalogue, not the 10-video performance window or the 50-video email
# scan. Deliberately a FLOOR with no upper bound: "30-40 videos" describes
# the smallest catalogue worth approaching, and a channel with 400 uploads
# clears that bar rather than failing it.
MIN_VIDEO_COUNT = 30

# The same 30-video floor, but counting only videos confirmed NOT to be
# Shorts. MIN_VIDEO_COUNT above reads statistics.videoCount, which lumps
# Shorts in with everything else — so a channel with 300 Shorts and 4
# long-form uploads cleared it, which is not what "30-40 videos minimum"
# means for a brand looking to place a product in real content.
#
# is_shorts_only() does NOT cover this: it discards only channels that are
# 100% Shorts, so the entire middle ground (a Shorts factory that posts the
# occasional long-form video) passed both checks. Measured on 47 otherwise-
# qualifying Home Theater candidates, 12 had fewer than 10 long-form videos
# in their newest 50 and were being written as prospects.
#
# Confirmed against up to ~200 videos — the newest 50 from enrichment plus
# LONGFORM_SCAN_MAX_PAGES more — see enrichment.count_longform_in_older_videos.
MIN_LONGFORM_VIDEO_COUNT = 30

# Content language must be English. The tag is the channel's own
# defaultAudioLanguage/defaultLanguage, reduced to the most common value
# across the sampled videos by enrichment.dominant_language().
#
# Matched on the "en" PREFIX, so en, en-US, en-GB and en-AU all pass, and the
# region subtag is still not noise to be normalised away — but the REASON
# changed on 2026-08-20. It used to be that main.resolve_country() read the
# subtag to place channels declaring no country. That fallback is deleted (an
# `en-US` tag describes the AUDIENCE, and it was placing Vietnamese and Kenyan
# creators in zone), so the subtag no longer feeds the zone filter at all.
# What survives is simpler: the full tag is written VERBATIM to the "Content
# Language" column, so stripping it to a bare "en" would silently rewrite that
# column for every row and make new rows incomparable with existing ones.
ENGLISH_LANGUAGE_PREFIX = "en"

# Character ranges that mean a channel description is written in a language
# other than English. Checked because is_english() above reads only the
# per-video language TAG, and a creator can tag uploads "en" while writing their
# channel bio in another language entirely — @LINTAN777 (2026-08-14) declared
# country US, tagged its videos "en", cleared every numeric gate, and had a bio
# 24% Chinese. The tag said English; the channel did not read as one.
#
# Scripts, not "non-ASCII". Emoji, box-drawing, arrows, currency symbols and
# accented Latin (é, ü, ñ) are all deliberately ABSENT: an English channel
# routinely uses those, and matching them would drop good prospects on
# decoration. Only ranges that carry actual language are listed.
NON_LATIN_SCRIPT_RANGES = (
    (0x0400, 0x04FF),   # Cyrillic
    (0x0590, 0x05FF),   # Hebrew
    (0x0600, 0x06FF),   # Arabic
    (0x0700, 0x074F),   # Syriac
    (0x0900, 0x097F),   # Devanagari
    (0x0980, 0x09FF),   # Bengali
    (0x0B80, 0x0BFF),   # Tamil
    (0x0E00, 0x0E7F),   # Thai
    (0x3040, 0x30FF),   # Hiragana + Katakana
    (0x3400, 0x4DBF),   # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),   # CJK Unified Ideographs
    (0xA000, 0xA4CF),   # Yi
    (0xAC00, 0xD7AF),   # Hangul syllables
    (0xF900, 0xFAFF),   # CJK compatibility ideographs
)

# Two thresholds, and both are needed. The RATIO catches a genuinely
# non-English bio (@LINTAN777 measured 24%). The absolute FLOOR stops a short
# bio tripping on decoration — "new video 日曜日!" is 2 CJK characters in a
# 20-character string, which is 10% by ratio but obviously not a language
# signal. A bilingual bio with a real paragraph in another language clears both.
MAX_NON_LATIN_DESCRIPTION_RATIO = 0.10
MIN_NON_LATIN_DESCRIPTION_CHARS = 8

# The per-video view floor, applied to BOTH niches. This is the per-video
# reading of "min 10k+ views" — stricter than the niche's min_avg_views floor,
# which one strong upload can carry over the line while other recent videos
# flopped. Kept alongside min_avg_views, not replacing it, so a niche can still
# be given its own average bar.
MIN_VIEWS_PER_VIDEO = 10_000

# ...but only MIN_VIEWS_PER_VIDEO_RATIO of the sampled LONG-FORM videos has to
# clear it, not all of them (changed 2026-08-14, at the user's direction, and
# applied to BOTH niches — Lifestyle Sofa included; its brief's 2,000 figure
# stays overridden). Shorts are excluded from the sample entirely; see
# enrichment.get_recent_video_performance.
#
# CALIBRATED against the live tables, not guessed. Re-checking all 80 tracked
# rows put the two groups here:
#
#   - Shorts-inflated channels scored 0-3 of 10 (Explore With Jasir 0/10 on a
#     140,885 average, Diva Angel 2/9, Kat and Sourabh 3/10).
#   - Channels a HUMAN REVIEWER had already marked Approved scored 5-6 of 10
#     (ETPC 6/10 at 22,198 avg, Bane Tech 5/10 at 23,914 avg).
#
# The bar has to sit in the gap between 3 and 5, and it must ADMIT 5 — a gate
# that rejects channels the reviewers themselves approved is miscalibrated by
# definition. 0.70 failed that (it rejected both). 0.60 failed it too, and less
# obviously: ceil(0.60 x 10) = 6 admits ETPC and still rejects Bane Tech by one
# video, so the stated goal of "let the reviewers' band through" was only half
# met. That is the trap in a ratio — the effective bar is the CEIL, not the
# percentage, and at n=10 a 0.60 and a 0.51 are five percentage points apart on
# paper and one whole video apart in practice.
#
# 0.50 is the first value that admits the whole approved band (ceil = 5) while
# still excluding every Shorts-inflated channel measured, the best of which
# managed 3. If this is retuned again, do it the same way — run
# audit_prospects.py and look at where the reviewers' own Approved/Rejected
# calls actually fall, then check the CEIL at the sample sizes you actually see.
#
# Why this changed. The gate used to test the window's MINIMUM, i.e. "EVERY
# recent video passed 10k". Read against the 10,000 AVERAGE floor next to it,
# that is far harsher than it looks: for a channel averaging 10k, roughly half
# its videos sit below the average, so its weakest recent upload almost
# certainly misses 10k and the channel is discarded. The effective bar was not
# "10,000 average" but something nearer a 25,000-50,000 average — well past
# anything the brief asked for, and the most plausible single cause of the ~1%
# discovery survival rate measured on 2026-08-13 (~97 creators examined for one
# qualified row).
#
# The ratio keeps what the floor was actually for — catching a channel whose
# average is propped up by one viral upload while the rest flopped — without
# demanding every upload be a hit. At the default PERFORMANCE_SAMPLE_SIZE of 10
# that is "at least 6 of the newest 10 long-form videos".
#
# The denominator is the count of SETTLED, REPORTED videos, not a flat 10: an
# upload still climbing toward 10k, or one with no public view count, is unknown
# rather than failing, and enrichment already excludes both from
# `settled_views`. So the rule reads "50% of the videos we can actually judge".
# LOWERED 0.50 -> 0.30 on 2026-08-21, at the operator's direction, because the
# pipeline was returning too few rows and sometimes none at all for Home Theater.
# At PERFORMANCE_SAMPLE_SIZE 10 the rule moves from "at least 5 of the newest 10
# judgeable long-form videos cleared 10k" to "at least 3 of 10".
#
# WHAT THIS COSTS, stated plainly so the next reader can undo it knowingly. The
# gate exists to catch a channel whose average is propped up by one viral upload
# while the rest flopped, and 0.30 lets more of exactly that through: a channel
# with 3 hits and 7 flops now passes. That is the trade the operator chose, and
# the reviewer is the backstop.
#
# EVIDENCE THIS IS THE RIGHT DIAL IS MIXED, and both readings are worth having:
#   - Against FRESH candidates it is the main limiter. The measured note in this
#     repo's learnings: the 0.50 ratio "rejects channels with a strong average
#     but uneven uploads (e.g. 116k subs / 26k avg views dropped)", at a measured
#     1 row per 100-150 creators.
#   - Against ALREADY-TRACKED rows it is nearly irrelevant. audit_prospects.py on
#     2026-08-21 re-checked 107 rows: 78 pass, 29 fail, and only ONE of those 29
#     failed on video_below_view_minimum. The dominant failures there are
#     outside_search_zone (11) and no_declared_country (5).
# Those are consistent: existing rows were mostly written before the 2026-08-20
# zone narrowing, so they now fail on zone, while fresh candidates die earlier on
# the ratio. If yield is still short after this, the zone is the bigger lever.
#
# The VALUE lives in config.py, not here: .env.example states that config.py is
# the only module that reads environment variables, and an os.getenv() in this
# file would quietly falsify that. Retune it the way the comment above says: run
# audit_prospects.py and look at where the reviewers' own Approved/Rejected calls
# actually fall.
MIN_VIEWS_PER_VIDEO_RATIO = CONFIG_MIN_VIEWS_PER_VIDEO_RATIO

# ...and below this many judgeable videos the ratio is SKIPPED, not applied.
#
# A floating denominator makes the gate meaningful at n=10 and arbitrary at
# n=3, because the ceil quantises hardest exactly where the sample is thinnest.
# Two live rows showed it: Kaitlyn :) at 1 of 3 needed 2 (a single upload
# decides the channel) and Adrianne MG at 2 of 7 needed 4. Neither number
# describes a channel — they describe a sample too small to describe one.
#
# So this follows the same rule as an unknown country, an unknown age and an
# unreported video_count: absent data is not evidence against the channel, and
# "we could only judge 3 videos" is absent data. A skipped ratio is not a free
# pass — min_avg_views, the 30-video floor, the 30-long-form floor, the cadence
# floor and the staleness check all still apply.
#
# 5 is the smallest sample where the ceil lands somewhere sane: at n=5 the bar
# is 3 of 5, which is a real majority, while at n=4 it is 2 of 4 — a coin flip
# dressed up as a criterion. It does NOT rescue Adrianne MG (7 judgeable videos
# is enough to judge, and 2 of 7 fails); it does leave Kaitlyn :) unjudged by
# this particular gate, which is the correct reading of 3 videos.
#
# Note this can only ever fire for a channel that ALREADY cleared the 30
# long-form floor — so it means "their recent long-form is mostly too new to
# score", not "they barely post long-form".
MIN_SETTLED_SAMPLE_FOR_RATIO = 5

# A live channel, applied to BOTH niches: at least this many uploads per year,
# read from the sampled window's cadence (enrichment.calc_upload_frequency,
# videos/month, annualised). Unknown cadence (fewer than two sampled uploads) is
# passed as None and never disqualifies, the same rule as an unknown age.
#
# LOWERED from 10 to 6 (2026-08-14). The audit of all 80 tracked rows turned up
# two channels this gate was the ONLY thing rejecting, both strong on every
# other measure: Ashley Devonna (94,750 long-form average, 10 of 10 recent
# videos over 10k) and Karin Bohn (19,530 average, 7 of 10). A rule whose only
# observed effect is discarding the best channel in the sample is worth
# doubting, and MAX_DAYS_SINCE_LAST_UPLOAD below is the gate that actually
# catches an abandoned channel.
#
# BUT 6 DOES NOT RESCUE EITHER OF THEM, and the reason is worth recording rather
# than re-deriving. Ashley Devonna's newest TEN uploads span 2022-06 to 2026-08
# — 2.4 uploads a year. The channel has 259 videos, so it was prolific once and
# has nearly stopped; it posted 4 days ago, so it is not dormant either. It is a
# genuinely low-cadence channel, not the "monthly creator" this comment first
# claimed. Keeping it would mean a floor near 2, which is the same as deleting
# the gate.
#
# So 6 is a compromise held on purpose: it stops the floor rejecting an ordinary
# every-six-weeks creator (~9/yr), while still excluding channels that publish
# two or three times a year. Whether a 2-3/yr channel with a 94k average is
# worth contacting is a business call about placement frequency, not something
# this file can settle — it is flagged to the user rather than decided here.
#
# CAVEAT: MIN_VIEWS_PER_VIDEO_RATIO was calibrated against 80 rows with reviewer
# verdicts to check against. This is 2 data points. Revisit once a few audits
# have produced a real cadence distribution.
MIN_UPLOADS_PER_YEAR = 6

# Still-active: the most recent sampled upload must be within this many days
# (a rolling ~12 months from today, NOT the calendar year). A channel that
# went quiet a year ago is not one to approach, however strong its back
# catalogue. An unknown last-upload date (nothing parseable in the window) is
# passed as None and never disqualifies.
MAX_DAYS_SINCE_LAST_UPLOAD = 365

DROP_DEAD_CHANNEL = "dead_channel"
DROP_SHORTS_ONLY = "shorts_only"
DROP_BELOW_VIEW_MINIMUM = "below_view_minimum"
DROP_VIDEO_BELOW_VIEW_MINIMUM = "video_below_view_minimum"
DROP_TOO_FEW_VIDEOS = "too_few_videos"
DROP_TOO_FEW_LONGFORM = "too_few_longform_videos"
DROP_NOT_ENGLISH = "not_english"
DROP_OUTSIDE_SEARCH_ZONE = "outside_search_zone"
# The channel declares NO country at all (2026-08-20). Named apart from
# DROP_OUTSIDE_SEARCH_ZONE on purpose: a run summary that cannot tell "we
# looked and they're in Kenya" from "they told us nothing" cannot tell a
# badly-targeted discovery query from a thin-metadata one, and the two need
# opposite responses. Both are discards; only the reason differs.
DROP_NO_DECLARED_COUNTRY = "no_declared_country"
# A television network or a TV show's own channel, not a creator.
DROP_BROADCAST_TV = "broadcast_tv"
DROP_EXCLUDED_TOPIC = "excluded_topic"
DROP_UPLOAD_CADENCE_TOO_LOW = "upload_cadence_too_low"
DROP_STALE_CHANNEL = "stale_channel"
DROP_NO_SOCIAL = "no_social_presence"
# Not a judgement on the channel: the run ran out of YouTube quota before it
# could look. Named distinctly from the other drop reasons so a run summary
# showing these is read as "come back tomorrow", not "these channels failed".
DROP_QUOTA_EXHAUSTED = "quota_exhausted"
# The channel's own bio isn't in English, whatever its video language tag says.
# Distinct from DROP_NOT_ENGLISH so a run summary shows which signal fired —
# they disagree in exactly the case this gate was added for.
DROP_NON_ENGLISH_DESCRIPTION = "non_english_description"
# Also not a judgement on the channel: it qualified, but the bucket its
# qualification lands in is already full for the day, so no row can be written
# for it. Named distinctly for the same reason as DROP_QUOTA_EXHAUSTED — a run
# summary showing these means "the cap worked", not "these channels failed", and
# the candidate is a genuine prospect to re-examine tomorrow.
#
# This drop exists to stop a CREDIT LEAK, and its position in process_candidate
# is the whole feature: qualification is knowable from channel age alone, before
# the email chain runs, so a candidate with nowhere to land can be dropped
# BEFORE the 0.2-credit influencers.club lookup rather than after it. See the
# has_room parameter.
# Share of recent video titles that must read as an OFF-TARGET vertical before
# the content rule fires — and it only fires when that share also EXCEEDS the
# on-target share, so this is a floor on evidence, not the whole test.
#
# 0.10 measured over the 147 rows live on 2026-08-21. The distribution has a
# real gap rather than a slope: verified-good channels score 0.00, every
# Lifestyle row is <= 0.04, and verified off-target Home Theater rows run
# 0.10-0.86. Lowering it to 0.05 buys nothing (nothing sits between) and starts
# reading noise; raising it to 0.20 loses Tofer.A (0.12, bio "Video game
# aficionado"), Technology Space HQ (0.14) and ETPC (0.10), all off-target.
OFF_TARGET_MIN_SHARE = 0.10

DROP_OFF_TARGET = "off_target_niche"
# The tag-based sibling of DROP_EXCLUDED_TOPIC. Distinct because they read
# DIFFERENT TEXT — that one the channel title and bio, this one the creator's
# own per-video tags — and collapsing them would hide which input fired, which
# is the only thing that tells you whether the vocabulary or the surface was
# wrong. See video_topics.py.
DROP_OFF_TOPIC_TAGS = "off_topic_tags"
DROP_NO_HEADROOM = "no_headroom_for_bucket"

# Drop reasons that say nothing about the CHANNEL, only about this run's
# circumstances. They must never reach the rejected-handle cache: a creator we
# simply did not get to is a genuine prospect, and recording it would blind the
# pipeline to it for REJECTED_HANDLES_RETENTION_DAYS to save 0.01 credits — a
# terrible trade, and an invisible one, since the symptom would be a table that
# quietly stopped finding people.
#
# "unreachable" is in here deliberately: a private, deleted or temporarily
# erroring channel may be back tomorrow, and a transient YouTube 5xx surfaces
# through this same reason (see enrichment's exception handling).
TRANSIENT_DROP_REASONS = frozenset({
    DROP_QUOTA_EXHAUSTED,
    DROP_NO_HEADROOM,
    "unreachable",
})



# One pattern per category, matching any listed term on a word boundary.
# Compiled once at import, not per candidate.
_EXCLUDED_TOPIC_PATTERNS = {
    category: re.compile(
        r"\b(?:" + "|".join(re.escape(term) for term in terms) + r")\b",
        re.IGNORECASE,
    )
    for category, terms in EXCLUDED_TOPIC_TERMS.items()
}


def excluded_topic_reason(*texts: str) -> str | None:
    """
    The first excluded category ('political' | 'asmr' | 'firearms') whose
    terms appear in `texts`, or None. Free — reads only data already fetched
    (the channel title and About description).
    """
    blob = " ".join(t for t in texts if t)
    for category, pattern in _EXCLUDED_TOPIC_PATTERNS.items():
        if pattern.search(blob):
            return category
    return None


# Two patterns, not one, because they are matched against DIFFERENT TEXT —
# see the comment on BROADCAST_TV_NAME_TERMS in niches.py for the measurement
# that forced the split.
_BROADCAST_TV_NAME_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in BROADCAST_TV_NAME_TERMS) + r")\b",
    re.IGNORECASE,
)
_BROADCAST_TV_PHRASE_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in BROADCAST_TV_PHRASE_TERMS) + r")\b",
    re.IGNORECASE,
)


def broadcast_tv_reason(channel_title: str, description: str) -> str | None:
    """
    Whether this channel is a television network or a TV show rather than a
    creator: 'broadcast_tv_name', 'broadcast_tv_phrase', or None.

    Free — reads only the title and About description `channels.list` already
    returned, so it sits with the other description checks and costs no
    performance fetch for a channel it discards.

    **The two arguments are NOT interchangeable and must not be joined into
    one blob.** Network NAMES are matched against the title only; a creator
    who mentions HGTV or the BBC in their bio is citing a credit, and two live
    rows (`Drew & Jonathan`, `Traveling with Kristin`) are exactly that. The
    self-describing PHRASES are matched against both, because a show's bio is
    where "a British daytime television ... programme" actually appears. This
    is why the function takes (title, description) positionally instead of
    `*texts` the way excluded_topic_reason does — the distinction is the whole
    point of the gate.

    Returns which HALF fired, not just that something did: the name list is
    unbounded whack-a-mole and the phrase list is meant to generalise, so a
    run summary showing only name hits means the phrase list has gone stale.
    """
    if _BROADCAST_TV_NAME_PATTERN.search(channel_title or ""):
        return "broadcast_tv_name"
    if _BROADCAST_TV_PHRASE_PATTERN.search(f"{channel_title or ''} {description or ''}"):
        return "broadcast_tv_phrase"
    return None


def is_english(content_language: str | None) -> bool:
    """
    Whether a content-language tag is English.

    An UNSET tag is not English here — a deliberate break from this
    pipeline's usual "absent data never disqualifies" rule (unknown channel
    age, unknown country). The requirement is that every row's Content
    Language reads as English, and a blank cannot satisfy it; keeping unsets
    would put "Unknown" rows in a table specified to hold English channels.

    The cost of the strict reading was measured before choosing it: across
    197 enriched candidates, ZERO of the 47 that passed every other gate had
    an unset language, because dominant_language() reads the whole 50-video
    window rather than one video. So this discards essentially nothing today
    — but it is the strict direction, and if a future sample does carry
    unsets they will be dropped rather than written as Unknown.
    """
    return (content_language or "").strip().lower().startswith(ENGLISH_LANGUAGE_PREFIX)


def non_latin_script_chars(text: str | None) -> int:
    """How many characters of `text` sit in a NON_LATIN_SCRIPT_RANGES block."""
    return sum(
        1
        for char in (text or "")
        if any(low <= ord(char) <= high for low, high in NON_LATIN_SCRIPT_RANGES)
    )


def description_is_non_english(description: str | None) -> bool:
    """
    True if a channel's description is substantially not in English.

    Complements is_english(), which reads the per-video language TAG. The two
    disagree for a creator who tags uploads "en" but writes their bio in another
    language, which is the case this exists for (see NON_LATIN_SCRIPT_RANGES).

    Measured on SCRIPT, not on language detection: a dependency-free script
    check is decisive for Chinese/Japanese/Korean/Cyrillic/Arabic/Devanagari
    text and needs no model. Its blind spot is a non-English bio written in
    Latin script (Spanish, German, Indonesian), which this cannot see at all —
    those still rely on the video language tag. Worth knowing before trusting
    this as a complete language gate; it is a targeted fix, not a general one.

    An empty description is not evidence of anything and returns False, the same
    "absent data never disqualifies" rule the zone and age checks follow.
    """
    text = (description or "").strip()
    if not text:
        return False
    count = non_latin_script_chars(text)
    if count < MIN_NON_LATIN_DESCRIPTION_CHARS:
        return False
    return (count / len(text)) >= MAX_NON_LATIN_DESCRIPTION_RATIO


def _clears_per_video_floor(settled_views: list[int]) -> bool:
    """
    True if at least MIN_VIEWS_PER_VIDEO_RATIO of `settled_views` reach
    MIN_VIEWS_PER_VIDEO — or if the sample is too small to judge at all.

    `settled_views` holds only videos whose count has settled and is reported
    (see enrichment), so this is a ratio over what can actually be judged, not
    over a flat window size. math.ceil, so "50% of 5" is 3 and a partial video
    always rounds toward requiring one more rather than one fewer.

    Returns True below MIN_SETTLED_SAMPLE_FOR_RATIO judgeable videos: at that
    size the ceil quantises so hard that one upload decides the channel, which
    is a measurement artefact rather than a verdict. Unknown is not a failure —
    the same rule an unknown country or an unreported video_count follows. See
    MIN_SETTLED_SAMPLE_FOR_RATIO for why the floor is 5 and what still applies.
    """
    if len(settled_views) < MIN_SETTLED_SAMPLE_FOR_RATIO:
        return True
    required = math.ceil(MIN_VIEWS_PER_VIDEO_RATIO * len(settled_views))
    clearing = sum(1 for views in settled_views if views >= MIN_VIEWS_PER_VIDEO)
    return clearing >= required


def pre_push_drop_reason(
    subscriber_count: int | None,
    avg_views: float | None,
    shorts_only: bool = False,
    min_avg_views: float = 0,
    video_count: int | None = None,
    content_language: str | None = None,
    settled_views: list[int] | None = None,
    uploads_per_year: float | None = None,
    days_since_last_upload: float | None = None,
) -> str | None:
    """
    Why this candidate should never reach Airtable, or None to continue.

    Applies regardless of what qualify() would have returned — a row for a
    dead channel is exactly the row this gate exists to stop writing.

    Deliberately does NOT cover the search zone, nor the long-form video
    floor, and the two sit on OPPOSITE sides of this function:

      - The search zone (location_drop_reason) runs BEFORE this, and before
        the performance fetch, because since 2026-08-20 every one of its
        inputs is free — title, About description, declared country, all on
        the channels.list response. It used to run after, when it still read
        the content language's region subtag.
      - The long-form floor (longform_drop_reason) runs AFTER, because it is
        the one gate that can spend quota: up to three extra pages of uploads.

    Everything in this function is answerable from data already fetched, which
    is what makes it a cheap place to discard — but not the cheapest. That is
    the zone gate above it.

    Unknown data never disqualifies, the same rule qualify() follows: a
    `video_count` of None means channels.list didn't report one, and an
    unreported catalogue size is not evidence of a small catalogue. A
    reported 0 is a real answer and is failed like any other number below
    the floor. The ONE exception is `content_language` — see is_english() for
    why an unset language is treated as a failure there.

    `content_language` defaults to None, which fails the English check. That
    default is deliberate: a caller that forgets to pass it gets a loud
    empty result rather than silently skipping the gate.

    `settled_views`, `uploads_per_year` and `days_since_last_upload` are the
    three activity/quality floors (at least MIN_VIEWS_PER_VIDEO_RATIO of the
    settled recent videos over 10k, at least 10 uploads a year, a last upload
    inside a rolling 12 months). They follow the video_count rule, NOT the
    content_language one: each defaults to None and a None never disqualifies,
    because "we couldn't measure it" (nothing settled in the window, fewer than
    two dated uploads, no parseable timestamp) is not evidence against the
    channel. process_candidate supplies real values.

    `settled_views` is a LIST of per-video view counts, not an aggregate: the
    per-video floor is a ratio ("70% of them cleared 10k"), which no single
    number can express. An empty list reads the same as None — unknown.
    """
    if shorts_only:
        return DROP_SHORTS_ONLY
    if not is_english(content_language):
        return DROP_NOT_ENGLISH
    if video_count is not None and video_count < MIN_VIDEO_COUNT:
        return DROP_TOO_FEW_VIDEOS
    if (avg_views or 0) < min_avg_views:
        return DROP_BELOW_VIEW_MINIMUM
    # The per-video floor sits right after the niche's own average floor: both
    # are "views" criteria, and reporting the niche's own bar first keeps the
    # log reading in the reviewer's terms.
    #
    # An empty/absent list means nothing in the window has settled yet — that is
    # unknown, not a failure, so the floor is skipped (the same rule video_count
    # follows above). A short-but-non-empty list is skipped too, inside
    # _clears_per_video_floor; see MIN_SETTLED_SAMPLE_FOR_RATIO.
    if settled_views and not _clears_per_video_floor(settled_views):
        return DROP_VIDEO_BELOW_VIEW_MINIMUM
    if uploads_per_year is not None and uploads_per_year < MIN_UPLOADS_PER_YEAR:
        return DROP_UPLOAD_CADENCE_TOO_LOW
    if days_since_last_upload is not None and days_since_last_upload > MAX_DAYS_SINCE_LAST_UPLOAD:
        return DROP_STALE_CHANNEL
    if (subscriber_count or 0) < JUNK_MIN_SUBSCRIBERS and (avg_views or 0) < JUNK_MIN_AVG_VIEWS:
        return DROP_DEAD_CHANNEL
    return None


def off_target_reason(niche_config: dict, description: str, video_titles) -> tuple[str | None, str]:
    """
    (reason, detail) when this channel's content is DOMINATED by an off-target
    vertical — gaming, phones/PCs, generic gadgets, AI/crypto — else (None, "").

    Answers the brief's question "what does this creator CONSISTENTLY publish?"
    by reading the last ~50 video TITLES, which enrichment already fetched for
    the duplicate filter and used to throw away. Titles beat the bio for this:
    a bio is written once and goes stale, and four tracked channels have no
    usable bio at all ("Hi!", a bare email address).

    ## Why this is negative-evidence only

    A positive "must match an on-niche term" gate was built, measured and
    REJECTED on 2026-08-15 (the reasoning is preserved in niches.py above
    EXCLUDED_TOPIC_TERMS). It discarded "Jasper Tran - House Design Ideas" — a
    real prospect — on a positive score of 0/50, because genuine prospects title
    videos things like "This Small House Will Make You Fall in Love", which no
    vocabulary anticipates. So NOTHING is required here. A channel is dropped
    only on positive evidence that it is something else, which keeps the
    pipeline's standing rule that absent data never disqualifies: no titles, or
    no `on_target_terms` configured, means no verdict.

    ## Why on-target terms only ever RESCUE

    Two rules fire, and both require off-target evidence to EXCEED on-target
    evidence:

      - CONTENT: off_share >= OFF_TARGET_MIN_SHARE and off_share > on_share.
      - PERSONA: the bio self-describes as a gaming or generic-tech channel
        (BIO_OFF_SIGNALS), the titles corroborate at all, and off > on.

    The persona rule exists because at the low end one signal cannot do the job.
    Measured 2026-08-21: "DragsterTV" (bio: "money glitches on games such as
    Forza Horizon 5"; titles: Rainbow Six, Modern Warfare) scores 0.04
    off-target, and "OCM Reviews" (Fosi Audio DACs, IEMs, an Atmos soundbar)
    scores 0.06 — nearly identical. What separates them is the on-target rescue:
    OCM scores 0.60 on-target and is kept; DragsterTV scores 0.00 and its bio
    names three games. Neither the titles alone nor the bio alone gets both
    right; "High-quality Tech, Unboxing, Reviews" is OCM's own bio.

    ## Calibration

    Measured over the 147 rows live on 2026-08-21, this drops 29 — all of them
    in Home Theater, every one hand-verified off-target (Grxnt/Fortnite 0.76,
    DanKamYouKnow/PC builds 0.86, Octorious/PlayStation 0.58, Paul Antill/phones
    and cameras 0.42, NFT TIGERS/crypto 0.34, Bane Tech/gadgets 0.18). Zero
    Lifestyle rows are flagged: the highest scores 0.04, so the threshold has
    real headroom rather than sitting on top of the distribution.
    """
    on_terms = niche_config.get("on_target_terms") or []
    if not on_terms:
        # No rescue vocabulary configured means the gate would run with
        # on_share pinned at 0, so ANY off-target term would outweigh it and the
        # gate would turn far more aggressive than it was calibrated to be.
        # Disabled is the safe reading of a missing key, not "run it harder".
        return None, ""

    judged = [t for t in (video_titles or []) if t and t.strip()]
    if not judged:
        return None, ""

    # A niche may restrict which off-target categories apply to it. Home Theater
    # does, and the reason is measured rather than aesthetic: see the comment on
    # `off_target_categories` in niches.py. Absent the key, every category
    # applies, which is the historical behaviour.
    allowed = niche_config.get("off_target_categories")
    active = (
        {c: t for c, t in OFF_TARGET_TERMS.items() if c in set(allowed)}
        if allowed is not None else OFF_TARGET_TERMS
    )

    off_hits = 0
    on_hits = 0
    categories: set[str] = set()
    for title in judged:
        low = title.lower()
        matched = {
            category
            for category, terms in active.items()
            if any(term in low for term in terms)
        }
        if matched:
            off_hits += 1
            categories |= matched
        if any(term in low for term in on_terms):
            on_hits += 1

    off_share = off_hits / len(judged)
    on_share = on_hits / len(judged)
    if off_share <= on_share:
        return None, ""

    detail = (
        f"{off_share:.0%} of {len(judged)} recent titles are "
        f"{'/'.join(sorted(categories))} vs {on_share:.0%} on-niche"
    )
    if off_share >= OFF_TARGET_MIN_SHARE:
        return DROP_OFF_TARGET, detail

    # The PERSONA rule is skipped for a niche with a restricted category set.
    # BIO_OFF_SIGNALS is entirely gaming and consumer-tech vocabulary — exactly
    # what a restricting niche has measured as anti-predictive — so letting it
    # fire would reinstate through the bio the drops the category list just
    # removed. Measured: it is what "about technology" would do to Bane Tech.
    if allowed is not None:
        return None, ""

    bio_signals = [s for s in BIO_OFF_SIGNALS if s in (description or "").lower()]
    if bio_signals and off_hits:
        return DROP_OFF_TARGET, f"{detail}; bio says {bio_signals[:2]}"

    return None, ""


def longform_drop_reason(longform_count: int) -> str | None:
    """
    Whether the channel showed MIN_LONGFORM_VIDEO_COUNT confirmed non-Shorts
    uploads, or None to continue.

    Split out from pre_push_drop_reason because establishing the count can
    cost quota (see enrichment.count_longform_in_older_videos), so it must
    run after every free check has had its chance to discard the candidate.

    Unlike `video_count`, a shortfall here IS a failure even though the data
    is partial. That is the point: a channel gets ~200 videos' worth of
    chances to show 30 long-form uploads, and not showing them is the
    evidence, not missing data.
    """
    if longform_count < MIN_LONGFORM_VIDEO_COUNT:
        return DROP_TOO_FEW_LONGFORM
    return None


def location_drop_reason(
    channel_title: str,
    description: str,
    declared_country: str,
    allowed_codes,
) -> tuple[str | None, str]:
    """
    The whole search-zone decision for one channel, as
    (drop_reason, detail) — (None, "") to keep it.

    REWRITTEN 2026-08-20. This replaces `resolve_country()` plus the two
    separate zone checks that used to sit on either side of the performance
    fetch in `process_candidate`. Three things changed, all measured over the
    144 rows already in the two niche tables (see search_zones' docstring):

      - **A missing country is now a DISCARD.** It used to be absent data and
        the channel was kept for a human. The instruction was "don't include
        channels unless they have a specific location listed on YouTube".
        Costs 8 of 144 rows.
      - **The content-language region subtag is gone as a location source.**
        `en-US` describes the AUDIENCE. It is how `Lý Thiên An` and
        `Her 86m2`, both Vietnamese, were placed in-zone.
      - **Two new signals OUTRANK the declared country**, because requiring
        the field fixes far less than it looks like it does: nine of the
        twelve genuinely out-of-zone rows declare US, GB or CA. A flag in the
        title and a country name in the title now vote "outside" over it, as
        the About-description cue already did.

    Precedence, highest first — the first three can only ever vote "outside",
    so ordering them among themselves only changes which reason is REPORTED,
    never whether the channel is dropped:

      1. flag emoji in the TITLE          -> outside_search_zone
      2. country name in the TITLE        -> outside_search_zone
      3. location cue in the DESCRIPTION  -> outside_search_zone
      4. declared snippet.country         -> outside_search_zone / no_declared_country

    Every input is free — the title and description come from the
    `channels.list` response and the declared country is a field on it — so
    unlike the old arrangement this whole gate runs BEFORE
    `get_recent_video_performance()`. That is worth ~3 quota units plus any
    long-form paging for every out-of-zone candidate, and it is only possible
    because step 4 no longer needs `content_language` from that fetch.

    There is deliberately no browser step. See browser_email.py: the About
    panel's country is the same field as snippet.country, so it recovered 0 of
    the 5 live channels that lack one.
    """
    flag_code = flag_country_outside_zone(channel_title, allowed_codes)
    if flag_code:
        return DROP_OUTSIDE_SEARCH_ZONE, f"title flies the {flag_code} flag"

    title_code = title_country_outside_zone(channel_title)
    if title_code:
        return DROP_OUTSIDE_SEARCH_ZONE, f"title names {title_code}"

    desc_code = description_location_outside_zone(description)
    if desc_code:
        return DROP_OUTSIDE_SEARCH_ZONE, f"description says {desc_code}"

    verdict = zone_verdict(declared_country, allowed_codes)
    if verdict is None:
        return DROP_NO_DECLARED_COUNTRY, "no country set on the channel"
    if verdict is False:
        return DROP_OUTSIDE_SEARCH_ZONE, f"declared country {declared_country}"
    return None, ""


def resolve_email_with_source(
    stats: dict, performance: dict, scraper=None, enricher=None
) -> tuple[str, str, bool | None]:
    """
    Email fallback chain, cheapest and strongest signal first, returning
    (email, source_label, has_external_links) — or ("", "", <flag>) when no
    step found an address:

      1. An address repeated across several recent video descriptions.
      2. A single mention in the channel's own About description.
      3. The same repeat test, extended over OLDER uploads.
      4. influencers.club's enrich-by-handle endpoint, keyed on channel ID.
      5. The channel's public external link list, followed in Playwright:
         each non-third-party link, then its /contact page.

    Steps 1-2 use data already fetched during enrichment and cost
    nothing. Step 3 costs 2 quota units per extra page and only runs when
    1-2 found nothing, so channels whose address is already known never
    trigger it.

    Step 4 precedes step 5 on cost, not on signal quality, and step 5 stays
    last because it reads the creator's OWN site — a different source
    rather than a worse one. influencers.py's module docstring carries that
    argument in full; don't restate it here, or the two drift.

    Step 5 reads the LINK LIST, not the About text — step 2 already has
    the full About description from channels.list, so re-reading it in a
    browser could never add an address. See browser_email.py.

    The third return value, has_external_links, is the "does this channel
    have any web/social presence" signal for the no-social drop (see
    DROP_NO_SOCIAL in process_candidate). It is known whenever the BROWSER is
    enabled, not only when step 5 runs: a channel whose address came from an
    earlier step still gets its link list read (`need_email=False`, one page
    load, no link-following), because having an address says nothing about
    whether the creator exists anywhere off YouTube. It is None only when the
    browser is off or the About panel couldn't be read — absent data, which
    never disqualifies.

    Reporting the source here is what lets callers attribute a hit to a
    step. Comparing the result back against stats/performance can't: steps
    3, 4 and 5 are indistinguishable that way.
    """
    # Both collaborators ship a null object precisely so "absent" is an
    # object that returns "". Normalising here keeps that as the ONE
    # soft-disable mechanism — an `if x is not None` guard per step would
    # be a second one doing the same job, and the two could disagree.
    if enricher is None:
        enricher = null_client()
    if scraper is None:
        scraper = null_scraper()

    email = performance.get("repeated_email")
    source = EMAIL_SOURCE_REPEATED

    if not email:
        email = stats.get("business_email", "")
        source = EMAIL_SOURCE_ABOUT

    if not email:
        email = scan_older_videos_for_email(
            stats["channel_id"],
            stats.get("uploads_playlist_id", ""),
            performance.get("next_page_token", ""),
            performance.get("video_descriptions", []),
        )
        source = EMAIL_SOURCE_OLDER

    if not email:
        email = enricher.find_email(stats["channel_id"])
        source = EMAIL_SOURCE_INFLUENCERS

    if email:
        # The address is settled, but the LINK LIST still has to be read — the
        # no-social drop asks a different question ("does this creator exist
        # anywhere off YouTube?") and an earlier step answering the email
        # question does not answer it.
        #
        # This used to short-circuit here with None, which made DROP_NO_SOCIAL
        # unreachable for any channel steps 1-4 resolved — i.e. nearly all of
        # them, since a repeated address in the video descriptions is the most
        # common hit of the five. Measured 2026-08-15: every one of the 20 rows
        # written that day carried an email, so the gate never once ran, and
        # "Timber Time" (171k subs, a genuinely EMPTY link list) was written as
        # a prospect on the strength of an address in its descriptions.
        #
        # need_email=False keeps the cost to the single About page load and
        # skips the up-to-four link/probe navigations, which are the expensive
        # part and are pointless once an address is in hand.
        _, has_external_links = scraper.find_contact(stats["channel_id"], need_email=False)
        return email, source, has_external_links

    # Step 5. find_contact returns (email, has_external_links); an inert
    # scraper yields ("", None), which correctly leaves the no-social drop
    # dormant rather than discarding every channel.
    email, has_external_links = scraper.find_contact(stats["channel_id"])
    if email:
        return email, EMAIL_SOURCE_BROWSER, has_external_links

    return "", "", has_external_links


def resolve_email(stats: dict, performance: dict, scraper=None, enricher=None) -> str:
    """The chain above, for callers that only need the address itself."""
    return resolve_email_with_source(stats, performance, scraper, enricher)[0]


def push_until_full(
    candidates: list[dict],
    build_record,
    table_name: str,
    qualified_headroom: int,
    flagged_headroom: int = 0,
    flagged_possible: bool = True,
) -> dict:
    """
    Push candidates until both daily budgets are exhausted or the
    candidates run out.

    `build_record(candidate)` returns `(record, qualification)`, or
    `(None, reason)` to skip the candidate without spending budget.

    A build_record that accepts a SECOND parameter is additionally handed a
    `has_room(qualification) -> bool` probe over the live counts below, so it can
    bail out before spending money on a row that has nowhere to land — see
    process_candidate's has_room. Arity is inspected once, up front, so the
    one-argument form keeps working unchanged.

    Only SUCCESSFUL pushes consume budget. The previous loop counted
    attempts, so a run of Airtable failures would have burned the day's
    allowance without writing anything.

    `flagged_possible` says whether this niche can produce a flagged row AT
    ALL — i.e. whether it sets a `min_channel_age_months`. False means
    scoring.qualify() returns QUALIFIED unconditionally, so once the qualified
    budget fills there is nothing left to find and the loop stops instead of
    paying enrichment to prove it. Defaults True so a caller that doesn't know
    gets the safe (keep looking, bounded by FLAGGED_ONLY_PATIENCE) behaviour.

    Returns counts plus "pushed_ids", the Channel IDs actually written —
    matching the original loop, which added to newly_tracked_ids only
    when push_record returned True — and "rejected_handles", the @handles
    dropped for a reason that describes the channel rather than the run (see
    TRANSIENT_DROP_REASONS), which feeds the rejected-handle cache so the vendor
    is not paid to return them again.
    """
    counts = {
        "qualified": 0, "flagged": 0, "skipped": 0, "pushed_ids": set(),
        # Handles dropped for a reason that describes the CHANNEL rather than
        # this run — the input to the rejected-handle cache. Only populated for
        # candidates that carry a handle, i.e. the discovery path, which is the
        # only path where a re-return costs money.
        "rejected_handles": set(),
        # Every drop reason this batch produced, counted. Nothing aggregated
        # these before: `qualification` was read once for the TRANSIENT_DROP_REASONS
        # test below and thrown away, so "which gate consumed the candidates" was
        # answerable only by reading log lines. run_metrics.jsonl needs it as data.
        "drop_reasons": Counter(),
    }
    # Candidates enriched since the qualified budget filled without producing a
    # flagged row. See FLAGGED_ONLY_PATIENCE.
    fruitless_flagged_hunt = 0

    def has_room(qualification) -> bool:
        """Whether a row of this qualification could still be written today."""
        if qualification == QUALIFIED:
            return counts["qualified"] < qualified_headroom
        return counts["flagged"] < flagged_headroom

    # Inspected ONCE, not per candidate, and by signature rather than by calling
    # with two arguments and catching TypeError: a TypeError raised from deep
    # inside process_candidate looks identical to a wrong arity, and swallowing
    # it would silently call build_record — and re-spend its quota — twice.
    try:
        accepts_room = len(inspect.signature(build_record).parameters) >= 2
    except (TypeError, ValueError):
        # An un-introspectable callable (a C builtin, some Mock configurations)
        # falls back to the one-argument contract, which is always safe.
        accepts_room = False

    for candidate in candidates:
        if counts["qualified"] >= qualified_headroom and counts["flagged"] >= flagged_headroom:
            logger.info("Both daily budgets are full — stopping this niche.")
            break

        # Once the qualified budget is full, only a FLAGGED row can still be
        # written, and a flagged row needs qualify() to return something other
        # than "Qualified" — which only channel age can cause.
        hunting_flagged_only = counts["qualified"] >= qualified_headroom
        if hunting_flagged_only:
            # For a niche with no age requirement that is not merely unlikely,
            # it is IMPOSSIBLE: qualify() returns QUALIFIED unconditionally when
            # min_channel_age_months is None (see scoring.qualify), so no
            # candidate can ever land in the flagged bucket. Stop immediately
            # rather than spending FLAGGED_ONLY_PATIENCE enrichments to
            # rediscover a fact the niche config already states.
            if not flagged_possible:
                logger.info(
                    "Qualified budget is full and this niche has no channel-age "
                    "requirement, so no flagged row can exist — stopping."
                )
                break
            # Otherwise the hunt is a gamble worth a bounded number of tries.
            # Enrichment (3-13 YouTube units) is charged by build_record BEFORE
            # qualification is knowable, so without a brake the loop pays for
            # every remaining candidate and discards each at the bucket check.
            if fruitless_flagged_hunt >= FLAGGED_ONLY_PATIENCE:
                logger.info(
                    "Qualified budget is full and %d candidates produced no flagged "
                    "row — stopping rather than enriching the rest of the batch.",
                    fruitless_flagged_hunt,
                )
                break

        record, qualification = (
            build_record(candidate, has_room) if accepts_room else build_record(candidate)
        )
        if record is None:
            counts["skipped"] += 1
            counts["drop_reasons"][qualification or "unknown"] += 1
            # `qualification` is the DROP REASON on this branch (build_record
            # returns (None, reason)), which is what makes the durable/transient
            # split readable here without a second return value.
            if qualification not in TRANSIENT_DROP_REASONS:
                handle = (candidate.get("handle") or "").strip().lstrip("@").lower()
                if handle:
                    counts["rejected_handles"].add(handle)
            # A gate-dropped candidate still cost enrichment, so it counts
            # against the hunt's patience.
            fruitless_flagged_hunt += hunting_flagged_only
            continue

        bucket = "qualified" if qualification == QUALIFIED else "flagged"
        # Increment and reset live together: a flagged row means the hunt is
        # paying off, anything else means it isn't.
        fruitless_flagged_hunt = 0 if bucket == "flagged" else (
            fruitless_flagged_hunt + hunting_flagged_only
        )

        headroom = qualified_headroom if bucket == "qualified" else flagged_headroom
        if counts[bucket] >= headroom:
            counts["skipped"] += 1
            continue

        if push_record(table_name, record):
            counts[bucket] += 1
            counts["pushed_ids"].add(record["Channel ID"])
        # A failed push is logged inside push_record and costs no budget.

    return counts


# --- Spreadsheet safety for reviewer-facing text -------------------------
# Characters that make a spreadsheet treat a cell's contents as a FORMULA
# rather than as text. The tab and CR are in here because a leading
# whitespace character is stripped by some importers before the formula
# check runs, which puts the "=" back at the front.
# csv_safe() and its inverse live in text_safety.py (extracted 2026-08-14) so
# outreach.py can reuse them without importing this module — `import main`
# executes the NICHES construction and drags in discovery, enrichment,
# influencers and browser_email, i.e. Playwright, into a process whose only
# job is rendering an email. Re-exported because this module's own call sites
# and several tests refer to `main.csv_safe`.
# Re-exported ONLY for existing callers: this module still calls csv_safe(),
# backfill_missing_emails.py imports it from here, and tests/test_csv_injection.py
# imports SPREADSHEET_FORMULA_PREFIXES from here. csv_unsafe() is deliberately
# NOT re-exported — nothing reaches it through main, and new code should import
# from text_safety directly rather than growing this compatibility surface.
from text_safety import SPREADSHEET_FORMULA_PREFIXES, csv_safe  # noqa: E402,F401


def process_candidate(
    candidate: dict,
    external_handles: ExternalIndex,
    blocklist,
    niche_config: dict,
    scraper,
    enricher=None,
    known_channel_ids: set[str] | None = None,
    has_room=None,
    verifier=None,
) -> tuple[dict | None, str]:
    """
    Enrich, screen, qualify, and build an Airtable record for one candidate.

    `has_room(qualification) -> bool` is an optional budget probe, called once
    the qualification is known and BEFORE the email chain spends anything. It
    answers "is there still a daily slot for a row of this kind?"; False drops
    the candidate as DROP_NO_HEADROOM. Omit it and nothing is skipped for
    budget reasons, which is the pre-2026-08-20 behaviour.

    Why it exists, and why it is a callback rather than a number: push_until_full
    owns the running counts, and they change with every push inside the batch, so
    a headroom figure passed in here would be stale by the second candidate. The
    callback reads the live counts at the moment they matter.
    """
    known_channel_ids = known_channel_ids or set()
    # A candidate carries EITHER a "channel_id" (discovery.py / YouTube search)
    # OR a "handle" (influencer_discovery.py, which surfaces creators by
    # @handle). get_channel_stats resolves the real UC… id off the response
    # either way, so everything below is keyed on the resolved id.
    channel_id = candidate.get("channel_id")
    cand_handle = candidate.get("handle")

    # Checkpoint 1 — free, before spending ~3 quota units on enrichment.
    hit = blocklist.match(name=candidate.get("channel_title", ""))
    if hit:
        logger.info("BLOCKED (pre-enrichment) %s — DO NOT CONTACT (%s).", candidate.get("channel_title"), hit)
        return None, "blocked"

    # Quota ceiling. Checked AFTER the free blocklist match (which costs
    # nothing, so there is no reason to let a quota refusal hide a blocklist
    # hit) and BEFORE the first paid call below.
    #
    # This is the only ceiling check on the influencers.club discovery path:
    # that source replaces search.list, so can_afford_search() — for a long
    # time the pipeline's ONLY QUOTA_CEILING check — is never reached, and
    # enrichment spend was bounded by nothing but the daily row cap. Row caps
    # bound rows WRITTEN, not candidates EXAMINED, and at a low gate-survival
    # rate those diverge by two orders of magnitude.
    if not can_afford_enrichment():
        return None, DROP_QUOTA_EXHAUSTED

    stats = get_channel_stats(channel_id) if channel_id else get_channel_stats(handle=cand_handle)
    time.sleep(API_SLEEP_SECONDS)
    if stats is None:
        return None, "unreachable"
    # From here on channel_id is the RESOLVED id: the input for a search
    # candidate, the forHandle lookup's result for a discovery candidate.
    channel_id = stats.get("channel_id")
    if not channel_id:
        logger.info("No channel ID resolved for %s — skipping.", cand_handle or "candidate")
        return None, "unreachable"

    # Checkpoint 2 — the reliable key, known only after channels.list.
    hit = blocklist.match(handle=stats.get("handle", ""), name=stats.get("channel_title", ""))
    if hit:
        logger.info("BLOCKED %s — DO NOT CONTACT (%s).", stats.get("channel_title"), hit)
        return None, "blocked"

    # Skip channels already tracked in the base's other YouTube outreach/
    # leads/influencer tables (see external_dedupe.py) — checked here rather
    # than pre-discovery, since we only know a candidate's @handle once
    # channels.list has run. Matches on @handle first and channel NAME second,
    # so a creator who renamed their handle is still caught by name.
    external_hit = match_external(
        external_handles, handle=stats.get("handle", ""), name=stats.get("channel_title", ""),
    )
    if external_hit:
        logger.info(
            "Skipping %s — already tracked in '%s'.",
            stats.get("channel_title"), external_hit,
        )
        return None, "duplicate"

    # Niche-table dedupe by the RESOLVED channel ID. run_niche pre-filters
    # search.list candidates by channel_id before this runs, but discovery
    # candidates arrive as @handles with no id, so this is the only place one
    # already tracked in THIS niche's table is caught. It costs the 1-unit
    # channels.list already spent above; the server-side exclude_handles is
    # what avoids even that once tracked handles are persisted.
    if channel_id in known_channel_ids:
        logger.info(
            "Skipping %s — already tracked in this niche's table.", stats.get("channel_title"),
        )
        return None, "duplicate"

    # Off-brand topic exclusion (political / ASMR / firearms). Reads the title
    # and About description already fetched, and runs BEFORE
    # get_recent_video_performance so an excluded channel skips the
    # performance / longform / email quota below. It is a post-response
    # BACKSTOP, not a cost-free filter: the channels.list unit above is already
    # spent by here, and on the discovery path the creator's 0.01 discovery
    # credit was already billed when the vendor returned it. Saving THOSE would
    # need a server-side negation filter in discovery_filters — a follow-up,
    # and only after the vendor's bio-negation field is verified live the way
    # gender/topics were — the same reason exclude_handles exists. This local
    # gate stays regardless: it is the only tier that also covers the
    # search.list fallback, and it is deterministic across both paths.
    topic = excluded_topic_reason(stats.get("channel_title", ""), stats.get("description", ""))
    if topic:
        logger.info(
            "Dropping %s before push — %s (%s).",
            stats.get("channel_title"), DROP_EXCLUDED_TOPIC, topic,
        )
        return None, DROP_EXCLUDED_TOPIC

    # Television networks and TV shows (2026-08-20). A separate gate from
    # excluded_topic_reason above because it matches network NAMES against the
    # title only — a creator citing an HGTV credit in their bio is not a
    # broadcaster — see broadcast_tv_reason and the niches.py comment. Free,
    # and placed with the other description checks so a discarded broadcaster
    # costs no performance fetch.
    tv = broadcast_tv_reason(stats.get("channel_title", ""), stats.get("description", ""))
    if tv:
        logger.info(
            "Dropping %s before push — %s (%s).",
            stats.get("channel_title"), DROP_BROADCAST_TV, tv,
        )
        return None, DROP_BROADCAST_TV

    # The channel's own bio must read as English, whatever its per-video
    # language tag says. Free (the description is already fetched) and placed
    # here with the other description checks, so a non-English channel costs no
    # performance fetch. is_english() further down still runs for everyone: the
    # two catch different things, and @LINTAN777 passed that one while failing
    # this.
    if description_is_non_english(stats.get("description", "")):
        logger.info(
            "Dropping %s before push — %s (%d non-Latin script chars in the bio).",
            stats.get("channel_title"), DROP_NON_ENGLISH_DESCRIPTION,
            non_latin_script_chars(stats.get("description", "")),
        )
        return None, DROP_NON_ENGLISH_DESCRIPTION

    # THE WHOLE SEARCH-ZONE DECISION, and it now runs HERE — before the paid
    # performance fetch — rather than half here and half after it. Every input
    # is free (title, About description, and snippet.country, all already on
    # the channels.list response), so an out-of-zone candidate no longer costs
    # ~3 quota units plus any long-form paging before being discarded. That
    # became possible on 2026-08-20 when the declared-country check stopped
    # falling back to the content language's region subtag, which was the one
    # input that needed the fetch. See location_drop_reason.
    #
    # A channel that declares NO country is dropped here too. That is a
    # deliberate break from "absent data never disqualifies" — see
    # search_zones.zone_verdict.
    zone_drop, zone_detail = location_drop_reason(
        stats.get("channel_title", ""),
        stats.get("description", ""),
        (stats.get("country") or "").strip(),
        niche_config["allowed_country_codes"],
    )
    if zone_drop:
        logger.info(
            "Dropping %s before push — %s (%s).",
            stats.get("channel_title"), zone_drop, zone_detail,
        )
        return None, zone_drop

    performance = get_recent_video_performance(channel_id, stats.get("uploads_playlist_id"))
    time.sleep(API_SLEEP_SECONDS)
    if performance is None:
        logger.info("Skipping %s — no accessible recent video performance data.", stats.get("channel_title"))
        return None, "unreachable"

    # RELEVANCE, and it runs HERE for a reason: this is the first point at which
    # the video titles exist (they arrive free on the response just fetched), and
    # it is still ahead of everything expensive — the long-form confirmation
    # paging (2 units a page), the scoring, and the email chain whose step 4 is a
    # 0.2-credit influencers.club lookup. So a gaming or generic-tech channel is
    # discarded before it can consume a paid email credit, which is what the
    # brief asks for: "irrelevant creators are filtered out before they consume a
    # creator credit or get added to Airtable."
    #
    # The creator's 0.01 DISCOVERY credit is already spent by here and cannot be
    # recovered — the vendor bills on RETURN, before any gate sees the creator.
    # What stops that recurring is the pair of changes upstream: the retuned
    # ai_search (which no longer asks for "gaming setup") and the
    # rejected-handle cache, which excludes this creator server-side from the
    # next run onward so the same 0.01 is never paid twice.
    off_target, off_detail = off_target_reason(
        niche_config, stats.get("description", ""), performance.get("video_titles"),
    )

    # Activity/quality signals for the gate, all free from data already
    # fetched. upload_freq (videos/month over the sampled window) is computed
    # HERE, before the gate, so the cadence check can read it — and it is
    # reused unchanged for the Overall Score and the "Upload Frequency" column
    # below, never recomputed.
    upload_dates = performance.get("upload_dates", [])
    upload_freq = calc_upload_frequency(upload_dates)
    # None (not 0) when the window is too thin to estimate a cadence, so an
    # unmeasurable channel isn't dropped on a made-up zero — see
    # pre_push_drop_reason's None rule. Shared with audit_prospects.py; the
    # rationale lives in calc_uploads_per_year's docstring.
    uploads_per_year = calc_uploads_per_year(upload_dates)
    days_since = days_since_last_upload(upload_dates)

    # Pre-push gate, placed before scoring and before the email chain so a
    # discarded candidate costs no browser session and no deep-scan quota.
    #
    # MOVED ABOVE THE GEMINI BLOCK 2026-08-24, and this is the whole point of the
    # move: every input below is FREE and already in hand as of the performance
    # fetch, while a Gemini request is the scarcest thing this pipeline spends.
    # Measured over run_metrics.jsonl's two completed 2026-08-24 runs: of the 169
    # candidates that reached the Gemini block, 108 (64%) were then dropped right
    # here on this arithmetic — below_view_minimum 79, shorts_only 20,
    # too_few_videos 7, not_english 2. Each of those had already cost a request
    # that could not be spent on a candidate whose verdict was still open.
    # Running this gate first cuts the population that needs a request from 169
    # to 61, which at the observed 78 requests/day is the difference between 46%
    # and 100% coverage of the candidates that reach it.
    #
    # The long-form floor is NOT part of this and stays below the Gemini block:
    # establishing its count can cost quota, so it is correctly split out into
    # longform_drop_reason. It still spent 16 requests across those two runs.
    # Moving it up too would trade YouTube quota (3,580 of 10,000 used) for
    # Gemini requests (78 of ~80) — probably right, but a different decision.
    #
    # DO NOT move this back below the Gemini block. tests/
    # test_gate_order_request_budget.py fails loudly if you do.
    drop_reason = pre_push_drop_reason(
        stats.get("subscriber_count"),
        performance.get("avg_views"),
        performance.get("shorts_only", False),
        min_avg_views=niche_config["min_avg_views"],
        video_count=stats.get("video_count"),
        content_language=performance.get("content_language"),
        settled_views=performance.get("settled_views"),
        uploads_per_year=uploads_per_year,
        days_since_last_upload=days_since,
    )
    if drop_reason:
        logger.info(
            "Dropping %s before push — %s (%s subs, %s avg views, min %s, %s videos, "
            "%s uploads/yr, %s days since upload, lang %s).",
            stats.get("channel_title"), drop_reason,
            stats.get("subscriber_count"), round(performance.get("avg_views") or 0, 1),
            performance.get("min_views"), stats.get("video_count"),
            round(uploads_per_year, 1) if uploads_per_year is not None else "unknown",
            days_since if days_since is not None else "unknown",
            performance.get("content_language") or "unset",
        )
        return None, drop_reason

    # Placed BELOW pre_push_drop_reason as of 2026-08-25, and for the same reason
    # the Gemini relevance block is: LAYER 2 below issues a paid Gemini request,
    # and every input to the free numeric gate above is already in hand. Putting
    # a paid call ahead of a free gate is precisely the defect R0 fixed for the
    # relevance tier, and wiring layer 2 in reintroduced it here — the guard test
    # only compared layer 1 against verifier.judge, so it did not catch a second
    # paid call appearing in between. It does now.
    #
    # TOPIC EVIDENCE FROM CREATOR TAGS — the free half of "what is this video
    # actually about", and a DIFFERENT input from every other gate here: the
    # others read what the channel is CALLED or what it NAMES its videos, none
    # read what a video is about. A firearms channel titling videos "Range Day
    # 47" and a Lego channel titling one "New Build Complete!" are invisible to
    # excluded_topic_reason and off_target_reason and legible here.
    #
    # Free in every sense: `video_tags` arrived on the videos.list response
    # already fetched above (a flat 1 unit regardless of parts), so this costs no
    # quota, no credits, no Gemini request and no network call. It therefore sits
    # with the other free gates, ahead of the paid Gemini block.
    #
    # ADVISORY unless VIDEO_TOPIC_GATE is set. The evidence is always computed
    # and always recorded; only the DROP is gated. That split is the repo's
    # standing pattern for a new relevance signal — the Gemini text tier is
    # advisory for the same reason — and it exists because three separate
    # relevance criteria in this pipeline have been caught pointing the wrong
    # way. Measured before shipping: at the default 0.40 share this fires on 0
    # of 81 Approved and 5 of 130 Rejected channels (measure_video_topics.py).
    topic_evidence = video_topics.topic_evidence(
        performance.get("video_tags"),
        {**EXCLUDED_TOPIC_TERMS, **OFF_TARGET_TERMS},
    )
    topic_summary = video_topics.summarise(
        topic_evidence, performance.get("video_category_ids"))
    topic_category, topic_share = video_topics.dominant_topic(
        topic_evidence, VIDEO_TOPIC_MIN_SHARE)
    # Restricted to the measured allowlist, NOT to whatever fired. phones_and_pcs
    # is net-harmful at every threshold where it fires and is deliberately absent
    # from VIDEO_TOPIC_CATEGORIES; a dominant topic outside the allowlist is
    # recorded and ignored.
    if topic_category and topic_category not in VIDEO_TOPIC_CATEGORIES:
        topic_category = None
    # LAYER 2 — CONTENT CONFIRMATION, and it runs ONLY on a Layer 1 hit.
    #
    # This is the whole two-layer shape: metadata for reach, content for
    # accuracy. Layer 1 above is free and reads the whole sampled catalogue, but
    # tags are the CREATOR'S OWN CLAIM about their content. Before a row is
    # removed, that claim is checked against what the video actually contains.
    #
    # Cost is why this works. Confirmation runs on ~2% of candidates (5 of 211
    # labelled channels fire at the shipping threshold), so it is ~1-3 requests
    # per run rather than one per candidate — the difference between fitting the
    # 70/run cap and being 4x over it.
    #
    # FAIL-OPEN. `confirmed` is True only on an explicit, confident yes; every
    # other edge (feature off, no verifier, no sampled video, cap reached,
    # timeout, malformed, low confidence, an explicit no) leaves it False and the
    # candidate is KEPT. So an outage can never remove a row. That asymmetry is
    # load-bearing because this is the only path where an AI answer reaches
    # rejected_handles.json, which excludes the creator server-side for 90 days.
    topic_confirmation = None
    if VIDEO_TOPIC_GATE and topic_category and verifier is not None:
        topic_confirmation = verifier.confirm_topic(
            video_topics.topic_label(topic_category),
            topic_evidence["terms"].get(topic_category, []),
            performance,
        )

    if VIDEO_TOPIC_GATE and topic_category:
        tag_detail = (f"{topic_category} at {100 * topic_share:.0f}% of "
                      f"{topic_evidence['tags_seen']} tags: "
                      f"{', '.join(topic_evidence['terms'].get(topic_category, []))}")
        if topic_confirmation is not None and topic_confirmation.confirmed:
            logger.info(
                "Dropping %s before push — %s (%s). Content CONFIRMS: %s. Said: %s",
                stats.get("channel_title"), DROP_OFF_TOPIC_TAGS, tag_detail,
                topic_confirmation.detail, topic_confirmation.spoken[:200] or "-",
            )
            return None, DROP_OFF_TOPIC_TAGS
        # Tags fired and the content did not back them up. KEPT, and the
        # disagreement is logged both ways round: a metadata gate that the
        # content keeps overturning is a gate that needs retuning, and that is
        # only visible if the near-misses are on the record too.
        why = (topic_confirmation.detail if topic_confirmation is not None
               else "no verifier configured")
        logger.info(
            "KEEPING %s — tags said %s but content did not confirm (%s). Said: %s",
            stats.get("channel_title"), tag_detail, why,
            (topic_confirmation.spoken[:200] if topic_confirmation else "-") or "-",
        )
    elif topic_category:
        logger.info(
            "TOPIC ADVISORY %s — %s at %.0f%% of %d tags. Not dropped: "
            "VIDEO_TOPIC_GATE is off.",
            stats.get("channel_title"), topic_category, 100 * topic_share,
            topic_evidence["tags_seen"],
        )

    # GEMINI RELEVANCE VERIFICATION — a RESCUE LADDER on the gate above, and
    # nothing else. Read the two branches below carefully, because the whole
    # safety argument for this feature is in their shape:
    #
    #   - a candidate the keyword gate did NOT flag is scored and CONTINUES,
    #     whatever the score says. The score is advisory: it is recorded for the
    #     reviewer and for the backtest that will decide whether it ever belongs
    #     in Overall Score. It is not a gate and must not become one without that
    #     measurement, because a positive must-match relevance gate was already
    #     built, measured and REJECTED in this repo (see off_target_reason).
    #
    #   - a candidate the keyword gate DID flag is dropped exactly as it is
    #     today, UNLESS both Gemini tiers confirm it is on-niche. Only then is
    #     the drop reversed.
    #
    # So this block can only ever ADD a row. There is no new DROP_ reason, and
    # nothing here writes to rejected_handles.json — a rescued candidate simply
    # stops being a reject, and a non-rescued one is recorded by the existing
    # gate exactly as before. Every failure edge (disabled, no key, 429, timeout,
    # 4xx, malformed, cap reached, no video, unreadable ledger) leaves
    # `judgement.rescued` False, which is indistinguishable from this feature not
    # existing. That is deliberate: it is what makes "the pipeline stays
    # functional when Gemini is unavailable" true by construction rather than by
    # careful coding.
    #
    # Placed HERE for the same reason off_target_reason is: the titles and
    # descriptions it reads arrive free on the response just fetched, and it is
    # still ahead of the long-form paging and the 0.2-credit email lookup. A
    # rescue therefore costs nothing a normal candidate does not.
    #
    # It now sits BELOW pre_push_drop_reason rather than above it (2026-08-24).
    # That gate is free and was discarding 73% of the candidates this block had
    # just paid a request for — see the comment on it above. The only candidates
    # reaching this point are ones whose verdict is genuinely still open, which
    # is what the request budget should be spent on. Nothing about the rescue
    # semantics changed: a candidate the free gates drop was already dropped
    # before this move, it just used to cost a request on the way out.
    judgement = None
    if verifier is not None:
        judgement = verifier.judge(
            niche_config, stats, performance, flagged=bool(off_target),
        )
        if off_target and judgement.rescued:
            logger.info(
                "RESCUED %s — the title gate flagged it (%s) but Gemini confirmed "
                "it is on-niche: %s",
                stats.get("channel_title"), off_detail, judgement.detail,
            )
            off_target = None

    if off_target:
        logger.info(
            "Dropping %s before push — %s (%s).%s",
            stats.get("channel_title"), off_target, off_detail,
            f" Gemini: {judgement.detail}." if judgement is not None else "",
        )
        return None, off_target

    # (The search-zone gate used to sit here, after the performance fetch,
    # because its language-region-subtag fallback needed content_language.
    # That fallback is gone and the whole gate moved ABOVE the fetch — see
    # location_drop_reason. Don't move it back down: doing so would re-spend
    # ~3 units on every out-of-zone candidate.)

    # Long-form floor, LAST of the discard gates because it is the only one
    # that can cost quota: confirming 30 non-Shorts uploads may need up to
    # LONGFORM_SCAN_MAX_PAGES extra pages (2 units each) for a channel whose
    # newest 50 videos didn't already show them. Every free reason to discard
    # has now had its turn, so nothing is paged for a candidate that was
    # going to be dropped anyway.
    longform_count = performance.get("longform_count", 0)
    if longform_count < MIN_LONGFORM_VIDEO_COUNT:
        longform_count = count_longform_in_older_videos(
            channel_id,
            stats.get("uploads_playlist_id", ""),
            performance.get("next_page_token", ""),
            already_counted=longform_count,
            target=MIN_LONGFORM_VIDEO_COUNT,
        )
    drop_reason = longform_drop_reason(longform_count)
    if drop_reason:
        logger.info(
            "Dropping %s before push — %s (%d confirmed non-Shorts of %s total videos).",
            stats.get("channel_title"), drop_reason, longform_count, stats.get("video_count"),
        )
        return None, drop_reason

    # upload_freq was computed once above the pre-push gate (the cadence
    # check needs it) and is reused here rather than recomputed.
    fake_risk = calc_fake_follower_risk(
        stats["subscriber_count"], performance["avg_views"], performance["avg_engagement_rate"]
    )
    overall_score = calc_overall_score(
        stats["subscriber_count"],
        performance["avg_views"],
        performance["avg_engagement_rate"],
        upload_freq,
        fake_risk,
        DEFAULT_NICHE_MATCH,
    )

    # Age is the only qualification question left — the view floor, the
    # video-count floor and the search zone are all hard gates above, so a
    # candidate that reaches here has already passed them.
    qualification = qualify(
        channel_age_months(stats.get("published_at", "")),
        niche_config["min_channel_age_months"],
    )

    # LAST FREE EXIT, and the only one placed on a budget rather than on the
    # channel. Everything above this line is spent (YouTube quota, and on the
    # discovery path the creator's 0.01 credit); everything below can cost REAL
    # MONEY — resolve_email_with_source step 4 is a 0.2-credit influencers.club
    # lookup.
    #
    # push_until_full used to make this decision AFTER build_record returned, by
    # which point the lookup was paid for and the record was thrown away at its
    # bucket check. Measured on the live shape (both tables are 100% "Qualified",
    # so DAILY_FLAGGED_CAP never fills and the flagged hunt keeps going): 8
    # candidates offered, 8 email lookups paid for, 1 row written. Up to
    # FLAGGED_ONLY_PATIENCE of those per round, per niche.
    #
    # Qualification is what makes the early exit possible: it turns on channel
    # age alone (see scoring.qualify), and age comes from `published_at`, which
    # channels.list already returned. So the bucket a candidate would land in is
    # knowable here, before the chain — no extra call, no reordering of any gate.
    #
    # The post-build bucket check in push_until_full deliberately STAYS as the
    # backstop: this probe is an optimisation and a caller may not pass one.
    if has_room is not None and not has_room(qualification):
        logger.info(
            "Dropping %s before the email chain — %s (a '%s' row has no daily "
            "headroom left, so no email lookup is worth paying for).",
            stats.get("channel_title"), DROP_NO_HEADROOM, qualification,
        )
        return None, DROP_NO_HEADROOM

    email, email_source, has_external_links = resolve_email_with_source(
        stats, performance, scraper, enricher
    )

    # No-social drop: a channel whose About link list was fetched (step 5 of
    # the email chain ran, because steps 1-4 found no address) and came back
    # EMPTY has no website and no social profile — no outreach surface beyond
    # YouTube, and nothing to vet the creator against. Only a positively-empty
    # list (False) discards; None means the list was never read (the browser
    # is off, or an address was already found at an earlier step) and the
    # channel is KEPT — the same "absent data never disqualifies" rule the
    # zone check follows. Runs after the email chain because the signal comes
    # OUT of that chain's own link-list fetch, at no extra cost.
    if has_external_links is False:
        logger.info(
            "Dropping %s before push — %s (no external links and no contact email).",
            stats.get("channel_title"), DROP_NO_SOCIAL,
        )
        return None, DROP_NO_SOCIAL

    # Checkpoint 3 — catches agency addresses shared across channels.
    if email:
        hit = blocklist.match(email=email)
        if hit:
            logger.info("BLOCKED %s — DO NOT CONTACT (%s).", stats.get("channel_title"), hit)
            return None, "blocked"

    # csv_safe() is applied to the FREE-TEXT fields only — see its docstring
    # for why (the reviewer exports this view to CSV and opens it in Excel).
    # Which fields are excluded, and why, matters as much as which are
    # wrapped; each exclusion is noted at its line below.
    record = {
        # Attacker-controlled: whatever the channel owner typed.
        "Channel Name": csv_safe(stats["channel_title"]),
        # NOT wrapped: "Channel URL" and "Channel ID" are matched on EXACTLY
        # elsewhere. Channel ID in particular is the dedupe key
        # airtable_client.channel_exists() looks up by, so a leading
        # apostrophe would make every existing row invisible to the lookup
        # and the pipeline would re-POST duplicates instead of PATCHing.
        # Neither field can carry a payload anyway: both are derived from a
        # YouTube channel ID, which is a fixed-alphabet "UC..." string.
        "Channel URL": f"https://www.youtube.com/channel/{channel_id}",
        "Channel ID": channel_id,
        # NOT wrapped: numeric fields. Airtable's Number fields reject
        # strings, and csv_safe() passes non-strings through untouched
        # precisely so that a mistake here fails loudly rather than being
        # papered over.
        "Subscriber Count": stats["subscriber_count"],
        "Avg Views (last 10 videos)": round(performance["avg_views"], 1),
        "Engagement Rate": round(performance["avg_engagement_rate"], 2),
        # "Upload Frequency" is a text field in Airtable (not Number) — it
        # rejects raw JSON numbers, so this must be sent as a string.
        # Rounded to a whole number for display; the unrounded value is
        # still what feeds calc_overall_score above.
        #
        # NOT wrapped: this string is built here and always starts with a
        # digit, so csv_safe() would be a guaranteed no-op. Harmless either
        # way; left off so the wrapped fields are exactly the ones carrying
        # third-party text.
        "Upload Frequency": f"{round(upload_freq)} videos/month",
        # Best-effort: most creators never set defaultAudioLanguage/
        # defaultLanguage on their videos, so this is frequently "Unknown".
        # Channel *country* (stats["country"]) is a separate signal and is
        # deliberately not used here, since it isn't the same thing as the
        # content's spoken language.
        #
        # Wrapped: it's a free-text field echoing a value the channel owner
        # set on their videos.
        "Content Language": csv_safe(performance.get("content_language") or "Unknown"),
        # Attacker-influenced: chain step 5 (browser_email.py) reads
        # arbitrary third-party websites for this.
        "Email": csv_safe(email),
        "Fake Follower Risk Score": fake_risk,
        "Overall Score": overall_score,
        # NOT wrapped: Single Select values that must match an existing
        # Airtable option EXACTLY. push_record sends typecast=True, which
        # silently CREATES a missing option rather than erroring — so a
        # mangled "'Qualified" would quietly mint a new option and drop the
        # row out of the reviewer's saved views. Both values are ours
        # (scoring.py / config.py), not third-party text.
        "Qualification": qualification,
        "Status": DEFAULT_STATUS,
        # Wrapped for consistency rather than out of fear: the keywords are
        # our own NICHES entries, so the risk is low, but it is still a text
        # field assembled from data and there is no reason to leave the one
        # free-text field uncovered.
        "Source": csv_safe(f"{SOURCE_LABEL} ({', '.join(candidate.get('matched_keywords', []))})"),
        "Notes": "",
        # NOT wrapped: a date value from prospect_day.today_iso(), not text.
        "Date Added": today_iso(),
    }

    # The creator's @handle, written ONLY when the column exists.
    #
    # Purpose: discovery's server-side exclude_handles takes handles, not channel
    # IDs, so without this column every row in this table is returned and BILLED
    # (0.01 each) on every run, then resolved at one YouTube unit just to be
    # recognised and discarded. Storing it here is what lets the NEXT run tell
    # the vendor not to send them.
    #
    # Guarded because push_record sends field names as-is and Airtable rejects
    # the WHOLE record for one unknown field — writing this blind before the
    # column exists would break every push. table_has_field() probes once per
    # table per run and caches, so this costs one tiny read and turns "column
    # not added yet" into a no-op.
    #
    # NOT wrapped by csv_safe: handles are matched on EXACTLY (by the vendor's
    # exclusion and by get_tracked_handles), so a prepended apostrophe would
    # silently stop every exclusion working — the same reasoning that keeps
    # Channel ID unwrapped. A YouTube handle cannot carry a formula payload.
    # table_name off niche_config, since process_candidate isn't given it
    # directly. Absent in unit-test configs, which correctly skips the probe.
    handle = (stats.get("handle") or "").strip().lstrip("@")
    niche_table = niche_config.get("table_name")
    if handle and niche_table and table_has_field(niche_table, "Handle"):
        record["Handle"] = handle

    # WHERE the address came from, and — when step 4 produced it — what the
    # vendor called it. Same table_has_field guard and same reasoning as
    # "Handle" above: push_record sends field names as-is and Airtable rejects
    # the whole record for one unknown field.
    #
    # Why this is worth two columns. The chain has five steps of very different
    # trustworthiness (a repeated address across recent videos vs. a regex hit on
    # a third-party website), and until now the source was computed by
    # resolve_email_with_source and then DISCARDED into `_email_source`, so every
    # cell looked equally authoritative and a blank one explained nothing. A
    # reviewer asking "why does this row have no email, and what is 'Other'?"
    # could not answer it from the table — the answer only existed in a log line
    # from a run that had already scrolled past.
    #
    # "Other" is specifically `email_type`, the vendor's own label, which this
    # pipeline never stored. Writing it puts the value the reviewer sees in the
    # vendor's dashboard next to the address in the table they actually work from.
    #
    # On a MISS the source is "" and the note carries the vendor's reason
    # (not_found / invalid_or_expired / declined), which is the difference
    # between "nobody has this address" and "the one on file stopped
    # validating" — verified live: 7 of the 8 email-less rows in the base are
    # the former and 1 is the latter.
    # Gemini relevance verdict, in four columns, each written ONLY when it
    # exists — same table_has_field guard and same reasoning as "Handle" above.
    #
    # A value is written on EVERY judgement, never left blank, because a blank
    # cell would otherwise mean four different things (feature off, column
    # absent, probe blipped, row predates the feature) — the exact ambiguity the
    # README already documents for "Email Source": "Without it a blank Email cell
    # cannot be told apart from a row written before the column existed."
    #
    # "Relevance State" is the only Single select, and only because its value set
    # is CLOSED (scored/rescued/unavailable). push_record sends typecast=True,
    # which silently MINTS a missing option — harmless for a closed set, but it
    # would turn the free-form detail into hundreds of one-off options and drop
    # rows out of the reviewer's saved views. That is why the detail is text.
    #
    # csv_safe on both text fields: "Relevance Notes" is the most
    # attacker-influenced field in this record — model-generated prose derived
    # from video a creator fully controls, and models reproduce on-screen text
    # faithfully. Newlines are already flattened at the assembly site in
    # gemini_verify._notes, because csv_safe only inspects value[0] and an
    # embedded newline in a CSV export can start a fresh logical line.
    #
    # "Verified Video URL" is deliberately NOT wrapped: it always starts with
    # "https://", so it cannot begin with a formula prefix. Do not "simplify" it
    # to a bare video ID — an 11-character YouTube ID can legitimately start with
    # "-", which IS a formula prefix, and the hole would reopen silently.
    if judgement is not None and niche_table:
        if table_has_field(niche_table, "Relevance State"):
            record["Relevance State"] = judgement.state
        if table_has_field(niche_table, "Relevance Detail"):
            record["Relevance Detail"] = csv_safe(judgement.detail)
        if judgement.notes and table_has_field(niche_table, "Relevance Notes"):
            record["Relevance Notes"] = csv_safe(judgement.notes)
        if judgement.video_url and table_has_field(niche_table, "Verified Video URL"):
            record["Verified Video URL"] = judgement.video_url

    if niche_table and table_has_field(niche_table, "Email Source"):
        record["Email Source"] = csv_safe(email_source or _email_miss_note(enricher))
    if niche_table and table_has_field(niche_table, "Email Type"):
        # Only step 4 has a type; the other four sources have no such concept,
        # so this is deliberately blank for them rather than guessed at.
        record["Email Type"] = csv_safe(
            getattr(enricher, "last_email_type", "") or ""
            if email_source == EMAIL_SOURCE_INFLUENCERS else ""
        )

    return record, qualification


def _email_miss_note(enricher) -> str:
    """
    What to write in "Email Source" when the whole chain came up empty.

    Prefers the vendor's own reason for declining (step 4 is the only step that
    gives one) and falls back to a plain statement that every step ran. Either
    way the cell says something, because an empty "Email Source" beside an empty
    "Email" is the ambiguity this column exists to remove — it would leave a
    reviewer unable to tell "we looked and there is nothing" from "this row
    predates the column".
    """
    note = (getattr(enricher, "last_email_note", "") or "").strip()
    return f"none found ({note})" if note else "none found (all 5 steps ran)"


def _external_priority(external_handles, source_hint: str) -> list[str]:
    """
    This niche's external handles first, then everyone else's — the order the
    10k cap is applied in.

    Why this is not just `sorted()`. The vendor caps `exclude_handles` at 10,000
    (INFLUENCERS_MAX_EXCLUDE_HANDLES) and this base holds 14,337, so ~4,300 are
    dropped from every request. Sorted alphabetically, that is the SAME
    alphabetical tail dropped on every run forever — measured 2026-08-15, the
    cut lands at "pinkkupinsku", and a --test run duly re-bought five creators
    from the o-v range that were already tracked externally.

    A handle only costs anything if THIS niche's query would return it, and the
    Home Theatre tables (12,388 handles) are the ones a Home Theater query can
    re-surface. Ranking them ahead of the 1,949 Lifestyle ones spends the scarce
    slots where re-bills actually happen. Measured effect: Lifestyle Sofa's own
    external handles (1,949) now fit entirely, taking it from ~538 uncovered to
    zero; Home Theater's coverage improves but cannot be complete until the
    exclusion is split across requests or the vendor's persistent list is wired.

    Ties are broken alphabetically so the set stays deterministic — a run's
    exclusions should not depend on dict iteration order.

    `source_hint` is matched case-insensitively as a SUBSTRING of the source
    table name, which is why NICHES carries it explicitly rather than reusing
    the niche name: the tables spell it "Home Theatre" and the niche is "Home
    Theater". An empty or unmatched hint degrades to plain alphabetical order,
    i.e. exactly the old behaviour.
    """
    hint = source_hint.casefold()

    def rank(handle: str) -> tuple[int, str]:
        source = ""
        try:
            source = external_handles[handle] or ""
        except (KeyError, TypeError):
            # A plain set/list of handles carries no source — every entry ranks
            # the same, and the sort degrades to alphabetical. Keeps this usable
            # from tests and from any caller that isn't holding an ExternalIndex.
            pass
        return (0 if hint and hint in source.casefold() else 1, handle)

    return sorted(external_handles, key=rank)


def _discovery_exclude_handles(
    blocklist, external_handles, seen_handles, tracked_handles=(),
    external_source_hint: str = "", rejected_handles=(),
) -> set[str]:
    """
    Assemble the discovery exclude set, PRIORITISED under the 10k cap.

    The order is deliberate, and DO NOT CONTACT comes first because it is the
    exclusion that matters most: a creator on the suppression list is going to
    be dropped by process_candidate's blocklist checkpoints no matter what, so
    any discovery credit spent surfacing them is pure waste — and excluding
    them server-side also means they never appear in results a reviewer sees.
    So the blocklist's handles are never dropped to make room.

    `seen_handles` (creators already examined THIS run) come next, so a later
    round never re-bills an earlier round's candidates.

    `rejected_handles` — creators this niche's query has already returned and our
    gates already rejected — rank alongside them too, and for the identical
    reason: the endpoint sorts by relevancy deterministically, so a reject is a
    creator the vendor WILL return again, not one it might. Measured 2026-08-20,
    28% of a live page was creators already known to us (7 tracked elsewhere, 4
    on DO NOT CONTACT). See rejected_handles.py, and note the vendor's 10,000
    cap is verified rather than assumed — 12,000 elements is a 400 — which is
    what makes this a BUDGET and the ordering below a real decision.

    `tracked_handles` — this niche's OWN Airtable rows — rank alongside them,
    and closing that gap is why they exist. The niche tables historically stored
    only a Channel ID, which `exclude_handles` cannot use, so every tracked
    creator was returned and BILLED (0.01) every run and then resolved at one
    YouTube unit just to be recognised and discarded. That waste grew with the
    table and never stopped. They are in must_keep rather than the leftover room
    because they are certain re-bills: unlike an external-table handle, which
    MIGHT never be returned by this niche's query, a row in this table came out
    of this query.

    External-table handles fill whatever room is left under the cap, ordered by
    _external_priority so THIS niche's own outreach tables claim the slots
    first — the base holds more handles than the cap allows, so which ones get
    dropped is a real decision and not a tie-break. The ones that don't fit are
    still caught after enrichment by process_candidate's external-handle check
    — at the cost of one channels.list unit each, not a wrong contact.

    The blocklist screening in process_candidate is unchanged and remains the
    authoritative, fail-closed suppression gate (it also matches on email and
    name, which a handle-only exclusion can't). This set is a cost-saver layered
    in front of it, never a replacement for it.
    """
    must_keep = (
        set(blocklist.handles) | set(seen_handles) | set(tracked_handles)
        | set(rejected_handles)
    )
    room = max(0, INFLUENCERS_MAX_EXCLUDE_HANDLES - len(must_keep))
    ranked = _external_priority(external_handles, external_source_hint)
    external = [h for h in ranked if h not in must_keep][:room]
    return must_keep | set(external)


def _run_discovery_rounds(
    niche_name: str,
    table_name: str,
    niche_config: dict,
    discovery,
    globally_tracked_ids: set[str],
    external_handles: ExternalIndex,
    blocklist,
    scraper,
    enricher,
    qualified_headroom: int,
    flagged_headroom: int,
    tracked_handles=(),
    verifier=None,
    drop_reasons=None,
) -> dict:
    """
    Fill a niche's daily budget from influencers.club discovery instead of
    search.list. Same refill shape as run_niche's keyword loop, with two
    differences that follow from the source:

      - the round's batch is a target creator COUNT handed to
        discovery.discover() (which paginates the endpoint internally), not a
        batch of keywords, and
      - dedupe is by @handle, because discovery returns a handle rather than a
        channel ID. exclude_handles asks the vendor to withhold creators
        already in the base so they are never returned — and, at 0.01 credits
        each, never billed.

    Returns the same counters run_niche tracks:
    {qualified, flagged, discovered, skipped, pushed_ids}.
    """
    pushed_qualified = 0
    pushed_flagged = 0
    total_discovered = 0
    total_skipped = 0
    pushed_ids: set[str] = set()

    logger.info("Discovery source for '%s': influencers.club creator search.", niche_name)
    # The exclude set is assembled per round (see _discovery_exclude_handles):
    # DO NOT CONTACT handles first and never dropped, then this run's seen
    # handles, then external-table handles filling the 10k cap. The niche
    # tables store a Channel ID, not a handle, so their own rows aren't
    # excluded server-side — the post-enrichment channel_id dedupe below
    # (known_channel_ids) is the backstop for a re-discovered niche-table
    # channel, at the cost of the 1-unit channels.list to resolve it.
    filters = niche_config["discovery_filters"]
    seen_handles: set[str] = set()
    # Loaded ONCE per niche, then grown in memory as this run rejects more, so a
    # later round in the same run already excludes what an earlier one rejected
    # without re-reading the file.
    rejected = rejected_handles.for_niche(niche_name)
    if rejected:
        logger.info(
            "'%s': %d previously-rejected handle(s) will be excluded server-side.",
            niche_name, len(rejected),
        )
    # Creators the vendor already billed us for but that this niche hasn't had
    # the headroom to examine yet. Drained before any new request is issued, so
    # a page bought in round 1 is spent over as many rounds as it takes instead
    # of being re-bought each round.
    backlog: list[dict] = []
    dry = False
    # Counts VENDOR REQUESTS, not loop iterations. A round that only drains the
    # backlog costs nothing, so charging it against the cap would let the cap
    # expire without the niche having spent — the "caps unreachable by
    # construction" failure CLAUDE.md records for the old single-pass loop.
    vendor_requests = 0

    while True:
        # Same "one opportunistic round for flagged once qualified is full"
        # rule as the keyword loop — the flagged budget is a ceiling, never a
        # target, and for a niche whose min_channel_age_months is None it can
        # never fill, so it must not drive the loop.
        if (vendor_requests or backlog) and pushed_qualified >= qualified_headroom:
            break

        rows_wanted = (qualified_headroom - pushed_qualified) + (flagged_headroom - pushed_flagged)
        # Oversized against the gate survival rate, exactly as the keyword
        # loop oversizes its candidate batch; the loop itself is what
        # guarantees the budget fills, so this only affects round count.
        target = max(1, int(rows_wanted * CANDIDATE_OVERSHOOT))

        if len(backlog) < target and not dry:
            if vendor_requests >= DISCOVERY_MAX_ROUNDS:
                logger.warning(
                    "'%s' hit the discovery request cap (%d) — stopping.",
                    niche_name, DISCOVERY_MAX_ROUNDS,
                )
                break
            vendor_requests += 1
            fetched = discovery.discover(
                filters=filters,
                target=target,
                exclude_handles=_discovery_exclude_handles(
                    blocklist, external_handles, seen_handles, tracked_handles,
                    external_source_hint=niche_config.get("external_source_hint", ""),
                    rejected_handles=rejected,
                ),
                source_label=f"influencers.club discovery ({niche_name})",
            )
            fresh = [c for c in fetched if c["handle"] not in seen_handles]
            # EVERY handle the vendor returned is marked seen, not just the ones
            # this round will examine — the vendor billed for all of them, and
            # anything left out here comes back in the next request's
            # exclude_handles gap and gets billed a second time. This line is
            # the actual fix for the 16-credits-for-one-row run.
            seen_handles.update(c["handle"] for c in fetched)
            backlog.extend(fresh)
            total_discovered += len(fresh)
            if not fresh:
                dry = True
            logger.info(
                "Discovery request for '%s': asked for %d, got %d new candidate(s) "
                "(%d now backlogged).",
                niche_name, target, len(fresh), len(backlog),
            )

        if not backlog:
            logger.info("Discovery is dry for '%s' — stopping.", niche_name)
            break

        # Examine only what there is headroom for. The rest stays backlogged:
        # handing all 50 to push_until_full would trade a bounded money leak for
        # an unbounded YouTube-quota one (~3-13 units per candidate, and
        # push_until_full only stops early when BOTH budgets are full).
        batch, backlog = backlog[:target], backlog[target:]

        counts = push_until_full(
            batch,
            lambda c, has_room: process_candidate(
                c, external_handles, blocklist, niche_config, scraper, enricher,
                known_channel_ids=globally_tracked_ids | pushed_ids,
                has_room=has_room,
                verifier=verifier,
            ),
            table_name,
            qualified_headroom - pushed_qualified,
            flagged_headroom - pushed_flagged,
            flagged_possible=niche_config["min_channel_age_months"] is not None,
        )
        pushed_qualified += counts["qualified"]
        pushed_flagged += counts["flagged"]
        total_skipped += counts["skipped"]
        pushed_ids |= counts["pushed_ids"]
        if drop_reasons is not None:
            drop_reasons.update(counts["drop_reasons"])

        # Persisted per ROUND, not once at the end of the niche: a run that dies
        # mid-niche (quota abort, runner timeout, the 60-minute CI ceiling) has
        # already PAID for these creators, and losing the record means paying for
        # them again next run.
        newly_rejected = counts["rejected_handles"] - rejected
        if newly_rejected:
            rejected |= newly_rejected
            rejected_handles.record(niche_name, newly_rejected)

        logger.info(
            "'%s' so far: %d/%d qualified, %d/%d flagged "
            "(%.2f discovery credits, %d request(s), %d backlogged).",
            niche_name, pushed_qualified, qualified_headroom,
            pushed_flagged, flagged_headroom, discovery.credits_spent,
            vendor_requests, len(backlog),
        )

    return {
        "qualified": pushed_qualified,
        "flagged": pushed_flagged,
        "discovered": total_discovered,
        "skipped": total_skipped,
        "pushed_ids": pushed_ids,
    }


def run_niche(
    niche_name: str,
    table_name: str,
    keywords: list[str],
    max_results_per_keyword: int,
    days_back: int,
    globally_tracked_ids: set[str],
    external_handles: ExternalIndex,
    blocklist,
    niche_config: dict,
    scraper,
    enricher=None,
    discovery=None,
    verifier=None,
    drop_reasons=None,
) -> tuple[int, int, set[str], bool]:
    """
    Run discovery -> pre-filter -> enrich -> score -> push for one niche's
    table. `globally_tracked_ids` is the base-wide dedupe set (union of
    every niche table's Channel IDs, not just this one) — a candidate
    already tracked in ANY niche's table is skipped here too, so a
    channel is claimed by whichever niche discovers it first rather than
    being trackable in more than one table.

    Returns (discovered_count, processed_count, newly_tracked_ids,
    cap_check_completed) — the caller merges newly_tracked_ids into the
    shared dedupe set so a later niche in the same run also sees channels
    just pushed by this one.

    cap_check_completed is True iff this niche's daily-cap check
    (count_added_today) actually ran and succeeded — regardless of
    whether it turned out the niche was already at cap. It is False when
    the niche was skipped for a reason that says nothing about today's
    cap (no table configured, a misconfigured NICHES entry, or an
    unreadable Airtable count). run() uses this to tell "every niche is
    legitimately full today" (a real no-op) apart from "nothing could be
    checked" (a run that silently did nothing) — see IMPORTANT 3 in the
    fix-wave review.
    """
    if not table_name:
        logger.error(
            "No Airtable table configured for niche '%s' — set the matching env var. Skipping this niche.",
            niche_name,
        )
        return 0, 0, set(), False

    # Read the niche's qualification criteria ONCE, here, rather than
    # indexing niche_config[...] inside process_candidate (which runs
    # once per candidate). Checked with "in" rather than truthiness:
    # min_channel_age_months legitimately holds None for niches with no
    # age requirement (e.g. Lifestyle Sofa), so its presence — not its
    # value — is what a misconfigured NICHES entry would get wrong.
    # Failing fast here means a bad config skips this niche instead of
    # raising a KeyError mid-niche and killing the whole run.
    if "min_avg_views" not in niche_config or "min_channel_age_months" not in niche_config:
        logger.error(
            "Niche '%s' is missing 'min_avg_views' or 'min_channel_age_months' in its NICHES "
            "config — skipping this niche rather than crashing partway through.",
            niche_name,
        )
        return 0, 0, set(), False

    logger.info("=== Niche: %s (table: %s) ===", niche_name, table_name)

    try:
        qualified_today = count_added_today(table_name, QUALIFIED)
        flagged_today = count_added_today(table_name) - qualified_today
    except AirtableReadError as e:
        logger.error("Cannot read today's counts for '%s' (%s) — skipping niche.", niche_name, e)
        return 0, 0, set(), False

    qualified_headroom = max(0, DAILY_QUALIFIED_CAP - qualified_today)
    flagged_headroom = max(0, DAILY_FLAGGED_CAP - flagged_today)
    logger.info(
        "'%s': %d/%d qualified and %d/%d flagged already added today.",
        niche_name, qualified_today, DAILY_QUALIFIED_CAP, flagged_today, DAILY_FLAGGED_CAP,
    )
    if qualified_headroom == 0 and flagged_headroom == 0:
        logger.info("'%s' is already at its daily cap — skipping (no quota spent).", niche_name)
        return 0, 0, set(), True

    # --- Refill loop --------------------------------------------------
    # Discover a batch of keywords, push what survives, and come back for
    # more while the QUALIFIED budget has room and keywords remain (see the
    # loop condition below for why flagged doesn't drive this).
    #
    # This used to be a single pass: discover
    # (headroom * CANDIDATE_OVERSHOOT) candidates once, push, done. That
    # worked while almost every candidate became a row, but the 2026-08
    # criteria change moved the view floor, the video-count floor and the
    # search zone into pre_push_drop_reason() as hard DISCARDS — measured,
    # only ~15% of fresh candidates now survive to be written (18 of 122 on
    # 2026-08-11). So a 40-row budget was handed 60 candidates, produced ~9
    # rows, and stopped with 6 of 9 keywords never searched. The cap was
    # unreachable by construction, and the only symptom was the
    # "finished under its qualified budget" warning below, which reads as
    # "discovery is running dry" — the wrong diagnosis, since the keywords
    # had plenty left.
    #
    # Refilling makes the loop self-correcting in both directions: a bad
    # survival rate keeps searching, and a good one stops early, so quota
    # still tracks what the day's headroom actually needs.
    pushed_qualified = 0
    pushed_flagged = 0
    total_discovered = 0
    total_skipped = 0
    pushed_ids: set[str] = set()

    # Discovery source selection. When an influencers.club key is configured
    # (discovery.enabled) and this niche carries discovery_filters, creator
    # search REPLACES the keyword loop below: it filters on the niche's
    # criteria server-side, so a far larger fraction of what it returns
    # survives the gates than raw YouTube search does. With no key — or a
    # niche without filters — control falls straight through to the keyword
    # loop, so the pipeline still runs when influencers.club is unavailable.
    # `discovery_source` lets a niche opt OUT of paid discovery and back onto the
    # free YouTube keyword loop below. Defaults to "influencers", so a niche that
    # says nothing behaves exactly as before.
    #
    # Why the choice needs to be per niche. The vendor filters server-side, so a
    # far larger fraction of what it returns survives the gates — that is why it
    # is the default and why Lifestyle Sofa keeps it. But its pool is FINITE per
    # query, and Home Theater has very nearly consumed its own: measured
    # 2026-08-22, gross 334, and 279 after its 262 cached rejects are excluded.
    # At the observed 1 row per 100-150 creators that is about two rows left in
    # the entire paid corpus, after which the niche returns zero however the
    # gates are tuned.
    #
    # `keywords` costs YouTube quota instead of vendor credits and indexes a
    # different, far larger corpus. It converts worse per candidate examined —
    # that trade is the whole point of the vendor — but "worse conversion on an
    # unbounded corpus" beats "good conversion on an exhausted one". No quality
    # gate changes either way: every candidate from both sources goes through
    # the same process_candidate.
    #
    # THIRD OPTION, "both": run paid discovery AND THEN top up from the free
    # keywords in the same run. Added 2026-08-22 because the two sources were
    # mutually exclusive for no good reason, and each is bounded in a different
    # way — the paid one by the credit budget (Lifestyle can afford ~600 of its
    # 2,814 available creators per run), the free one by the search window. A
    # niche that exhausts its paid budget has no reason to stop when a free
    # corpus is sitting there, and the keyword loop already respects the
    # remaining headroom, so it simply fills what discovery could not.
    discovery_source = niche_config.get("discovery_source", "influencers")
    use_discovery = (
        discovery is not None and discovery.enabled
        and "discovery_filters" in niche_config
        and discovery_source in ("influencers", "both")
    )
    if discovery_source == "search_list":
        logger.info(
            "'%s' is configured for discovery_source=%r — using the free YouTube "
            "keyword loop instead of paid influencers.club discovery.",
            niche_name, discovery_source,
        )
    if use_discovery:
        # This niche's own tracked handles, so the vendor never returns — and
        # never bills for — a creator already in this table. Empty until the
        # "Handle" column exists (see airtable_client.get_tracked_handles), in
        # which case behaviour is exactly as before.
        d = _run_discovery_rounds(
            niche_name, table_name, niche_config, discovery,
            globally_tracked_ids, external_handles, blocklist, scraper, enricher,
            qualified_headroom, flagged_headroom,
            tracked_handles=get_tracked_handles(table_name),
            verifier=verifier,
            drop_reasons=drop_reasons,
        )
        pushed_qualified = d["qualified"]
        pushed_flagged = d["flagged"]
        total_discovered = d["discovered"]
        total_skipped = d["skipped"]
        pushed_ids = d["pushed_ids"]

    # Local copy — the caller's set is shared across niches and must not be
    # mutated here. Grows with every candidate this niche has ALREADY
    # examined (pushed or dropped), so a later batch never re-enriches a
    # channel an earlier one already paid for. Emptied when discovery already
    # ran, so the keyword loop below is skipped entirely in that mode.
    seen_ids = set(globally_tracked_ids)
    # Emptied only when paid discovery is the SOLE source. Under
    # discovery_source="both" the keyword loop runs after the discovery rounds
    # and tops up whatever headroom they left, which is the point of that mode.
    #
    # seen_ids is seeded with what discovery already pushed, so the keyword loop
    # does not re-enrich a channel this run just wrote. Without it the duplicate
    # would still be caught by process_candidate's known_channel_ids check, but
    # only after paying a channels.list unit for it.
    if use_discovery and discovery_source == "both":
        remaining_keywords = list(keywords)
        seen_ids |= pushed_ids
    elif use_discovery:
        remaining_keywords = []
    else:
        remaining_keywords = list(keywords)
    rounds = 0

    while remaining_keywords:
        # Only the QUALIFIED budget is worth spending another 100-unit
        # search on. The flagged budget is a CEILING, not a target — it
        # exists so a weak discovery day can't crowd the table with
        # below-criteria channels, so flagged rows are written
        # opportunistically as they turn up and never hunted for. Chasing it
        # would also never terminate for a niche that cannot produce one at
        # all: Lifestyle Sofa's min_channel_age_months is None, so qualify()
        # can only ever return "Qualified" there and its flagged budget goes
        # permanently unused (documented, expected). A loop that kept
        # searching until flagged filled would burn every keyword in that
        # niche, every day, for rows that can't exist.
        #
        # Tested after at least one round, so a niche whose qualified budget
        # is already full still gets a single opportunistic pass for flagged.
        if rounds and pushed_qualified >= qualified_headroom:
            break
        rounds += 1

        rows_wanted = (qualified_headroom - pushed_qualified) + (flagged_headroom - pushed_flagged)
        # How many keywords to search this round. Ceiling division, floor of
        # 1, so a small shortfall still searches one keyword rather than
        # zero (which would spin the loop without spending or progressing).
        wanted_candidates = max(1, int(rows_wanted * CANDIDATE_OVERSHOOT))
        batch_size = max(
            1,
            -(-wanted_candidates // max(1, EXPECTED_CANDIDATES_PER_KEYWORD)),
        )
        batch = remaining_keywords[:batch_size]
        remaining_keywords = remaining_keywords[batch_size:]

        logger.info(
            "Discovery round for '%s': %d keyword(s) %s — %d row(s) still wanted.",
            niche_name, len(batch), batch, rows_wanted,
        )
        # target_fresh is deliberately NOT passed: the batch was already
        # sized to the shortfall, and stopping part-way through it would
        # consume keywords from remaining_keywords without searching them.
        discovered = run_discovery(
            batch,
            max_results_per_keyword=max_results_per_keyword,
            days_back=days_back,
            exclude_ids=seen_ids,
        )
        total_discovered += len(discovered)
        logger.info("Discovered %d unique candidate channel(s).", len(discovered))

        # Straight set-membership filter, deliberately not a DataFrame: a
        # round trip through pandas rewrote the candidates on the way out —
        # it appended its own bookkeeping column to every record and filled a
        # NaN wherever one candidate carried a key another lacked (which also
        # promoted that column's ints to floats). Nothing downstream wanted
        # either.
        if not discovered:
            logger.info("No candidates discovered — nothing to process.")
        new_candidates = [c for c in discovered if c["channel_id"] not in seen_ids]

        logger.info(
            "%d candidate(s) already tracked or already examined, %d remaining to process.",
            len(discovered) - len(new_candidates), len(new_candidates),
        )

        counts = push_until_full(
            new_candidates,
            lambda c, has_room: process_candidate(
                c, external_handles, blocklist, niche_config, scraper, enricher,
                has_room=has_room,
                verifier=verifier,
            ),
            table_name,
            qualified_headroom - pushed_qualified,
            flagged_headroom - pushed_flagged,
            flagged_possible=niche_config["min_channel_age_months"] is not None,
        )

        pushed_qualified += counts["qualified"]
        pushed_flagged += counts["flagged"]
        total_skipped += counts["skipped"]
        pushed_ids |= counts["pushed_ids"]
        if drop_reasons is not None:
            drop_reasons.update(counts["drop_reasons"])
        # Every candidate offered to push_until_full is now spent, whether it
        # was written or dropped. push_until_full only returns before reading
        # its whole list when BOTH budgets are full, which also ends this
        # loop — so nothing unexamined is being discarded here.
        seen_ids.update(c["channel_id"] for c in new_candidates)

        logger.info(
            "'%s' so far: %d/%d qualified, %d/%d flagged (%d keyword(s) left).",
            niche_name, pushed_qualified, qualified_headroom,
            pushed_flagged, flagged_headroom, len(remaining_keywords),
        )

    logger.info(
        "'%s': pushed %d qualified, %d flagged, skipped %d.",
        niche_name, pushed_qualified, pushed_flagged, total_skipped,
    )

    if not use_discovery and pushed_qualified < qualified_headroom:
        logger.warning(
            "'%s' finished under its qualified budget (%d of %d) with every keyword "
            "searched. Discovery really is running dry for these keywords — widen "
            "--days-back for a one-off sweep, or add keywords from the brief's "
            "secondary content types.",
            niche_name, pushed_qualified, qualified_headroom,
        )

    return total_discovered, pushed_qualified + pushed_flagged, pushed_ids, True


# A NICHES entry missing any of these crashes run() with a bare KeyError
# (table_name/keywords, indexed directly in run() before run_niche() is
# even called), run_niche() itself (min_avg_views/min_channel_age_months,
# checked there — see its docstring), or process_candidate() partway through
# a niche (allowed_country_codes, indexed by the zone gate). Checking all
# five here, up front, means a bad config skips just that niche instead of
# killing the whole run partway through — which matters most for the last of
# them, whose KeyError would land only after quota had already been spent.
REQUIRED_NICHE_KEYS = (
    "table_name",
    "keywords",
    "min_avg_views",
    "min_channel_age_months",
    # Required rather than defaulted (2026-08-20) so a new niche cannot
    # silently inherit search_zones.ALLOWED_COUNTRY_CODES, which is the WIDEST
    # zone the module knows and includes all of Europe — the zone both current
    # niches were just taken OUT of. A missing key skips that niche with a
    # logged error, which is loud; a silent Europe-wide default would not be.
    "allowed_country_codes",
)


def run(
    niches: dict,
    max_results_per_keyword: int,
    days_back: int,
    max_discovery_credits=None,
) -> None:
    try:
        blocklist = fetch_blocklist()
    except BlocklistUnavailable as e:
        logger.error("ABORTING: %s", e)
        raise SystemExit(1)

    # Checked ONCE here, beside the blocklist, for the same fail-closed reason.
    # Without it the first corrupt read happens deep inside a paid call site,
    # which disables only that half: the run then spends YouTube quota, logs two
    # ERROR lines, prints "LEDGER UNAVAILABLE" in the summary, and exits ZERO —
    # a scheduled run reported green having produced no influencers-sourced rows.
    # That is exactly what any_cap_check_completed already refuses to do.
    try:
        assert_credit_ledger_readable()
    except CreditLedgerUnavailable as e:
        logger.error("ABORTING: %s", e)
        raise SystemExit(1)

    # Global (base-wide) dedupe: fetched once across every niche's table
    # before any niche runs, so a channel already tracked anywhere in the
    # base — not just in the niche currently being processed — is skipped.
    # get_existing_channel_ids() raises AirtableReadError rather than
    # returning a partial set on a mid-pagination failure (a 429 on page 7
    # of 14, say); a partial set here would make already-tracked channels
    # look fresh and get re-pushed, reverting reviewer Status/Notes (see
    # IMPORTANT 2 in the fix-wave review). Abort the whole run rather than
    # proceed on a set we can't trust, mirroring the blocklist abort above.
    globally_tracked_ids: set[str] = set()
    try:
        for niche_config in niches.values():
            if niche_config.get("table_name"):
                globally_tracked_ids |= get_existing_channel_ids(niche_config["table_name"])
    except AirtableReadError as e:
        logger.error("ABORTING: cannot establish the existing-channel-ID dedupe set (%s).", e)
        raise SystemExit(1)

    # Handles already tracked in the base's other YouTube outreach/leads/
    # influencer tables (see external_dedupe.py) — cached, so this is
    # near-instant on any run within EXTERNAL_CACHE_MAX_AGE_HOURS of the
    # last one.
    external_handles = fetch_external_handles()

    total_discovered = 0
    total_processed = 0
    # Whether ANY niche actually completed its daily-cap check this run
    # (see run_niche()'s cap_check_completed return value). An expired
    # Airtable token, or a NICHES dict where every entry is missing a
    # required key, would otherwise skip every niche and still exit 0 —
    # a daily scheduled job silently doing nothing forever. See IMPORTANT 3.
    any_cap_check_completed = False

    # Email chain step 4. One client per run so the lookup budget and the
    # credit-cap breaker are scoped to the run, and inert when no API key
    # is set — the same soft-disable contract as null_scraper() below.
    enricher = InfluencersClient.from_config()
    # Said out loud for the same reason the browser warning below is. This is
    # the only step that costs money, and from_config() logs only when the key
    # is ABSENT — so a live run was otherwise silent about whether step 4 ran
    # at all, which is indistinguishable from it running and finding nothing.
    if enricher.enabled:
        logger.info("Email chain step 4 is live (influencers.club enrichment).")

    # Discovery source, one client per run so its credit ceiling is run-scoped.
    # Inert when no API key is set, in which case run_niche falls back to the
    # YouTube search.list keyword loop — so the pipeline still runs without it.
    discovery = InfluencerDiscovery.from_config(max_credits=max_discovery_credits)
    if discovery.enabled:
        logger.info("Discovery source: influencers.club creator search (replacing search.list).")
    else:
        logger.info("influencers.club discovery unavailable — discovery falls back to YouTube search.list.")

    # Gemini relevance verification. One verifier per run, for the same reason
    # the two clients above are per-run: the request counters, the quota latch and
    # the wall-clock budget all describe ONE run. from_config() returns None when
    # the feature is off, the key is missing, the model is not allowlisted, or the
    # ledger is unreadable — so `verifier is None` is the single inert state and
    # every downstream branch is a no-op by construction. It logs its own
    # enabled/disabled line, at WARNING when the configuration is contradictory.
    verifier = GeminiVerifier.from_config()

    scraper = BrowserEmailScraper.launch() if USE_PLAYWRIGHT_STEALTH else null_scraper()
    # launch() fails SOFT — a missing Chromium binary, a missing shared
    # library, or an unimportable playwright all return an inert scraper
    # rather than raising, so the run continues with email chain step 5
    # silently doing nothing. That is the right behaviour (one email source
    # is not worth killing a run over) but it is invisible: the symptom is
    # simply fewer emails, on a metric nobody watches per-run. Say it out
    # loud instead, since "USE_PLAYWRIGHT_STEALTH is set" and "the browser
    # actually started" are different facts and only the second one matters.
    if USE_PLAYWRIGHT_STEALTH and not scraper.enabled:
        logger.warning(
            "USE_PLAYWRIGHT_STEALTH is on but the browser could not start — email "
            "chain step 5 (linked site / contact page) is doing nothing this run. "
            "On CI this usually means the 'Install Chromium for Playwright' step "
            "was skipped; locally, run: python -m playwright install chromium"
        )
    elif USE_PLAYWRIGHT_STEALTH:
        logger.info("Browser email step is live (Playwright + stealth).")

    # Per-run yield accounting. Populated as the loop goes and written in the
    # `finally` below, so a run that dies mid-way still records what it spent —
    # see the write call for why that placement is load-bearing.
    run_started_at = datetime.now(timezone.utc).isoformat()
    _run_completed = False
    drop_reasons: Counter = Counter()
    niche_metrics: dict[str, dict] = {}

    try:
        for niche_name, niche_config in niches.items():
            missing_keys = [key for key in REQUIRED_NICHE_KEYS if key not in niche_config]
            if missing_keys:
                logger.error(
                    "Niche '%s' is missing required NICHES key(s) %s — skipping this niche "
                    "rather than crashing the whole run.",
                    niche_name, missing_keys,
                )
                continue

            discovered, processed, newly_tracked_ids, cap_check_completed = run_niche(
                niche_name,
                niche_config["table_name"],
                niche_config["keywords"],
                max_results_per_keyword,
                days_back,
                globally_tracked_ids,
                external_handles,
                blocklist,
                niche_config,
                scraper,
                enricher,
                discovery,
                verifier,
                drop_reasons=drop_reasons,
            )
            total_discovered += discovered
            total_processed += processed
            # Per niche, not just the total: the question this file was built to
            # answer is "did Home Theater stop returning zero", and a combined
            # figure cannot answer it.
            niche_metrics[niche_name] = {
                "rows": processed, "discovered": discovered,
                "cap_check_completed": cap_check_completed,
            }
            any_cap_check_completed = any_cap_check_completed or cap_check_completed
            # So a later niche in this same run also sees channels this one
            # just pushed, rather than only picking up prior runs' state.
            globally_tracked_ids |= newly_tracked_ids
        # Last statement in the try: reaching it means every niche returned.
        _run_completed = True
    finally:
        scraper.close()
        # IN THE `finally`, deliberately. The run summary below is not: it sits
        # after this block and is skipped by any exception from run_niche, by
        # the `raise SystemExit(1)` further down, by a CI timeout and by
        # KeyboardInterrupt. Those are exactly the runs worth recording, because
        # they already spent money. The counters above accumulate in place, so
        # a partial write is truthful rather than empty.
        #
        # Honest limitation: run_niche merges its counters only on return, so a
        # crash MID-niche loses that niche's rows. The record is therefore "as
        # of the last completed niche", which is what `status` is for.
        run_metrics.write(run_metrics.build(
            status="completed" if _run_completed else "aborted",
            started_at=run_started_at,
            finished_at=datetime.now(timezone.utc).isoformat(),
            niches=niche_metrics,
            drop_reasons=drop_reasons,
            credits_spent=getattr(discovery, "credits_spent", 0.0),
            creators_billed=getattr(discovery, "creators_billed", 0),
            quota_used=get_today_spend(),
            config_snapshot={
                "DAILY_QUALIFIED_CAP": DAILY_QUALIFIED_CAP,
                "DAILY_FLAGGED_CAP": DAILY_FLAGGED_CAP,
                "max_discovery_credits": max_discovery_credits,
                "MIN_VIEWS_PER_VIDEO_RATIO": MIN_VIEWS_PER_VIDEO_RATIO,
                # Read through the module, not a `from config import` copy:
                # this one is not otherwise bound in main, and going through
                # the module keeps the snapshot honest if the value is retuned.
                "DISCOVERY_SUBSCRIBER_FLOOR_RATIO": getattr(
                    _config, "DISCOVERY_SUBSCRIBER_FLOOR_RATIO", None),
            },
        ))

    quota_used = get_today_spend()
    print("\n--- Run summary ---")
    print(f"Total discovered:  {total_discovered}")
    print(f"Total processed:   {total_processed}")
    print(f"Quota used today:  {quota_used} / {DAILY_QUOTA_BUDGET}")
    # An upper bound on credits, not a count of them: a lookup that found
    # no address was free (see influencers.py). Reported because a credit
    # spend nobody watches per-run is exactly how a budget gets a surprise.
    if enricher.lookups_spent:
        print(
            f"influencers.club:  {enricher.lookups_spent} billable lookup(s), "
            f"{enricher.credits_reported:g} credits reported by the vendor"
        )
    # Discovery credits are the exact figure the vendor billed (credits_cost),
    # not an upper bound — every returned creator is charged, unlike an enrich
    # miss which is free.
    #
    # Reported WITH the creators-billed count and the credits-per-row ratio,
    # because the absolute figure alone hid a 32x waste bug: 16 credits looked
    # unremarkable until it sat next to "1 qualified row". A run that starts
    # buying creators it never examines shows up here as the ratio moving, which
    # is the number to watch.
    if discovery.credits_spent:
        # total_processed is rows actually PUSHED (qualified + flagged), which
        # is the denominator that matters — not candidates examined.
        per_row = (
            f"{discovery.credits_spent / total_processed:.3g}"
            if total_processed else "n/a (no rows)"
        )
        print(
            f"discovery credits: {discovery.credits_spent:g} spent on creator search "
            f"({discovery.creators_billed} creators billed, {per_row} credits/row)"
        )

    # The two figures above describe THIS RUN; this one describes ALL RUNS today
    # and this month, which was previously impossible to see. Labelled explicitly
    # because on a second run of the day these numbers legitimately differ from
    # the per-run ones above, and two different totals under similar labels is how
    # a reader concludes one of them is wrong. Printed unconditionally — including
    # when this run spent nothing — since "already at 9.8 of 10 today" is most
    # worth knowing on the run that is about to be refused.
    print(f"credits, all runs today: {credit_spend_summary()}")

    # Gemini, printed UNCONDITIONALLY whenever the feature is enabled — zeros
    # included. The `if …spent:` guards above are right for spend that may
    # legitimately be zero, but they are WRONG here: the case most worth
    # surfacing is "enabled and issued nothing", which on CI means the workflow
    # env: entry or the cache path is missing. A conditional line would hide the
    # feature in exactly its most common failure. Same reasoning as
    # credit_spend_summary() printing unconditionally.
    #
    # The RESCUED count is the number to watch, and it is the direct analogue of
    # the credits-per-row ratio above: if it stays at 0 the feature is a
    # well-tested no-op and should be switched off or retuned, and that is
    # visible within one run instead of after a month of reading verdicts.
    if verifier is not None:
        for line in verifier.summary_lines():
            print(line)
        verifier.flush_cache()
    else:
        print('gemini relevance:  DISABLED (see the startup log line for why)')

    if not any_cap_check_completed:
        logger.error(
            "No niche completed its daily-cap check this run — every niche was skipped for "
            "a non-cap reason (missing NICHES config, no table configured, or an unreadable "
            "Airtable count). Exiting non-zero so a scheduled run that did nothing is never "
            "reported as green."
        )
        raise SystemExit(1)

    # ZERO ROWS AFTER REAL WORK IS ALSO A RESULT THAT NEEDS SAYING OUT LOUD.
    #
    # The check above only catches a run that never got as far as a cap check. It
    # says nothing about the case that actually happened repeatedly on this
    # pipeline: discovery ran, credits were spent, candidates were examined, and
    # NOT ONE row was written — and the run exited 0 and reported green. From the
    # outside that is indistinguishable from a healthy quiet day, which is why
    # "the pipeline gives no records" went undiagnosed.
    #
    # Deliberately a LOUD ERROR and not a non-zero exit. The run genuinely
    # succeeded at everything it was asked to do; the finding is about YIELD, not
    # correctness, and failing a scheduled job for a weak day would train whoever
    # watches it to ignore red. The line carries the two numbers that tell a weak
    # day apart from a broken one, and names the first thing to check.
    if total_processed == 0 and total_discovered > 0:
        logger.error(
            "ZERO ROWS WRITTEN this run, from %d creator(s) discovered. The run "
            "itself worked — this is a yield result, not a crash. Check, in this "
            "order: (1) the drop-reason counts printed above, which say which gate "
            "consumed the candidates; (2) whether the niche's discovery pool is "
            "exhausted, which applies ONLY to a niche still on influencers.club "
            "discovery — run `measure_discovery_pool.py --net` for the real "
            "figure, because gross totals ignore exclude_handles and overstate "
            "it (Home Theater measured 334 gross but 279 net on 2026-08-22). A "
            "niche on discovery_source='search_list' cannot run dry this way, "
            "so read its drop reasons instead; (3) TODOS.md 'Yield levers', which ranks "
            "the gates by how many rows each is actually costing.",
            total_discovered,
        )
    elif total_processed == 0:
        logger.warning(
            "Zero rows written and zero creators discovered. Nothing was examined, "
            "so no gate is at fault — discovery returned nothing. Either the pool "
            "is exhausted for every niche, or the discovery source is refusing "
            "(check the credit ledger and the influencers.club warnings above)."
        )


def main() -> None:
    global DAILY_QUALIFIED_CAP, DAILY_FLAGGED_CAP

    parser = argparse.ArgumentParser(description="YouTube channel vetting pipeline")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run a cheap end-to-end smoke test: 1 keyword, max_results=5, first niche only.",
    )
    parser.add_argument(
        "--daily-cap",
        type=int,
        default=None,
        help=(
            "Override both DAILY_QUALIFIED_CAP and DAILY_FLAGGED_CAP for this run, so the "
            "capping path can be tested cheaply against production Airtable without also "
            "leaving the flagged budget at its full (10/day) size."
        ),
    )
    parser.add_argument(
        "--days-back",
        type=int,
        default=DISCOVERY_DAYS_BACK,
        help="How many days back to search for videos. Defaults to DISCOVERY_DAYS_BACK "
             "(7). Pass a larger value for a one-off backlog sweep, e.g. --days-back 90.",
    )
    args = parser.parse_args()

    if args.daily_cap is not None:
        DAILY_QUALIFIED_CAP = args.daily_cap
        DAILY_FLAGGED_CAP = args.daily_cap

    if args.test:
        # Bound the daily caps too, not just max_results. max_results only
        # limits the search.list FALLBACK path; when influencers.club discovery
        # is active it ignores max_results and fills the daily cap, so without
        # this a --test run would discover, enrich, and push toward a full
        # 30-row day (real credits, real quota, real rows) instead of a cheap
        # smoke test. A caller who wants a specific size can still pass
        # --daily-cap, which takes precedence.
        if args.daily_cap is None:
            DAILY_QUALIFIED_CAP = 2
            DAILY_FLAGGED_CAP = 1
        # The row caps do NOT bound discovery spend — they make it worse, so the
        # credit ceiling has to be passed separately. See
        # INFLUENCERS_TEST_DISCOVERY_CREDITS in config.py for why (a smaller cap
        # shrinks `target` below the vendor's 50-result minimum billable page,
        # so each round buys 50 creators to examine 3). The old version of this
        # log line claimed the caps "bound discovery spend too"; a live --test
        # run spent 16 credits for one row, which is the refutation.
        logger.info(
            "Running in --test mode: first niche only, max_results=5, capped to "
            "%d qualified / %d flagged, discovery ceiling %.2f credits.",
            DAILY_QUALIFIED_CAP, DAILY_FLAGGED_CAP, INFLUENCERS_TEST_DISCOVERY_CREDITS,
        )
        first_niche_name = next(iter(NICHES))
        first_niche = NICHES[first_niche_name]
        test_niches = {first_niche_name: {**first_niche, "keywords": first_niche["keywords"][:1]}}
        run(
            niches=test_niches,
            max_results_per_keyword=5,
            days_back=args.days_back,
            max_discovery_credits=INFLUENCERS_TEST_DISCOVERY_CREDITS,
        )
    else:
        run(niches=NICHES, max_results_per_keyword=50, days_back=args.days_back)


if __name__ == "__main__":
    main()
