"""
Gemini relevance verification.

Every Gemini call here is mocked. tests/conftest.py additionally hard-fails any
real HTTP at HTTPAdapter.send, which is the reason this integration is raw REST
rather than the google-genai SDK — an SDK ships its own transport that the guard
cannot see, and a missed mock would spend the operator's real free-tier quota
from a test run.

THE INVARIANT UNDER TEST, above all others: this feature is RESCUE-ONLY. It can
re-admit a candidate the title gate flagged; it can never drop one. Every failure
path must be indistinguishable from the feature not existing.
"""
import json
import os
import subprocess
import sys

import pytest

import config
import gemini_tracker
import gemini_verify as gv


# --- fixtures -------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolate_gemini_state(tmp_path, monkeypatch):
    """
    Point the ledger and cache at tmp_path, and give the run generous caps.

    Patches the names bound IN THE MODULES, not in `config`: both modules do
    `from config import ...`, so those values are copied into their own globals at
    import and patching config afterwards has no effect. conftest.py documents
    this trap for the credit ledger; it applies identically here.

    Without the tmp_path redirect a test run appends its fixture requests to the
    repo's real gemini_log.json and starts refusing live calls — the same way the
    credit suite once wrote 10.14 real credits into the repo's own ledger.
    """
    monkeypatch.setattr(gemini_tracker, "GEMINI_LOG_FILE", str(tmp_path / "gl.json"))
    monkeypatch.setattr(gemini_tracker, "GEMINI_MAX_REQUESTS_PER_DAY", 10_000)
    monkeypatch.setattr(gemini_tracker, "GEMINI_MAX_VIDEO_REQUESTS_PER_DAY", 10_000)
    monkeypatch.setattr(gv, "GEMINI_API_KEY", "test-key")


@pytest.fixture
def verifier(tmp_path):
    return gv.GeminiVerifier(
        model="gemini-3.5-flash-lite",
        cache_path=str(tmp_path / "cache.json"),
        max_requests_per_run=100,
        max_video_requests_per_run=50,
        max_seconds_per_run=900,
        min_confidence=0.6,
        verdict_version=1,
        # The real chain, or the free-model fallback has nowhere to fall to and
        # every quota test would pass for the wrong reason.
        model_chain=("gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
                     "gemini-3.7-flash"),
        min_criteria_ratio=1.0,   # aggregate-only unless a test opts into the ratio
    )


NICHE = {
    "text_criteria": [{"name": "t", "test": "is it on niche"}],
    "video_criteria": [{"name": "v", "test": "is it on camera"}],
}
STATS = {"channel_id": "UC1", "description": "a bio", "channel_title": "Chan"}
PERF = {
    "video_titles": ["a", "b"],
    "video_descriptions": ["d1", "d2"],
    "settled_longform": [
        {"video_id": "vidA", "views": 100, "duration_s": 600},
        {"video_id": "vidB", "views": 50_000, "duration_s": 600},
        {"video_id": "vidC", "views": 900_000, "duration_s": 600},
    ],
}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or json.dumps(payload or {})
        self.headers = {}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def body_for(matches=True, confidence=0.9, on_niche=None, relevance=80,
             finish="STOP", model="gemini-3.5-flash-lite", tokens=2400,
             tier="video"):
    """
    A response body for ONE tier, carrying only that tier's own keys.

    This deliberately does NOT put both `matches` and `on_niche` in every
    payload. It used to, and that masked a real bug: _parse_verdict hardcoded
    "matches", so every well-formed TEXT verdict — which uses `on_niche` — was
    rejected as malformed. The model was answering correctly and the parser was
    throwing the answer away, and the tests could not see it because the fixture
    was more generous than the API. A fixture must be exactly as strict as the
    thing it stands in for.
    """
    if tier == "text":
        inner = {"on_niche": on_niche if on_niche is not None else True,
                 "relevance": relevance, "confidence": confidence,
                 "reason": "because",
                 "criteria_results": [{"criterion": "c", "matches": True,
                                       "evidence": "seen"}]}
    else:
        inner = {"matches": matches, "confidence": confidence, "reason": "because",
                 "criteria_results": [{"criterion": "c", "matches": matches,
                                       "evidence": "seen"}]}
    return {
        "candidates": [{"finishReason": finish,
                        "content": {"parts": [{"text": json.dumps(inner)}]}}],
        "modelVersion": model,
        "usageMetadata": {"promptTokenCount": tokens},
    }


def stub_post(monkeypatch, *responses):
    """Queue responses and record every request body sent."""
    calls = []
    seq = list(responses)

    def _post(url, headers=None, json=None, timeout=None):  # noqa: A002
        calls.append({"url": url, "headers": headers or {}, "body": json})
        return seq.pop(0) if seq else FakeResponse(200, body_for())

    monkeypatch.setattr(gv.HTTP, "post", _post)
    return calls


# --- 1/2. configuration ---------------------------------------------------

def test_allowlist_is_hardcoded_and_current():
    """An operator-overridable allowlist is not an allowlist."""
    assert isinstance(config.GEMINI_FREE_TIER_MODELS, frozenset)
    assert config.GEMINI_MODEL in config.GEMINI_FREE_TIER_MODELS
    # Models first-party guidance calls deprecated must not be reachable.
    for dead in ("gemini-2.5-flash", "gemini-2.5-flash-lite",
                 "gemini-3.6-flash", "gemini-3.5-flash", "gemini-1.5-flash"):
        assert dead not in config.GEMINI_FREE_TIER_MODELS
        assert not gv.model_is_allowed(dead)


@pytest.mark.parametrize("raw", ["", "0", "no", "off", "fasle", "TRUE", "1", "  "])
def test_free_only_survives_a_typo(raw, monkeypatch):
    """
    GEMINI_FREE_ONLY must stay True for anything but the literal "false".

    The repo contains a competing raw `os.getenv(...) == "true"` idiom. Under
    that one, GEMINI_FREE_ONLY=ture evaluates FALSE and silently switches off the
    model allowlist — the exact control this flag exists to hold.
    """
    monkeypatch.setenv("GEMINI_FREE_ONLY", raw)
    assert config.env_flag("GEMINI_FREE_ONLY", default=True) is True


@pytest.mark.parametrize("raw", ["false", "False", "FALSE", " false "])
def test_free_only_is_disabled_only_by_the_literal_false(raw, monkeypatch):
    """env_flag strips and lowercases, so the literal is case-insensitive."""
    monkeypatch.setenv("GEMINI_FREE_ONLY", raw)
    assert config.env_flag("GEMINI_FREE_ONLY", default=True) is False


def test_off_allowlist_model_makes_no_request(monkeypatch, verifier):
    calls = stub_post(monkeypatch)
    verifier.model = "gemini-2.5-flash"
    v = gv.call({"contents": []}, model="gemini-2.5-flash")
    assert v.reason_code == gv.MODEL_NOT_ALLOWED
    assert calls == [], "a rejected model must cost zero HTTP calls"


# --- 3. successful rescue -------------------------------------------------

def test_the_video_tier_alone_rescues_a_flagged_candidate(monkeypatch, verifier):
    """
    Video DECIDES (changed 2026-08-21). It used to be gated behind the text
    tier's on_niche, which made a signal since measured as non-predictive a
    precondition for every rescue.
    """
    calls = stub_post(monkeypatch, FakeResponse(200, body_for(matches=True, confidence=0.88)))
    j = verifier.judge(NICHE, STATS, PERF, flagged=True)
    assert j.rescued is True
    assert j.state == gv.STATE_RESCUED
    assert "rescued" in j.detail
    assert j.video_url.startswith("https://www.youtube.com/watch?v=vidB")
    assert len(calls) == 1, "one VIDEO call, and no text call — the text tier is off"
    assert verifier.rescued == 1
    assert verifier.video_requests == 1


def test_the_video_tier_runs_on_an_unflagged_candidate_too(monkeypatch, verifier):
    """
    video_always: every candidate gets a video-checked verdict, not just the
    rescues. It still cannot drop anything — an unflagged candidate continues
    whatever the verdict says.
    """
    calls = stub_post(monkeypatch, FakeResponse(200, body_for(matches=False, confidence=0.95)))
    j = verifier.judge(NICHE, STATS, PERF, flagged=False)
    assert j.rescued is False, "an unflagged candidate is never dropped by this"
    assert j.state == gv.STATE_SCORED
    assert "did not confirm" in j.detail
    assert j.video_url, "the URL must be recorded so a human can audit it"
    assert len(calls) == 1


def test_video_always_off_restricts_video_to_the_rescue_path(monkeypatch, verifier):
    verifier.video_always = False
    calls = stub_post(monkeypatch, FakeResponse(200, body_for(matches=True, confidence=0.9)))
    j = verifier.judge(NICHE, STATS, PERF, flagged=False)
    assert len(calls) == 0, "no video request for an unflagged candidate"
    assert j.rescued is False
    verifier._cache = {}
    verifier.judge(NICHE, STATS, PERF, flagged=True)
    assert len(calls) == 1, "but the rescue path still gets one"


def test_the_text_tier_is_advisory_and_cannot_block_a_rescue(monkeypatch, verifier):
    """
    With the text tier ON and saying OFF-niche, a confirming video must STILL
    rescue. This is the regression the 2026-08-21 backtest forced: the text
    signal is recorded, never authoritative.
    """
    verifier.text_tier = True
    stub_post(monkeypatch,
              FakeResponse(200, body_for(matches=True, confidence=0.9)),
              FakeResponse(200, body_for(tier="text", on_niche=False, relevance=3)))
    j = verifier.judge(NICHE, STATS, PERF, flagged=True)
    assert j.rescued is True, "an off-niche text score must not veto a confirmed video"
    assert j.relevance == 3, "...but it is still recorded for the reviewer"
    assert "text score 3" in j.detail


# --- 4. failed criteria = today's behaviour -------------------------------

def test_video_saying_no_does_not_rescue(monkeypatch, verifier):
    stub_post(monkeypatch, FakeResponse(200, body_for(matches=False, confidence=0.95)))
    j = verifier.judge(NICHE, STATS, PERF, flagged=True)
    assert j.rescued is False, "a 'no' leaves the existing gate's drop in place"
    assert verifier.rescued == 0


def test_low_confidence_never_rescues(monkeypatch, verifier):
    """
    The confidence floor guards the branch that ACTS.

    Rev 1 had this backwards: it gated the reversible outcome and left the
    destructive one unguarded. Under rescue-only the only acting branch is the
    rescue, so that is what the threshold must hold.
    """
    stub_post(monkeypatch, FakeResponse(200, body_for(matches=True, confidence=0.41)))
    j = verifier.judge(NICHE, STATS, PERF, flagged=True)
    assert j.rescued is False
    assert "below confidence" in j.detail


def test_confident_true_with_no_evidence_does_not_rescue(monkeypatch, verifier):
    """Hostile-QA case: matches=true, confidence 0.0, empty criteria_results."""
    stub_post(monkeypatch, FakeResponse(200, body_for(matches=True, confidence=0.0)))
    assert verifier.judge(NICHE, STATS, PERF, flagged=True).rescued is False


# --- 5. malformed, distinguished -----------------------------------------

@pytest.mark.parametrize("resp,expected", [
    (FakeResponse(200, {"candidates": []}), gv.MALFORMED),
    (FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": "{oops"}]}}]}),
     gv.MALFORMED),
    (FakeResponse(200, {"candidates": [{"finishReason": "MAX_TOKENS"}]}), gv.MAX_TOKENS),
    (FakeResponse(200, {"candidates": [{"finishReason": "SAFETY"}]}), gv.SAFETY_BLOCKED),
])
def test_bad_bodies_get_distinct_named_reasons(monkeypatch, resp, expected):
    """
    MAX_TOKENS and SAFETY stay distinct from generic malformed: the first says
    shorten the prompt, the second is a fact about the creator's video.
    """
    stub_post(monkeypatch, resp)
    assert gv.call({"contents": []}).reason_code == expected


@pytest.mark.parametrize("conf", [1.7, -0.1, "high", True, None])
def test_out_of_range_confidence_is_malformed(monkeypatch, conf):
    inner = {"matches": True, "confidence": conf, "reason": "r", "criteria_results": []}
    stub_post(monkeypatch, FakeResponse(200, {
        "candidates": [{"content": {"parts": [{"text": json.dumps(inner)}]}}]}))
    assert gv.call({"contents": []}).reason_code == gv.MALFORMED


def test_malformed_leaves_the_candidate_alone(monkeypatch, verifier):
    stub_post(monkeypatch, FakeResponse(200, {"candidates": []}))
    j = verifier.judge(NICHE, STATS, PERF, flagged=True)
    assert j.rescued is False and j.state == gv.STATE_UNAVAILABLE


# --- 6. timeout -----------------------------------------------------------

def test_timeout_is_survivable(monkeypatch, verifier):
    import requests

    def _boom(*a, **k):
        raise requests.Timeout("too slow")

    monkeypatch.setattr(gv.HTTP, "post", _boom)
    j = verifier.judge(NICHE, STATS, PERF, flagged=True)
    assert j.rescued is False
    assert verifier.wall is None, "a timeout must not latch the run"


# --- 7/8. rate limit and quota ------------------------------------------

def test_the_same_model_is_never_retried_after_a_429(monkeypatch, verifier):
    """
    THE invariant, stated precisely. Retrying the same model against the same
    wall is the one behaviour this integration must never have. Moving to a
    DIFFERENT free model is the fallback and is allowed — so the assertion is
    about uniqueness, not about the call count.
    """
    calls = stub_post(monkeypatch, *[FakeResponse(429, text="RESOURCE_EXHAUSTED PerDay")] * 9)
    verifier.judge(NICHE, STATS, PERF, flagged=True)
    urls = [c["url"] for c in calls]
    assert len(urls) == len(set(urls)), f"a model was retried: {urls}"
    assert len(urls) <= len(verifier.model_chain), "at most one attempt per free model"


def test_per_day_429_pins_that_model_in_the_ledger(monkeypatch, verifier):
    """
    Pins the MODEL, not the whole day — the other free models have their own
    quotas. The ledger must remember, so a same-day re-run skips it without
    spending a request to rediscover the wall.
    """
    stub_post(monkeypatch, *[FakeResponse(429, text="quota exceeded PerDay limit")] * 6)
    verifier.judge(NICHE, STATS, PERF, flagged=True)
    assert "gemini-3.5-flash-lite" in gemini_tracker.exhausted_models()
    assert gemini_tracker.can_afford(video=True, model="gemini-3.5-flash-lite") is False
    # Whole chain spent in this test, so the run latches too.
    assert verifier.wall == gv.QUOTA_EXHAUSTED


def test_per_minute_429_pauses_but_does_not_latch(monkeypatch, verifier):
    stub_post(monkeypatch, FakeResponse(429, text="PerMinute limit exceeded"))
    verifier.judge(NICHE, STATS, PERF, flagged=True)
    assert verifier.wall is None, "a per-minute limit clears on its own"
    assert verifier.rate_limited_until > 0


def test_after_the_whole_chain_is_spent_no_further_request_is_issued(monkeypatch, verifier):
    calls = stub_post(monkeypatch, *[FakeResponse(429, text="PerDay quota")] * 9)
    verifier.judge(NICHE, STATS, PERF, flagged=True)
    spent = len(calls)
    assert spent == len(verifier.model_chain), "one attempt per free model, then stop"
    for _ in range(3):
        assert verifier.judge(NICHE, STATS, PERF, flagged=True).rescued is False
    assert len(calls) == spent, "zero requests once every free model is spent"


# --- 9/10. cache ---------------------------------------------------------

def test_a_cached_verdict_costs_no_request(monkeypatch, verifier):
    calls = stub_post(monkeypatch, FakeResponse(200, body_for(matches=True, confidence=0.9)))
    assert verifier.judge(NICHE, STATS, PERF, flagged=True).rescued is True
    before = len(calls)
    assert verifier.judge(NICHE, STATS, PERF, flagged=True).rescued is True
    assert len(calls) == before, "second identical judgement must be served from cache"
    assert verifier.cache_hits >= 1


def test_editing_criteria_invalidates_the_cache(monkeypatch, verifier):
    stub_post(monkeypatch, *[FakeResponse(200, body_for(tier="text", on_niche=True, confidence=0.9))] * 2)
    a = verifier._cache_key("text", "UC1", gv.criteria_hash(NICHE["text_criteria"]))
    b = verifier._cache_key("text", "UC1", gv.criteria_hash(
        [{"name": "t", "test": "DIFFERENT"}]))
    assert a != b


def test_criteria_hash_is_stable_across_processes():
    """
    The builtin hash() is salted per process by PYTHONHASHSEED, which would mean
    a different key every run: 100% cache miss, forever, silently, burning the
    day cap with no symptom other than a request count nobody watches.
    """
    code = ("import gemini_verify as g;"
            "print(g.criteria_hash([{'name':'a','test':'b'}]))")
    seen = set()
    for seed in ("0", "1", "12345"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, env=env, cwd=os.getcwd())
        assert out.returncode == 0, out.stderr
        seen.add(out.stdout.strip())
    assert len(seen) == 1, f"criteria hash is not process-stable: {seen}"


def test_an_unwritable_cache_is_not_fatal(verifier):
    verifier.cache_path = "/nonexistent-dir/cache.json"
    verifier._load_cache()
    verifier._cache["k"] = {"ts": 9e9}
    verifier._cache_dirty = True
    verifier.flush_cache()  # must not raise — a cache is an optimisation


# --- 11. non-429 4xx ----------------------------------------------------

@pytest.mark.parametrize("status,text,expected", [
    (403, "permission denied", gv.REQUEST_REJECTED),
    (404, "model not found", gv.REQUEST_REJECTED),
    (400, "unsupported video or youtube URL not accessible", gv.VIDEO_UNAVAILABLE),
])
def test_non_429_4xx_is_named_and_not_retried(monkeypatch, status, text, expected):
    """
    A 4xx here is most often NOT our bug: Gemini's YouTube ingestion returns one
    for an age-restricted, region-blocked, members-only or newly-privated video.
    """
    calls = stub_post(monkeypatch, FakeResponse(status, text=text))
    assert gv.call({"contents": []}).reason_code == expected
    assert len(calls) == 1


def test_three_consecutive_rejections_latch_the_run(monkeypatch, verifier):
    """A stale request field fails identically every time; 100 ERRORs is not observability."""
    stub_post(monkeypatch, *[FakeResponse(400, text="bad field")] * 6)
    for _ in range(3):
        verifier.judge(NICHE, STATS, PERF, flagged=True)
    assert verifier.wall == gv.REQUEST_REJECTED


# --- 12. clip window ----------------------------------------------------

@pytest.mark.parametrize("duration,expected", [
    (600, (150, 175)),   # 25% rule
    (200, (90, 115)),    # the >=90s floor wins
    (181, (90, 115)),    # shortest reachable long-form video
    (1800, (450, 475)),  # matches the live probe
    (25, (0, 25)),       # unreachable, kept as defence-in-depth
    (12, (0, 12)),       # unreachable
    (0, (0, 25)),        # unreachable
    (None, (0, 25)),     # unparseable duration
])
def test_clip_window_boundaries(duration, expected):
    assert gv.clip_window(duration) == expected


def test_offsets_serialise_as_whole_seconds():
    """
    f"{start}s" on a float emits "150.0s", and that string is a cache-key
    component — so a float/int inconsistency between two call paths would
    silently split the cache.
    """
    import re
    body = gv.build_video_request("v", 601, NICHE["video_criteria"])
    vm = body["contents"][0]["parts"][0]["videoMetadata"]
    assert re.fullmatch(r"\d+s", vm["startOffset"]), vm["startOffset"]
    assert re.fullmatch(r"\d+s", vm["endOffset"]), vm["endOffset"]


# --- 13. caps -----------------------------------------------------------

def test_run_cap_stops_requests(monkeypatch, verifier):
    calls = stub_post(monkeypatch, *[FakeResponse(200, body_for(tier="text"))] * 20)
    verifier.max_requests = 2
    # Distinct channels, or the cache would serve four of the five and the cap
    # would never be reached — which is the cache working, not the cap.
    for i in range(5):
        verifier.judge(NICHE, dict(STATS, channel_id=f"UC{i}"), PERF, flagged=False)
    assert len(calls) == 2
    assert verifier._may_request(video=False) == "run_cap_reached"


def test_video_cap_leaves_the_text_tier_working(verifier):
    verifier.video_requests = verifier.max_video_requests
    assert verifier._may_request(video=True) == "video_run_cap_reached"
    assert verifier._may_request(video=False) is None


def test_time_budget_latches(verifier):
    verifier.seconds = verifier.max_seconds + 1
    assert verifier._may_request(video=False) == "time_budget_reached"


def test_day_cap_persists_across_two_verifiers(monkeypatch, tmp_path):
    """
    A second run the same day must top up, not get a fresh allowance — the exact
    bug credit_tracker's docstring was written about.
    """
    monkeypatch.setattr(gemini_tracker, "GEMINI_MAX_REQUESTS_PER_DAY", 2)
    stub_post(monkeypatch, *[FakeResponse(200, body_for(tier="text"))] * 10)
    for _ in range(2):
        v = gv.GeminiVerifier("gemini-3.5-flash-lite", str(tmp_path / "c.json"),
                              100, 50, 900, 0.6, 1)
        v.judge(NICHE, STATS, PERF, flagged=False)
    total, _ = gemini_tracker.requests_today()
    assert total == 2
    v3 = gv.GeminiVerifier("gemini-3.5-flash-lite", str(tmp_path / "c.json"),
                           100, 50, 900, 0.6, 1)
    assert v3._may_request(video=False) == "day_cap_reached"


def test_unreadable_ledger_fails_closed(monkeypatch, tmp_path):
    bad = tmp_path / "corrupt.json"
    bad.write_text("{not json")
    monkeypatch.setattr(gemini_tracker, "GEMINI_LOG_FILE", str(bad))
    assert gemini_tracker.can_afford(video=False) is False
    with pytest.raises(gemini_tracker.GeminiLedgerUnavailable):
        gemini_tracker.assert_readable()


# --- 14. never a paid model --------------------------------------------

def test_no_request_ever_carries_a_billable_feature(monkeypatch, verifier):
    """
    Over every path: allowlisted model, no Search grounding, no paid context
    caching, no Batch endpoint, and no deprecated temperature.
    """
    responses = [
        FakeResponse(200, body_for()), FakeResponse(429, text="PerMinute"),
        FakeResponse(500, text="boom"), FakeResponse(200, {"candidates": []}),
        FakeResponse(400, text="bad"),
    ]
    calls = stub_post(monkeypatch, *responses)
    for _ in range(len(responses)):
        verifier.rate_limited_until = 0.0
        verifier.wall = None
        verifier.judge(NICHE, STATS, PERF, flagged=True)
    assert calls, "the test must actually have issued requests"
    for c in calls:
        assert "batch" not in c["url"].lower()
        assert any(m in c["url"] for m in config.GEMINI_FREE_TIER_MODELS)
        for banned in ("tools", "toolConfig", "cachedContent"):
            assert banned not in c["body"], banned
        assert "temperature" not in c["body"]["generationConfig"]
        assert c["headers"].get("x-goog-api-key"), "key must travel as a header"


def test_a_served_model_off_the_allowlist_latches_off(monkeypatch, verifier):
    """
    modelVersion is the only SERVER-SIDE statement of what actually ran. The
    request-side check proves what we asked for, not what we got.
    """
    stub_post(monkeypatch, FakeResponse(200, body_for(model="gemini-9-ultra-paid")))
    v = gv.call({"contents": []})
    assert v.reason_code == gv.SERVED_MODEL_NOT_ALLOWED
    verifier._record(v, video=False, elapsed=0.1)
    assert verifier.wall == gv.SERVED_MODEL_NOT_ALLOWED


# --- 15/16. the data-plumbing bug --------------------------------------

def test_no_long_form_video_is_survivable(monkeypatch, verifier):
    """
    Reachable, with proof: longform_drop_reason is satisfied by
    count_longform_in_older_videos, which pages BEYOND the fetched window, so a
    channel can clear MIN_LONGFORM_VIDEO_COUNT with zero long-form uploads here.
    """
    stub_post(monkeypatch, *[FakeResponse(200, body_for(tier="text", on_niche=True, confidence=0.9))] * 6)
    base = {k: v for k, v in PERF.items() if k != "settled_longform"}
    cases = [
        base,                                                   # key absent entirely
        dict(base, settled_longform=[]),                         # present but empty
        dict(base, settled_longform=[{"video_id": "", "views": 1, "duration_s": 0}]),
        dict(base, settled_longform=[{"video_id": "v", "views": 1}]),  # no duration
    ]
    for i, p in enumerate(cases):
        verifier._cache = {}
        j = verifier.judge(NICHE, dict(STATS, channel_id=f"UCn{i}"), p, flagged=True)
        assert j.rescued is False, f"case {i}: must not IndexError or rescue"
        assert "no long-form video" in j.detail or j.state == gv.STATE_SCORED


def test_the_median_video_is_picked_not_the_outlier(verifier):
    """
    A channel's max-view upload is its BREAKOUT OUTLIER, frequently the one
    off-niche video the algorithm rewarded. Median is the representative pick.
    """
    pick = verifier._pick_video(PERF)
    assert pick["video_id"] == "vidB"


def test_the_picked_duration_belongs_to_the_picked_video(verifier):
    """
    The bug this whole keyed-record change exists to prevent: enrichment builds
    three per-video lists and NO TWO SHARE AN ORDERING, so zip()-ing any two
    pairs a video with another video's duration — a confident verdict about the
    wrong 25 seconds, with no exception raised.
    """
    perf = {"settled_longform": [
        {"video_id": "short", "views": 10, "duration_s": 200},
        {"video_id": "mid", "views": 500, "duration_s": 1800},
        {"video_id": "long", "views": 9000, "duration_s": 4000},
    ]}
    pick = verifier._pick_video(perf)
    assert (pick["video_id"], pick["duration_s"]) == ("mid", 1800)
    assert gv.clip_window(pick["duration_s"]) == (450, 475)


def test_notes_are_flattened_and_capped():
    """
    csv_safe only inspects value[0], so an embedded newline in a CSV export can
    start a fresh logical line with an unguarded '=' at position 0 of it.
    """
    notes = gv.GeminiVerifier._notes({
        "reason": "line one\nline two\r\n=cmd|' /c calc'!A0",
        "criteria_results": [{"criterion": "c", "matches": True, "evidence": "x" * 5000}],
    })
    assert "\n" not in notes and "\r" not in notes
    assert len(notes) <= 1500


# --- 18. the feature off is byte-identical ------------------------------

def test_disabled_returns_no_verifier(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_ENABLED", False)
    assert gv.GeminiVerifier.from_config() is None


def test_enabled_without_a_key_returns_no_verifier(monkeypatch, caplog):
    """
    A misconfiguration, not a configuration — so it must be loud. "GEMINI_ENABLED
    is set" and "the feature can actually run" are different facts.
    """
    monkeypatch.setattr(config, "GEMINI_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", None)
    import logging
    with caplog.at_level(logging.WARNING):
        assert gv.GeminiVerifier.from_config() is None
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


def test_off_allowlist_model_returns_no_verifier(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_ENABLED", True)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(config, "GEMINI_MODEL", "gemini-2.5-flash")
    assert gv.GeminiVerifier.from_config() is None


def test_a_text_verdict_is_not_rejected_for_lacking_the_video_key(monkeypatch, verifier):
    """
    REGRESSION. _parse_verdict once hardcoded "matches", the VIDEO tier's boolean,
    so every well-formed TEXT verdict — which uses `on_niche` — was thrown away as
    malformed. Three real channels came back "unavailable (malformed)" while the
    model had in fact answered `on_niche: true, relevance: 95, confidence: 0.95`
    with good evidence.

    This asserts the text tier parses a payload carrying ONLY its own key.
    """
    payload = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(
        {"on_niche": True, "relevance": 95, "confidence": 0.95, "reason": "r",
         "criteria_results": [{"criterion": "c", "matches": True, "evidence": "e"}]})}]}}],
        "modelVersion": "gemini-3.5-flash-lite",
        "usageMetadata": {"promptTokenCount": 6403}}
    assert gv._parse_verdict(payload, verdict_key="on_niche").ok is True
    # ...and the video key is still required on the video tier.
    assert gv._parse_verdict(payload, verdict_key="matches").reason_code == gv.MALFORMED


def test_a_text_shaped_reply_does_not_satisfy_the_video_tier(monkeypatch, verifier):
    """Each tier requires its OWN boolean; the shapes are not interchangeable."""
    stub_post(monkeypatch, FakeResponse(200, body_for(tier="text", on_niche=True)))
    j = verifier.judge(NICHE, STATS, PERF, flagged=True)
    assert j.state == gv.STATE_UNAVAILABLE, "a text-shaped reply is not a video verdict"
    assert j.rescued is False


# --- the free-model fallback chain --------------------------------------
#
# Google's free RPD is PER MODEL (measured: gemini-3.5-flash-lite refused at ~106
# requests while the others were untouched), so when one is spent the chain moves
# to the next FREE one. These tests pin that it is free-models-only and that it is
# not the forbidden "retry a 429" behaviour.

def test_a_per_day_429_falls_through_to_the_next_free_model(monkeypatch, verifier):
    calls = stub_post(monkeypatch,
                      FakeResponse(429, text="PerDay quota exceeded"),
                      FakeResponse(200, body_for(matches=True, confidence=0.9)))
    j = verifier.judge(NICHE, STATS, PERF, flagged=True)
    assert len(calls) == 2, "the spent model, then the next free one"
    assert calls[0]["url"] != calls[1]["url"], "a DIFFERENT model, never a retry"
    assert "gemini-3.5-flash-lite" in calls[0]["url"]
    assert "gemini-3.1-flash-lite" in calls[1]["url"]
    assert j.rescued is True, "the fallback produced a usable verdict"
    assert verifier.wall is None, "one spent model must not latch the run"


def test_every_model_in_the_chain_is_free_tier(monkeypatch, verifier):
    """The chain can never contain a paid model, whatever config says."""
    calls = stub_post(monkeypatch, *[FakeResponse(429, text="PerDay")] * 6)
    verifier.judge(NICHE, STATS, PERF, flagged=True)
    assert calls, "must have tried something"
    for c in calls:
        assert any(m in c["url"] for m in config.GEMINI_FREE_TIER_MODELS), c["url"]
    assert len(calls) == len(verifier.model_chain), "each free model tried once, no more"


def test_the_run_latches_only_when_every_free_model_is_spent(monkeypatch, verifier):
    calls = stub_post(monkeypatch, *[FakeResponse(429, text="PerDay")] * 6)
    verifier.judge(NICHE, STATS, PERF, flagged=True)
    assert verifier.wall == gv.QUOTA_EXHAUSTED
    before = len(calls)
    for _ in range(3):
        assert verifier.judge(NICHE, STATS, PERF, flagged=True).rescued is False
    assert len(calls) == before, "zero further requests once the whole chain is spent"


def test_a_per_minute_429_does_not_burn_the_chain(monkeypatch, verifier):
    """
    A per-minute limit clears on its own, so it must PAUSE rather than fall
    through and mark a model spent for the day.
    """
    calls = stub_post(monkeypatch, FakeResponse(429, text="PerMinute limit"))
    verifier.judge(NICHE, STATS, PERF, flagged=True)
    assert len(calls) == 1, "no fallthrough on a per-minute limit"
    assert verifier.models_spent == set(), "no model marked spent for the day"
    assert verifier.wall is None
    assert verifier.rate_limited_until > 0


def test_an_off_allowlist_chain_entry_is_dropped_not_tried(monkeypatch):
    class Cfg:
        GEMINI_MODEL = "gemini-3.5-flash-lite"
        GEMINI_FALLBACK_ENABLED = True
        GEMINI_MODEL_CHAIN = ("gemini-3.1-flash-lite", "gemini-9-ultra-paid")
    chain = gv.GeminiVerifier._build_chain(Cfg)
    assert "gemini-9-ultra-paid" not in chain
    assert set(chain) <= config.GEMINI_FREE_TIER_MODELS


def test_fallback_off_means_a_single_model(monkeypatch):
    class Cfg:
        GEMINI_MODEL = "gemini-3.5-flash-lite"
        GEMINI_FALLBACK_ENABLED = False
        GEMINI_MODEL_CHAIN = ("gemini-3.1-flash-lite", "gemini-3.7-flash")
    assert gv.GeminiVerifier._build_chain(Cfg) == ("gemini-3.5-flash-lite",)


# --- the strictness knob that does not touch the criteria ---------------

def test_partial_criteria_match_confirms_at_the_configured_ratio(monkeypatch, verifier):
    """
    The model's aggregate says NO, but half the criteria matched. At ratio 0.5
    that confirms; at 1.0 it does not. Same criteria text either way.
    """
    payload = body_for(matches=False, confidence=0.9)
    payload["candidates"][0]["content"]["parts"][0]["text"] = json.dumps({
        "matches": False, "confidence": 0.9, "reason": "r",
        "criteria_results": [{"criterion": "a", "matches": True, "evidence": "e"},
                             {"criterion": "b", "matches": False, "evidence": "e"}]})
    verifier.min_criteria_ratio = 0.5
    stub_post(monkeypatch, FakeResponse(200, payload))
    j = verifier.judge(NICHE, STATS, PERF, flagged=True)
    assert j.rescued is True
    assert "partly confirmed" in j.detail and "1/2 criteria" in j.detail

    verifier.min_criteria_ratio = 1.0
    verifier._cache = {}
    stub_post(monkeypatch, FakeResponse(200, payload))
    j = verifier.judge(NICHE, dict(STATS, channel_id="UC2"), PERF, flagged=True)
    assert j.rescued is False, "ratio 1.0 restores aggregate-only strictness"


def test_confidence_still_gates_the_partial_route(monkeypatch, verifier):
    """Loosening the criteria bar must not loosen the conviction bar."""
    payload = body_for(matches=False, confidence=0.2)
    payload["candidates"][0]["content"]["parts"][0]["text"] = json.dumps({
        "matches": False, "confidence": 0.2, "reason": "r",
        "criteria_results": [{"criterion": "a", "matches": True, "evidence": "e"},
                             {"criterion": "b", "matches": True, "evidence": "e"}]})
    verifier.min_criteria_ratio = 0.5
    stub_post(monkeypatch, FakeResponse(200, payload))
    j = verifier.judge(NICHE, STATS, PERF, flagged=True)
    assert j.rescued is False
    assert "below confidence" in j.detail


def test_an_empty_criteria_breakdown_never_confirms(monkeypatch):
    """No evidence at all must not sneak through the ratio route."""
    ok, why = gv.verdict_confirms(
        {"matches": False, "confidence": 1.0, "criteria_results": []}, 0.6, 0.0)
    assert ok is False


# --- required criteria are a veto, not a score ---------------------------

REQ = [{"name": "brand", "required": True, "test": "x"}]


def _breakdown(brand_ok, other_ok, conf=0.9):
    return {"matches": False, "confidence": conf, "criteria_results": [
        {"criterion": "brand", "matches": brand_ok},
        {"criterion": "a", "matches": other_ok},
        {"criterion": "b", "matches": other_ok}]}


def test_a_failed_required_criterion_vetoes_however_good_the_rest_is():
    """
    Measured 2026-08-21: the creator-vs-brand test correctly caught ADAM Audio
    ("a branded watermark throughout and promotional marketing content from a
    manufacturer") and the 0.5 ratio then re-admitted it at 2/3. A manufacturer
    is not two-thirds eligible.
    """
    ok, why = gv.verdict_confirms(_breakdown(False, True), 0.6, 0.5, REQ)
    assert ok is False
    assert "required criterion" in why and "brand" in why


def test_a_required_criterion_also_vetoes_a_true_aggregate():
    """The veto is checked BEFORE the model's own aggregate, not after."""
    payload = _breakdown(False, True)
    payload["matches"] = True
    ok, why = gv.verdict_confirms(payload, 0.6, 0.5, REQ)
    assert ok is False, "the aggregate must not override a failed veto"


def test_passing_the_veto_still_needs_the_content_ratio():
    assert gv.verdict_confirms(_breakdown(True, False), 0.6, 0.5, REQ)[0] is False
    assert gv.verdict_confirms(_breakdown(True, True), 0.6, 0.5, REQ)[0] is True


def test_both_niches_mark_the_brand_criterion_required():
    import niches
    for name, cfg in niches.NICHES.items():
        req = [c for c in cfg["video_criteria"] if c.get("required")]
        assert len(req) == 1, f"{name}: expected exactly one required criterion"
        assert "brand" in req[0]["name"], f"{name}: {req[0]['name']}"
