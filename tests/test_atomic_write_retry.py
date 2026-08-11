"""
The atomic rename behind quota_log.json and search_cache.json must survive a
transient Windows file lock.

os.replace() raises PermissionError (WinError 5) when another process holds a
handle to either path — antivirus and search indexers routinely open a file
microseconds after it is written, which a tmp-then-rename pattern reliably
provokes. record_spend() runs after nearly every API response and nothing up
the stack catches it, so an unretried failure ends the whole run with the
quota already spent. Observed three times on 2026-08-11, once four keywords
into a real discovery pass. POSIX rename() has no equivalent failure, which is
why CI never saw it.
"""
import json

import pytest

import quota_tracker


def test_retries_past_a_transient_lock(monkeypatch, tmp_path):
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(5, "Access is denied")
        return None

    monkeypatch.setattr(quota_tracker.os, "replace", flaky_replace)
    monkeypatch.setattr(quota_tracker.time, "sleep", lambda s: None)

    quota_tracker._replace_with_retry("a.tmp", "a")

    assert calls["n"] == 3, "must keep trying, not give up on the first lock"


def test_gives_up_loudly_rather_than_silently_skipping_the_write(monkeypatch):
    """
    A swallowed failure is the FAIL-OPEN direction: the ledger would stop
    advancing while quota kept being spent, and can_afford_search() would
    authorise the rest of the day against a stale total. Losing the run is
    the safe outcome; losing the accounting is not.
    """
    monkeypatch.setattr(
        quota_tracker.os, "replace",
        lambda src, dst: (_ for _ in ()).throw(PermissionError(5, "Access is denied")),
    )
    monkeypatch.setattr(quota_tracker.time, "sleep", lambda s: None)

    with pytest.raises(PermissionError):
        quota_tracker._replace_with_retry("a.tmp", "a")


def test_a_non_permission_error_is_not_retried(monkeypatch):
    """Only the lock is transient. A missing tmp file is a real bug and must
    surface immediately rather than after five sleeps."""
    calls = {"n": 0}

    def missing(src, dst):
        calls["n"] += 1
        raise FileNotFoundError(src)

    monkeypatch.setattr(quota_tracker.os, "replace", missing)
    monkeypatch.setattr(quota_tracker.time, "sleep", lambda s: None)

    with pytest.raises(FileNotFoundError):
        quota_tracker._replace_with_retry("a.tmp", "a")
    assert calls["n"] == 1


def test_the_spend_log_survives_a_lock_end_to_end(monkeypatch, tmp_path):
    """record_spend() must complete, and the ledger must be readable after."""
    log_file = tmp_path / "quota_log.json"
    monkeypatch.setattr(quota_tracker, "QUOTA_LOG_FILE", str(log_file))
    monkeypatch.setattr(quota_tracker.time, "sleep", lambda s: None)

    real_replace = quota_tracker.os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(5, "Access is denied")
        return real_replace(src, dst)

    monkeypatch.setattr(quota_tracker.os, "replace", flaky_replace)

    quota_tracker.record_spend(100, call_name="search.list('kw')")

    written = json.loads(log_file.read_text(encoding="utf-8"))
    assert written[quota_tracker.today_pacific()] == 100


def test_the_search_cache_uses_the_same_retry(monkeypatch, tmp_path):
    """search_cache.json has the identical rename; losing it re-spends 100
    units per keyword, and losing it by exception costs the run."""
    import discovery

    cache_file = tmp_path / "search_cache.json"
    monkeypatch.setattr(discovery, "SEARCH_CACHE_FILE", str(cache_file))
    monkeypatch.setattr(quota_tracker.time, "sleep", lambda s: None)

    real_replace = discovery.os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(5, "Access is denied")
        return real_replace(src, dst)

    monkeypatch.setattr(quota_tracker.os, "replace", flaky_replace)

    discovery._save_cache({"2026-08-11::7d::n50::kw": []})

    assert json.loads(cache_file.read_text(encoding="utf-8")) == {"2026-08-11::7d::n50::kw": []}
    assert calls["n"] == 2
