"""
The topic gate inside process_candidate: advisory by default, drop when armed.

The split being pinned here is the whole safety design. The evidence is computed
and recorded on EVERY candidate; only the DROP is behind VIDEO_TOPIC_GATE. That
is the pattern the Gemini text tier uses, and it exists because three relevance
criteria in this pipeline have been caught pointing the wrong way — so a new one
gets measured in production as advisory before it is allowed to remove a row.
"""
from channel_vetting import pipeline
from channel_vetting.discovery import niches
from channel_vetting.verification import gemini as gv
from channel_vetting.discovery.search_zones import ZONE_CORE
from tests.test_csv_injection import _NullBlocklist, _stub_performance, _stub_stats


class _Enricher:
    last_email_type = ""
    last_email_note = ""


class _StubVerifier:
    """
    Stands in for layer 2. Records what it was asked so a test can prove the
    call is made only on a layer-1 hit, and with a READABLE topic label.
    """

    def __init__(self, confirmed=True, detail="stub", spoken="they discuss it"):
        self._v = gv.TopicVerdict(confirmed, detail, spoken=spoken)
        self.asked = []

    def confirm_topic(self, topic, terms, performance):
        self.asked.append((topic, tuple(terms)))
        return self._v

    def judge(self, niche_config, stats, performance, *, flagged):
        return gv.Judgement(gv.STATE_SCORED, "stub", notes="")

    def review_transcripts(self, niche_config, stats, performance, *, flagged):
        return self.judge(niche_config, stats, performance, flagged=flagged)


NICHE = {"min_avg_views": 10_000, "min_channel_age_months": None,
         "allowed_country_codes": ZONE_CORE, "table_name": "tbl"}

# 5 of 5 tags are Lego, i.e. share 1.0 — well over the 0.40 default.
LEGO_TAGS = ["lego", "lego moc", "minifigure", "brickheadz", "bricklink"]
# The measured-harmful category: present so a test can prove it is ignored.
PHONE_TAGS = ["iphone review", "android phone", "smartphone camera", "pixel phone"]


def _run(monkeypatch, *, tags=None, gate=False, categories=None, share=0.40,
         verifier=None):
    monkeypatch.setattr(pipeline, "get_channel_stats", lambda cid: _stub_stats())
    monkeypatch.setattr(pipeline, "get_recent_video_performance",
                        lambda cid, pl: _stub_performance(video_tags=tags or [],
                                                          video_category_ids=["20"]))
    monkeypatch.setattr(pipeline, "channel_age_months", lambda p: 100)
    monkeypatch.setattr(pipeline, "resolve_email_with_source",
                        lambda *a, **k: ("a@b.com", pipeline.EMAIL_SOURCE_REPEATED, None))
    monkeypatch.setattr(pipeline, "table_has_field", lambda table, field: True)
    monkeypatch.setattr(pipeline.time, "sleep", lambda s: None)
    monkeypatch.setattr(pipeline, "off_target_reason", lambda *a, **k: (None, ""))
    monkeypatch.setattr(pipeline, "VIDEO_TOPIC_GATE", gate)
    monkeypatch.setattr(pipeline, "VIDEO_TOPIC_MIN_SHARE", share)
    if categories is not None:
        monkeypatch.setattr(pipeline, "VIDEO_TOPIC_CATEGORIES", categories)
    return pipeline.process_candidate(
        {"channel_id": "UC1", "channel_title": "Chan", "matched_keywords": []},
        {}, _NullBlocklist(), NICHE, None, _Enricher(), verifier=verifier,
    )


# --- default posture: advisory, never a drop ---

def test_a_dominant_topic_does_NOT_drop_while_the_gate_is_off(monkeypatch, caplog):
    """The default. Five Lego tags out of five, and the row still ships."""
    import logging
    caplog.set_level(logging.INFO)
    record, reason = _run(monkeypatch, tags=LEGO_TAGS, gate=False)
    assert record is not None, f"advisory mode must not drop; got {reason!r}"
    assert "TOPIC ADVISORY" in caplog.text
    assert "toys_and_kids" in caplog.text


def test_the_same_candidate_IS_dropped_once_the_gate_is_armed_AND_content_confirms(
        monkeypatch):
    """Both layers must agree. Tags propose; content decides."""
    v = _StubVerifier(confirmed=True)
    record, reason = _run(monkeypatch, tags=LEGO_TAGS, gate=True, verifier=v)
    assert record is None
    assert reason == pipeline.DROP_OFF_TOPIC_TAGS
    assert len(v.asked) == 1, "layer 2 runs exactly once on a layer-1 hit"


# --- fail-open: layer 2 must be able to overturn layer 1, never the reverse ---

def test_content_overturning_the_tags_KEEPS_the_candidate(monkeypatch):
    """
    The point of a confirmation layer. Tags are the creator's own claim; when the
    video does not back it up, the row survives.
    """
    v = _StubVerifier(confirmed=False, detail="content says NOT toys (0.88)")
    record, reason = _run(monkeypatch, tags=LEGO_TAGS, gate=True, verifier=v)
    assert record is not None, f"unconfirmed tags must not drop; got {reason!r}"


def test_no_verifier_means_no_drop_however_strong_the_tags(monkeypatch):
    """Fail-open. Layer 1 alone can never remove a row."""
    record, reason = _run(monkeypatch, tags=LEGO_TAGS, gate=True, verifier=None)
    assert record is not None, f"tags alone must not drop; got {reason!r}"


def test_layer_2_is_asked_with_a_readable_label_not_the_internal_key(monkeypatch):
    """
    Handing a model `toys_and_kids` instead of a description makes it guess the
    intent of an identifier before it can answer.
    """
    v = _StubVerifier(confirmed=True)
    _run(monkeypatch, tags=LEGO_TAGS, gate=True, verifier=v)
    topic, terms = v.asked[0]
    assert "toys_and_kids" != topic
    assert "construction-brick" in topic, topic
    assert "lego" in terms, terms


def test_layer_2_is_NOT_called_when_layer_1_does_not_fire(monkeypatch):
    """
    The cost argument for the whole design: confirmation runs on ~2% of
    candidates. If it ran on all of them the request budget would be 4x over.
    """
    v = _StubVerifier(confirmed=True)
    record, _ = _run(monkeypatch, tags=["home theater", "projector"], gate=True,
                     verifier=v)
    assert record is not None
    assert v.asked == [], "no layer-1 hit means no layer-2 request"


def test_layer_2_is_NOT_called_while_the_gate_is_off(monkeypatch):
    """Advisory mode must cost nothing."""
    v = _StubVerifier(confirmed=True)
    _run(monkeypatch, tags=LEGO_TAGS, gate=False, verifier=v)
    assert v.asked == [], "advisory mode must issue no confirmation request"


# --- the allowlist is the measurement, not a suggestion ---

def test_a_measured_harmful_topic_is_recorded_but_never_drops(monkeypatch):
    """
    phones_and_pcs is net-negative at every threshold where it fires (-2 at 10%,
    -1 at 25%) and matches the 2026-08-21 title backtest that found the same
    category anti-predictive. It must not drop even with the gate armed.
    """
    assert "phones_and_pcs" not in pipeline.VIDEO_TOPIC_CATEGORIES
    record, reason = _run(monkeypatch, tags=PHONE_TAGS, gate=True,
                          verifier=_StubVerifier(confirmed=True))
    assert record is not None, f"phones_and_pcs must never drop; got {reason!r}"


def test_a_topic_outside_the_allowlist_never_drops(monkeypatch):
    record, reason = _run(monkeypatch, tags=LEGO_TAGS, gate=True,
                          categories=("gaming",),
                          verifier=_StubVerifier(confirmed=True))
    assert record is not None, f"toys_and_kids not allowlisted here; got {reason!r}"


def test_an_empty_allowlist_disables_the_gate_as_surely_as_the_flag(monkeypatch):
    record, reason = _run(monkeypatch, tags=LEGO_TAGS, gate=True, categories=(),
                          verifier=_StubVerifier(confirmed=True))
    assert record is not None, f"empty allowlist must be inert; got {reason!r}"


# --- the standing rule ---

def test_no_tags_never_drops_however_the_gate_is_set(monkeypatch):
    """Absent data never disqualifies — the rule this repo states everywhere."""
    for tags in ([], None):
        record, reason = _run(monkeypatch, tags=tags, gate=True,
                              verifier=_StubVerifier(confirmed=True))
        assert record is not None, f"no tags must be no verdict; got {reason!r}"


def test_one_stray_tag_in_forty_never_drops(monkeypatch):
    """Share, not count. A home-theatre channel with one Lego tag ships."""
    tags = ["lego"] + [f"home theater {i}" for i in range(39)]
    record, reason = _run(monkeypatch, tags=tags, gate=True,
                          verifier=_StubVerifier(confirmed=True))
    assert record is not None, f"1 tag in 40 is noise; got {reason!r}"


def test_the_threshold_is_what_decides(monkeypatch):
    """Same tags, two thresholds, opposite outcomes — so the knob is real."""
    tags = ["lego", "minifigure"] + [f"home theater {i}" for i in range(4)]
    # share = 2/6 = 33%
    record, _ = _run(monkeypatch, tags=tags, gate=True, share=0.40,
                     verifier=_StubVerifier(confirmed=True))
    assert record is not None, "33% is below a 40% bar"
    record, reason = _run(monkeypatch, tags=tags, gate=True, share=0.25,
                          verifier=_StubVerifier(confirmed=True))
    assert record is None and reason == pipeline.DROP_OFF_TOPIC_TAGS


# --- the gate is free, so it must sit ahead of the paid call ---

def test_the_topic_gate_runs_before_the_paid_gemini_call():
    """
    It reads tags that arrived on a response already fetched, so it costs
    nothing and belongs with the other free gates — ahead of verifier.judge,
    for the same reason pre_push_drop_reason was moved there.
    """
    import inspect
    src = inspect.getsource(pipeline.process_candidate)
    assert src.index("video_topics.topic_evidence(") < src.index("verifier.judge("), (
        "the topic gate is free and must run before the paid Gemini request"
    )


def test_the_vocabularies_come_from_the_existing_lists(monkeypatch):
    """
    Not a new hand-written term list. A positive result here has to be an INPUT
    change, or it is just a fresh unmeasured vocabulary wearing a measurement.
    """
    from channel_vetting.verification import video_topics as vt
    ev = vt.topic_evidence(LEGO_TAGS, {**niches.EXCLUDED_TOPIC_TERMS,
                                       **niches.OFF_TARGET_TERMS})
    assert "toys_and_kids" in ev["hits"]
    assert "lego" in niches.OFF_TARGET_TERMS["toys_and_kids"]
    assert "firearm" in niches.EXCLUDED_TOPIC_TERMS["firearms"]
