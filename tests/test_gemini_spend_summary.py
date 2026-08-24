"""
The run summary reports per-model ceilings, because that is how they are enforced.

REGRESSION. `spend_summary()` printed the day's GLOBAL total against a PER-MODEL
cap. Observed live on 2026-08-24 with three models in the chain, it read
`83/80 requests today (83/40 video)` — apparently a 104% and 208% breach — while
every model was inside its own limit at 40/40, 40/40 and 3/40. With N allowlisted
models the line could show N x the cap while describing a perfectly healthy run,
which inverts the one question the summary exists to answer at 2am.
"""
import json

import gemini_tracker as gt
from config import GEMINI_MAX_REQUESTS_PER_DAY as DAY
from config import GEMINI_MAX_VIDEO_REQUESTS_PER_DAY as VDAY


def _ledger(tmp_path, monkeypatch, models):
    """A log file whose day totals are the SUM across models, as the real one is."""
    day = gt.today_pacific()
    total = sum(m.get("total", 0) for m in models.values())
    video = sum(m.get("video", 0) for m in models.values())
    path = tmp_path / "gemini_log.json"
    path.write_text(json.dumps({"days": {day: {
        "total": total, "video": video, "models": models}}}))
    monkeypatch.setattr(gt, "GEMINI_LOG_FILE", str(path))
    return path


def test_the_global_sum_is_never_printed_as_a_ratio_against_a_per_model_cap(
        tmp_path, monkeypatch):
    """The exact 2026-08-24 state: 83 requests across 3 models, none over cap."""
    _ledger(tmp_path, monkeypatch, {
        "gemini-3.5-flash-lite": {"total": 40, "video": 40},
        "gemini-3.1-flash-lite": {"total": 40, "video": 40},
        "gemini-3.7-flash": {"total": 3, "video": 3},
    })
    line = gt.spend_summary()
    assert f"83/{DAY}" not in line, f"the day SUM must not be a ratio: {line}"
    assert f"83/{VDAY}" not in line, f"the day SUM must not be a ratio: {line}"
    # Each model reported against its own ceiling.
    assert f"gemini-3.5-flash-lite 40/{DAY} (40/{VDAY} video)" in line
    assert f"gemini-3.7-flash 3/{DAY} (3/{VDAY} video)" in line
    # The sum survives, labelled as a sum.
    assert "day sum 83 requests" in line


def test_a_model_at_its_cap_is_flagged_and_one_with_headroom_is_not(
        tmp_path, monkeypatch):
    _ledger(tmp_path, monkeypatch, {
        "gemini-3.5-flash-lite": {"total": 40, "video": 40},
        "gemini-3.7-flash": {"total": 3, "video": 3},
    })
    line = gt.spend_summary()
    spent, healthy = line.split(";")[0], line.split(";")[1]
    assert "CAPPED" in spent, spent
    assert "CAPPED" not in healthy, healthy


def test_googles_own_per_day_429_outranks_our_ceiling_in_the_report(
        tmp_path, monkeypatch):
    """
    `exhausted` is Google refusing the model, which is a different operator
    action from hitting a limit we set ourselves — so it reads differently.
    """
    _ledger(tmp_path, monkeypatch, {
        "gemini-3.5-flash-lite": {"total": 5, "video": 5, "exhausted": True},
    })
    line = gt.spend_summary()
    assert "429-SPENT" in line, line
    assert "CAPPED" not in line, line


def test_an_empty_day_still_prints(tmp_path, monkeypatch):
    """The summary prints UNCONDITIONALLY; it must never raise on a fresh day."""
    path = tmp_path / "gemini_log.json"
    path.write_text(json.dumps({"days": {}}))
    monkeypatch.setattr(gt, "GEMINI_LOG_FILE", str(path))
    line = gt.spend_summary()
    assert "no model recorded yet" in line
    assert "day sum 0 requests" in line


def test_an_unreadable_ledger_says_so_rather_than_printing_zeros(
        tmp_path, monkeypatch):
    """
    A run summary that silently reads 0/80 on an unreadable ledger is worse than
    one that admits it cannot tell — the numbers drive a spend decision.
    """
    path = tmp_path / "gemini_log.json"
    path.write_text("{not json")
    monkeypatch.setattr(gt, "GEMINI_LOG_FILE", str(path))
    assert "LEDGER UNAVAILABLE" in gt.spend_summary()
