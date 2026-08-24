"""
transcripts.fetch — free spoken text, and every failure path returning None.

This module exists because an earlier pass concluded transcripts were
unobtainable, having probed only the raw caption endpoints (all HTTP 200 with an
empty body) and inferred unavailability from a blocked route. The library reaches
the same captions another way. These tests pin the CONTRACT rather than the
route, so a future library change cannot quietly turn a missing transcript into
an exception or a drop.
"""
import json

import pytest

import transcripts


class _Snippet:
    def __init__(self, text):
        self.text = text


class _Api:
    """The library's shape: .fetch(video_id, languages=...) -> iterable of snippets."""

    def __init__(self, result=None, raises=None):
        self._result, self._raises = result, raises
        self.calls = []

    def fetch(self, video_id, languages=("en",)):
        self.calls.append((video_id, tuple(languages)))
        if self._raises:
            raise self._raises
        return [_Snippet(t) for t in self._result]


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """A per-test cache file, and never the repo's real one."""
    monkeypatch.setattr(transcripts, "TRANSCRIPT_CACHE_FILE",
                        str(tmp_path / "tc.json"))
    monkeypatch.setattr(transcripts, "_CACHE", None)


LONG = ["Welcome back to the channel. " * 20]


def test_a_real_transcript_comes_back_as_one_clean_string(monkeypatch):
    monkeypatch.setattr(transcripts, "MIN_TRANSCRIPT_CHARS", 10)
    api = _Api(["Hello there.", "  We build a set today.  ", "Thanks!"])
    text = transcripts.fetch("vid1", api=api)
    assert text == "Hello there. We build a set today. Thanks!"
    assert "  " not in text, "whitespace must be collapsed"


def test_snippets_are_joined_across_the_whole_video(monkeypatch):
    """
    The reason a transcript beats a clip: it is the WHOLE video, not a window.
    Two real uploads measured 1,838 and 4,155 characters end to end.
    """
    monkeypatch.setattr(transcripts, "MIN_TRANSCRIPT_CHARS", 10)
    api = _Api([f"segment {i}" for i in range(200)])
    text = transcripts.fetch("vid1", api=api)
    assert "segment 0" in text and "segment 199" in text


def test_the_minimum_length_bar_is_real_and_defaults_high_enough():
    """
    200 chars, because auto-captions on a music video are "[music]" and a few
    stray fragments are not evidence of a subject.
    """
    assert transcripts.MIN_TRANSCRIPT_CHARS >= 100
    short = "a" * (transcripts.MIN_TRANSCRIPT_CHARS - 1)
    assert transcripts.fetch("vid1", api=_Api([short])) is None
    long_enough = "b " * transcripts.MIN_TRANSCRIPT_CHARS
    assert transcripts.fetch("vid2", api=_Api([long_enough]))


def test_an_empty_video_id_never_calls_the_api():
    api = _Api(LONG)
    for empty in ("", "   ", None):
        assert transcripts.fetch(empty, api=api) is None
    assert api.calls == []


# --- every failure path returns None, because absent data never disqualifies ---

@pytest.mark.parametrize("exc_name", [
    "TranscriptsDisabled", "NoTranscriptFound", "VideoUnavailable",
    "AgeRestricted", "InvalidVideoId", "SomeFutureLibraryError",
])
def test_every_library_failure_returns_None_rather_than_raising(exc_name):
    exc = type(exc_name, (Exception,), {})()
    assert transcripts.fetch("vid1", api=_Api(raises=exc)) is None


def test_a_network_error_returns_None():
    import requests
    assert transcripts.fetch("vid1", api=_Api(raises=requests.RequestException("x"))) is None


def test_a_transcript_too_short_to_mean_anything_is_treated_as_none():
    """A music video's "[music]" track is not evidence of a subject."""
    assert transcripts.fetch("vid1", api=_Api(["[music]"])) is None


def test_a_missing_library_returns_None_rather_than_breaking_the_import(monkeypatch):
    """
    The dependency stays optional: without it this layer reports no transcript,
    which is the same as the layer not existing.
    """
    import builtins

    real_import = builtins.__import__

    def no_lib(name, *a, **k):
        if name == "youtube_transcript_api":
            raise ImportError("not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_lib)
    assert transcripts.fetch("vid1") is None


# --- caching ---

def test_a_second_fetch_of_the_same_video_does_not_call_the_api():
    api = _Api(LONG)
    assert transcripts.fetch("vid1", api=api)
    assert transcripts.fetch("vid1", api=api)
    assert len(api.calls) == 1


def test_a_NEGATIVE_result_is_cached_too():
    """Captions disabled today are disabled next run; re-asking edges the rate limit."""
    exc = type("TranscriptsDisabled", (Exception,), {})()
    api = _Api(raises=exc)
    assert transcripts.fetch("vid1", api=api) is None
    assert transcripts.fetch("vid1", api=api) is None
    assert len(api.calls) == 1, "the negative must be cached"


def test_an_IP_BLOCK_is_NOT_cached_because_it_is_about_us_not_the_video():
    """
    Caching a block would poison every video seen while it lasted, and those
    videos may have perfectly good transcripts once it clears.
    """
    exc = type("IpBlocked", (Exception,), {})()
    api = _Api(raises=exc)
    assert transcripts.fetch("vid1", api=api) is None
    assert transcripts.fetch("vid1", api=api) is None
    assert len(api.calls) == 2, "a block must be retried, not cached"


def test_the_cache_round_trips_through_disk(tmp_path, monkeypatch):
    api = _Api(LONG)
    transcripts.fetch("vid1", api=api)
    transcripts.flush_cache()
    monkeypatch.setattr(transcripts, "_CACHE", None)   # simulate a new process
    fresh = _Api(raises=AssertionError("must not refetch"))
    assert transcripts.fetch("vid1", api=fresh)
    assert fresh.calls == []


def test_the_language_preference_is_part_of_the_cache_key():
    api = _Api(LONG)
    transcripts.fetch("vid1", api=api, languages=("en",))
    transcripts.fetch("vid1", api=api, languages=("de",))
    assert len(api.calls) == 2
    assert api.calls[1][1] == ("de",)


# --- size bound ---

def test_a_very_long_transcript_is_cut_at_a_word_boundary(monkeypatch):
    """A three-hour stream must not dominate a prompt or the token bill."""
    monkeypatch.setattr(transcripts, "MAX_TRANSCRIPT_CHARS", 100)
    text = transcripts.fetch("vid1", api=_Api(["alpha bravo charlie delta " * 50]))
    assert len(text) <= 110, len(text)
    assert text.endswith("…")
    assert not text.replace(" …", "").endswith(("alph", "brav", "charli"))


def test_a_failed_disk_write_is_swallowed(monkeypatch, tmp_path):
    """A cache miss costs a second; it must never stop a run."""
    monkeypatch.setattr(transcripts, "TRANSCRIPT_CACHE_FILE",
                        str(tmp_path / "nope" / "deep" / "tc.json"))
    transcripts.fetch("vid1", api=_Api(LONG))
    transcripts.flush_cache()   # must not raise


def test_an_unreadable_cache_file_starts_empty(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setattr(transcripts, "TRANSCRIPT_CACHE_FILE", str(bad))
    monkeypatch.setattr(transcripts, "_CACHE", None)
    assert transcripts.fetch("vid1", api=_Api(LONG))
