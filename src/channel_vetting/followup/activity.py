"""
Channel activity for the follow-up categorizer — the "has this creator stopped
uploading?" signal, on the cheapest FREE path that answers it.

WHY THIS IS NOT `enrichment.get_recent_video_performance()`
----------------------------------------------------------
That function is documented at channels.py:753-756 as costing "always exactly 2
units" — playlistItems.list + videos.list — because it computes average views
and engagement over the newest 10 videos. We need ONE thing: the date of the
newest upload. `videos.list` buys view counts we then throw away.

So this module calls playlistItems.list with maxResults=1 and stops. Combined
with the channels.list?forHandle lookup needed to turn a legacy @handle into an
uploads-playlist id, the per-channel figure is:

    channels.list?forHandle   1 unit   (get_channel_stats, bills itself)
    playlistItems.list        1 unit   (here)
    --------------------------------
    TOTAL                     2 units

against the 3 units a naive reuse of get_recent_video_performance() would
charge. Over the 9,991 legacy rows that survive the free screens (measured
2026-08-25, see FOLLOWUP_PLAN.md W0) that is 19,982 units instead of 29,973.

Once `Channel ID` is persisted the channels.list half is never paid again, so a
RE-sweep is 1 unit per channel. That is the whole reason to persist it.

MONEY: THERE IS NONE
--------------------
YouTube Data API v3 quota is a free daily allowance (10,000 units) and a rate
limit. There is no per-unit billing and no way to buy units — a quota increase
is a request form, not a purchase. `budget/credit_tracker.py` says it outright:
"Credits are the only spend in this pipeline that is real money... Free YouTube
quota has quota_log.json". So a unit is a share of a free resource that
discovery also needs, and the only real cost of this sweep is that discovery
cannot spend the same unit. `assert_free_only()` below makes that structural.

CLOCK
-----
`enrichment.days_since_last_upload()` calls datetime.now() internally, so it
cannot be tested at a fixed date and a stored delta drifts. `days_since_upload()`
here takes `now` as an argument for the same reason `followup_eligibility()`
takes `clock=` — every follow-up test computes an age, and a test that reads the
wall clock passes today and fails in February.
"""
import logging
from dataclasses import dataclass
from datetime import datetime

import requests

from channel_vetting.budget.quota_tracker import record_spend
from channel_vetting.config import (
    QUOTA_COST_CHANNELS_LIST,
    QUOTA_COST_PLAYLIST_ITEMS_LIST,
    YOUTUBE_API_BASE_URL,
)
from channel_vetting.core.http_client import YOUTUBE as HTTP, safe_body
from channel_vetting.core.iso_time import parse_iso_utc
from channel_vetting.enrichment.channels import get_channel_stats

logger = logging.getLogger(__name__)

# The only two endpoints this module may touch. Both are free YouTube Data API
# quota. Anything not on this list is a bug, not a feature.
FREE_ENDPOINTS = ("channels.list", "playlistItems.list")

# Free quota units per channel on the FIRST pass, and on a re-sweep once
# Channel ID is stored. Asserted by tests so the docstring cannot drift.
UNITS_FIRST_PASS = QUOTA_COST_CHANNELS_LIST + QUOTA_COST_PLAYLIST_ITEMS_LIST
UNITS_RESWEEP = QUOTA_COST_PLAYLIST_ITEMS_LIST

# --- Activity verdicts -------------------------------------------------------
# UNKNOWN is not a synonym for INACTIVE, and this is the whole reason the
# constant exists. `enrichment.days_since_last_upload()` returns None for an
# unreadable or empty upload list and its docstring instructs callers to treat
# that as unknown, NOT as stale. pipeline.py:740 does exactly that for
# discovery, where "unknown -> keep the lead" is the safe direction.
#
# For a follow-up the safe direction is the OPPOSITE. Copying discovery's
# predicate would let a channel with no readable upload data fall through to
# eligible, which is the same inversion `followup_eligibility()` rule 4 makes
# for a blank Reply State. So UNKNOWN gets its own verdict and its own bucket,
# and it never reads as ACTIVE.
ACTIVE = "active"
INACTIVE = "inactive"
UNKNOWN = "unknown"

# Failure reasons, surfaced verbatim in Follow-Up Reason so an operator reading
# a row can tell a dead channel from an unfinished sweep.
REASON_CHANNEL_GONE = "channel_gone"
REASON_NO_UPLOADS = "no_uploads"
REASON_UNREADABLE_DATE = "upload_date_unreadable"
REASON_NOT_CHECKED = "not_checked"
REASON_API_ERROR = "api_error"


class PaidSurfaceError(RuntimeError):
    """
    Raised when a module that can spend real money is reachable from a code
    path advertised as free.

    Deliberately an exception rather than a log line: "this costs nothing" is a
    claim the operator is relying on, and a claim like that must fail loudly
    when it stops being true rather than degrade quietly.
    """


def assert_free_only() -> tuple[list[str], list[str]]:
    """
    Prove, at startup, that THIS RUN cannot spend money. Returns (passed, warnings).

    Two real-money surfaces exist in this repo:

      1. influencers.club credits (`budget/credit_tracker.py` — "the only spend
         in this pipeline that is real money"). The follow-up work never needs
         them: the legacy rows already carry the email addresses that credits
         would have bought.
      2. Gemini (`verification/gemini.py`).

    NOTE ON WHAT THE HARD CHECKS PROVE. `sys.modules` is process-global, so
    "the credit module is not loaded" is a statement about this PROCESS, not a
    proof about this code path — inside a test runner that imported the whole
    package it fires spuriously, which is exactly how that limitation was found.
    It is a real smoke check at the CLI entry point, where the only thing
    imported is the sweep's own graph, and it is NOT a substitute for the static
    guarantee. The proof is
    `tests/test_followup_activity.py::test_no_followup_module_imports_a_paid_path`,
    which reads this package's source and fails if any module here imports a
    credit- or model-spending path at all.

    The HARD checks are about reachability from this process, because that is
    what actually bounds this run. An earlier version of this function also
    hard-failed on GEMINI_ENABLED, which was the wrong shape: this module never
    calls Gemini, so a global flag about a different subsystem is not a fact
    about this run, and blocking on it stops free work for no safety gain.

    Ambient billable configuration is reported as a WARNING instead — it is
    still worth an operator seeing, because it governs the scheduled pipeline
    even when it does not govern this script.
    """
    import sys

    passed, warnings = [], []

    # HARD: the credit-spending modules must not be loaded. This is the check
    # that catches a future refactor quietly pulling the email enricher in.
    forbidden = [
        "channel_vetting.discovery.influencers_club",
        "channel_vetting.enrichment.email_influencers",
    ]
    loaded = [m for m in forbidden if m in sys.modules]
    if loaded:
        raise PaidSurfaceError(
            f"credit-spending module(s) loaded: {loaded}. The follow-up sweep "
            "must not import influencers.club paths — those spend real money."
        )
    passed.append("no influencers.club module loaded — credit spend impossible")

    # HARD: the Gemini client must not be loaded either.
    if "channel_vetting.verification.gemini" in sys.modules:
        raise PaidSurfaceError(
            "channel_vetting.verification.gemini is loaded. This path must not "
            "reach a model — FOLLOWUP_PLAN.md W9 keeps relevance off Gemini."
        )
    passed.append("no Gemini client loaded — model spend impossible")
    passed.append(f"endpoints limited to {', '.join(FREE_ENDPOINTS)} — free YouTube quota only")
    passed.append("YouTube Data API quota is a free daily allowance, not billed")

    # WARN: ambient config that can bill on OTHER runs.
    from channel_vetting.config import (
        GEMINI_ENABLED, GEMINI_FREE_ONLY, GEMINI_MODEL, GEMINI_FREE_TIER_MODELS,
    )
    if GEMINI_ENABLED:
        detail = "free-tier model" if GEMINI_MODEL in GEMINI_FREE_TIER_MODELS else "NON-free-tier model"
        warnings.append(
            f"GEMINI_ENABLED=true in this environment ({GEMINI_MODEL}, {detail}, "
            f"GEMINI_FREE_ONLY={GEMINI_FREE_ONLY}). Does not affect this sweep, "
            "but docs/TODOS.md records it should be false pending the criteria "
            "rewrite. The free-tier allowlist prevents a typo, not a charge."
        )
    return passed, warnings


# --- Pure functions (no I/O, injectable clock) --------------------------------

def days_since_upload(last_upload_at: str | None, now: datetime) -> int | None:
    """
    Whole days between `last_upload_at` and `now`, or None if the timestamp
    cannot be read.

    None means "cannot establish", and every caller must treat it as UNKNOWN
    rather than as old. Returns an int (timedelta.days), not a float — the
    annotation on enrichment.days_since_last_upload() says float and is wrong
    about its own return type.

    A NEGATIVE result (an upload timestamped in the future, which YouTube does
    emit for scheduled premieres) is clamped to 0 rather than returned: a
    negative age would compare as "very recent" against any threshold, which is
    accidentally the right answer here, but only by luck. Clamping makes it
    deliberate.
    """
    parsed = parse_iso_utc(last_upload_at or "")
    if parsed is None:
        return None
    return max(0, (now - parsed).days)


def classify_activity(days: int | None, threshold_days: int) -> tuple[str, str]:
    """
    Map an age in days to (verdict, reason).

    `days is None` -> UNKNOWN, never INACTIVE. See the ACTIVE/INACTIVE/UNKNOWN
    comment above for why this asymmetry is the point of the module.
    """
    if days is None:
        return UNKNOWN, REASON_UNREADABLE_DATE
    if days > threshold_days:
        return INACTIVE, f"last upload {days}d ago, threshold {threshold_days}d"
    return ACTIVE, f"last upload {days}d ago"


# --- The 2-unit probe --------------------------------------------------------

@dataclass(frozen=True)
class ActivityProbe:
    """
    One channel's activity, plus what it cost and what it resolved to.

    `channel_title` is carried so the CALLER can compare it against the name
    stored on the legacy row. A 2024 @handle can be taken over by a different
    creator by 2026 — the repo has the recorded case (@Newrecordday2013 ->
    @newrecordday) — and resolving the wrong channel would measure the wrong
    activity, judge the wrong relevance, and email someone we never contacted.
    This module reports the title; it does not decide the mismatch.
    """
    handle: str
    ok: bool
    units_spent: int
    channel_id: str = ""
    channel_title: str = ""
    last_upload_at: str = ""
    reason: str = ""


def fetch_last_upload(handle: str, *, channel_id: str = "") -> ActivityProbe:
    """
    Newest upload timestamp for `handle`, for 2 free quota units (1 if
    `channel_id` is already known and its uploads playlist can be derived).

    Never raises for an inaccessible channel — returns ok=False with a reason,
    matching `get_channel_stats()`'s contract, because one dead channel out of
    9,991 must not end a multi-day sweep. A quota failure is indistinguishable
    from a 404 through that contract, which is why the CLI carries its own
    consecutive-failure breaker instead of inferring quota state from here.
    """
    spent = 0
    stats = get_channel_stats(channel_id=channel_id or None,
                              handle=None if channel_id else handle)
    if stats is None:
        # get_channel_stats bills only a call that returned data, so a miss
        # here cost 0 units.
        return ActivityProbe(handle, False, spent, reason=REASON_CHANNEL_GONE)
    spent += QUOTA_COST_CHANNELS_LIST

    playlist = stats.get("uploads_playlist_id")
    if not playlist:
        return ActivityProbe(
            handle, False, spent,
            channel_id=stats.get("channel_id", ""),
            channel_title=stats.get("channel_title", "") or "",
            reason=REASON_NO_UPLOADS,
        )

    # maxResults=1 is the entire optimisation: playlistItems returns
    # newest-first, so one item IS the last upload. Same 1-unit cost as 50, but
    # a smaller response and no temptation to compute averages from it.
    params = {"part": "contentDetails", "playlistId": playlist, "maxResults": 1}
    ident = stats.get("channel_id") or handle
    try:
        resp = HTTP.get(f"{YOUTUBE_API_BASE_URL}/playlistItems", params=params, timeout=30)
    except requests.RequestException as e:
        logger.warning("playlistItems.list request failed for %s: %s", ident, e)
        return ActivityProbe(handle, False, spent,
                             channel_id=stats.get("channel_id", ""),
                             channel_title=stats.get("channel_title", "") or "",
                             reason=REASON_API_ERROR)

    if resp.status_code != 200:
        logger.warning("playlistItems.list failed for %s: %s %s",
                       ident, resp.status_code, safe_body(resp))
        return ActivityProbe(handle, False, spent,
                             channel_id=stats.get("channel_id", ""),
                             channel_title=stats.get("channel_title", "") or "",
                             reason=REASON_API_ERROR)

    # Billed only for a call that returned data — same rule as get_channel_stats().
    record_spend(QUOTA_COST_PLAYLIST_ITEMS_LIST, call_name=f"playlistItems.list({ident}, newest)")
    spent += QUOTA_COST_PLAYLIST_ITEMS_LIST

    items = (resp.json() or {}).get("items", [])
    if not items:
        return ActivityProbe(handle, False, spent,
                             channel_id=stats.get("channel_id", ""),
                             channel_title=stats.get("channel_title", "") or "",
                             reason=REASON_NO_UPLOADS)

    newest = items[0].get("contentDetails", {}).get("videoPublishedAt", "")
    return ActivityProbe(
        handle, True, spent,
        channel_id=stats.get("channel_id", ""),
        channel_title=stats.get("channel_title", "") or "",
        last_upload_at=newest,
    )


# --- Dead-channel detection: the 1-unit variant ------------------------------
# CHOSEN 2026-08-26 over the 2-unit activity sweep, on measurement.
#
# A 700-handle sample of the legacy population found:
#
#     active                      592   84.6%
#     channel_gone                 56    8.0%   <- this
#     inactive (upload age)        26    3.7%
#     unresolvable                 23    3.3%   (mostly a false positive, see below)
#     api_error                     3    0.4%
#
# So the upload-age half of the sweep filters 3.7% for its second quota unit,
# while `channel_gone` filters 8.0% for the first one — and a deleted channel is
# a stronger fact than a dormant one. Under D1 nothing on this surface is ever
# emailed, so dormancy costs nothing to get wrong; a deleted channel is still
# worth knowing because it should be suppressed from DISCOVERY too.
#
# `channels.list?forHandle` alone answers it. Dropping `playlistItems.list`
# halves the per-channel cost to 1 free unit and the full pass from ~7.6 days to
# ~3.8 at the measured discovery reserve.
ALIVE = "alive"
GONE = "gone"

UNITS_DEAD_ONLY = QUOTA_COST_CHANNELS_LIST


def fold_title(name: str) -> str:
    """
    Fold a channel title for comparison: casefold and drop everything that is
    not alphanumeric.

    STRICTER folding than `external_dedupe._normalize_name`, which collapses
    whitespace and casefolds but keeps punctuation — and that was not enough.
    A first pass at title comparison flagged 23 of 700 handles as mismatched and
    MOST were cosmetic:

        "Han's Tech Talk"   vs "HansTechTalk"
        "2ToRamble"         vs "2 To Ramble"
        "BehindTheGlass"    vs "Behind The Glass"
        "Late Model Racecraft" vs "LATE MODEL RACECRAFT"

    All four are the same channel. Folding punctuation and spaces away makes
    them compare equal, which is the correct answer.
    """
    return "".join(c for c in (name or "").casefold() if c.isalnum())


def title_changed(stored: str, live: str) -> bool:
    """
    True when the live channel title is not merely a cosmetic variant of the
    stored one.

    IMPORTANT: this is ADVISORY, never an exclusion. A changed title cannot
    distinguish a REBRAND from a handle TAKEOVER, and the sample contains real
    rebrands:

        "With Love, Leena"  -> "Leena Snoubar"
        "DIY with KB"       -> "Kiva Brent"
        "Barry + Jordan"    -> "Brownstone Boys"
        "MyCrazyMakeup"     -> "Leticia Sánchez"

    A rebrand is the SAME creator and is not a reason to drop anyone. A takeover
    is a different creator and would be. From the title alone the two are
    indistinguishable, so an earlier version of this code that treated a
    mismatch as `unresolvable` was wrong: it excluded rebranded creators on no
    evidence. Only a human (or the channel's content) can tell them apart, and
    under D1 nothing here is emailed, so the honest handling is a flag for
    review rather than a verdict.
    """
    f_stored, f_live = fold_title(stored), fold_title(live)
    if not f_stored or not f_live:
        return False
    return f_stored != f_live


@dataclass(frozen=True)
class AliveProbe:
    """One channel's existence, for 1 free unit."""
    handle: str
    verdict: str          # ALIVE | GONE | UNKNOWN
    units_spent: int
    channel_id: str = ""
    channel_title: str = ""
    subscriber_count: int = 0
    title_flag: bool = False
    reason: str = ""


def fetch_channel_alive(handle: str, *, stored_name: str = "") -> AliveProbe:
    """
    Does this channel still exist? 1 free quota unit (`channels.list?forHandle`).

    `get_channel_stats()` returns None for a deleted, private or terminated
    channel AND for a 403 quotaExceeded AND for an auth failure — the contract
    cannot tell them apart, which is why the CLI carries a consecutive-failure
    breaker rather than inferring quota state from a single None. A miss costs 0
    units (that function bills only a call that returned data), so a quota wall
    burns nothing.
    """
    stats = get_channel_stats(handle=handle)
    if stats is None:
        return AliveProbe(handle, GONE, 0, reason=REASON_CHANNEL_GONE)
    live_title = stats.get("channel_title") or ""
    return AliveProbe(
        handle, ALIVE, QUOTA_COST_CHANNELS_LIST,
        channel_id=stats.get("channel_id", "") or "",
        channel_title=live_title,
        subscriber_count=stats.get("subscriber_count") or 0,
        title_flag=title_changed(stored_name, live_title),
        reason="channel resolves",
    )
