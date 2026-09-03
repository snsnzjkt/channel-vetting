"""
Tracks influencers.club CREDIT spend in a local JSON log that survives the run.

**Why this exists.** Credits are the only spend in this pipeline that is real
money, and until now they were the only spend with NO ledger. Free YouTube quota
has `quota_log.json`, atomic writes, a ceiling and a gate in front of every
expensive call. Credits had a counter on the `InfluencerDiscovery` INSTANCE, so
it described one run and vanished with the process: two runs on the same day each
got a full allowance, nothing could report a monthly total, and the fair-use cap
(which resets only at subscription renewal, and which the vendor signals with a
bodyless 429 that no retry clears) could be walked into blind.

**Three limits, in order of authority.**

1. `vendor_credits_left` — the vendor's OWN reported balance, which arrives free
   on every discovery response as `credits_left`. Authoritative, so it wins.
   Persisted here because the email step never sees a discovery response.
2. `INFLUENCERS_MAX_CREDITS_PER_MONTH` — our brake in front of the fair-use cap.
   A guess: the API exposes neither the plan's allowance nor its renewal date, so
   this is derived from measured usage. Only load-bearing while (1) is unknown.
3. `INFLUENCERS_MAX_CREDITS_PER_DAY` — paces a single day and stops a second run
   quietly doubling the spend.

**And a fourth limit on a DIFFERENT meter entirely.**
`INFLUENCERS_MAX_DISCOVERY_HANDLES_PER_PERIOD` counts HANDLES — creators the
discovery endpoint returned — over a rolling `INFLUENCERS_HANDLE_PERIOD_DAYS`
window. It is not a credit figure and does not convert to one: email enrichment
spends credits and zero handles, so no credit ceiling can bound it. Added
2026-09-02 after the vendor mailed to say we had used 5,042 of 5,000 handles in
a period during which every credit ceiling above was reading comfortably green.

The window is ROLLING because the fair-use period resets at subscription
renewal, a date the API does not expose — see the config comment. A trailing
window at least as long as the longest possible billing period bounds every
billing period inside it without needing to know where the boundary falls.

**The clock.** Keyed on `prospect_day.today_iso()` — NOT a new fourth clock, and
not the Pacific quota day (that tracks Google's reset and has nothing to do with
a vendor invoice). Credits buy rows stamped with a prospect day and counted
against a prospect-day cap, so a day's spend and a day's row count must be
readable side by side; credits-per-row is the ratio that caught the last two
leaks. The month key is derived from the same value and is OUR accounting month.

**Failure direction is the opposite of quota_tracker's, on purpose.** An
unreadable `quota_log.json` returns `{}` and reads as "0 spent" — fail-open, and
fine for free quota that resets nightly. Here the same corruption would authorise
a fresh budget against money already spent, so a ledger that cannot be read
means "do not spend": `assert_readable()` raises at run start (beside
`fetch_blocklist()`, the established once-per-run fail-closed gate), and
mid-run `can_afford()` returns False rather than guessing. A MISSING file is not
corruption — that is a first run.
"""
import json
import logging
import os
from datetime import date, timedelta

from channel_vetting.config import (
    CREDIT_LOG_FILE,
    INFLUENCERS_HANDLE_PERIOD_DAYS,
    INFLUENCERS_HANDLE_PERIOD_START,
    INFLUENCERS_MAX_CREDITS_PER_DAY,
    INFLUENCERS_MAX_CREDITS_PER_MONTH,
    INFLUENCERS_MAX_DISCOVERY_HANDLES_PER_PERIOD,
)
from channel_vetting.core.prospect_day import today_iso
from channel_vetting.budget.quota_tracker import _replace_with_retry

logger = logging.getLogger(__name__)

# Credit kinds, so the log answers "what did we buy?" and not merely "how much?".
# Discovery bills 0.01 per creator RETURNED (before any gate sees them) and email
# bills 0.2 per validated address; a month that drifts is almost always drifting
# on one of the two, and an undifferentiated total cannot say which.
KIND_DISCOVERY = "discovery"
KIND_EMAIL = "email"
# The Mythumi social path's spend, bucketed separately from the YouTube run's.
#
# WHY A THIRD KIND RATHER THAN A SECOND LEDGER. There is ONE subscription, one
# real balance and one fair-use handle meter, so two ledgers would not create
# credits — each would simply believe it had a full allowance and the pair would
# spend double against a single pool, with nothing left to stop them. That fails
# OPEN, silently, which is the one direction this module refuses to fail.
#
# Bucketing by kind inside ONE ledger gives the thing a second ledger was wanted
# for — a daily slice each business can rely on, so neither starves the other —
# while keeping a single real backstop. See credits_today_for_kind().
KIND_SOCIAL = "social"

# Daily detail is pruned to keep the file small; MONTHLY totals never are, since
# they are the only long-run record of money spent and cost ~30 bytes a month.
DAILY_RETENTION_DAYS = 62


class CreditLedgerUnavailable(RuntimeError):
    """
    The credit ledger could not be read or written.

    Raised only by `assert_readable()`, at run start. Mid-run the public helpers
    return False instead, so one corrupt read stops paid calls without unwinding
    a run that has already spent YouTube quota.
    """


def _month_of(day: str) -> str:
    """The YYYY-MM key a YYYY-MM-DD day belongs to."""
    return day[:7]


def _empty() -> dict:
    return {"days": {}, "months": {}, "vendor_credits_left": None}


def load_log() -> dict:
    """
    The ledger, or raise CreditLedgerUnavailable if it exists but is unreadable.

    A missing file returns an empty ledger — that is a first run, and a truthful
    "nothing spent yet". A file that exists and does not parse is a different
    thing, and guessing zero there is what overspends.
    """
    if not os.path.exists(CREDIT_LOG_FILE):
        return _empty()
    try:
        with open(CREDIT_LOG_FILE, "r", encoding="utf-8") as f:
            log = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise CreditLedgerUnavailable(
            f"Credit ledger {CREDIT_LOG_FILE} is unreadable ({exc}). Refusing to "
            "spend credits against an unknown balance — inspect or delete the file."
        ) from exc
    if not isinstance(log, dict):
        raise CreditLedgerUnavailable(
            f"Credit ledger {CREDIT_LOG_FILE} is not a JSON object."
        )
    for key, default in _empty().items():
        log.setdefault(key, default)
    return log


def assert_readable() -> None:
    """
    Fail the run NOW if the ledger is corrupt.

    Called once from `run()` beside `fetch_blocklist()`. Without it the first
    corrupt read happens deep inside a paid call site, which disables only its
    own half and lets a scheduled run finish green having produced no
    influencers-sourced rows — the outcome `any_cap_check_completed` already
    refuses to report as success.
    """
    load_log()


def _totals(log: dict) -> tuple[float, float]:
    """(today, this month) from ONE already-loaded ledger — so the two figures
    can never come from two different reads of the same file."""
    day = today_iso()
    return (
        float(log["days"].get(day, {}).get("total", 0.0)),
        float(log["months"].get(_month_of(day), 0.0)),
    )


def _prune(log: dict) -> dict:
    """
    Drop daily detail older than DAILY_RETENTION_DAYS. Monthly totals are never
    touched.

    Unparseable keys are KEPT, matching quota_tracker._prune: this file is
    hand-inspectable and silently deleting a key we don't understand is worse
    than carrying it.
    """
    try:
        cutoff = date.fromisoformat(today_iso()) - timedelta(days=DAILY_RETENTION_DAYS)
    except ValueError:
        return log
    kept = {}
    for day, entry in log.get("days", {}).items():
        try:
            if date.fromisoformat(day) >= cutoff:
                kept[day] = entry
        except (ValueError, TypeError):
            kept[day] = entry
    log["days"] = kept
    return log


def _save_log(log: dict) -> None:
    """
    Persist atomically: `.tmp` sibling, fsync, then os.replace().

    Same discipline as quota_tracker._save_log, and reusing its
    `_replace_with_retry` so the Windows file-lock fix (antivirus or a search
    indexer holding the path, WinError 5) has one implementation — that bug
    ended three real runs, and a second copy would be a second chance to get it
    wrong. The fsync is load-bearing: os.replace orders the rename, not the data.
    """
    log = _prune(log)
    # UNIQUE PER PROCESS. This was f"{CREDIT_LOG_FILE}.tmp" — one shared name —
    # and two concurrent runs then raced: both wrote the same tmp file, the
    # first os.replace moved it away, and the second raised
    # FileNotFoundError. _replace_with_retry only retries PermissionError,
    # so the loser CRASHED MID-RUN. Observed 2026-08-22 when a Home Theater
    # sweep and a Lifestyle run overlapped: the Lifestyle run died in
    # record_spend after examining 9 candidates and wrote no rows.
    #
    # os.getpid() is enough: the collision is between PROCESSES, and a
    # single process serialises its own writes.
    tmp_path = f"{CREDIT_LOG_FILE}.{os.getpid()}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(log, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(tmp_path, CREDIT_LOG_FILE)
    except BaseException:
        # BaseException so a KeyboardInterrupt — the interrupt this function
        # defends against — doesn't strand a .tmp for the next run to trip on.
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def credits_today() -> float:
    """Credits spent so far today (prospect day), all kinds. 0.0 if unreadable."""
    try:
        return _totals(load_log())[0]
    except CreditLedgerUnavailable:
        return 0.0


def credits_today_for_kind(kind: str) -> float:
    """
    Today's spend under ONE kind, for per-business daily reservations.

    Reads the same `by_kind` bucket record_spend already maintains, so this adds
    no new state to keep in sync. Returns 0.0 when the ledger cannot be read —
    the CALLER must still consult can_afford(), which fails closed; this figure
    only decides how much of a shared day one business may claim.
    """
    try:
        log = load_log()
    except Exception:
        return 0.0
    day = log.get("days", {}).get(today_iso()) or {}
    return float((day.get("by_kind") or {}).get(kind, 0.0) or 0.0)


def credits_this_month() -> float:
    """Credits spent so far this accounting month. 0.0 if unreadable."""
    try:
        return _totals(load_log())[1]
    except CreditLedgerUnavailable:
        return 0.0


def handles_this_period() -> int:
    """
    Discovery handles bought in the trailing INFLUENCERS_HANDLE_PERIOD_DAYS.

    0 if the ledger is unreadable — the same shape as `credits_today()`. This is
    the REPORTING helper and it fails open on purpose; the SPENDING gate is
    `can_afford_handles()`, which fails closed. Keeping the two apart means a
    corrupt ledger cannot both hide a total from the run summary and authorise
    more buying.
    """
    try:
        return _handles_in_window(load_log())
    except CreditLedgerUnavailable:
        return 0


def _handle_window_start():
    """
    The first day that counts toward the handle allowance, or None to count all
    retained days.

    Two modes, and the fallback direction matters. With
    INFLUENCERS_HANDLE_PERIOD_START set to the real renewal date we count from
    it exactly. With it empty — or unparseable, or dated in the future, both of
    which are typos rather than instructions — we fall back to the trailing
    window, which over-counts rather than under-counts. A typo in a date should
    never widen a cap.
    """
    try:
        today = date.fromisoformat(today_iso())
    except ValueError:
        return None

    if INFLUENCERS_HANDLE_PERIOD_START:
        try:
            anchored = date.fromisoformat(INFLUENCERS_HANDLE_PERIOD_START.strip())
        except ValueError:
            logger.warning(
                "INFLUENCERS_HANDLE_PERIOD_START=%r is not a YYYY-MM-DD date — "
                "falling back to the %d-day rolling window.",
                INFLUENCERS_HANDLE_PERIOD_START, INFLUENCERS_HANDLE_PERIOD_DAYS,
            )
        else:
            if anchored <= today:
                return anchored
            logger.warning(
                "INFLUENCERS_HANDLE_PERIOD_START=%s is in the future — falling "
                "back to the %d-day rolling window.",
                INFLUENCERS_HANDLE_PERIOD_START, INFLUENCERS_HANDLE_PERIOD_DAYS,
            )

    return today - timedelta(days=INFLUENCERS_HANDLE_PERIOD_DAYS - 1)


def _handles_in_window(log: dict) -> int:
    """
    Sum `handles` across every retained day inside the trailing window.

    Days are summed rather than a running counter being kept, because a counter
    would need to know when the window rolls and a sum does not. Daily detail is
    pruned at DAILY_RETENTION_DAYS (62), which must stay comfortably above
    INFLUENCERS_HANDLE_PERIOD_DAYS (31) or this silently starts undercounting —
    the one coupling between those two numbers.

    An unparseable day key is COUNTED, not skipped: `_prune` deliberately keeps
    keys it does not understand, and the safe reading of "I cannot tell when this
    spend happened" is that it might be inside the window.
    """
    cutoff = _handle_window_start()

    total = 0
    for day, entry in log.get("days", {}).items():
        if cutoff is not None:
            try:
                if date.fromisoformat(day) < cutoff:
                    continue
            except (ValueError, TypeError):
                pass  # undated spend counts against us, see docstring
        try:
            total += int(entry.get("handles", 0) or 0)
        except (TypeError, ValueError):
            continue
    return total


def can_afford_handles(handles: int, what: str = "call") -> bool:
    """
    Whether buying `handles` MORE discovery handles stays inside the allowance.

    Projects the cost exactly as `can_afford` does, and for the same reason: the
    caller must pass what the next call could COST, not ask whether the current
    total is fine. A discovery page returns up to PAGE_LIMIT (50) creators, so a
    balance-only check could overshoot by 50 handles per page.

    Fails CLOSED on an unreadable ledger. That is the opposite of
    `handles_this_period()` and matches `can_afford`: not knowing the balance
    means not spending.
    """
    if handles <= 0:
        return True

    try:
        log = load_log()
    except CreditLedgerUnavailable as exc:
        logger.error("Skipping %s: credit ledger unreadable (%s).", what, exc)
        return False

    used = _handles_in_window(log)
    if used + handles > INFLUENCERS_MAX_DISCOVERY_HANDLES_PER_PERIOD:
        logger.warning(
            "Skipping %s: projected %d discovery handles in the last %d days "
            "would exceed INFLUENCERS_MAX_DISCOVERY_HANDLES_PER_PERIOD %d (%d "
            "already bought). This is the vendor's OWN fair-use meter, not a "
            "credit ceiling — going past it is what triggered the 2026-09-01 "
            "over-limit email. Discovery stops here; the free YouTube keyword "
            "loop still runs for any niche configured discovery_source=both.",
            what, used + handles, INFLUENCERS_HANDLE_PERIOD_DAYS,
            INFLUENCERS_MAX_DISCOVERY_HANDLES_PER_PERIOD, used,
        )
        return False
    return True


def record_spend(
    credits: float, *, kind: str, detail: str = "", handles: int = 0
) -> bool:
    """
    Add `credits` to today's and this month's totals. True if persisted.

    `handles` is the count of creators the DISCOVERY endpoint returned for this
    same charge — the vendor's fair-use meter, which does not derive from the
    credit figure and so has to be passed in alongside it. Email enrichment
    passes nothing and correctly contributes 0.

    Call at the point the VENDOR bills, not where we decide to keep the result —
    the rule `enrichment.email_influencers._record_billable` already follows. Rejecting an address
    on our own policy does not refund it.

    Returns False if the ledger could not be read or written, so the caller can
    stop paying rather than continue against a total the ledger no longer
    reflects. A zero or negative cost is a no-op returning True: an empty
    `must_have` result genuinely costs nothing, and callers should not have to
    branch on that to stay honest.
    """
    # Both meters must be zero to skip. Guarding on `credits <= 0` alone would
    # drop the handle count of any page the vendor returned but did not charge
    # for, and an uncounted handle is exactly the leak this meter exists to stop.
    if credits <= 0 and handles <= 0:
        return True

    day = today_iso()
    month = _month_of(day)
    try:
        log = load_log()
        entry = log["days"].setdefault(day, {"total": 0.0, "by_kind": {}})
        if credits > 0:
            entry["total"] = round(float(entry.get("total", 0.0)) + credits, 4)
            by_kind = entry.setdefault("by_kind", {})
            by_kind[kind] = round(float(by_kind.get(kind, 0.0)) + credits, 4)
            log["months"][month] = round(
                float(log["months"].get(month, 0.0)) + credits, 4
            )
        if handles > 0:
            entry["handles"] = int(entry.get("handles", 0) or 0) + int(handles)
        _save_log(log)
    except (CreditLedgerUnavailable, OSError) as exc:
        logger.error(
            "Could not record %.3f %s credits (%s) — the money is spent either "
            "way; the caller should stop spending more of it blind. Check the "
            "vendor dashboard for the true total.",
            credits, kind, exc,
        )
        return False

    logger.info(
        "Credit spend: +%.3f (%s%s) -> %.3f today, %.3f this month%s",
        credits, kind, f", {detail}" if detail else "",
        entry["total"], log["months"].get(month, 0.0),
        (
            f"; +{handles} handles -> {_handles_in_window(log)} in the last "
            f"{INFLUENCERS_HANDLE_PERIOD_DAYS}d of "
            f"{INFLUENCERS_MAX_DISCOVERY_HANDLES_PER_PERIOD}"
            if handles > 0 else ""
        ),
    )
    return True


def record_vendor_balance(credits_left: float | None) -> None:
    """
    Persist the vendor's OWN remaining-balance figure.

    `credits_left` arrives free on every discovery response and used to be read
    only into a log line. It is the authoritative number — our monthly ceiling is
    a guess derived from measured usage, while this is the entitlement — so
    storing it lets `can_afford` gate on truth, and lets the EMAIL step (which
    never sees a discovery response) benefit from it too.

    Best-effort: a failed write is logged and ignored, because losing this
    only falls back to the local ceilings, which is the previous behaviour.
    """
    if credits_left is None:
        return
    try:
        log = load_log()
        log["vendor_credits_left"] = float(credits_left)
        _save_log(log)
    except (CreditLedgerUnavailable, OSError, TypeError, ValueError) as exc:
        logger.warning("Could not persist the vendor credit balance: %s", exc)


def can_afford(cost: float, what: str = "call") -> bool:
    """
    Whether spending `cost` MORE credits stays inside every limit.

    **Takes the cost and checks the PROJECTED total.** The per-run check this
    replaces was `if spent >= max: break`, which tested the balance without the
    price: at 5.9 of a 6.0 ceiling it authorised another page, and a discovery
    page bills up to 0.5 (50 creators x 0.01), so every ceiling could be
    overshot by a page, per niche. Projecting the cost is what makes a ceiling a
    ceiling — and it is why callers must ask this rather than compare totals
    themselves.

    Returns False rather than raising — including when the ledger is unreadable,
    which means "we do not know the balance, so do not spend". Callers degrade to
    "no more paid calls" and keep what they already gathered.
    """
    if cost <= 0:
        return True

    try:
        log = load_log()
    except CreditLedgerUnavailable as exc:
        logger.error("Skipping %s: credit ledger unreadable (%s).", what, exc)
        return False

    today, month = _totals(log)

    # The vendor's own balance outranks both of our ceilings: ours are calibrated
    # from measured usage, this is the entitlement.
    vendor_left = log.get("vendor_credits_left")
    if isinstance(vendor_left, (int, float)) and not isinstance(vendor_left, bool):
        if vendor_left < cost:
            logger.warning(
                "Skipping %s: the vendor reports only %.3f credits left, and this "
                "would cost %.3f. This is the account's real balance, not our "
                "estimate.", what, vendor_left, cost,
            )
            return False

    if today + cost > INFLUENCERS_MAX_CREDITS_PER_DAY:
        logger.warning(
            "Skipping %s: projected %.3f credits today would exceed "
            "INFLUENCERS_MAX_CREDITS_PER_DAY %.2f (%.3f already spent).",
            what, today + cost, INFLUENCERS_MAX_CREDITS_PER_DAY, today,
        )
        return False

    if month + cost > INFLUENCERS_MAX_CREDITS_PER_MONTH:
        logger.warning(
            "Skipping %s: projected %.3f credits this month would exceed "
            "INFLUENCERS_MAX_CREDITS_PER_MONTH %.2f (%.3f already spent). This is "
            "the brake in front of the vendor's fair-use cap.",
            what, month + cost, INFLUENCERS_MAX_CREDITS_PER_MONTH, month,
        )
        return False

    return True


def spend_summary() -> str:
    """
    One line for the run summary: today's spend by kind, against both ceilings,
    plus the vendor's own balance when known.

    A credit figure alone has twice failed to look wrong — CLAUDE.md records that
    "16 credits looked unremarkable until it sat next to 1 qualified row". The
    split and the headroom are what make a drift legible.
    """
    try:
        log = load_log()
    except CreditLedgerUnavailable as exc:
        return f"LEDGER UNAVAILABLE ({exc})"
    today, month = _totals(log)
    by_kind = log["days"].get(today_iso(), {}).get("by_kind", {})
    split = ", ".join(f"{k} {v:.2f}" for k, v in sorted(by_kind.items())) or "none"
    vendor = log.get("vendor_credits_left")
    vendor_note = (
        f"; vendor reports {float(vendor):.2f} left"
        if isinstance(vendor, (int, float)) and not isinstance(vendor, bool)
        else ""
    )
    # Handles ride in the same line as the credits rather than a line of their
    # own, because the failure this summary exists to make visible is a run
    # where one meter looks fine and the other does not — which is precisely
    # what happened on 2026-09-01, when every credit figure was green.
    handles = _handles_in_window(log)
    return (
        f"today {today:.2f}/{INFLUENCERS_MAX_CREDITS_PER_DAY:.2f} ({split}); "
        f"month {month:.2f}/{INFLUENCERS_MAX_CREDITS_PER_MONTH:.2f}; "
        f"handles {handles}/{INFLUENCERS_MAX_DISCOVERY_HANDLES_PER_PERIOD} "
        f"in {INFLUENCERS_HANDLE_PERIOD_DAYS}d{vendor_note}"
    )
