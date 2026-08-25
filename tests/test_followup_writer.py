"""
Tests for followup/writer.py — the batched category write.

The two properties that matter here are both about failure: a bad category value
must be refused BEFORE any request goes out, and a failed batch must not be
counted as written.
"""
import pytest

from channel_vetting.followup import writer as W
from channel_vetting.followup.categorizer import CATEGORIES, CAT_FOLLOW_UP, CAT_DNC_BLOCKED


class FakeResp:
    """`safe_body()` reads .text on a non-200, so a stub without it masks the
    real failure with an AttributeError."""
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text
        self.headers = {}
    def json(self): return {}


def test_batch_size_matches_airtables_documented_maximum():
    assert W.BATCH_SIZE == 10


def test_a_valid_category_passes_preflight():
    W.validate_categories({"rec1": {"Follow-Up Category": CAT_FOLLOW_UP}})


def test_a_typo_is_refused_before_any_request(monkeypatch):
    """
    typecast=True would MINT a fourteenth option and every page filtered on the
    correct thirteen would silently stop showing that row. The repo has been
    burned by this twice, so the value is checked here as well as by the API.
    """
    called = []
    monkeypatch.setattr(W.HTTP, "patch", lambda *a, **k: called.append(1))
    with pytest.raises(W.CategoryVocabularyError, match="outside CATEGORIES"):
        W.patch_records("tbl", {"rec1": {"Follow-Up Category": "Follow Up Needed"}})
    assert called == [], "a request went out despite an invalid category"


def test_preflight_checks_every_record_not_just_the_first():
    with pytest.raises(W.CategoryVocabularyError):
        W.validate_categories({
            "rec1": {"Follow-Up Category": CAT_FOLLOW_UP},
            "rec2": {"Follow-Up Category": "Nonsense"},
        })


def test_records_without_a_category_field_are_allowed_through():
    """A partial update (e.g. only Channel ID) must not trip the vocabulary check."""
    W.validate_categories({"rec1": {"Channel ID": "UC123"}})


def test_writes_are_batched_ten_at_a_time(monkeypatch):
    sizes = []

    def fake_patch(url, headers=None, json=None, timeout=None):
        sizes.append(len(json["records"]))
        assert json["typecast"] is False, "typecast must be False on a select write"
        return FakeResp(200)

    monkeypatch.setattr(W.HTTP, "patch", fake_patch)
    monkeypatch.setattr(W, "API_SLEEP_SECONDS", 0)
    updates = {f"rec{i}": {"Follow-Up Category": CAT_DNC_BLOCKED} for i in range(25)}
    written, failed = W.patch_records("tbl", updates)
    assert sizes == [10, 10, 5]
    assert written == 25 and failed == []


def test_a_failed_batch_is_reported_not_counted(monkeypatch):
    """
    A ten-record PATCH fails as a unit. Counting it as written is how a table
    ends up half-categorised while the run reports success.
    """
    calls = {"n": 0}

    def fake_patch(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        return FakeResp(200 if calls["n"] != 2 else 422)

    monkeypatch.setattr(W.HTTP, "patch", fake_patch)
    monkeypatch.setattr(W, "API_SLEEP_SECONDS", 0)
    updates = {f"rec{i}": {"Follow-Up Category": CAT_DNC_BLOCKED} for i in range(25)}
    written, failed = W.patch_records("tbl", updates)
    assert written == 15
    assert len(failed) == 10
    assert all(f.startswith("rec") for f in failed)


def test_a_network_error_does_not_lose_the_batches_that_succeeded(monkeypatch):
    import requests
    calls = {"n": 0}

    def fake_patch(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResp(200)
        raise requests.ConnectionError("boom")

    monkeypatch.setattr(W.HTTP, "patch", fake_patch)
    monkeypatch.setattr(W, "API_SLEEP_SECONDS", 0)
    updates = {f"rec{i}": {"Follow-Up Category": CAT_FOLLOW_UP} for i in range(20)}
    written, failed = W.patch_records("tbl", updates)
    assert written == 10 and len(failed) == 10


def test_the_writer_and_the_categoriser_share_one_vocabulary():
    """
    Two copies of these strings is how a filter typo silently empties a page.
    The writer must import the tuple, not restate it.
    """
    import inspect
    src = inspect.getsource(W)
    assert "from channel_vetting.followup.categorizer import CATEGORIES" in src
    for cat in CATEGORIES:
        assert f'"{cat}"' not in src, f"writer restates the literal {cat!r}"
