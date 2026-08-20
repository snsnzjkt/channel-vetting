"""
Creators this niche's discovery query has already returned and REJECTED, kept
across runs so the vendor is not paid to surface them again.

**The waste this closes.** Discovery bills 0.01 per creator RETURNED, before any
gate sees them, and the endpoint sorts by relevancy — deterministically. So the
same creators arrive at the top of the same query every run, and any of them
that our gates reject is re-bought, re-resolved at a YouTube unit, and re-dropped,
forever. `exclude_handles` is the only lever that prevents it, and it had two
gaps: `seen_handles` covered only the CURRENT run and vanished with the process,
and `tracked_handles` covered only creators that became ROWS. A creator we
examined and rejected was in neither, so it was the one class of certain re-bill
with no defence.

Measured on a live Home Theater round (2026-08-20): of 40 creators examined, 7
were already tracked elsewhere in the base and 4 were on DO NOT CONTACT — 28% of
the page, paid for and discarded, and the same handles had been returned by the
previous run.

**Why it cannot simply be "exclude everything we know".** The vendor caps
`exclude_handles` at 10,000 elements — verified, not assumed: 12,000 returns
`{"exclude_handles": ["Ensure this field has no more than 10000 elements."]}` and
30,000 returns a 500. The base already holds 14,337 external handles, so 4,834 of
them do not fit today. The cap is a BUDGET, and this module exists to put the
highest-value handles in it: a creator this query has already returned is a
PROVEN re-bill, where an external-table handle is only a possible one.

**Scoped per niche**, because relevancy order is per query. A creator rejected by
Home Theater's query is not evidence about Lifestyle Sofa's, and mixing them
would spend one niche's exclusion budget on the other's misses.

**Only DURABLE rejections are recorded.** A creator dropped because the run ran
out of quota, or because the day's bucket was full, is a genuine prospect we
simply did not get to — writing those here would permanently blind the pipeline
to them, which is far more expensive than the credit it saves. See
main.TRANSIENT_DROP_REASONS.

**Failure direction is fail-OPEN, unlike credit_tracker.** An unreadable file
returns an empty set and the run proceeds with the old behaviour: a wider
exclusion gap, i.e. some wasted credits. Refusing to run over a corrupt cache
would trade a small money leak for zero rows, and this file is an optimisation,
never a safety gate — the DO NOT CONTACT blocklist and process_candidate's own
checkpoints remain the authoritative suppression, exactly as before.
"""
import json
import logging
import os
from datetime import date, timedelta

from config import REJECTED_HANDLES_FILE, REJECTED_HANDLES_RETENTION_DAYS
from prospect_day import today_iso
from quota_tracker import _replace_with_retry

logger = logging.getLogger(__name__)


def _empty() -> dict:
    return {"niches": {}}


def load() -> dict:
    """
    The ledger, or an empty one if it is missing or unreadable.

    Fail-open on purpose — see the module docstring. The corruption is logged
    once so it is fixable, rather than silently degrading forever.
    """
    if not os.path.exists(REJECTED_HANDLES_FILE):
        return _empty()
    try:
        with open(REJECTED_HANDLES_FILE, "r", encoding="utf-8") as f:
            log = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "Rejected-handle cache %s is unreadable (%s) — continuing without it. "
            "Already-rejected creators may be re-returned and re-billed at 0.01 "
            "each; delete the file to reset it cleanly.",
            REJECTED_HANDLES_FILE, exc,
        )
        return _empty()
    if not isinstance(log, dict) or not isinstance(log.get("niches"), dict):
        logger.warning(
            "Rejected-handle cache %s is not in the expected shape — ignoring it.",
            REJECTED_HANDLES_FILE,
        )
        return _empty()
    return log


def for_niche(niche_name: str) -> set[str]:
    """Every handle this niche has rejected inside the retention window."""
    entries = load()["niches"].get(niche_name, {})
    if not isinstance(entries, dict):
        return set()
    return {h for h, day in entries.items() if isinstance(h, str) and h}


def _prune(entries: dict) -> dict:
    """
    Drop entries older than REJECTED_HANDLES_RETENTION_DAYS.

    The window is what keeps this from being a permanent blacklist. A channel
    that failed the view floor in March may clear it in September — growth is
    the whole reason a creator becomes a prospect — so a rejection has to
    expire and let the pipeline look again. Unparseable dates are KEPT (matching
    quota_tracker._prune): silently deleting a key we don't understand is worse
    than carrying it in a file that is meant to be hand-inspectable.
    """
    try:
        cutoff = date.fromisoformat(today_iso()) - timedelta(days=REJECTED_HANDLES_RETENTION_DAYS)
    except ValueError:
        return entries
    kept = {}
    for handle, day in entries.items():
        try:
            if date.fromisoformat(day) >= cutoff:
                kept[handle] = day
        except (ValueError, TypeError):
            kept[handle] = day
    return kept


def record(niche_name: str, handles) -> int:
    """
    Add `handles` to `niche_name`'s rejection set. Returns how many are now held.

    Re-stamps a handle that is already present, so a creator the query keeps
    returning stays excluded rather than aging out and being re-bought on a
    fixed schedule. Best-effort: a failed write is logged and ignored, because
    losing it only costs the credits this module exists to save.
    """
    fresh = {(h or "").strip().lstrip("@").lower() for h in handles}
    fresh.discard("")
    if not fresh:
        return len(for_niche(niche_name))

    day = today_iso()
    log = load()
    entries = log["niches"].setdefault(niche_name, {})
    if not isinstance(entries, dict):
        entries = {}
    for handle in fresh:
        entries[handle] = day
    log["niches"][niche_name] = _prune(entries)

    tmp_path = f"{REJECTED_HANDLES_FILE}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(log, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(tmp_path, REJECTED_HANDLES_FILE)
    except BaseException:
        # BaseException so a KeyboardInterrupt doesn't strand a .tmp for the
        # next run to trip on — same discipline as quota_tracker._save_log.
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        logger.warning(
            "Could not persist %d rejected handle(s) for '%s' — they may be "
            "re-returned and re-billed next run.", len(fresh), niche_name,
        )
        return len(log["niches"].get(niche_name, {}))

    held = len(log["niches"][niche_name])
    logger.info(
        "Rejected-handle cache: +%d for '%s' (%d held, %d-day window) — the "
        "vendor will not be paid to return these again.",
        len(fresh), niche_name, held, REJECTED_HANDLES_RETENTION_DAYS,
    )
    return held
