"""
LAYER 3: video analysis, reached ONLY when layer 2 has no transcript.

The flow is broad metadata sweep -> transcript review -> [no captions?] video
analysis -> manual approval. Roughly one video in three has captions disabled, so
this path is common rather than exceptional, and before it existed those
candidates reached the manager with no stage-2 evidence at all.

What these tests pin: it fires ONLY on a missing transcript, it never fires when
a transcript exists, it is free to arrive at, and the resulting verdict says
which evidence decided it.
"""
import json

import pytest

import config
import gemini_verify as gv
import niches
from tests.test_gemini_verify import FakeResponse, stub_post, verifier  # noqa: F401

NICHE = niches.NICHES["Home Theater"]
PERF = {"settled_longform": [{"video_id": "vid1", "duration_s": 900, "views": 5000}],
        "video_titles": ["a"], "video_descriptions": ["d"]}
STATS = {"channel_id": "UC1", "channel_title": "Chan", "description": ""}


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setattr(gv, "GEMINI_API_KEY", "test-key")


def _body(matches=True, conf=0.9, summary="a summary"):
    return {"candidates": [{"content": {"parts": [{"text": json.dumps({
        "matches": matches, "confidence": conf, "reason": "r",
        "summary": summary,
        "criteria_results": [{"criterion": c["name"], "matches": matches,
                              "evidence": "e"}
                             for c in NICHE["video_criteria"]],
    })}]}, "finishReason": "STOP"}],
        "modelVersion": "gemini-3.5-flash-lite-002",
        "usageMetadata": {"totalTokenCount": 1000}}


# --- it fires only when there is no transcript ---

def test_no_transcript_falls_back_to_video(monkeypatch, verifier):
    monkeypatch.setattr(gv.transcripts, "fetch", lambda vid, **kw: None)
    calls = stub_post(monkeypatch, FakeResponse(200, _body()))
    j = verifier.review_transcripts(NICHE, STATS, PERF, flagged=True)
    assert "layer 3 video fallback" in j.detail
    assert "no transcript" in j.detail
    assert len(calls) == 1, "exactly one request — the video one"
    assert verifier.video_requests == 1, "the fallback IS a video request"


def test_a_transcript_that_EXISTS_never_reaches_the_fallback(monkeypatch, verifier):
    monkeypatch.setattr(gv.transcripts, "fetch",
                        lambda vid, **kw: "We toured the basement media room. " * 20)
    stub_post(monkeypatch, FakeResponse(200, _body()))
    j = verifier.review_transcripts(NICHE, STATS, PERF, flagged=True)
    assert "layer 3" not in j.detail
    assert "transcript" in j.detail
    assert verifier.video_requests == 0, "layer 2 sends no video"


def test_arriving_at_the_fallback_is_FREE(monkeypatch, verifier):
    """
    transcripts.fetch spends no Gemini request when it fails, so a failed layer 2
    costs nothing and the video call is the FIRST spend for that candidate. If
    this ever stops being true the fallback becomes pure waste on ~1/3 of rows.
    """
    monkeypatch.setattr(gv.transcripts, "fetch", lambda vid, **kw: None)
    calls = stub_post(monkeypatch, FakeResponse(200, _body()))
    verifier.review_transcripts(NICHE, STATS, PERF, flagged=True)
    assert len(calls) == 1, f"expected 1 request total, got {len(calls)}"


# --- the verdict says which evidence decided it ---

def test_the_reviewer_can_tell_which_evidence_was_used(monkeypatch, verifier):
    """
    A row whose verdict came from frames because the captions were off must not
    be indistinguishable from one read from a transcript. The manager is the next
    stage and needs to know what the machine actually looked at.
    """
    monkeypatch.setattr(gv.transcripts, "fetch", lambda vid, **kw: None)
    stub_post(monkeypatch, FakeResponse(200, _body()))
    fallback = verifier.review_transcripts(NICHE, STATS, PERF, flagged=True)
    assert "video" in fallback.detail


def test_a_fallback_rescue_still_works_and_is_labelled(monkeypatch, verifier):
    monkeypatch.setattr(gv.transcripts, "fetch", lambda vid, **kw: None)
    stub_post(monkeypatch, FakeResponse(200, _body(matches=True, conf=0.95)))
    j = verifier.review_transcripts(NICHE, STATS, PERF, flagged=True)
    assert j.rescued is True
    assert j.state == gv.STATE_RESCUED
    assert "layer 3" in j.detail


# --- the standing rules survive the extra hop ---

def test_the_fallback_can_be_switched_off(monkeypatch, verifier):
    monkeypatch.setattr(gv, "GEMINI_VIDEO_FALLBACK", False)
    monkeypatch.setattr(gv.transcripts, "fetch", lambda vid, **kw: None)
    calls = stub_post(monkeypatch, FakeResponse(200, _body()))
    j = verifier.review_transcripts(NICHE, STATS, PERF, flagged=True)
    assert j.detail == "no transcript available"
    assert calls == [], "switched off means no request"


def test_no_video_criteria_means_no_fallback(monkeypatch, verifier):
    monkeypatch.setattr(gv.transcripts, "fetch", lambda vid, **kw: None)
    calls = stub_post(monkeypatch, FakeResponse(200, _body()))
    niche = {**NICHE, "video_criteria": []}
    j = verifier.review_transcripts(niche, STATS, PERF, flagged=True)
    assert "no video_criteria" in j.detail
    assert calls == []


def test_a_failing_fallback_never_drops_the_candidate(monkeypatch, verifier):
    """
    Rescue-only survives the extra hop: every failure edge leaves rescued False,
    which leaves the candidate with whatever the existing gates gave it.
    """
    monkeypatch.setattr(gv.transcripts, "fetch", lambda vid, **kw: None)
    for resp in (FakeResponse(500, text="boom"),
                 FakeResponse(400, text="video not accessible"),
                 FakeResponse(200, {"candidates": []})):
        verifier.wall = None
        stub_post(monkeypatch, resp)
        j = verifier.review_transcripts(NICHE, STATS, PERF, flagged=True)
        assert j.rescued is False, f"{resp} must not rescue"


def test_no_sampled_video_means_neither_layer_runs(verifier):
    j = verifier.review_transcripts(NICHE, STATS, {"settled_longform": []},
                                    flagged=True)
    assert "no long-form video" in j.detail


def test_the_video_share_of_the_run_stays_inside_its_own_ceiling():
    """
    Sanity on the budget the fallback assumes: ~1/3 of ~61 candidates falling
    back is ~20 video requests, and the video run cap is 30.
    """
    assert config.GEMINI_MAX_VIDEO_REQUESTS_PER_RUN >= 25, (
        "the layer 3 fallback sizes at ~20 video requests per run; a video run "
        "cap below that silently starves it"
    )
