"""
Tests for followup/activity.py.

Every age here is computed against an INJECTED clock. A test that reads the wall
clock to build "six months ago" passes today and fails in February, which is the
exact trap `followup_eligibility()` takes `clock=` to avoid.
"""
from datetime import datetime, timezone

import pytest

from channel_vetting.followup import activity as A

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


# --- cost claims -------------------------------------------------------------

def test_the_two_unit_claim_in_the_docstring_is_true():
    """
    The whole reason this module exists instead of reusing
    get_recent_video_performance() is that it costs 2 units, not 3. If the
    constants drift, the plan's arithmetic silently becomes wrong.
    """
    assert A.UNITS_FIRST_PASS == 2
    assert A.UNITS_RESWEEP == 1


def test_only_free_endpoints_are_declared():
    assert A.FREE_ENDPOINTS == ("channels.list", "playlistItems.list")


PAID_MODULES = (
    "channel_vetting.discovery.influencers_club",
    "channel_vetting.enrichment.email_influencers",
    "channel_vetting.verification.gemini",
)


@pytest.fixture
def clean_modules(monkeypatch):
    """
    assert_free_only() reads sys.modules, which is PROCESS-global — running the
    full suite loads the paid modules for unrelated reasons and the guard then
    fires spuriously. That is a real limitation of the check, documented in its
    docstring; these tests isolate it so they exercise the guard rather than the
    test runner's import history.
    """
    import sys
    for m in PAID_MODULES:
        monkeypatch.delitem(sys.modules, m, raising=False)


def test_free_guard_raises_when_a_credit_module_is_loaded(clean_modules, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "channel_vetting.discovery.influencers_club", object())
    with pytest.raises(A.PaidSurfaceError, match="credit-spending"):
        A.assert_free_only()


def test_free_guard_raises_when_the_gemini_client_is_loaded(clean_modules, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "channel_vetting.verification.gemini", object())
    with pytest.raises(A.PaidSurfaceError, match="must not reach a model"):
        A.assert_free_only()


def test_free_guard_warns_but_does_not_raise_on_ambient_gemini_flag(clean_modules, monkeypatch):
    """
    An earlier version hard-failed here, which stopped free work over a flag
    governing a subsystem this module never calls. It must warn instead.
    """
    monkeypatch.setattr("channel_vetting.config.GEMINI_ENABLED", True)
    passed, warnings = A.assert_free_only()
    assert passed
    assert any("GEMINI_ENABLED=true" in w for w in warnings)


def test_no_followup_module_imports_a_paid_path():
    """
    THIS is the proof, not the sys.modules check.

    Reads every source file in the followup package and fails if any of them
    imports a credit- or model-spending module. Unlike the runtime guard this
    cannot be defeated by import order, and it fails on the commit that
    introduces the dependency rather than on the run that spends the money.
    """
    import pathlib
    pkg = pathlib.Path(A.__file__).parent
    offenders = []
    for f in sorted(pkg.glob("*.py")):
        src = f.read_text()
        for paid in PAID_MODULES:
            # the guard NAMES these modules in strings; only real imports count
            if f"import {paid}" in src or f"from {paid} import" in src:
                offenders.append(f"{f.name} -> {paid}")
    assert not offenders, f"followup package reaches a paid path: {offenders}"


# --- days_since_upload -------------------------------------------------------

def test_days_since_upload_counts_whole_days():
    assert A.days_since_upload("2026-06-02T00:00:00Z", NOW) == 85


def test_days_since_upload_returns_none_for_an_unreadable_timestamp():
    for bad in ["", None, "not-a-date", "2026-13-99T00:00:00Z"]:
        assert A.days_since_upload(bad, NOW) is None, bad


def test_days_since_upload_reads_airtable_millisecond_form():
    """
    The repo has been bitten by this: a value round-tripped through an Airtable
    dateTime comes back as ...T12:00:00.000Z and a strict strptime rejects it.
    """
    assert A.days_since_upload("2026-06-02T00:00:00.000Z", NOW) == 85


def test_a_scheduled_premiere_in_the_future_clamps_to_zero():
    assert A.days_since_upload("2026-12-01T00:00:00Z", NOW) == 0


# --- classify_activity -------------------------------------------------------

def test_unknown_is_never_inactive():
    """
    The asymmetry that is the point of this module. days_since_last_upload()
    returns None for unreadable data and discovery treats that as "keep the
    lead"; for a follow-up, unknown must not read as either active OR stale.
    """
    verdict, reason = A.classify_activity(None, 365)
    assert verdict == A.UNKNOWN
    assert verdict != A.INACTIVE
    assert reason == A.REASON_UNREADABLE_DATE


def test_unknown_is_never_active_either():
    verdict, _ = A.classify_activity(None, 365)
    assert verdict != A.ACTIVE


@pytest.mark.parametrize("days,expected", [
    (0, A.ACTIVE),
    (364, A.ACTIVE),
    (365, A.ACTIVE),      # boundary: equal to the threshold is still active
    (366, A.INACTIVE),    # strictly greater is inactive
    (1140, A.INACTIVE),   # the legacy population's median age
])
def test_classify_activity_boundary(days, expected):
    assert A.classify_activity(days, 365)[0] == expected


def test_inactive_reason_names_both_numbers():
    _, reason = A.classify_activity(928, 365)
    assert "928" in reason and "365" in reason


# --- the 1-unit dead-channel variant ----------------------------------------

def test_dead_only_costs_one_unit():
    """
    The reason this variant was chosen: 1 unit filters the 8.0% channel_gone
    share, where the second unit bought only the 3.7% inactive share.
    """
    assert A.UNITS_DEAD_ONLY == 1
    assert A.UNITS_DEAD_ONLY < A.UNITS_FIRST_PASS


@pytest.mark.parametrize("stored,live", [
    ("Han's Tech Talk", "HansTechTalk"),
    ("2ToRamble", "2 To Ramble"),
    ("BehindTheGlass", "Behind The Glass"),
    ("Late Model Racecraft", "LATE MODEL RACECRAFT"),
    ("haus-automatisierung.com", "haus automatisierung com"),
    ("Move # Electric", "Move Electric"),
])
def test_cosmetic_title_differences_are_not_a_change(stored, live):
    """
    Every one of these is a REAL pair from the 700-handle sample that an earlier
    punctuation-preserving comparison wrongly flagged as a mismatch — and then
    wrongly excluded the creator for it.
    """
    assert A.title_changed(stored, live) is False


@pytest.mark.parametrize("stored,live", [
    ("With Love, Leena", "Leena Snoubar"),
    ("DIY with KB", "Kiva Brent"),
    ("Barry + Jordan", "Brownstone Boys"),
    ("MyCrazyMakeup", "Leticia Sanchez"),
])
def test_a_real_rename_is_detected(stored, live):
    assert A.title_changed(stored, live) is True


def test_a_title_change_is_advisory_and_never_an_exclusion():
    """
    A rebrand and a handle takeover are indistinguishable from the title alone,
    and the sample proves rebrands are common. So title_changed() must be a
    FLAG, not a verdict — an earlier version excluded rebranded creators on no
    evidence. Asserted structurally: the probe carries `title_flag` and its
    verdict stays ALIVE.
    """
    import inspect
    src = inspect.getsource(A.fetch_channel_alive)
    assert "title_flag=title_changed" in src
    assert "unresolvable" not in src.lower()
    # and the dataclass exposes it as a flag, not a verdict
    fields = A.AliveProbe.__dataclass_fields__
    assert "title_flag" in fields
    assert fields["title_flag"].type in (bool, "bool")


def test_a_blank_title_on_either_side_is_not_a_change():
    assert A.title_changed("", "Something") is False
    assert A.title_changed("Something", "") is False


def test_gone_costs_zero_units():
    """
    get_channel_stats() bills only a call that returned data, so a dead channel
    (or a quota wall) spends nothing. This is what makes the breaker safe.
    """
    import inspect
    src = inspect.getsource(A.fetch_channel_alive)
    assert "AliveProbe(handle, GONE, 0" in src
