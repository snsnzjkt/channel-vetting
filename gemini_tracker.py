"""
Tracks Gemini free-tier REQUEST counts in a local JSON log that survives the run.

**Why this exists.** The per-run counters live on the `GeminiVerifier` instance,
which is right for them: they describe one run and should vanish with the process
(see influencers.py for the same argument). But a *daily* ceiling cannot live
there. Two runs on the same day would each get a full allowance, which is the
exact bug `credit_tracker`'s docstring was written about, and a `workflow_dispatch`
fired minutes after the cron would re-issue requests into a quota it already
knew was exhausted.

**THE CLOCK IS PACIFIC, AND THAT IS DELIBERATE.** `credit_tracker` keys on
`prospect_day.today_iso()` (Toronto) with an explicit argument: credits buy rows
stamped with a prospect day and counted against a prospect-day cap, so a day's
spend and a day's row count must be readable side by side. **That argument does
not transfer here.** These caps brake *Google's* free-tier RPD, and Google's
quota day resets at midnight Pacific — which is `quota_tracker`'s entire
documented reason for existing. Keying on Toronto would offset our window ~3
hours from the limit it protects, so a late-evening manual dispatch could spend
into a fresh local allowance against a Google day that had not turned over. This
is not a fourth clock: it is the existing Google-quota clock, used for a Google
quota.

**Two ceilings, because two different things are scarce.** Total requests bound
the RPD; VIDEO requests separately bound the free tier's 8-hours-of-YouTube-per-
day allowance, which the text tier never touches. A run can therefore exhaust its
video budget while the text tier keeps scoring.

**Failure direction: CLOSED**, like `credit_tracker` and unlike `quota_tracker`.
An unreadable `quota_log.json` returns `{}` and reads as "0 spent" — fine for
free quota that resets nightly and is only ever spent by us. Here a corrupt read
would authorise a fresh allowance against requests already made. Closing costs
almost nothing in this design: verification switching off means candidates keep
whatever verdict the existing gates gave them, which is exactly the pipeline's
behaviour without this feature at all. There is no reason to take the fail-open
risk for a benefit that small.

**NOTE FOR CI.** This file is only a real daily ceiling if it PERSISTS between
runs. In GitHub Actions that requires `gemini_log.json` in the workflow's
`actions/cache` path lists — same as `credit_log.json`. Without it every run
starts from an empty ledger and the day cap silently degrades to a per-run cap.
That is fail-OPEN in the one place this file exists to fail closed, and it is why
the workflow is a first-class file in GEMINI_VERIFY_PLAN.md rather than "docs".

**Deliberate duplication.** This is ~70% the shape of `credit_tracker.py` and is
kept separate on purpose rather than factored into a shared ledger: generalising
would mean editing a *money* ledger whose exact failure direction is hardened and
heavily commented, for the benefit of a non-money counter. See credit_tracker.py
for the other half of this pair.
"""
import json
import logging
import os
from datetime import datetime, timedelta

from config import (
    GEMINI_LOG_FILE,
    GEMINI_MAX_REQUESTS_PER_DAY,
    GEMINI_MAX_VIDEO_REQUESTS_PER_DAY,
)
from quota_tracker import PACIFIC_TZ, _replace_with_retry, today_pacific

logger = logging.getLogger(__name__)

# Request kinds, so the log answers "what did we spend it on?" and not merely
# "how much?". Only VIDEO consumes the 8h/day YouTube allowance.
KIND_TEXT = "text"
KIND_VIDEO = "video"

# Daily detail is pruned to keep the file small. Longer than quota_tracker's 7
# days because a human debugging "why did verification stop last Thursday" has no
# other record — there is no vendor dashboard for this that we can read.
RETENTION_DAYS = 30


class GeminiLedgerUnavailable(RuntimeError):
    """
    The request ledger could not be read.

    Raised only by `assert_readable()`, at run start. Mid-run the public helpers
    return False instead, so one corrupt read disables verification without
    unwinding a run that has already spent YouTube quota and vendor credits.
    """


def _empty() -> dict:
    return {"days": {}}


def load_log() -> dict:
    """
    The ledger, or raise if it exists and cannot be read.

    A MISSING file returns an empty ledger — that is a first run, and a truthful
    "nothing spent yet". A file that exists and does not parse is a different
    thing, and guessing zero there is what overspends.
    """
    if not os.path.exists(GEMINI_LOG_FILE):
        return _empty()
    try:
        with open(GEMINI_LOG_FILE, "r", encoding="utf-8") as f:
            log = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise GeminiLedgerUnavailable(
            f"Gemini ledger {GEMINI_LOG_FILE} is unreadable ({exc}). Refusing to "
            "issue requests against an unknown count — inspect or delete the file."
        ) from exc
    if not isinstance(log, dict):
        raise GeminiLedgerUnavailable(
            f"Gemini ledger {GEMINI_LOG_FILE} is not a JSON object."
        )
    log.setdefault("days", {})
    return log


def assert_readable() -> None:
    """
    Fail loudly NOW if the ledger is corrupt, rather than deep inside a call site.

    Called once from `run()` beside `credit_tracker.assert_readable()`. Unlike
    that one this does NOT abort the run — verification is optional and the
    pipeline is fully functional without it — so the caller logs and disables.
    """
    load_log()


def _prune(log: dict) -> dict:
    cutoff = datetime.now(PACIFIC_TZ).date() - timedelta(days=RETENTION_DAYS)
    kept = {}
    for key, value in (log.get("days") or {}).items():
        try:
            day = datetime.strptime(key, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            # Unparseable keys are KEPT, not discarded: this file is
            # hand-inspectable and silently deleting a key we don't understand
            # is worse than carrying it. Same rule as quota_tracker._prune.
            kept[key] = value
            continue
        if day >= cutoff:
            kept[key] = value
    log["days"] = kept
    return log


def _save_log(log: dict) -> None:
    """
    Persist atomically: write a `.tmp` sibling, fsync, then os.replace().

    The fsync is load-bearing — os.replace() orders the rename but not the data,
    so without it a crash can leave the renamed file present and empty, which
    `load_log()` would then raise on. Raising is the safe direction here, but a
    ledger that raises every run needs a human, so don't create that state
    casually. `_replace_with_retry` is borrowed from quota_tracker for the
    Windows file-lock case documented there.
    """
    log = _prune(log)
    # UNIQUE PER PROCESS. This was f"{GEMINI_LOG_FILE}.tmp" — one shared name —
    # and two concurrent runs then raced: both wrote the same tmp file, the
    # first os.replace moved it away, and the second raised
    # FileNotFoundError. _replace_with_retry only retries PermissionError,
    # so the loser CRASHED MID-RUN. Observed 2026-08-22 when a Home Theater
    # sweep and a Lifestyle run overlapped: the Lifestyle run died in
    # record_spend after examining 9 candidates and wrote no rows.
    #
    # os.getpid() is enough: the collision is between PROCESSES, and a
    # single process serialises its own writes.
    tmp_path = f"{GEMINI_LOG_FILE}.{os.getpid()}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(log, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(tmp_path, GEMINI_LOG_FILE)
    except BaseException:
        # BaseException, not Exception, so a KeyboardInterrupt doesn't leave a
        # stale .tmp for the next run to trip over. The real log is safe either
        # way — os.replace() hasn't run.
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _today_entry(log: dict) -> dict:
    return (log.get("days") or {}).get(today_pacific()) or {}


def _model_entry(day_entry: dict, model: str) -> dict:
    """
    Per-model counters inside a day.

    WHY PER MODEL. Google's free RPD is per model, not per project — measured
    2026-08-21: `gemini-3.5-flash-lite` answered a PerDay 429 after ~106
    requests while the other allowlisted models were untouched. So a single flat
    day counter cannot express "this model is spent but that one is not", which
    is exactly what the fallback in gemini_verify needs to know.

    Top-level `total` / `text` / `video` are kept alongside for the run summary
    and for the older flat shape, so an existing ledger stays readable.
    """
    return (day_entry.setdefault("models", {})).setdefault(model, {})


def requests_today() -> tuple[int, int]:
    """
    (total, video) requests recorded today (Pacific). (0, 0) if unreadable.

    Returns zeros rather than raising because every mid-run caller treats a
    failure to read as "do not spend" via `can_afford()` — this is the reporting
    path, and a run summary that cannot print is worse than one printing zeros.
    """
    try:
        entry = _today_entry(load_log())
    except GeminiLedgerUnavailable:
        return 0, 0
    return int(entry.get("total", 0)), int(entry.get(KIND_VIDEO, 0))


def exhausted_models() -> set:
    """
    Models Google has already refused today with a PerDay 429.

    The fallback chain skips these without issuing a request, so a second run on
    the same day does not spend one request per model rediscovering what the
    first run already learned.
    """
    try:
        entry = _today_entry(load_log())
    except GeminiLedgerUnavailable:
        return set()
    return {m for m, v in (entry.get("models") or {}).items() if v.get("exhausted")}


def record_request(*, video: bool, model: str = "", detail: str = "") -> None:
    """
    Add one request to today's counts and persist.

    Called AFTER a response is received, successful or not — a 4xx or a 429 still
    consumed the request as far as Google is concerned, and counting only
    successes is how a ledger drifts under exactly the conditions it exists for.
    """
    try:
        log = load_log()
    except GeminiLedgerUnavailable as exc:
        logger.warning("Not recording a Gemini request: %s", exc)
        return
    day = today_pacific()
    entry = (log.setdefault("days", {})).setdefault(day, {})
    entry["total"] = int(entry.get("total", 0)) + 1
    kind = KIND_VIDEO if video else KIND_TEXT
    entry[kind] = int(entry.get(kind, 0)) + 1
    if model:
        me = _model_entry(entry, model)
        me["total"] = int(me.get("total", 0)) + 1
        if video:
            me[KIND_VIDEO] = int(me.get(KIND_VIDEO, 0)) + 1
    try:
        _save_log(log)
    except OSError as exc:
        logger.warning("Could not persist the Gemini ledger (%s).", exc)
        return
    logger.debug(
        "Gemini request recorded (%s%s) -> %d today (%d video)",
        kind, f", {detail}" if detail else "", entry["total"], entry.get(KIND_VIDEO, 0),
    )


def can_afford(*, video: bool, model: str = "") -> bool:
    """
    Whether one more request of this kind stays inside today's ceilings.

    Returns **False** on an unreadable ledger — fail closed. A refusal here means
    the candidate keeps whatever verdict the existing gates gave it, i.e. exactly
    today's pipeline behaviour, so closing is cheap.
    """
    try:
        log = load_log()
    except GeminiLedgerUnavailable as exc:
        logger.warning("Refusing a Gemini request: %s", exc)
        return False
    day = _today_entry(log)
    # Caps are counted PER MODEL, mirroring Google's own per-model RPD. A single
    # project-wide counter would let one spent model lock out two healthy ones,
    # which is the opposite of what the fallback chain is for.
    entry = (day.get("models") or {}).get(model, {}) if model else day
    if model and (day.get("models") or {}).get(model, {}).get("exhausted"):
        logger.warning(
            "Skipping %s: Google already refused it today with a PerDay 429. "
            "The fallback chain moves to the next allowlisted FREE model.", model,
        )
        return False
    total = int(entry.get("total", 0))
    if total + 1 > GEMINI_MAX_REQUESTS_PER_DAY:
        # INFO, not WARNING, when a model is named: the caps are per model and
        # the fallback chain simply moves to the next free one, so this is routine
        # rather than a problem. The caller logs a WARNING only when EVERY model
        # in the chain is out, which is the state that actually needs attention.
        (logger.info if model else logger.warning)(
            "Gemini day cap reached for %s: %d/%d requests today (Pacific).%s",
            model or "the configured model", total, GEMINI_MAX_REQUESTS_PER_DAY,
            " Trying the next FREE model in the chain." if model else
            " Verification is paused until the Pacific day rolls over; candidates "
            "keep the verdict the existing gates gave them.",
        )
        return False
    if video:
        used = int(entry.get(KIND_VIDEO, 0))
        if used + 1 > GEMINI_MAX_VIDEO_REQUESTS_PER_DAY:
            logger.warning(
                "Gemini VIDEO day cap reached: %d/%d video requests today "
                "(Pacific). This is the cap that guards the free tier's 8h/day "
                "YouTube allowance. The text tier is unaffected.",
                used, GEMINI_MAX_VIDEO_REQUESTS_PER_DAY,
            )
            return False
    return True


def exhaust_day(*, video: bool, model: str = "") -> None:
    """
    Write today's counter straight to its ceiling.

    Called when Google returns a PerDay 429: we now know the real allowance is
    gone, which our own counter had no way to predict (Google stopped publishing
    per-model free RPD — it is per-project and only visible in AI Studio). Writing
    the ceiling means a second run today short-circuits at `can_afford()` without
    issuing a request to rediscover the same fact.
    """
    try:
        log = load_log()
    except GeminiLedgerUnavailable:
        return
    day = today_pacific()
    entry = (log.setdefault("days", {})).setdefault(day, {})
    # Pin the MODEL that was refused, not the whole day: the other allowlisted
    # free models have their own quotas and are still usable.
    if model:
        me = _model_entry(entry, model)
        me["total"] = max(int(me.get("total", 0)), GEMINI_MAX_REQUESTS_PER_DAY)
        me["exhausted"] = True
    else:
        entry["total"] = max(int(entry.get("total", 0)), GEMINI_MAX_REQUESTS_PER_DAY)
    entry["quota_exhausted"] = True
    try:
        _save_log(log)
    except OSError:
        return
    logger.warning(
        "Google reported the free daily allowance exhausted for %s; that MODEL is "
        "pinned for today so a re-run skips it without spending a request. Other "
        "models on the free allowlist have their own quotas and are still tried.",
        model or "the configured model",
    )


def spend_summary() -> str:
    """
    One line for the run summary: today's counts against today's ceilings,
    reported PER MODEL because that is how the ceilings are enforced.

    This used to print the day's GLOBAL total against a PER-MODEL cap, and the
    two are not comparable. Observed 2026-08-24 with three models in the chain:
    it printed `83/80 requests today (83/40 video)` — reading as a 104% and 208%
    breach — while every model was inside its own limit at 40/40, 40/40 and 3/40.
    With N allowlisted models the old line could show N x the cap and still be
    describing a healthy run, which is exactly backwards for the 2am question
    the summary exists to answer. See `_model_entry` for why caps are per model.

    A total is still printed, labelled as a SUM with no ratio, because "how many
    requests did this day cost" is a real question — it just is not a ceiling.
    """
    try:
        entry = _today_entry(load_log())
    except GeminiLedgerUnavailable as exc:
        return f"LEDGER UNAVAILABLE ({exc})"

    total = int(entry.get("total", 0))
    video = int(entry.get(KIND_VIDEO, 0))
    models = entry.get("models") or {}

    parts = []
    for model in sorted(models):
        counts = models[model] or {}
        m_total = int(counts.get("total", 0))
        m_video = int(counts.get(KIND_VIDEO, 0))
        # `exhausted` is Google's own PerDay 429 on this model, which outranks
        # our ceilings — flag it distinctly from merely reaching our cap.
        if counts.get("exhausted"):
            state = " 429-SPENT"
        elif (m_total >= GEMINI_MAX_REQUESTS_PER_DAY
              or m_video >= GEMINI_MAX_VIDEO_REQUESTS_PER_DAY):
            state = " CAPPED"
        else:
            state = ""
        parts.append(f"{model} {m_total}/{GEMINI_MAX_REQUESTS_PER_DAY}"
                     f" ({m_video}/{GEMINI_MAX_VIDEO_REQUESTS_PER_DAY} video){state}")

    per_model = "; ".join(parts) if parts else "no model recorded yet"
    return (f"today per model — {per_model} "
            f"[day sum {total} requests, {video} video]")
