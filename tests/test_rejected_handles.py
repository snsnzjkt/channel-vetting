"""
A creator we already examined and rejected is not bought again.

The leak: discovery bills 0.01 per creator RETURNED and the endpoint sorts by
relevancy deterministically, so the same rejects arrive at the top of the same
query every run. `seen_handles` only covered the current process and
`tracked_handles` only covered creators that became ROWS — a creator we paid for,
examined and rejected was in neither. Measured on a live Home Theater round:
28% of the page was creators already known to us (7 tracked elsewhere in the
base, 4 on DO NOT CONTACT).

The vendor's `exclude_handles` cap is 10,000 (verified: 12,000 elements returns
"Ensure this field has no more than 10000 elements"), so the exclusion set is a
BUDGET and a proven re-bill has to outrank a speculative one.
"""
import json

import pytest

import main
import rejected_handles
from scoring import QUALIFIED


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Never touch the real cache file from a test."""
    path = tmp_path / "rejected_handles.json"
    monkeypatch.setattr(rejected_handles, "REJECTED_HANDLES_FILE", str(path))
    return path


# --- the ledger ------------------------------------------------------------


def test_a_rejected_handle_survives_the_process():
    rejected_handles.record("Home Theater", ["someCreator"])
    assert rejected_handles.for_niche("Home Theater") == {"somecreator"}


def test_handles_are_normalised_on_the_way_in():
    """The vendor's exclude_handles is matched exactly, and discovery yields
    bare lowercase handles — so '@Foo' and 'foo' must not become two entries."""
    rejected_handles.record("N", ["@Foo", "foo", " FOO "])
    assert rejected_handles.for_niche("N") == {"foo"}


def test_niches_do_not_share_a_rejection_set():
    """Relevancy order is per query, so one niche's misses are not evidence
    about another's — and mixing them spends one niche's exclusion budget on
    the other's."""
    rejected_handles.record("Home Theater", ["a"])
    rejected_handles.record("Lifestyle Sofa", ["b"])
    assert rejected_handles.for_niche("Home Theater") == {"a"}
    assert rejected_handles.for_niche("Lifestyle Sofa") == {"b"}


def test_an_old_rejection_expires(monkeypatch, _isolated_cache):
    """A WINDOW, not a blacklist: a channel that failed the view floor may have
    grown past it, and only the gates can notice."""
    _isolated_cache.write_text(json.dumps(
        {"niches": {"N": {"stale": "2000-01-01", "fresh": "2026-08-20"}}}))
    monkeypatch.setattr(rejected_handles, "today_iso", lambda: "2026-08-20")
    rejected_handles.record("N", ["another"])
    assert rejected_handles.for_niche("N") == {"fresh", "another"}


def test_a_re_rejected_handle_is_restamped(monkeypatch, _isolated_cache):
    """A creator the query keeps returning must not age out mid-window and get
    re-bought on a fixed schedule."""
    monkeypatch.setattr(rejected_handles, "today_iso", lambda: "2026-05-01")
    rejected_handles.record("N", ["x"])
    monkeypatch.setattr(rejected_handles, "today_iso", lambda: "2026-08-20")
    rejected_handles.record("N", ["x"])
    assert json.loads(_isolated_cache.read_text())["niches"]["N"]["x"] == "2026-08-20"


def test_an_unreadable_cache_fails_OPEN(_isolated_cache):
    """This is an optimisation, never a safety gate. Refusing to run would trade
    a small money leak for zero rows; DO NOT CONTACT screening is unaffected."""
    _isolated_cache.write_text("{ not json")
    assert rejected_handles.for_niche("N") == set()


def test_a_cache_of_the_wrong_shape_is_ignored(_isolated_cache):
    _isolated_cache.write_text(json.dumps(["not", "a", "dict"]))
    assert rejected_handles.for_niche("N") == set()


def test_recording_nothing_is_a_no_op(_isolated_cache):
    rejected_handles.record("N", [])
    rejected_handles.record("N", ["", "  ", "@"])
    assert rejected_handles.for_niche("N") == set()


# --- what counts as a rejection -------------------------------------------


def _collect(reason, handle="creator"):
    counts = main.push_until_full(
        [{"handle": handle, "channel_id": "UC1"}],
        lambda c: (None, reason),
        "tbl", qualified_headroom=5, flagged_headroom=5,
    )
    return counts["rejected_handles"]


def test_a_gate_rejection_is_recorded():
    assert _collect(main.DROP_BELOW_VIEW_MINIMUM) == {"creator"}
    assert _collect(main.DROP_SHORTS_ONLY) == {"creator"}
    assert _collect("blocked") == {"creator"}
    assert _collect("duplicate") == {"creator"}


@pytest.mark.parametrize("reason", sorted(main.TRANSIENT_DROP_REASONS))
def test_a_run_circumstance_is_never_recorded(reason):
    """
    These say nothing about the CHANNEL. Recording one would blind the pipeline
    to a genuine prospect for the whole retention window to save 0.01 credits,
    and the only symptom would be a table that quietly stopped finding people.
    """
    assert _collect(reason) == set()


def test_a_pushed_candidate_is_not_recorded(monkeypatch):
    """It became a row; tracked_handles covers it, and re-rejecting it here
    would be wrong the day a reviewer deletes the row."""
    monkeypatch.setattr(main, "push_record", lambda t, r: True)
    counts = main.push_until_full(
        [{"handle": "creator", "channel_id": "UC1"}],
        lambda c: ({"Channel ID": "UC1"}, QUALIFIED),
        "tbl", qualified_headroom=5, flagged_headroom=5,
    )
    assert counts["rejected_handles"] == set()


def test_a_candidate_with_no_handle_is_skipped():
    """The search.list fallback yields channel_ids, not handles, and
    exclude_handles cannot use those — nothing to record and nothing to save."""
    counts = main.push_until_full(
        [{"channel_id": "UC1"}], lambda c: (None, main.DROP_SHORTS_ONLY),
        "tbl", qualified_headroom=5, flagged_headroom=5,
    )
    assert counts["rejected_handles"] == set()


# --- the exclusion budget -------------------------------------------------


class _Blocklist:
    handles = frozenset({"blocked1"})


def test_rejected_handles_outrank_external_ones(monkeypatch):
    """
    The cap is a budget. A creator this query already returned is a PROVEN
    re-bill; an external-table handle might never be returned at all. So the
    proven one must survive when the two compete for the last slots.
    """
    external = type("E", (), {"handles": [f"ext{i}" for i in range(50)]})()
    monkeypatch.setattr(main, "_external_priority", lambda e, hint: list(e.handles))
    result = main._discovery_exclude_handles(
        _Blocklist(), external, seen_handles={"seen1"},
        tracked_handles={"tracked1"}, rejected_handles={"rejected1"},
    )
    for required in ("blocked1", "seen1", "tracked1", "rejected1"):
        assert required in result, f"{required} must never be dropped for room"


def test_omitting_rejected_handles_keeps_the_old_behaviour(monkeypatch):
    """The parameter is optional, so an existing caller is unchanged."""
    external = type("E", (), {"handles": []})()
    monkeypatch.setattr(main, "_external_priority", lambda e, hint: [])
    assert main._discovery_exclude_handles(
        _Blocklist(), external, seen_handles=set()
    ) == {"blocked1"}
