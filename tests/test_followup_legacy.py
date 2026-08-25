"""
Tests for followup/legacy.py + followup/categorizer.py.

These assert through `categorize_population()`, which is the ONE rule. An earlier
version of this file tested a separate `free_screen()` that computed its own date
floor and touch count — a second implementation of "may this creator be
re-contacted" living beside `followup_eligibility()`. That function is gone and
these tests are the reason it cannot come back: they pin the behaviour to the
delegated path.
"""
from datetime import datetime, timedelta, timezone

import pytest

from channel_vetting.followup import categorizer as C
from channel_vetting.followup import legacy as L

NOW = datetime(2026, 8, 26, tzinfo=timezone.utc)
FLOOR, TOUCHES = 180, 2


class FakeBlocklist:
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


def cat(main, follow=(), bl=None, key="foo", **kw):
    out = L.categorize_population(list(main), list(follow), bl or FakeBlocklist(),
                                  now=NOW, floor_days=FLOOR, max_touches=TOUCHES, **kw)
    return out[key]


# --- the single-rule guarantee -----------------------------------------------

def test_free_screen_is_gone_so_the_rule_cannot_diverge():
    assert not hasattr(L, "free_screen")
    assert not hasattr(L, "FREE_BUCKETS")


def test_the_categoriser_delegates_rather_than_recomputing():
    """
    No date arithmetic and no touch counting in the categoriser: those come from
    followup_eligibility() via REASON_TO_CATEGORY.
    """
    import inspect
    src = inspect.getsource(C.categorize)
    for forbidden in ("timedelta", "days", "min_days", "max_touches", "len(prior"):
        assert forbidden not in src, f"categorize() recomputes {forbidden!r}"


def test_every_ledger_refusal_reason_maps_to_a_category():
    from channel_vetting.outreach import ledger as LG
    reasons = {LG.REASON_NO_PRIOR_SEND, LG.REASON_MAX_TOUCHES, LG.REASON_REPLIED,
               LG.REASON_REPLY_STATE_UNKNOWN, LG.REASON_TOO_SOON, LG.REASON_NOT_QUALIFIED}
    assert reasons <= set(C.REASON_TO_CATEGORY)


def test_an_unmapped_reason_raises_instead_of_falling_through_to_eligible():
    from channel_vetting.outreach.ledger import FollowUpVerdict
    with pytest.raises(ValueError, match="unmapped"):
        C.categorize(FollowUpVerdict(False, "some_new_reason"),
                     C.Signals(handle="foo"))


def test_every_category_is_in_the_ordered_vocabulary():
    for name, val in vars(C).items():
        if name.startswith("CAT_"):
            assert val in C.CATEGORIES, f"{name} missing from CATEGORIES"


# --- refusal ORDER -----------------------------------------------------------

def test_dnc_outranks_every_other_refusal():
    c, r = cat([row(handle="blocked", email="", mail_sent=False, date="")],
               bl=FakeBlocklist(handles=["blocked"]), key="blocked")
    assert c == C.CAT_DNC_BLOCKED
    assert "no action needed" in r


def test_dnc_matches_on_handle_alone():
    """
    MEASURED: of 560 suppressed legacy creators, 476 match by handle and only 84
    by email. An email-only check misses 85% of them.
    """
    c, _ = cat([row(handle="onlyhandle", email="notlisted@x.com")],
               bl=FakeBlocklist(handles=["onlyhandle"]), key="onlyhandle")
    assert c == C.CAT_DNC_BLOCKED


def test_dnc_matches_on_name_alone():
    c, _ = cat([row(name="Suppressed Co")], bl=FakeBlocklist(names=["suppressed co"]))
    assert c == C.CAT_DNC_BLOCKED


def test_a_dead_channel_outranks_the_touch_ceiling():
    c, _ = cat([row()], [row(record_id="f1")], activity={"foo": False})
    assert c == C.CAT_INACTIVE


# --- delegated refusals ------------------------------------------------------

def test_no_prior_send_when_mail_sent_is_unticked():
    c, _ = cat([row(mail_sent=False)])
    assert c == C.CAT_NO_PRIOR_SEND


def test_touch_limit_comes_from_the_ledger_not_from_a_local_count():
    c, r = cat([row()], [row(record_id="f1")], activity={"foo": True})
    assert c == C.CAT_TOUCH_LIMIT
    assert "ceiling" in r or "send(s) already" in r


def test_a_row_inside_the_floor_is_not_yet_eligible():
    c, _ = cat([row(date="2026-08-01")], activity={"foo": True})
    assert c == C.CAT_NOT_YET


def test_boundary_at_the_floor():
    older = (NOW - timedelta(days=181)).strftime("%Y-%m-%d")
    newer = (NOW - timedelta(days=179)).strftime("%Y-%m-%d")
    assert cat([row(date=older)], activity={"foo": True})[0] != C.CAT_NOT_YET
    assert cat([row(date=newer)], activity={"foo": True})[0] == C.CAT_NOT_YET


def test_an_unreadable_date_refuses_rather_than_becoming_eligible():
    for bad in ["", "not-a-date", "2023-13-45"]:
        c, _ = cat([row(date=bad)], activity={"foo": True})
        assert c != C.CAT_FOLLOW_UP, bad


# --- the unknowns never read as eligible -------------------------------------

def test_no_reply_history_is_reply_unknown_not_follow_up_needed():
    """
    The legacy tables have no Reply State column, so 'did not reply' is unproven.
    """
    c, r = cat([row()], activity={"foo": True}, relevance={"foo": True})
    assert c == C.CAT_REPLY_UNKNOWN
    assert "unproven" in r


def test_unchecked_activity_is_its_own_bucket_not_eligible():
    c, r = cat([row()], relevance={"foo": True})
    assert c != C.CAT_FOLLOW_UP
    assert "not a judgement" in r or "unproven" in r


def test_follow_up_needed_requires_positive_proof_on_every_axis():
    """Reachable only when reply, activity and relevance are all established."""
    from channel_vetting.outreach.ledger import FollowUpVerdict
    v = FollowUpVerdict(True, "granted", touch_number=2, detail="1140d since touch 1")
    c, _ = C.categorize(v, C.Signals(handle="foo", reply_known=True,
                                     channel_alive=True, relevant=True))
    assert c == C.CAT_FOLLOW_UP
    # remove any one axis and it is no longer actionable
    for kw in ({"reply_known": False}, {"channel_alive": None}, {"relevant": None},
               {"relevant": False}):
        base = {"handle": "foo", "reply_known": True, "channel_alive": True,
                "relevant": True}
        base.update(kw)
        assert C.categorize(v, C.Signals(**base))[0] != C.CAT_FOLLOW_UP, kw


# --- monotonicity ------------------------------------------------------------

@pytest.mark.parametrize("terminal", sorted(C.TERMINAL))
def test_a_terminal_category_is_never_demoted_to_follow_up_needed(terminal):
    """
    Idempotence is not the property that matters: the INPUTS change every run, so
    a half-failed read could otherwise re-open someone already filed as
    suppressed or replied.
    """
    from channel_vetting.outreach.ledger import FollowUpVerdict
    v = FollowUpVerdict(True, "granted", detail="ok")
    c, r = C.categorize(v, C.Signals(handle="foo", reply_known=True,
                                     channel_alive=True, relevant=True),
                        previous=terminal)
    assert c == terminal
    assert "terminal" in r


def test_a_non_terminal_previous_category_does_not_stick():
    from channel_vetting.outreach.ledger import FollowUpVerdict
    v = FollowUpVerdict(True, "granted", detail="ok")
    c, _ = C.categorize(v, C.Signals(handle="foo", reply_known=True,
                                     channel_alive=True, relevant=True),
                        previous=C.CAT_ACTIVITY_UNKNOWN)
    assert c == C.CAT_FOLLOW_UP


# --- the touch-2 join -------------------------------------------------------

def test_join_on_handle():
    m, f = row(record_id="m1", handle="samehandle"), row(record_id="f1", handle="samehandle")
    matched, misses = L.touch2_record_ids([m], [f])
    assert matched == {"m1"} and misses == 0


def test_join_falls_back_to_name_when_the_handle_is_missing():
    m = row(record_id="m1", handle="newhandle", name="New Record Day")
    f = row(record_id="f1", handle="", name="new record  day")
    matched, misses = L.touch2_record_ids([m], [f])
    assert matched == {"m1"} and misses == 0


def test_an_unjoinable_followup_row_is_counted_not_swallowed():
    matched, misses = L.touch2_record_ids(
        [row(record_id="m1", handle="a", name="A")],
        [row(record_id="f1", handle="zzz", name="Unknown Co")])
    assert matched == set() and misses == 1


def test_a_duplicated_handle_marks_every_copy_at_touch_two():
    """174 handles are duplicated; one follow-up row must mark all copies."""
    rows = [row(record_id="m1", handle="dup"), row(record_id="m2", handle="dup")]
    matched, _ = L.touch2_record_ids(rows, [row(record_id="f1", handle="dup")])
    assert matched == {"m1", "m2"}


def test_one_verdict_per_handle_even_with_duplicate_rows():
    out = L.categorize_population(
        [row(record_id="m1", handle="dup"), row(record_id="m2", handle="dup")],
        [], FakeBlocklist(), now=NOW, floor_days=FLOOR, max_touches=TOUCHES)
    assert list(out) == ["dup"]


def test_a_handle_less_row_still_gets_a_bucket():
    out = L.categorize_population([row(record_id="m9", handle="")], [], FakeBlocklist(),
                                  now=NOW, floor_days=FLOOR, max_touches=TOUCHES)
    assert out["rec:m9"][0] == C.CAT_UNRESOLVABLE


# --- the store ---------------------------------------------------------------

def test_the_legacy_store_refuses_to_be_used_for_writes():
    """It must never reach claim() — the legacy tables are not the Outreach Log."""
    store = L.LegacyLedgerStore([row()], [])
    for meth in ("find_by_key", "create_claim", "patch", "count_claimed_on", "find_stranded"):
        with pytest.raises(NotImplementedError):
            getattr(store, meth)("x")


def test_the_store_emits_one_sent_row_per_recorded_touch():
    store = L.LegacyLedgerStore([row()], [])
    assert len(store.find_sent_for_channel("foo")) == 1
    store2 = L.LegacyLedgerStore([row()], [row(record_id="f1")])
    assert len(store2.find_sent_for_channel("foo")) == 2


def test_an_unsent_row_contributes_no_touch():
    store = L.LegacyLedgerStore([row(mail_sent=False)], [])
    assert store.find_sent_for_channel("foo") == []


def test_needs_paid_signal_selects_only_the_unknown_buckets():
    cats = {"a": (C.CAT_REPLY_UNKNOWN, ""), "b": (C.CAT_DNC_BLOCKED, ""),
            "c": (C.CAT_ACTIVITY_UNKNOWN, ""), "d": (C.CAT_TOUCH_LIMIT, ""),
            "e": (C.CAT_FOLLOW_UP, "")}
    assert sorted(L.needs_paid_signal(cats)) == ["a", "c"]
