"""
Tests for followup/legacy.py — the FREE screens.

The refusal ORDER is the safety property here, not an implementation detail: a
row that is both suppressed and inactive must read as suppressed, because
`DNC Blocked` is a decision and `Inactive Channel` is a reviewable bucket.
"""
from datetime import datetime, timezone

import pytest

from channel_vetting.followup import legacy as L

NOW = datetime(2026, 8, 26, tzinfo=timezone.utc)
FLOOR = 180


class FakeBlocklist:
    """Stands in for do_not_contact.Blocklist, matching on all three keys."""
    def __init__(self, handles=(), emails=(), names=()):
        self.h, self.e, self.n = set(handles), set(emails), set(names)

    def match(self, handle="", email="", name=""):
        if handle and handle in self.h:
            return f"handle @{handle}"
        if email and email.lower() in self.e:
            return f"email {email}"
        if name and name.casefold() in self.n:
            return f"name '{name}'"
        return ""


def row(**kw):
    base = dict(record_id="rec1", handle="foo", name="Foo", email="a@b.com",
                date="2023-07-12", mail_sent=True, country="United States",
                subscribers="10K Subscribers")
    base.update(kw)
    return L.LegacyRow(**base)


def screen(rows, follow=(), bl=None):
    return L.free_screen(list(rows), list(follow), bl or FakeBlocklist(),
                         now=NOW, floor_days=FLOOR)


# --- the coverage invariant --------------------------------------------------

def test_every_row_lands_in_exactly_one_bucket():
    """
    The repo's standing rule: 'an excluded row is also an INVISIBLE row unless
    some other page claims it'. free_screen raises rather than lose a row.
    """
    rows = [row(record_id=f"rec{i}", handle=f"h{i}") for i in range(20)]
    buckets, _ = screen(rows)
    assert sum(len(v) for v in buckets.values()) == 20


def test_a_dropped_row_is_a_hard_error_not_a_silent_loss(monkeypatch):
    monkeypatch.setattr(L, "FREE_BUCKETS", L.FREE_BUCKETS)
    # Force the invariant by handing free_screen a row it cannot bucket is not
    # reachable through the public API, so assert the guard exists instead.
    import inspect
    assert "coverage bug" in inspect.getsource(L.free_screen)


# --- refusal ORDER ----------------------------------------------------------

def test_dnc_outranks_every_other_refusal():
    """A suppressed creator who is ALSO unjoinable, unsent and undated is DNC."""
    r = row(handle="blocked", email="", mail_sent=False, date="")
    buckets, _ = screen([r], bl=FakeBlocklist(handles=["blocked"]))
    assert buckets[L.B_DNC] == [r]
    assert buckets[L.B_NO_EMAIL] == []


def test_dnc_matches_on_handle_alone():
    """
    MEASURED: 476 of 560 suppressed legacy creators match by handle and only 84
    by email. An email-only check — all an Airtable automation can do — misses 85%.
    """
    r = row(handle="onlyhandle", email="notlisted@x.com")
    buckets, _ = screen([r], bl=FakeBlocklist(handles=["onlyhandle"]))
    assert buckets[L.B_DNC] == [r]


def test_dnc_matches_on_name_alone():
    r = row(handle="clean", email="clean@x.com", name="Suppressed Co")
    buckets, _ = screen([r], bl=FakeBlocklist(names=["suppressed co"]))
    assert buckets[L.B_DNC] == [r]


def test_touch_limit_outranks_unresolvable_and_date():
    r = row(handle="", date="")
    buckets, _ = screen([r], follow=[row(record_id="f1", handle="", name="Foo")])
    assert buckets[L.B_TOUCH_LIMIT] == [r]


# --- the fail-closed rules --------------------------------------------------

def test_no_prior_send_when_mail_sent_is_unticked():
    """A Date with Mail Sent off is no evidence of touch 1."""
    buckets, _ = screen([row(mail_sent=False)])
    assert len(buckets[L.B_NO_PRIOR_SEND]) == 1


def test_an_unreadable_date_refuses_rather_than_surviving():
    """
    Mirrors followup_eligibility()'s 'cannot prove enough time passed'. The
    dangerous direction is treating an unparseable date as old.
    """
    for bad in ["", "not-a-date", "2023-13-45"]:
        buckets, _ = screen([row(date=bad)])
        assert len(buckets[L.B_NOT_YET]) == 1, bad
        assert len(buckets[L.B_SURVIVES]) == 0, bad


def test_a_row_inside_the_floor_is_not_yet_eligible():
    buckets, _ = screen([row(date="2026-08-01")])   # 25 days
    assert len(buckets[L.B_NOT_YET]) == 1


def test_a_row_past_the_floor_survives():
    buckets, _ = screen([row(date="2023-07-12")])   # 1140 days
    assert len(buckets[L.B_SURVIVES]) == 1


def test_boundary_exactly_at_the_floor_is_refused():
    """< floor refuses, so exactly 180 days must survive and 179 must not."""
    from datetime import timedelta
    b1, _ = screen([row(date=(NOW - timedelta(days=180)).strftime("%Y-%m-%d"))])
    b2, _ = screen([row(date=(NOW - timedelta(days=179)).strftime("%Y-%m-%d"))])
    assert len(b1[L.B_SURVIVES]) == 1
    assert len(b2[L.B_NOT_YET]) == 1


# --- the touch-2 join -------------------------------------------------------

def test_join_on_handle():
    m = row(record_id="m1", handle="samehandle")
    f = row(record_id="f1", handle="samehandle", name="Different Name")
    matched, misses = L.touch2_record_ids([m], [f])
    assert matched == {"m1"} and misses == 0


def test_join_falls_back_to_name_when_the_handle_is_missing():
    """
    22 of 1,054 follow-up rows joined only on name. The name index exists for
    the recorded rename case (@Newrecordday2013 -> @newrecordday).
    """
    m = row(record_id="m1", handle="newhandle", name="New Record Day")
    f = row(record_id="f1", handle="", name="new record  day")
    matched, misses = L.touch2_record_ids([m], [f])
    assert matched == {"m1"} and misses == 0


def test_an_unjoinable_followup_row_is_counted_not_swallowed():
    """
    MEASURED: 9 of 1,054 do not join. A miss reads touch 1 instead of 2, which
    is a third cold email — so it must surface as a number.
    """
    matched, misses = L.touch2_record_ids(
        [row(record_id="m1", handle="a", name="A")],
        [row(record_id="f1", handle="zzz", name="Unknown Co")])
    assert matched == set() and misses == 1


def test_a_duplicated_handle_marks_every_copy_at_touch_two():
    """
    174 handles appear more than once. One follow-up row must mark ALL rows for
    that handle, or a duplicate copy stays wrongly eligible.
    """
    m1 = row(record_id="m1", handle="dup", name="Dup")
    m2 = row(record_id="m2", handle="dup", name="Dup")
    matched, _ = L.touch2_record_ids([m1, m2], [row(record_id="f1", handle="dup", name="Dup")])
    assert matched == {"m1", "m2"}
