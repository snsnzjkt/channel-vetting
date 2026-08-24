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


# --- video must never run when a transcript exists, at the PIPELINE level ---

def test_pipeline_level_a_transcript_means_ZERO_video_requests(monkeypatch):
    """
    The operator's constraint, asserted through process_candidate rather than
    through the verifier alone: video analysis is not always on, it runs only
    when the transcript fails.

    The verifier-level test above covers review_transcripts in isolation. This
    one covers the wiring, because a future change to main.py's routing could
    reintroduce an always-on video call without touching gemini_verify at all.
    """
    import main
    from tests.test_csv_injection import _NullBlocklist, _stub_performance, _stub_stats
    from search_zones import ZONE_CORE

    class _Enricher:
        last_email_type = ""
        last_email_note = ""

    monkeypatch.setattr(main, "GEMINI_STAGE2_MODE", "transcript")
    monkeypatch.setattr(gv.transcripts, "fetch",
                        lambda vid, **kw: "We toured the basement media room. " * 20)
    monkeypatch.setattr(main, "get_channel_stats", lambda cid: _stub_stats())
    monkeypatch.setattr(main, "get_recent_video_performance",
                        lambda cid, pl: _stub_performance(
                            settled_longform=[{"video_id": "vX", "views": 5000,
                                               "duration_s": 900}],
                            video_titles=["t"], video_descriptions=["d"]))
    monkeypatch.setattr(main, "channel_age_months", lambda p: 100)
    monkeypatch.setattr(main, "resolve_email_with_source",
                        lambda *a, **k: ("a@b.com", main.EMAIL_SOURCE_REPEATED, None))
    monkeypatch.setattr(main, "table_has_field", lambda t, f: True)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)

    real = gv.GeminiVerifier(
        model="gemini-3.5-flash-lite", cache_path="/dev/null",
        max_requests_per_run=100, max_video_requests_per_run=50,
        max_seconds_per_run=900, min_confidence=0.6, verdict_version=1,
        model_chain=("gemini-3.5-flash-lite",), video_always=True,
    )
    stub_post(monkeypatch, FakeResponse(200, _body(matches=True, conf=0.9)))
    main.process_candidate(
        {"channel_id": "UC1", "channel_title": "Chan", "matched_keywords": []},
        {}, _NullBlocklist(),
        {"min_avg_views": 10_000, "min_channel_age_months": None,
         "allowed_country_codes": ZONE_CORE, "table_name": "tbl",
         "text_criteria": NICHE["text_criteria"],
         "video_criteria": NICHE["video_criteria"],
         "on_target_terms": NICHE["on_target_terms"]},
        None, _Enricher(), verifier=real,
    )
    assert real.video_requests == 0, (
        "a candidate WITH a transcript must cost zero video requests — video "
        "analysis is a fallback, not an always-on stage"
    )


def test_the_startup_banner_does_not_claim_video_runs_on_everything():
    """
    The banner read "video=every candidate" straight off GEMINI_VIDEO_ALWAYS,
    which stopped being true when stage 2 became a transcript review. An operator
    reading that will reasonably conclude the pipeline does something it does not.

    Tests the pure `stage2_banner` rather than driving `from_config`. The first
    version of this test called from_config and read the log, which passed
    locally and FAILED in CI: GEMINI_ENABLED is unset there, so the banner reads
    "DISABLED (...)", which still contains "relevance verification" and slipped
    past the skip guard. A test that needs an API key, a readable ledger and an
    enabled feature to check a sentence is asserting the environment.
    """
    class _Cfg:
        GEMINI_STAGE2_MODE = "transcript"
        GEMINI_TRANSCRIPT_VIDEOS = 2
        GEMINI_VIDEO_FALLBACK = True

    line = gv.stage2_banner(_Cfg, video_always=True)
    assert "TRANSCRIPT of up to 2 video(s)" in line
    assert "FALLBACK ONLY" in line
    assert "every candidate" not in line, (
        "video is a fallback now; a banner claiming otherwise misleads the operator"
    )


def test_the_banner_says_OFF_when_the_fallback_is_disabled():
    class _Cfg:
        GEMINI_STAGE2_MODE = "transcript"
        GEMINI_TRANSCRIPT_VIDEOS = 2
        GEMINI_VIDEO_FALLBACK = False

    assert "video = OFF" in gv.stage2_banner(_Cfg, video_always=True)


def test_the_banner_still_describes_the_legacy_video_mode():
    """The mode is still reachable, so its wording still has to be right."""
    class _Cfg:
        GEMINI_STAGE2_MODE = "video"

    assert "VIDEO clip on every candidate" in gv.stage2_banner(_Cfg, video_always=True)
    assert "the rescue path only" in gv.stage2_banner(_Cfg, video_always=False)


def test_the_shipping_config_produces_the_fallback_wording():
    """Guards the DEFAULTS, not just the function: transcript + fallback on."""
    import config

    line = gv.stage2_banner(config, video_always=True)
    assert "TRANSCRIPT" in line and "FALLBACK ONLY" in line, line
