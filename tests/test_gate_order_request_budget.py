"""
A candidate that fails a FREE gate must never cost a paid Gemini request.

This pins the ordering established 2026-08-24 (R0): `pre_push_drop_reason` runs
ABOVE the Gemini block, not below it. Before the move, 108 of the 169 candidates
that reached the Gemini block on 2026-08-24 (73%) were dropped immediately
afterwards on arithmetic that was already free and in hand — each having spent a
request that could not then be spent on a candidate whose verdict was still open.

The invariant is about BUDGET, not about verdicts: nothing here changes which
candidates are dropped or why. It only pins that the drop happens before the
spend. Without this test the ordering is one innocuous-looking edit away from
regressing, and the regression is invisible — the row count is identical and the
only symptom is a request counter nobody watches.
"""
import main
import gemini_verify as gv
from search_zones import ZONE_CORE
from tests.test_csv_injection import _NullBlocklist, _stub_performance, _stub_stats


class _Enricher:
    last_email_type = ""
    last_email_note = ""


class _CountingVerifier:
    """Records every judge() call so a test can assert there were none."""

    def __init__(self):
        self.calls = []

    def judge(self, niche_config, stats, performance, *, flagged):
        self.calls.append(flagged)
        return gv.Judgement(gv.STATE_SCORED, "stub", notes="")


NICHE = {"min_avg_views": 10_000, "min_channel_age_months": None,
         "allowed_country_codes": ZONE_CORE, "table_name": "tbl"}


def _run(monkeypatch, verifier, _stats=None, **performance_overrides):
    stats = _stats if _stats is not None else _stub_stats()
    monkeypatch.setattr(main, "get_channel_stats", lambda cid: stats)
    monkeypatch.setattr(main, "get_recent_video_performance",
                        lambda cid, pl: _stub_performance(**performance_overrides))
    monkeypatch.setattr(main, "channel_age_months", lambda p: 100)
    monkeypatch.setattr(main, "resolve_email_with_source",
                        lambda *a, **k: ("a@b.com", main.EMAIL_SOURCE_REPEATED, None))
    monkeypatch.setattr(main, "table_has_field", lambda table, field: True)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    # The keyword gate is not what this test is about — hold it neutral so the
    # only thing deciding the outcome is the free numeric gate.
    monkeypatch.setattr(main, "off_target_reason", lambda *a, **k: (None, ""))
    return main.process_candidate(
        {"channel_id": "UC1", "channel_title": "Chan", "matched_keywords": []},
        {}, _NullBlocklist(), NICHE, None, _Enricher(), verifier=verifier,
    )


# The three free gates that were firing AFTER the paid request, with the drop
# counts they produced across both 2026-08-24 runs.
def test_below_view_minimum_costs_no_gemini_request(monkeypatch):
    """79 of the 124 wasted requests. avg_views under the niche floor."""
    v = _CountingVerifier()
    record, reason = _run(monkeypatch, v, avg_views=100)
    assert record is None
    assert reason == "below_view_minimum"
    assert v.calls == [], "a free view-floor drop must not spend a Gemini request"


def test_shorts_only_costs_no_gemini_request(monkeypatch):
    """20 of the 108."""
    v = _CountingVerifier()
    record, reason = _run(monkeypatch, v, shorts_only=True)
    assert record is None
    assert reason == "shorts_only"
    assert v.calls == [], "a free shorts-only drop must not spend a Gemini request"


def test_too_few_videos_costs_no_gemini_request(monkeypatch):
    """7 of the 108. video_count arrives on stats, not on performance."""
    v = _CountingVerifier()
    record, reason = _run(monkeypatch, v, _stats=_stub_stats(video_count=1))
    assert record is None
    assert reason == main.DROP_TOO_FEW_VIDEOS
    assert v.calls == [], "a free video-count drop must not spend a Gemini request"


def test_the_longform_floor_is_DELIBERATELY_still_below_the_gemini_block(monkeypatch):
    """
    NOT part of R0, and this test exists so nobody "fixes" it by accident.

    `longform_drop_reason` is split out of `pre_push_drop_reason` precisely
    because establishing the count can cost quota
    (`enrichment.count_longform_in_older_videos`), so it must run after every
    FREE check. It therefore still sits below the Gemini block, and still spent
    16 requests across the two 2026-08-24 runs.

    Moving it up would trade YouTube quota (3,580 of 10,000 used) for Gemini
    requests (78 of ~80) — probably the right trade, but it is a different
    decision with a different cost, and it has not been reviewed. Until it is,
    this asserts the current, deliberate placement.
    """
    v = _CountingVerifier()
    record, reason = _run(monkeypatch, v, longform_count=0, duration_sample_size=50)
    assert record is None
    assert reason == main.DROP_TOO_FEW_LONGFORM
    assert v.calls == [False], (
        "the long-form floor is intentionally below the Gemini block because it "
        "can cost quota to establish — see longform_drop_reason's docstring"
    )


def test_non_english_costs_no_gemini_request(monkeypatch):
    """2 of the 108, and the cheapest check of all."""
    v = _CountingVerifier()
    record, reason = _run(monkeypatch, v, content_language="de")
    assert record is None
    assert v.calls == [], "a free language drop must not spend a Gemini request"


def test_a_candidate_that_clears_every_free_gate_still_reaches_the_verifier(monkeypatch):
    """
    The control, and the half of the invariant that stops the "fix" from being
    "never call Gemini". A candidate whose verdict is genuinely still open must
    reach the tier — that is what the reclaimed budget is FOR.
    """
    v = _CountingVerifier()
    record, reason = _run(monkeypatch, v)
    assert record is not None, f"expected a pushed record, got drop {reason!r}"
    assert v.calls == [False], "an unflagged surviving candidate is judged exactly once"


def test_every_free_gate_runs_before_every_paid_call_in_source_order():
    """
    Belt and braces on the ordering, and it is deliberately written as ALL free
    gates before ALL paid calls rather than as one named pair.

    The narrow version of this test compared `pre_push_drop_reason` against
    `verifier.judge` only. When the topic gate gained a second paid call
    (`verifier.confirm_topic`) that landed BETWEEN them, the test still passed
    while a paid Gemini request had moved ahead of a free numeric gate — the
    exact defect R0 existed to fix. A guard naming one paid call cannot see a new
    one appear, so this names the free gates and requires every paid call to
    follow all of them.

    Add a paid call to process_candidate and it must be added here too. That is
    the point: the list is a decision record, not a convenience.
    """
    import inspect

    src = inspect.getsource(main.process_candidate)

    free_gates = {
        "off_target_reason(": "keyword relevance, reads titles already fetched",
        "pre_push_drop_reason(": "views/shorts/cadence, all free from stats",
        "video_topics.topic_evidence(": "creator tags, already on the response",
    }
    paid_calls = {
        "verifier.confirm_topic(": "one Gemini request, topic confirmation",
        "verifier.judge(": "one Gemini request, relevance tier",
        "resolve_email_with_source(": "0.20 vendor credits",
    }

    for gate in free_gates:
        assert gate in src, f"free gate {gate} vanished from process_candidate"
    for call in paid_calls:
        assert call in src, f"paid call {call} vanished from process_candidate"

    last_free = max(src.index(g) for g in free_gates)
    first_paid = min(src.index(c) for c in paid_calls)
    latest_free = next(g for g in free_gates if src.index(g) == last_free)
    earliest_paid = next(c for c in paid_calls if src.index(c) == first_paid)

    assert last_free < first_paid, (
        f"{earliest_paid} (paid: {paid_calls[earliest_paid]}) runs BEFORE "
        f"{latest_free} (free: {free_gates[latest_free]}). Every free gate must "
        f"precede every paid call — a candidate the free gates discard must not "
        f"cost a request on the way out. See the comment on the pre-push gate."
    )


def test_the_topic_confirmation_call_is_the_first_paid_call_after_the_free_gates():
    """
    Ordering AMONG the paid calls also matters, in one specific way: topic
    confirmation can drop a candidate outright, so running it before the
    relevance tier saves that tier's request on the ~2% it removes. The reverse
    order would spend both.
    """
    import inspect

    src = inspect.getsource(main.process_candidate)
    assert src.index("verifier.confirm_topic(") < src.index("verifier.judge("), (
        "topic confirmation can drop a candidate, so it must run before the "
        "relevance tier — otherwise both requests are spent on a row that is "
        "about to be removed"
    )
