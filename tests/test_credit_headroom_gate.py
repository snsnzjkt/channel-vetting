"""
A creator with no daily headroom must not cost an email credit.

The leak this pins: push_until_full decided the bucket AFTER build_record
returned, so the 0.2-credit influencers.club lookup inside process_candidate was
already paid for a record that was then dropped at the bucket check. It is not a
hypothetical shape — both live niche tables are 100% "Qualified", so
DAILY_FLAGGED_CAP never fills and the flagged hunt keeps handing candidates to
build_record after the qualified budget is full. Measured before the fix: 8
candidates offered, 8 lookups paid, 1 row written.

The fix is a `has_room(qualification)` probe passed to a two-argument
build_record, checked in process_candidate between qualification (free — channel
age, already fetched) and the email chain (paid).
"""
import inspect

from channel_vetting import pipeline
from channel_vetting.ranking.scoring import QUALIFIED


def _spy_builder(paid, *, qualification=QUALIFIED, honour_probe=True):
    """A build_record whose 'payment' is recorded, mirroring process_candidate's
    order: qualify first, then spend."""
    def build(candidate, has_room):
        if honour_probe and not has_room(qualification):
            return None, pipeline.DROP_NO_HEADROOM
        paid.append(candidate["id"])
        return {"Channel ID": candidate["id"]}, qualification
    return build


def _run(monkeypatch, builder, **kwargs):
    pushed = []
    monkeypatch.setattr(pipeline, "push_record", lambda t, rec: (pushed.append(rec["Channel ID"]) or True))
    counts = pipeline.push_until_full(
        [{"id": f"UC{i}"} for i in range(8)], builder, "tbl", **kwargs
    )
    return pushed, counts


def test_no_email_credit_is_spent_once_the_qualified_bucket_is_full(monkeypatch):
    """The exact live shape: qualified full, flagged open, everything qualifies."""
    paid = []
    pushed, counts = _run(
        monkeypatch, _spy_builder(paid),
        qualified_headroom=1, flagged_headroom=10, flagged_possible=True,
    )
    assert pushed == ["UC0"]
    # One lookup for the one row. Before the fix this was 8.
    assert paid == ["UC0"], f"paid for {len(paid)} lookups to write {len(pushed)} row(s)"
    assert counts["qualified"] == 1


def test_the_probe_reflects_rows_pushed_earlier_in_the_same_batch(monkeypatch):
    """
    has_room must read the LIVE counts, not a figure captured before the batch —
    that is why it is a callback and not a number. With room for 3, exactly 3
    lookups are paid for.
    """
    paid = []
    pushed, _ = _run(
        monkeypatch, _spy_builder(paid),
        qualified_headroom=3, flagged_headroom=0, flagged_possible=False,
    )
    assert len(pushed) == 3
    assert len(paid) == 3


def test_a_flagged_candidate_is_still_paid_for_while_flagged_room_remains(monkeypatch):
    """
    The gate is per-BUCKET, not global. A flagged row is exactly what the hunt is
    for, so it must not be starved by the qualified budget being full.
    """
    paid = []
    pushed, counts = _run(
        monkeypatch, _spy_builder(paid, qualification="New Channel"),
        qualified_headroom=0, flagged_headroom=2, flagged_possible=True,
    )
    assert counts["flagged"] == 2
    assert len(pushed) == 2
    assert len(paid) == 2


def test_the_post_build_bucket_check_survives_as_a_backstop(monkeypatch):
    """
    A one-argument build_record gets no probe (an external caller, or a test), so
    push_until_full must still refuse to overfill a bucket. The optimisation is
    allowed to be absent; the cap is not.
    """
    paid = []

    def build_one_arg(candidate):
        paid.append(candidate["id"])
        return {"Channel ID": candidate["id"]}, QUALIFIED

    pushed, counts = _run(
        monkeypatch, build_one_arg,
        qualified_headroom=1, flagged_headroom=10, flagged_possible=True,
    )
    assert counts["qualified"] == 1
    assert len(pushed) == 1          # the cap held
    assert len(paid) > len(pushed)   # and this is the leak the probe closes


def test_process_candidate_checks_headroom_before_the_email_chain():
    """
    Order is the whole feature. If has_room were consulted after
    resolve_email_with_source the credit would already be spent, so pin the
    positions in the source rather than trusting the comment.
    """
    src = inspect.getsource(pipeline.process_candidate)
    assert src.index("has_room(qualification)") < src.index("resolve_email_with_source"), (
        "the headroom probe must run BEFORE the paid email chain"
    )


def test_process_candidate_without_a_probe_keeps_the_old_behaviour():
    """has_room is optional and defaults to off."""
    assert inspect.signature(pipeline.process_candidate).parameters["has_room"].default is None
