"""
iso_time.parse_iso_utc is the ONE tolerant ISO 8601 parse in this codebase.

It exists because two modules independently wrote the same rule with a strict
`datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")` and both were wrong the same
way. The enrichment copy RAISED (killing a whole pipeline run over one odd
`videoPublishedAt`); the outreach_ledger copy returned None and broke two
mechanisms silently:

- `followup_eligibility()` refused every follow-up with "prior send has no
  readable timestamp", so OUTREACH_RESPAM_MIN_DAYS was unreachable.
- `_lease_is_stale()` returned False at any age, so a stranded outreach lease
  never aged out and had to be cleared by hand.

The ledger bug was a ROUND TRIP break, which is what made it invisible:
`_utc_now_iso()` writes `%Y-%m-%dT%H:%M:%SZ`, Airtable stores that in a
dateTime field and returns it with milliseconds, and the strict reader rejected
the value it had itself written. Every `Settled At`/`Claimed At` fixture in the
suite used the bare-Z write format, so the tests agreed with the bug.
"""
from datetime import datetime, timedelta, timezone

import pytest

from iso_time import parse_iso_utc


# The shapes the two real producers actually emit.
@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-08-14T12:00:00Z", datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)),
        # Airtable's dateTime read-back form — the one that broke the ledger.
        ("2026-08-14T12:00:00.000Z", datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)),
        ("2026-08-14T12:00:00.123456Z", datetime(2026, 8, 14, 12, 0, 0, 123456, tzinfo=timezone.utc)),
        ("2026-08-14T12:00:00+00:00", datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)),
        # A bare date parses tz-naive and must be coerced to UTC here, so no
        # caller can hit an aware/naive TypeError.
        ("2026-08-14", datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc)),
    ],
)
def test_every_real_producer_shape_parses(value, expected):
    assert parse_iso_utc(value) == expected


def test_the_result_is_always_aware():
    """Every caller subtracts this from datetime.now(timezone.utc)."""
    for value in ("2026-08-14T12:00:00Z", "2026-08-14", "2026-08-14T12:00:00+00:00"):
        assert parse_iso_utc(value).tzinfo is not None


@pytest.mark.parametrize("value", ["", None, "not-a-date", "garbage", 12345, [], {}])
def test_missing_or_unreadable_is_none_never_a_raise(value):
    """Unknown, never a verdict — and never an exception, because the
    enrichment call site sits inside process_candidate where nothing between it
    and run() catches anything."""
    assert parse_iso_utc(value) is None


def test_an_offset_timestamp_is_normalised_for_comparison():
    """A non-UTC offset must still compare correctly against a UTC value."""
    assert parse_iso_utc("2026-08-14T14:00:00+02:00") == parse_iso_utc("2026-08-14T12:00:00Z")


# --- the rule has exactly one implementation --------------------------------

def test_both_former_copies_now_delegate_here():
    import enrichment
    import outreach_ledger

    for value in ("2026-08-14T12:00:00.000Z", "2026-08-14T12:00:00+00:00"):
        assert enrichment._parse_iso_timestamp(value) == parse_iso_utc(value)
        assert outreach_ledger._parse_utc(value) == parse_iso_utc(value)


@pytest.mark.parametrize("module_name", ["enrichment", "outreach_ledger"])
def test_neither_module_still_uses_a_strict_strptime_on_a_timestamp(module_name):
    """A regression guard on the mechanism. Re-adding a strict strptime would
    pass every behavioural test that happens to use the bare-Z form."""
    import importlib
    import inspect

    src = inspect.getsource(importlib.import_module(module_name))
    # The CALL form: both modules mention strptime in prose to explain why it is
    # gone, and both still use strftime legitimately for FORMATTING on write.
    assert "strptime(" not in src


def test_iso_time_imports_nothing_from_this_project():
    """It is a leaf module on purpose: outreach_ledger is storage-agnostic and
    must not pull in enrichment's http_client/config chain to read a date."""
    import ast
    import pathlib

    src = pathlib.Path(__file__).resolve().parent.parent / "iso_time.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported == {"datetime"}, f"iso_time.py grew a dependency: {imported}"


# --- the two mechanisms the ledger bug disabled -----------------------------

def test_a_stale_lease_now_ages_out_in_airtables_own_format():
    """_lease_is_stale returned False for ANY age in the millisecond form, so a
    stranded lease locked outreach out until someone cleared the row by hand."""
    from outreach_ledger import _lease_is_stale

    old = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    assert _lease_is_stale(old, 60) is True


def test_a_fresh_lease_is_still_treated_as_live():
    from outreach_ledger import _lease_is_stale

    fresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    assert _lease_is_stale(fresh, 60) is False


def test_an_unreadable_lease_timestamp_still_fails_closed():
    """The None policy is unchanged: refusing to start is recoverable,
    double-sending is not."""
    from outreach_ledger import _lease_is_stale

    assert _lease_is_stale("who-knows", 60) is False
