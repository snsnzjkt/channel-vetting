"""
One JSON line per run, so "did that change help?" is answerable.

Why this exists. Drop reasons went to stdout and nowhere else, `credit_log.json`
records spend but not yield, and nothing tied the two together. So the pipeline
could be tuned for weeks with no way to compare before against after — and it
was: three consecutive plans proposed yield changes against a ledger holding a
single day of data.

WHAT IS DELIBERATELY NOT STORED. `rows_per_credit` is the number everyone wants,
and storing it guarantees it drifts from its own numerator and denominator the
first time one of them is redefined. Derive it at read time from `rows_pushed`
and `credits_spent`.

WHAT THE NUMBERS MEAN, because the failure they exist to catch is subtle. Rows
per run and rows per credit are THROUGHPUT, not quality. A discovery change that
surfaces many mediocre-but-gate-passing channels RAISES both while lowering the
share the human reviewer approves — so read them next to reviewer approval rate
(scripts/analysis/backtest_relevance.py against the labelled rows), never alone. A rising
rows_per_credit with a falling approval rate is the alarm, and neither number
sees it by itself.
"""
import json
import logging
import os
from channel_vetting.core.paths import data_path

logger = logging.getLogger(__name__)

RUN_METRICS_FILE = os.getenv("RUN_METRICS_FILE", data_path("run_metrics.jsonl"))

# Bump when a field changes meaning, never when one is merely added. A reader
# comparing across a rename otherwise silently averages two different things.
SCHEMA_VERSION = 1

# Keeps one record comfortably under PIPE_BUF (4096), which is what makes the
# append atomic — see write().
MAX_DROP_REASONS = 20


def write(record: dict, path: str | None = None) -> bool:
    """
    Append one record. Returns True on success.

    NEVER RAISES. A metrics failure must not kill a run that has already spent
    money on discovery and enrichment — same posture as
    credit_tracker.record_vendor_balance.

    Atomicity: the whole line, trailing newline included, goes out in ONE
    write() to a file opened O_APPEND. On POSIX an append under PIPE_BUF cannot
    interleave with a concurrent writer, so two runs overlapping (a cron and a
    manual run) cannot corrupt each other's lines. Deliberately NOT the
    tmp+fsync+os.replace dance credit_tracker uses: that rewrites the whole file
    every run, which is O(n) forever on an append-only log.
    """
    path = path or RUN_METRICS_FILE
    try:
        reasons = record.get("drop_reasons") or {}
        if len(reasons) > MAX_DROP_REASONS:
            top = sorted(reasons.items(), key=lambda kv: -kv[1])[:MAX_DROP_REASONS]
            dropped = len(reasons) - MAX_DROP_REASONS
            record = dict(record, drop_reasons=dict(top), drop_reasons_truncated=dropped)
        line = json.dumps(record, sort_keys=True, default=str) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        return True
    except (OSError, TypeError, ValueError) as exc:
        logger.warning(
            "Could not write the run metrics record (%s) — the run itself is "
            "unaffected, but this run will be missing from any before/after "
            "comparison.", exc,
        )
        return False


def build(*, status, started_at, finished_at, niches, drop_reasons,
          credits_spent, creators_billed, quota_used, config_snapshot) -> dict:
    """
    Assemble a record. Pure — no I/O, so it is trivially testable.

    `status` is "completed" or "aborted". Aborted records are truthful partials
    and must be filterable, because averaging them with complete runs silently
    depresses every yield figure.

    `niches` is per-niche, not a total, because the question that motivated this
    file is "did Home Theater stop returning zero" — which a combined figure
    cannot answer.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_s": _duration(started_at, finished_at),
        "niches": niches,
        "rows_pushed": sum(n.get("rows", 0) for n in niches.values()),
        "creators_discovered": sum(n.get("discovered", 0) for n in niches.values()),
        "drop_reasons": dict(drop_reasons or {}),
        "credits_spent": round(credits_spent or 0, 4),
        "creators_billed": creators_billed or 0,
        "youtube_quota_used": quota_used,
        # The knobs in force. A before/after spanning a config change is
        # uninterpretable without them, and config changes are exactly what
        # this file is used to evaluate.
        "config": config_snapshot,
    }


def _ts(value):
    """Epoch seconds from an ISO string, or None. Never raises."""
    import datetime
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def _duration(started_at, finished_at):
    """Seconds between two ISO stamps, or None if either is unusable."""
    start, end = _ts(started_at), _ts(finished_at)
    if start is None or end is None:
        return None
    return round(end - start, 1)
