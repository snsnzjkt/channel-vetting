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

logger = logging.getLogger(__name__)

# data_path() returns a str; Path is what this module wants.
FOLLOWUP_POPULATION_CACHE = Path(_CACHE)

TABLE_MAIN = "tblFDvQiElfy7sER7"    # Home Theatre – YouTube Outreach   (11,666)
TABLE_FOLLOW = "tblWJm5pRazEtBVqb"  # Home Theatre – YouTube Follow-up  (1,054)

FIELDS = ["Channel Name", "Link", "Email", "Email 2", "Date",
          "Mail Sent", "Country", "Subscribers (Display)", "Sent By"]

# Refusal buckets reachable from free data alone. Ordered exactly as
# FOLLOWUP_PLAN.md's chain, because the order IS the safety property: a row that
# is both suppressed and inactive must read as suppressed.
B_DNC = "DNC Blocked"
B_NO_EMAIL = "No Email"
B_NO_PRIOR_SEND = "No Prior Send"
B_TOUCH_LIMIT = "Touch Limit Reached"
B_UNRESOLVABLE = "Unresolvable"
B_NOT_YET = "Not Yet Eligible"
B_SURVIVES = "needs paid signals"

FREE_BUCKETS = (B_DNC, B_NO_EMAIL, B_NO_PRIOR_SEND, B_TOUCH_LIMIT,
                B_UNRESOLVABLE, B_NOT_YET, B_SURVIVES)


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


def free_screen(main: list[LegacyRow], follow: list[LegacyRow], blocklist,
                *, now: datetime, floor_days: int
                ) -> tuple[dict[str, list[LegacyRow]], int]:
    """
    Apply every refusal that costs nothing. Returns (buckets, unjoinable_count).

    `blocklist` is a `do_not_contact.Blocklist`; it is matched on ALL THREE keys
    (handle, email, name). MEASURED 2026-08-25: of 560 suppressed legacy
    creators, 476 match by handle and only 84 by email — so an email-only check,
    which is all an Airtable automation can do, would miss 85% of them.
    """
    touch2, unjoinable = touch2_record_ids(main, follow)
    buckets: dict[str, list[LegacyRow]] = {b: [] for b in FREE_BUCKETS}

    for r in main:
        if blocklist.match(handle=r.handle, email=r.email, name=r.name):
            buckets[B_DNC].append(r)
        elif not r.email:
            buckets[B_NO_EMAIL].append(r)
        elif not r.mail_sent:
            # A Date with Mail Sent unticked is no evidence of a first touch,
            # and a follow-up to someone never contacted is undefined.
            buckets[B_NO_PRIOR_SEND].append(r)
        elif r.record_id in touch2:
            buckets[B_TOUCH_LIMIT].append(r)
        elif not r.handle:
            buckets[B_UNRESOLVABLE].append(r)
        else:
            d = parse_iso_utc(r.date)
            if d is None or (now - d).days < floor_days:
                # Unreadable date REFUSES, matching followup_eligibility()'s
                # "cannot prove enough time passed".
                buckets[B_NOT_YET].append(r)
            else:
                buckets[B_SURVIVES].append(r)

    total = sum(len(v) for v in buckets.values())
    if total != len(main):
        raise AssertionError(f"coverage bug: {total} bucketed != {len(main)} rows")
    return buckets, unjoinable
