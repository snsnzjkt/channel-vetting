"""
The legacy outreach population, and the FREE screens that shrink it.

This is FOLLOWUP_PLAN.md W4 + W6. It reads the two hand-maintained tables the
2023/2024 campaign lived in and reduces them to the rows that still need a paid
signal, using only data already in Airtable — no YouTube quota, no model, no
money.

WHY A LOCAL CACHE
-----------------
Paginating 11,666 + 1,054 rows at the repo's 0.5s inter-page pacing takes ~2
minutes and 128 Airtable requests against a 5 req/s per-base limit shared with
human editors and ten live automations. `external_dedupe` already caches its own
index over the same tables for exactly this reason. So the read is cached and a
re-run of the sweep is free.

WHY KEY ON THE HANDLE, NOT THE ROW
----------------------------------
MEASURED 2026-08-25: 11,666 rows carry only 11,429 distinct handles — 174
handles appear more than once. Two rows for one creator, categorized
independently, can disagree, and a per-row count is not a per-creator count. The
verdict is therefore per handle and every summary reports both figures.
"""
import json
import logging
from pathlib import Path
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

from channel_vetting.airtable.client import get_records
from channel_vetting.config import FOLLOWUP_POPULATION_CACHE as _CACHE
from channel_vetting.core.iso_time import parse_iso_utc
from channel_vetting.enrichment.channels import normalize_handle
from channel_vetting.enrichment.external_dedupe import _normalize_name
from channel_vetting.followup import categorizer as C
from channel_vetting.outreach import ledger as L

logger = logging.getLogger(__name__)

# data_path() returns a str; Path is what this module wants.
FOLLOWUP_POPULATION_CACHE = Path(_CACHE)

TABLE_MAIN = "tblFDvQiElfy7sER7"    # Home Theatre – YouTube Outreach   (11,666)
TABLE_FOLLOW = "tblWJm5pRazEtBVqb"  # Home Theatre – YouTube Follow-up  (1,054)

FIELDS = ["Channel Name", "Link", "Email", "Email 2", "Date",
          "Mail Sent", "Country", "Subscribers (Display)", "Sent By"]


# The category vocabulary lives in categorizer.CATEGORIES — one definition,
# read by the writer and the page builder alike.


@dataclass(frozen=True)
class LegacyRow:
    record_id: str
    handle: str          # "" when the Link is not a /@handle URL
    name: str
    email: str
    date: str
    mail_sent: bool
    country: str
    subscribers: str


def _row(r: dict) -> LegacyRow:
    f = r.get("fields", {})
    link = (f.get("Link") or "").strip()
    return LegacyRow(
        record_id=r["id"],
        handle=normalize_handle(link) or "",
        name=(f.get("Channel Name") or "").strip(),
        email=(f.get("Email") or "").strip(),
        date=(f.get("Date") or "").strip(),
        mail_sent=bool(f.get("Mail Sent")),
        country=(f.get("Country") or "").strip(),
        subscribers=(f.get("Subscribers (Display)") or "").strip(),
    )


def load_population(*, refresh: bool = False) -> tuple[list[LegacyRow], list[LegacyRow]]:
    """
    (main_rows, followup_rows). Cached; pass refresh=True to re-read Airtable.

    `get_records` raises AirtableReadError rather than returning a partial list,
    which is the behaviour this depends on: a short read would look like a
    smaller population and silently leave rows uncategorized.
    """
    if not refresh and FOLLOWUP_POPULATION_CACHE.exists():
        raw = json.loads(FOLLOWUP_POPULATION_CACHE.read_text())
        logger.info("Population cache hit: %d main, %d follow-up rows (cached %s)",
                    len(raw["main"]), len(raw["follow"]), raw.get("cached_at", "?"))
        return ([LegacyRow(**d) for d in raw["main"]],
                [LegacyRow(**d) for d in raw["follow"]])

    main = [_row(r) for r in get_records(TABLE_MAIN, FIELDS)]
    follow = [_row(r) for r in get_records(TABLE_FOLLOW, FIELDS)]
    FOLLOWUP_POPULATION_CACHE.write_text(json.dumps({
        "cached_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "main": [asdict(r) for r in main],
        "follow": [asdict(r) for r in follow],
    }))
    logger.info("Population cached: %d main, %d follow-up rows", len(main), len(follow))
    return main, follow


def touch2_record_ids(main: list[LegacyRow], follow: list[LegacyRow]) -> tuple[set[str], int]:
    """
    Record ids in `main` that also appear in the follow-up table, plus the count
    of follow-up rows that could NOT be joined.

    A join MISS reads touch 1 instead of 2, and a wrong touch count is a third
    cold email. MEASURED 2026-08-25: 1,023 join on handle, 22 on the name
    fallback, 9 (0.85%) do not join. The caller must treat those 9 as
    touch-limited anyway — fail closed, never permissive.
    """
    by_handle: dict[str, list[LegacyRow]] = {}
    by_name: dict[str, list[LegacyRow]] = {}
    for r in main:
        if r.handle:
            by_handle.setdefault(r.handle, []).append(r)
        n = _normalize_name(r.name)
        if n:
            by_name.setdefault(n, []).append(r)

    matched, misses = set(), 0
    for r in follow:
        if r.handle and r.handle in by_handle:
            matched.update(x.record_id for x in by_handle[r.handle])
            continue
        n = _normalize_name(r.name)
        if n and n in by_name:
            matched.update(x.record_id for x in by_name[n])
            continue
        misses += 1
    return matched, misses


def needs_paid_signal(categories: dict[str, tuple[str, str]]) -> list[str]:
    """
    Handles still awaiting a paid signal, from an existing categorisation.

    This USED to be `free_screen()`, which computed its own buckets — the date
    floor, the touch count, the suppression check — and that was a second
    implementation of "may this creator be re-contacted" sitting beside
    `followup_eligibility()`. `ledger.py:136-143` describes where that leads.
    Selecting who to probe is a different question from deciding a category, so
    this now reads the categorisation rather than recomputing it.

    Anything already refused for a free reason (suppressed, no address,
    unkeyable, dead, touch-limited, too recent) needs no paid call. Only the
    Unknown buckets do.
    """
    return [h for h, (cat, _) in categories.items() if cat in C.UNDECIDED]


# --- Letting the REAL eligibility function see a legacy row ------------------

class LegacyLedgerStore:
    """
    A read-only `ledger.LedgerStore` over the legacy tables, so
    `followup_eligibility()` can judge a 2023 hand-sent creator without the
    Outreach Log knowing anything about them.

    WHY SYNTHESISE INSTEAD OF BACKFILL. Writing 11,628 rows into the real
    Outreach Log was considered and rejected: `Last Contacted At` and
    `Last Send State` are rollups on the PROSPECTS tables, reached through the
    ledger's record-link field, and `external_dedupe` deliberately stops legacy
    creators becoming Prospects rows. Materialising them to make the rollups fire
    would pollute the review surface and every funnel metric.

    So the legacy tables ARE the ledger for this population, and one synthetic
    `Sent` row is emitted per recorded touch:

      * the main table's `Date`, when `Mail Sent` is ticked  -> touch 1
      * a matching row in the follow-up table                -> touch 2

    That makes `OUTREACH_MAX_TOUCHES` bind on evidence rather than on a counter,
    which is the whole point of `followup_eligibility()` precondition 6.

    Only `find_sent_for_channel` is implemented. The write methods raise: this
    store must never be handed to `claim()`, which would try to create a claim
    row in a table that is not the ledger.
    """

    def __init__(self, main: list[LegacyRow], follow: list[LegacyRow]):
        touch2, self.unjoinable = touch2_record_ids(main, follow)
        self._by_handle: dict[str, list[dict]] = {}
        for r in main:
            if not r.handle:
                continue
            rows = []
            if r.mail_sent:
                rows.append({"fields": {"Settled At": r.date, "Campaign": "legacy-2023"}})
                if r.record_id in touch2:
                    rows.append({"fields": {"Settled At": r.date, "Campaign": "legacy-followup-2024"}})
            self._by_handle.setdefault(r.handle, []).extend(rows)

    def find_sent_for_channel(self, channel_id: str) -> list[dict]:
        return self._by_handle.get(channel_id, [])

    def _refuse(self, *a, **k):
        raise NotImplementedError(
            "LegacyLedgerStore is read-only. It must not be passed to claim() — "
            "the legacy tables are not the Outreach Log."
        )

    find_by_key = create_claim = patch = count_claimed_on = find_stranded = _refuse


# The legacy population has no `Qualification` column, so something has to be
# passed for precondition 2. QUALIFIED is passed DELIBERATELY: gating the triage
# on a pipeline verdict these rows never received would dump all 11,628 into
# `No Longer Relevant` and destroy the review list. Relevance is answered by the
# relevance SIGNAL instead, which has its own bucket and its own unknown state.
LEGACY_QUALIFICATION = L.SENDABLE_QUALIFICATION

# No `Reply State` column either. A BLANK is not passed, and the reason is an
# ordering fact rather than a preference: `followup_eligibility()` checks reply
# state at rule 4, before the touch ceiling and the age floor, so a blank
# short-circuits and every row returns REASON_REPLY_STATE_UNKNOWN — measured,
# and it collapsed the 990 touch-limited rows and every date bucket into one.
#
# So REPLY_NONE is passed to get the age and touch verdict the ledger CAN
# establish, and `Signals.reply_known=False` carries the missing-reply fact into
# the categoriser, which refuses on it AFTER those two. Same refusal, correct
# position. The SEND path still passes the real field, so a blank there refuses
# at rule 4 as designed.
LEGACY_REPLY_STATE = L.REPLY_NONE
LEGACY_REPLY_KNOWN = False


def categorize_population(main: list[LegacyRow], follow: list[LegacyRow], blocklist,
                          *, now, floor_days: int, max_touches: int,
                          activity: dict | None = None,
                          relevance: dict | None = None,
                          previous: dict | None = None,
                          ) -> dict[str, tuple[str, str]]:
    """
    handle -> (category, reason) for every distinct handle in the population.

    Keyed on HANDLE, not row: 11,666 rows carry only 11,429 distinct handles
    (174 duplicated, measured 2026-08-25), and two rows for one creator that were
    categorised independently could disagree. Rows with no handle at all are
    keyed by record id so they still get a bucket rather than vanishing.

    `activity` and `relevance` are handle -> value lookups; a MISSING key means
    not-yet-established and routes to the matching Unknown bucket. That is the
    difference between "we checked and it is fine" and "we have not checked",
    and collapsing them is what would let an unfinished sweep mark a row sendable.
    """
    activity = activity or {}
    relevance = relevance or {}
    previous = previous or {}
    store = LegacyLedgerStore(main, follow)

    out: dict[str, tuple[str, str]] = {}
    for r in main:
        key = r.handle or f"rec:{r.record_id}"
        if key in out:
            continue                    # one verdict per handle
        verdict = L.followup_eligibility(
            store,
            channel_id=r.handle,
            qualification=LEGACY_QUALIFICATION,
            reply_state=LEGACY_REPLY_STATE,
            followup_requested=True,    # triage asks; the SEND path applies the human gate
            campaign_prefix="legacy",
            min_days_since_send=floor_days,
            max_touches=max_touches,
            clock=lambda: now,
        )
        sig = C.Signals(
            dnc_hit=blocklist.match(handle=r.handle, email=r.email, name=r.name),
            has_email=bool(r.email),
            handle=r.handle,
            reply_known=LEGACY_REPLY_KNOWN,
            channel_alive=activity.get(r.handle),
            relevant=relevance.get(r.handle),
        )
        out[key] = C.categorize(verdict, sig, previous=previous.get(key, ""))
    return out
