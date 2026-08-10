"""
external_dedupe.py is a DEDUPE list, not a suppression list, so its read
failures deliberately log and return partial results (CLAUDE.md: worst
case a known channel is re-added). What must be bulletproof instead is the
CACHE WRITE: a plain open(..., "w") truncates before writing, so an
interrupted refresh used to leave a corrupt or zero-length JSON file
behind and discard a still-good 24h index — costing thousands of Airtable
requests on the next run to rebuild something that was already correct.

HTTP is mocked by patching `external_dedupe.HTTP` (the shared retrying
session), not `external_dedupe.requests`, which now exists only for its
exception types.
"""
import json
import logging
import os

import pytest


class _Resp:
    def __init__(self, status_code, payload=None, text="error body"):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


@pytest.fixture
def cache_path(tmp_path, monkeypatch):
    """Point the module's cache filename at a throwaway dir.

    The module global is read at call time, so patching it is enough — no
    chdir needed, which keeps these tests safe to run in parallel with
    anything else that touches the real cache file.
    """
    import external_dedupe

    path = tmp_path / "external_handles_cache.json"
    monkeypatch.setattr(external_dedupe, "EXTERNAL_HANDLES_CACHE_FILE", str(path))
    monkeypatch.setattr(external_dedupe.time, "sleep", lambda s: None)
    return path


def _stub_fetch(monkeypatch, handles_by_table):
    """Replace the per-table Airtable pagination with canned handle sets."""
    import external_dedupe

    calls = iter(handles_by_table)
    monkeypatch.setattr(
        external_dedupe, "_fetch_table_handles", lambda table_id, link_field: set(next(calls))
    )


def test_cache_write_leaves_no_tmp_file_behind(cache_path, monkeypatch):
    import external_dedupe

    _stub_fetch(monkeypatch, [{"one"}, {"two"}, set(), {"three"}])

    handles = external_dedupe.fetch_external_handles(force_refresh=True)

    assert set(handles) == {"one", "two", "three"}
    # The real file is complete and parseable...
    with open(cache_path, "r", encoding="utf-8") as f:
        written = json.load(f)
    assert set(written["handles"]) == {"one", "two", "three"}
    assert written["fetched_at"] > 0
    # ...and the staging file is gone, not accumulating in the repo root.
    assert not os.path.exists(f"{cache_path}.tmp")
    assert os.listdir(cache_path.parent) == [cache_path.name]


def test_existing_cache_survives_a_write_that_raises_midway(cache_path, monkeypatch):
    """The regression this replaced: open(..., "w") truncated the real
    file first, so a crash mid-serialize destroyed a good cache. Writing
    to a .tmp sibling and os.replace()-ing it means the old file is either
    wholly replaced or wholly untouched."""
    import external_dedupe

    good = {"fetched_at": 1_000.0, "handles": {"already": "Home Theatre – YouTube Leads"}}
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(good, f)
    before = cache_path.read_bytes()

    _stub_fetch(monkeypatch, [{"new"}, set(), set(), set()])

    def boom(*a, **k):
        raise RuntimeError("interrupted mid-serialize")

    monkeypatch.setattr(external_dedupe.json, "dump", boom)

    with pytest.raises(RuntimeError):
        external_dedupe.fetch_external_handles(force_refresh=True)

    # The previous cache is byte-for-byte intact and still loads.
    assert cache_path.read_bytes() == before
    with open(cache_path, "r", encoding="utf-8") as f:
        assert json.load(f) == good
    # And the failed attempt cleaned up after itself.
    assert not os.path.exists(f"{cache_path}.tmp")


def test_no_cache_file_is_created_at_all_when_the_write_fails(cache_path, monkeypatch):
    """The same guarantee when there was nothing to protect: a failed
    refresh must not leave a half-written file that the next run would
    treat as a fresh cache."""
    import external_dedupe

    _stub_fetch(monkeypatch, [{"new"}, set(), set(), set()])
    monkeypatch.setattr(
        external_dedupe.json, "dump", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    with pytest.raises(RuntimeError):
        external_dedupe.fetch_external_handles(force_refresh=True)

    assert not os.path.exists(cache_path)
    assert not os.path.exists(f"{cache_path}.tmp")


def test_read_failure_returns_partial_rather_than_raising(monkeypatch, caplog):
    """Pins the deliberate asymmetry with do_not_contact.py: this list may
    come back short. Do NOT "harden" this into raising — a missing handle
    costs a duplicate row a human can spot, while an abort costs the run."""
    import external_dedupe

    monkeypatch.setattr(external_dedupe.time, "sleep", lambda s: None)
    monkeypatch.setattr(external_dedupe.HTTP, "get", lambda *a, **k: _Resp(500))

    caplog.set_level(logging.ERROR, logger="external_dedupe")
    assert external_dedupe._fetch_table_handles("tblX", "Link") == set()
    assert caplog.records  # the failure is loud, just not fatal


def test_pagination_error_body_is_truncated_in_the_log(monkeypatch, caplog):
    """resp.text went straight into this logging call before; an Airtable
    error can echo an entire rejected payload, and this call site runs once
    per page across four ~18k-row tables."""
    import external_dedupe

    huge = "y" * 20_000
    monkeypatch.setattr(external_dedupe.time, "sleep", lambda s: None)
    monkeypatch.setattr(external_dedupe.HTTP, "get", lambda *a, **k: _Resp(500, text=huge))

    caplog.set_level(logging.ERROR, logger="external_dedupe")
    external_dedupe._fetch_table_handles("tblX", "Link")

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert huge not in logged
    assert "truncated" in logged
