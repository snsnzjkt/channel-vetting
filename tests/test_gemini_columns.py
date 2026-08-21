"""
The Gemini verdict reaches the Airtable record — and an ABSENT column is a no-op.

This is the highest-blast-radius path in the whole change and the rev-1 plan
asserted it only in prose: `push_record` sends field names as-is and Airtable
rejects the WHOLE record for one unknown field, so writing an optional column
blind would take down every push to that table, not just the column.

It also pins the two invariants that protect the ~15 existing tests which stub
`get_recent_video_performance` with a helper carrying none of the new keys:
the verifier reads every new field with .get(), and with the feature off the
record is byte-identical to before.
"""
import main
import gemini_verify as gv
from search_zones import ZONE_CORE
from tests.test_csv_injection import _NullBlocklist, _stub_performance, _stub_stats


class _Enricher:
    last_email_type = ""
    last_email_note = ""


class _FakeVerifier:
    """A verifier stand-in — the ladder itself is tested in test_gemini_verify."""

    def __init__(self, judgement):
        self._j = judgement
        self.seen = []

    def judge(self, niche_config, stats, performance, *, flagged):
        self.seen.append(flagged)
        return self._j


def _record(monkeypatch, *, columns, verifier=None, off_target=None):
    monkeypatch.setattr(main, "get_channel_stats", lambda cid: _stub_stats())
    monkeypatch.setattr(main, "get_recent_video_performance",
                        lambda cid, pl: _stub_performance())
    monkeypatch.setattr(main, "channel_age_months", lambda p: 100)
    monkeypatch.setattr(main, "resolve_email_with_source",
                        lambda *a, **k: ("a@b.com", main.EMAIL_SOURCE_REPEATED, None))
    monkeypatch.setattr(main, "table_has_field", lambda table, field: field in columns)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    monkeypatch.setattr(main, "off_target_reason",
                        lambda *a, **k: (off_target, "detail" if off_target else ""))
    return main.process_candidate(
        {"channel_id": "UC1", "channel_title": "Chan", "matched_keywords": []},
        {}, _NullBlocklist(),
        {"min_avg_views": 10_000, "min_channel_age_months": None,
         "allowed_country_codes": ZONE_CORE, "table_name": "tbl"},
        None, _Enricher(), verifier=verifier,
    )


ALL_COLUMNS = {"Relevance State", "Relevance Detail", "Relevance Notes",
               "Verified Video URL"}
SCORED = gv.Judgement(gv.STATE_SCORED, "score 78 (on-niche, 0.90)", notes="ok")


def test_the_verdict_is_written_when_the_columns_exist(monkeypatch):
    record, _ = _record(monkeypatch, columns=ALL_COLUMNS,
                        verifier=_FakeVerifier(SCORED))
    assert record["Relevance State"] == gv.STATE_SCORED
    assert record["Relevance Detail"] == "score 78 (on-niche, 0.90)"
    assert record["Relevance Notes"] == "ok"


def test_nothing_is_written_when_the_columns_do_not_exist_yet(monkeypatch):
    """The Handle rule: an absent column is a no-op, never an outage."""
    record, _ = _record(monkeypatch, columns=set(), verifier=_FakeVerifier(SCORED))
    assert record is not None, "the row must still push"
    for col in ALL_COLUMNS:
        assert col not in record


def test_each_column_is_guarded_independently(monkeypatch):
    """An operator who adds only one of the four gets that one, not a crash."""
    record, _ = _record(monkeypatch, columns={"Relevance State"},
                        verifier=_FakeVerifier(SCORED))
    assert record["Relevance State"] == gv.STATE_SCORED
    assert "Relevance Detail" not in record


def test_no_verifier_means_no_new_keys_at_all(monkeypatch):
    """
    With the feature off the record must be byte-identical to before. This is the
    invariant protecting every existing test that stubs the performance dict.
    """
    record, _ = _record(monkeypatch, columns=ALL_COLUMNS, verifier=None)
    for col in ALL_COLUMNS:
        assert col not in record


def test_the_video_url_is_written_unwrapped(monkeypatch):
    """
    Deliberately NOT csv_safe'd: it always starts with "https://" so it cannot
    begin with a formula prefix. A bare 11-char video ID could legitimately start
    with "-", which IS one — hence a full URL, and hence this test.
    """
    j = gv.Judgement(gv.STATE_RESCUED, "rescued 0.88 (video confirmed)",
                     notes="n", video_url="https://www.youtube.com/watch?v=abc&t=90s",
                     rescued=True)
    record, _ = _record(monkeypatch, columns=ALL_COLUMNS, verifier=_FakeVerifier(j))
    assert record["Verified Video URL"] == "https://www.youtube.com/watch?v=abc&t=90s"
    assert not record["Verified Video URL"].startswith("'")


def test_a_formula_in_the_model_notes_is_neutralised(monkeypatch):
    """
    Relevance Notes is the most attacker-influenced field in the record: model
    prose derived from video a creator fully controls, and models reproduce
    on-screen text faithfully.
    """
    j = gv.Judgement(gv.STATE_SCORED, "=cmd|' /c calc'!A0", notes="=HYPERLINK(\"x\")")
    record, _ = _record(monkeypatch, columns=ALL_COLUMNS, verifier=_FakeVerifier(j))
    assert record["Relevance Notes"].startswith("'")
    assert record["Relevance Detail"].startswith("'")


# --- the rescue actually reverses the drop -------------------------------

def test_a_rescue_reverses_the_title_gate_drop(monkeypatch):
    j = gv.Judgement(gv.STATE_RESCUED, "rescued 0.88 (video confirmed)",
                     notes="n", video_url="https://y/v", rescued=True)
    fake = _FakeVerifier(j)
    record, reason = _record(monkeypatch, columns=ALL_COLUMNS, verifier=fake,
                            off_target=main.DROP_OFF_TARGET)
    assert record is not None, "a confirmed rescue must produce a row"
    assert fake.seen == [True], "the verifier must be told the candidate was flagged"


def test_a_non_rescue_leaves_the_drop_exactly_as_it_was(monkeypatch):
    """
    The safety property, asserted: every non-rescue is byte-identical to the
    pipeline without this feature. Compare against the no-verifier baseline.
    """
    baseline_record, baseline_reason = _record(
        monkeypatch, columns=ALL_COLUMNS, verifier=None,
        off_target=main.DROP_OFF_TARGET)
    record, reason = _record(
        monkeypatch, columns=ALL_COLUMNS, verifier=_FakeVerifier(SCORED),
        off_target=main.DROP_OFF_TARGET)
    assert baseline_record is None and record is None
    assert reason == baseline_reason == main.DROP_OFF_TARGET


def test_an_unavailable_verdict_still_leaves_the_drop_in_place(monkeypatch):
    for detail in ("unavailable (quota_exhausted)", "unavailable (unreachable)",
                   "unavailable (day_cap_reached)", "unavailable (malformed)"):
        j = gv.Judgement(gv.STATE_UNAVAILABLE, detail)
        record, reason = _record(monkeypatch, columns=ALL_COLUMNS,
                                 verifier=_FakeVerifier(j),
                                 off_target=main.DROP_OFF_TARGET)
        assert record is None and reason == main.DROP_OFF_TARGET


def test_an_unflagged_candidate_is_never_dropped_by_a_bad_score(monkeypatch):
    """The broad tier is advisory. An off-niche score must not gate anything."""
    j = gv.Judgement(gv.STATE_SCORED, "score 2 (off-niche, 0.99)", notes="n")
    record, _ = _record(monkeypatch, columns=ALL_COLUMNS, verifier=_FakeVerifier(j),
                        off_target=None)
    assert record is not None
    assert record["Relevance Detail"] == "score 2 (off-niche, 0.99)"


# --- integration: the REAL verifier, wired through process_candidate ------
#
# Everything above uses a _FakeVerifier so the record path is tested in
# isolation. These two use the real GeminiVerifier with only the HTTP layer
# mocked, which is the only way to catch a wiring mistake between the two —
# a signature drift, a keyword that never arrives, or a performance dict whose
# real keys the ladder cannot read.

import json

import gemini_tracker
import pytest


@pytest.fixture
def real_verifier(tmp_path, monkeypatch):
    monkeypatch.setattr(gemini_tracker, "GEMINI_LOG_FILE", str(tmp_path / "gl.json"))
    monkeypatch.setattr(gemini_tracker, "GEMINI_MAX_REQUESTS_PER_DAY", 10_000)
    monkeypatch.setattr(gemini_tracker, "GEMINI_MAX_VIDEO_REQUESTS_PER_DAY", 10_000)
    monkeypatch.setattr(gv, "GEMINI_API_KEY", "test-key")
    return gv.GeminiVerifier("gemini-3.5-flash-lite", str(tmp_path / "c.json"),
                             100, 50, 900, 0.6, 1)


class _Resp:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._p = payload
        self.text = json.dumps(payload)
        self.headers = {}

    def json(self):
        return self._p


def _gemini_body(matches=True, confidence=0.9, on_niche=True, relevance=80):
    inner = {"matches": matches, "confidence": confidence, "on_niche": on_niche,
             "relevance": relevance, "reason": "r",
             "criteria_results": [{"criterion": "c", "matches": matches,
                                   "evidence": "e"}]}
    return {"candidates": [{"finishReason": "STOP",
                            "content": {"parts": [{"text": json.dumps(inner)}]}}],
            "modelVersion": "gemini-3.5-flash-lite",
            "usageMetadata": {"promptTokenCount": 2400}}


NICHE_CRITERIA = {
    "text_criteria": [{"name": "t", "test": "on niche?"}],
    "video_criteria": [{"name": "v", "test": "on camera?"}],
}


def _record_real(monkeypatch, verifier, *, off_target, performance_extra=None):
    """Same harness as _record, but with the real verifier and real niche keys."""
    perf = _stub_performance(**(performance_extra or {}))
    monkeypatch.setattr(main, "get_channel_stats", lambda cid: _stub_stats())
    monkeypatch.setattr(main, "get_recent_video_performance", lambda cid, pl: perf)
    monkeypatch.setattr(main, "channel_age_months", lambda p: 100)
    monkeypatch.setattr(main, "resolve_email_with_source",
                        lambda *a, **k: ("a@b.com", main.EMAIL_SOURCE_REPEATED, None))
    monkeypatch.setattr(main, "table_has_field", lambda t, f: f in ALL_COLUMNS)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    monkeypatch.setattr(main, "off_target_reason",
                        lambda *a, **k: (off_target, "titles look off"))
    niche = {"min_avg_views": 10_000, "min_channel_age_months": None,
             "allowed_country_codes": ZONE_CORE, "table_name": "tbl"}
    niche.update(NICHE_CRITERIA)
    return main.process_candidate(
        {"channel_id": "UC1", "channel_title": "Chan", "matched_keywords": []},
        {}, _NullBlocklist(), niche, None, _Enricher(), verifier=verifier,
    )


def test_real_verifier_rescues_a_flagged_candidate(monkeypatch, real_verifier):
    """
    End to end: the title gate flags it, both tiers confirm, a row comes out.

    The performance dict here carries the REAL keys enrichment now returns, so a
    rename in either direction fails this test rather than silently producing an
    unavailable verdict forever.
    """
    seq = [_Resp(_gemini_body(on_niche=True, confidence=0.9)),
           _Resp(_gemini_body(matches=True, confidence=0.88))]
    monkeypatch.setattr(gv.HTTP, "post",
                        lambda *a, **k: seq.pop(0) if seq else _Resp(_gemini_body()))
    record, _ = _record_real(
        monkeypatch, real_verifier, off_target=main.DROP_OFF_TARGET,
        performance_extra={
            "video_titles": ["a"], "video_descriptions": ["d"],
            "settled_longform": [{"video_id": "vX", "views": 5_000, "duration_s": 900}],
        })
    assert record is not None, "a confirmed rescue must produce a row"
    assert record["Relevance State"] == gv.STATE_RESCUED
    assert "vX" in record["Verified Video URL"]
    assert real_verifier.rescued == 1
    assert real_verifier.requests == 2


def test_real_verifier_reads_a_stub_performance_dict_without_crashing(monkeypatch,
                                                                     real_verifier):
    """
    The reverse-compatibility hazard: ~15 existing tests stub the performance
    dict with a helper carrying NONE of the keys the ladder wants. Every read
    must be .get()-guarded, or those tests KeyError the moment the feature is on.

    _stub_performance() with no overrides is exactly that dict.
    """
    monkeypatch.setattr(gv.HTTP, "post",
                        lambda *a, **k: _Resp(_gemini_body(on_niche=True, confidence=0.9)))
    record, reason = _record_real(monkeypatch, real_verifier, off_target=None)
    assert record is not None, "must not crash on a dict with no video keys"
    assert record["Relevance State"] == gv.STATE_SCORED
    # No settled_longform on the stub, so the video tier must not have been tried.
    assert real_verifier.video_requests == 0
