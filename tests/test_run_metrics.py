"""
run_metrics must record truthfully, and must never take a run down with it.

The two behaviours worth pinning are both about failure: a run that CRASHES is
exactly the run worth recording (it already spent money), and a metrics write
that fails must not become the thing that kills it.
"""
import json
from collections import Counter

import pytest

import run_metrics
from search_zones import ZONE_CORE


def _record(**over):
    base = dict(
        status="completed",
        started_at="2026-08-22T10:00:00+00:00",
        finished_at="2026-08-22T10:05:30+00:00",
        niches={"Home Theater": {"rows": 0, "discovered": 120},
                "Lifestyle Sofa": {"rows": 5, "discovered": 480}},
        drop_reasons=Counter({"outside_search_zone": 11, "below_view_minimum": 3}),
        credits_spent=6.0, creators_billed=600, quota_used=4200,
        config_snapshot={"DAILY_QUALIFIED_CAP": 30},
    )
    base.update(over)
    return run_metrics.build(**base)


def test_per_niche_breakdown_survives(tmp_path):
    """A combined total cannot answer 'did Home Theater stop returning zero'."""
    r = _record()
    assert r["niches"]["Home Theater"]["rows"] == 0
    assert r["niches"]["Lifestyle Sofa"]["rows"] == 5
    assert r["rows_pushed"] == 5


def test_rows_per_credit_is_not_stored():
    """Storing it guarantees drift from its own numerator and denominator."""
    assert "rows_per_credit" not in _record()


def test_aborted_runs_are_distinguishable():
    """Averaging partials with complete runs silently depresses every figure."""
    assert _record(status="aborted")["status"] == "aborted"


def test_write_appends_one_line_per_run(tmp_path):
    path = tmp_path / "run_metrics.jsonl"
    assert run_metrics.write(_record(), str(path))
    assert run_metrics.write(_record(status="aborted"), str(path))

    lines = path.read_text().strip().split("\n")
    assert len(lines) == 2
    assert [json.loads(l)["status"] for l in lines] == ["completed", "aborted"]


def test_write_never_raises_on_an_unwritable_path(tmp_path):
    """
    A metrics failure must not kill a run that already spent money on discovery
    and enrichment. Same posture as credit_tracker.record_vendor_balance.
    """
    unwritable = tmp_path / "no-such-dir" / "run_metrics.jsonl"
    assert run_metrics.write(_record(), str(unwritable)) is False


def test_write_coerces_unserialisable_content_rather_than_failing(tmp_path):
    """
    `json.dumps(default=str)` means no value can fail to serialise — an exotic
    object is stringified instead. That is the wanted behaviour: losing fidelity
    on one odd field beats losing the whole record for a run that spent money.
    Pinned because it is easy to "tighten" default= away and not notice.
    """
    path = tmp_path / "m.jsonl"
    assert run_metrics.write({"bad": {object()}, "good": 1}, str(path)) is True
    written = json.loads(path.read_text())
    assert written["good"] == 1
    assert isinstance(written["bad"], str)


def test_drop_reasons_are_truncated_to_keep_the_append_atomic(tmp_path):
    """
    One line must stay under PIPE_BUF (4096) or a concurrent append can
    interleave. A pathological run must not silently break that.
    """
    many = {f"reason_{i}": i for i in range(200)}
    path = tmp_path / "m.jsonl"
    assert run_metrics.write(_record(drop_reasons=many), str(path))
    written = json.loads(path.read_text())
    assert len(written["drop_reasons"]) == run_metrics.MAX_DROP_REASONS
    assert written["drop_reasons_truncated"] == 200 - run_metrics.MAX_DROP_REASONS
    # The ones kept must be the biggest, not an arbitrary slice.
    assert min(written["drop_reasons"].values()) > max(
        v for k, v in many.items() if k not in written["drop_reasons"]
    )
    assert len(path.read_text()) < 4096


def test_duration_tolerates_unusable_timestamps():
    assert _record(started_at="nonsense", finished_at=None)["duration_s"] is None
    assert _record()["duration_s"] == 330.0


# --- placement: the record must survive the runs worth recording -------------

def test_metrics_are_written_even_when_a_niche_raises(monkeypatch):
    """
    The run summary sits AFTER run()'s try/finally, so it is skipped by an
    exception from run_niche, by the `raise SystemExit(1)` below it, by a CI
    timeout and by KeyboardInterrupt. Those are precisely the runs worth
    recording: they already paid for discovery and enrichment.

    So the write lives in the `finally`. This test is what stops it drifting
    back out — a regression nothing else in the suite would notice, because the
    pipeline would still work perfectly and just silently stop recording.
    """
    import main

    written = []
    monkeypatch.setattr(main.run_metrics, "write",
                        lambda rec, p=None: written.append(rec) or True)
    monkeypatch.setattr(main, "fetch_blocklist", lambda: _NullBlocklist())
    monkeypatch.setattr(main, "get_existing_channel_ids", lambda t: set())
    monkeypatch.setattr(main, "fetch_external_handles", lambda: {})
    monkeypatch.setattr(main, "run_niche",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("niche exploded")))

    niches = {
        "Test Niche": {
            "table_name": "tbl", "keywords": ["kw"],
            "min_avg_views": 0, "min_channel_age_months": None,
            "allowed_country_codes": ZONE_CORE,
        }
    }

    with pytest.raises(RuntimeError):
        main.run(niches, max_results_per_keyword=5, days_back=7)

    assert written, "a crashed run must still leave a metrics record"
    assert written[0]["status"] == "aborted", (
        "a partial run must be filterable — averaging it with complete runs "
        "silently depresses every yield figure"
    )


def test_metrics_record_a_completed_run_as_completed(monkeypatch):
    import main

    written = []
    monkeypatch.setattr(main.run_metrics, "write",
                        lambda rec, p=None: written.append(rec) or True)
    monkeypatch.setattr(main, "fetch_blocklist", lambda: _NullBlocklist())
    monkeypatch.setattr(main, "get_existing_channel_ids", lambda t: set())
    monkeypatch.setattr(main, "fetch_external_handles", lambda: {})
    monkeypatch.setattr(main, "run_niche", lambda *a, **k: (7, 3, {"UC1"}, True))

    niches = {
        "Test Niche": {
            "table_name": "tbl", "keywords": ["kw"],
            "min_avg_views": 0, "min_channel_age_months": None,
            "allowed_country_codes": ZONE_CORE,
        }
    }
    main.run(niches, max_results_per_keyword=5, days_back=7)

    assert written[0]["status"] == "completed"
    assert written[0]["niches"]["Test Niche"] == {
        "rows": 3, "discovered": 7, "cap_check_completed": True,
    }


class _NullBlocklist:
    handles: list = []

    def __contains__(self, _):
        return False

    def is_blocked(self, *a, **k):
        return False


def test_the_metrics_log_is_isolated_from_production():
    """
    The autouse `isolate_run_metrics` fixture must be in force.

    Without it the run() tests append fixture records to the repo's real
    run_metrics.jsonl — which happened: ~35 junk records, mostly a "Test Niche"
    with every counter zero. Readers of this file average across runs, so
    zero-row fixture records drag every before/after toward zero. A polluted
    metrics file does not look broken; it looks like bad results.
    """
    assert run_metrics.RUN_METRICS_FILE != "run_metrics.jsonl", (
        "the autouse isolation fixture is not active — a run() test would "
        "append fixture rows to the production metrics log"
    )


def test_production_metrics_log_has_no_fixture_records():
    """Guards the cleanup: 'Test Niche' is not a real niche."""
    import pathlib

    path = pathlib.Path("run_metrics.jsonl")
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        niches_seen = json.loads(line).get("niches") or {}
        assert "Test Niche" not in niches_seen, "fixture records are back in the log"
