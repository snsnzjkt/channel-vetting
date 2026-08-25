"""
The blocklist is a suppression list: it fails closed and matches
generously. Wrongly skipping a prospect costs one lead; wrongly
contacting a blocklisted person is the harm being prevented.

HTTP is mocked by patching `do_not_contact.HTTP` (the shared retrying
session from core/http_client.py), NOT `do_not_contact.requests` — which the
module now imports only for its exception TYPES. Patching the wrong one
would send these tests at the real Airtable base; tests/conftest.py's
autouse guard turns that into a hard failure rather than a live request.
"""
import logging

import pytest


class _Resp:
    def __init__(self, status_code, payload=None, text="error body"):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


FIELD_NAME = "fldCExrqXONKfUxd5"
FIELD_URL = "fldBFsOvwaBkTN7yX"
FIELD_EMAIL = "fldA5r2RO4xZJ1Nbl"


def _page(records, offset=None):
    payload = {"records": records}
    if offset:
        payload["offset"] = offset
    return _Resp(200, payload)


def test_parses_all_observed_url_formats(monkeypatch):
    from channel_vetting.airtable import do_not_contact

    records = [
        {"fields": {FIELD_URL: "https://www.youtube.com/@EmmaMariesWorld"}},
        {"fields": {FIELD_URL: "youtube.com/@Tarasimonstudios"}},
        {"fields": {FIELD_URL: "https://www.youtube.com/@Chroniques_Atlas/videos"}},
    ]
    monkeypatch.setattr(do_not_contact.HTTP, "get", lambda *a, **k: _page(records))
    monkeypatch.setattr(do_not_contact.time, "sleep", lambda s: None)

    bl = do_not_contact.fetch_blocklist()
    assert bl.handles == {"emmamariesworld", "tarasimonstudios", "chroniques_atlas"}


def test_matches_handle_case_insensitively(monkeypatch):
    from channel_vetting.airtable import do_not_contact

    records = [{"fields": {FIELD_URL: "https://www.youtube.com/@LinusTechTips"}}]
    monkeypatch.setattr(do_not_contact.HTTP, "get", lambda *a, **k: _page(records))
    monkeypatch.setattr(do_not_contact.time, "sleep", lambda s: None)

    bl = do_not_contact.fetch_blocklist()
    assert bl.match(handle="linustechtips")
    assert bl.match(handle="LINUSTECHTIPS")
    assert not bl.match(handle="someoneelse")


def test_matches_email_and_name(monkeypatch):
    from channel_vetting.airtable import do_not_contact

    records = [
        {"fields": {FIELD_EMAIL: "  Info@LinusMediaGroup.com \n", FIELD_NAME: "Linus Tech Tips"}},
        {"fields": {FIELD_NAME: "superwog"}},  # no URL at all
    ]
    monkeypatch.setattr(do_not_contact.HTTP, "get", lambda *a, **k: _page(records))
    monkeypatch.setattr(do_not_contact.time, "sleep", lambda s: None)

    bl = do_not_contact.fetch_blocklist()
    assert bl.match(email="info@linusmediagroup.com")
    assert bl.match(name="Superwog")
    assert not bl.match(name="Completely Different Channel")


def test_blank_field_values_never_enter_the_index(monkeypatch):
    """Blank cells must not add "" to any index set, even alongside a
    real row (a lone all-blank row would now correctly be rejected by the
    "non-empty table but empty index" backstop, so this mixes in a real
    row to isolate the behaviour under test: the indexing-side guard)."""
    from channel_vetting.airtable import do_not_contact

    records = [
        {"fields": {FIELD_URL: "https://www.youtube.com/@RealChannel"}},
        {"fields": {FIELD_URL: "", FIELD_EMAIL: "", FIELD_NAME: ""}},
    ]
    monkeypatch.setattr(do_not_contact.HTTP, "get", lambda *a, **k: _page(records))
    monkeypatch.setattr(do_not_contact.time, "sleep", lambda s: None)

    bl = do_not_contact.fetch_blocklist()
    assert "" not in bl.handles
    assert "" not in bl.emails
    assert "" not in bl.names


def test_blank_inputs_never_match_even_against_a_blank_index_entry():
    """The most dangerous bug: empty string matching an empty set entry.
    Construct a Blocklist directly with blank entries (simulating a bug
    on the indexing side slipping through) and confirm the match-side
    guard holds independently -- this is the case the old single-record
    fetch test could not isolate, since removing either guard alone still
    passed it."""
    from channel_vetting.airtable.do_not_contact import Blocklist

    bl = Blocklist(handles={""}, emails={""}, names={""})
    assert bl.match(handle="", email="", name="") == ""
    assert bl.match(handle="anyone") == ""
    assert bl.match(email="anyone@example.com") == ""
    assert bl.match(name="Anyone") == ""


def test_raises_on_non_200(monkeypatch):
    from channel_vetting.airtable import do_not_contact

    monkeypatch.setattr(do_not_contact.HTTP, "get", lambda *a, **k: _Resp(500))

    with pytest.raises(do_not_contact.BlocklistUnavailable):
        do_not_contact.fetch_blocklist()


def test_raises_on_request_exception(monkeypatch):
    from channel_vetting.airtable import do_not_contact

    def boom(*a, **k):
        raise do_not_contact.requests.RequestException("network down")

    monkeypatch.setattr(do_not_contact.HTTP, "get", boom)

    with pytest.raises(do_not_contact.BlocklistUnavailable):
        do_not_contact.fetch_blocklist()


def test_raises_when_blocklist_is_suspiciously_empty(monkeypatch):
    """A 200 with no rows means the table moved or was emptied, not that
    nobody is blocklisted."""
    from channel_vetting.airtable import do_not_contact

    monkeypatch.setattr(do_not_contact.HTTP, "get", lambda *a, **k: _page([]))
    monkeypatch.setattr(do_not_contact.time, "sleep", lambda s: None)

    with pytest.raises(do_not_contact.BlocklistUnavailable):
        do_not_contact.fetch_blocklist()


def test_page_missing_records_key_raises_instead_of_returning_partial(monkeypatch):
    """C1: a 200 body that omits "records" entirely (API shape change,
    truncated proxy response, ...) must not silently be treated as "this
    page had zero rows" -- that would let a partial index through as if
    it were the whole list. Only page 1 has a valid body; page 2 (still
    200) is missing the "records" key altogether."""
    from channel_vetting.airtable import do_not_contact

    page1 = _page([{"fields": {FIELD_URL: "https://www.youtube.com/@ChannelOne"}}], offset="o1")
    page2 = _Resp(200, {})  # 200, but no "records" key at all

    responses = [page1, page2]
    monkeypatch.setattr(do_not_contact.HTTP, "get", lambda *a, **k: responses.pop(0))
    monkeypatch.setattr(do_not_contact.time, "sleep", lambda s: None)

    with pytest.raises(do_not_contact.BlocklistUnavailable):
        do_not_contact.fetch_blocklist()


def test_records_keyed_by_wrong_field_raises_instead_of_empty_index(monkeypatch):
    """C2: a non-empty table can still produce a completely empty index if
    the fields come back keyed wrong -- e.g. returnFieldsByFieldId gets
    dropped/typo'd, or a fld... constant goes stale because someone
    deleted and re-added a column in the manually maintained table (which
    mints a brand-new field ID; this is not caught by the by-ID read
    alone). Rows exist but nothing indexes -- must raise, not return an
    empty-but-"successful" Blocklist."""
    from channel_vetting.airtable import do_not_contact

    records = [
        {"fields": {"Name": "Someone", "URL": "https://www.youtube.com/@someone"}}
        for _ in range(5)
    ]
    monkeypatch.setattr(do_not_contact.HTTP, "get", lambda *a, **k: _page(records))
    monkeypatch.setattr(do_not_contact.time, "sleep", lambda s: None)

    with pytest.raises(do_not_contact.BlocklistUnavailable):
        do_not_contact.fetch_blocklist()


def test_non_json_response_body_raises_blocklist_unavailable(monkeypatch):
    """I1: a 200 with a non-JSON body (proxy interstitial, captive portal
    HTML, ...) must convert to BlocklistUnavailable, not escape as a raw
    JSONDecodeError that callers weren't told to expect."""
    from channel_vetting.airtable import do_not_contact

    class _BadJsonResp:
        status_code = 200
        text = "<html>not json</html>"

        def json(self):
            raise do_not_contact.requests.exceptions.JSONDecodeError(
                "Expecting value", "<html>not json</html>", 0
            )

    monkeypatch.setattr(do_not_contact.HTTP, "get", lambda *a, **k: _BadJsonResp())

    with pytest.raises(do_not_contact.BlocklistUnavailable):
        do_not_contact.fetch_blocklist()


def test_multi_page_results_accumulate(monkeypatch):
    """I3: pagination must accumulate across pages, not just read page 1."""
    from channel_vetting.airtable import do_not_contact

    page1 = _page([{"fields": {FIELD_URL: "https://www.youtube.com/@ChannelOne"}}], offset="o1")
    page2 = _page([{"fields": {FIELD_URL: "https://www.youtube.com/@ChannelTwo"}}])
    responses = [page1, page2]

    monkeypatch.setattr(do_not_contact.HTTP, "get", lambda *a, **k: responses.pop(0))
    monkeypatch.setattr(do_not_contact.time, "sleep", lambda s: None)

    bl = do_not_contact.fetch_blocklist()
    assert bl.handles == {"channelone", "channeltwo"}


def test_offset_is_forwarded_into_the_next_page_request(monkeypatch):
    """I3: the offset from page 1's response must be sent as a request
    param on page 2's request, and page 1's request must not send one."""
    from channel_vetting.airtable import do_not_contact

    page1 = _page([{"fields": {FIELD_URL: "https://www.youtube.com/@ChannelOne"}}], offset="abc123")
    page2 = _page([{"fields": {FIELD_URL: "https://www.youtube.com/@ChannelTwo"}}])
    responses = [page1, page2]
    captured_params = []

    def fake_get(*a, **k):
        captured_params.append(k.get("params"))
        return responses.pop(0)

    monkeypatch.setattr(do_not_contact.HTTP, "get", fake_get)
    monkeypatch.setattr(do_not_contact.time, "sleep", lambda s: None)

    do_not_contact.fetch_blocklist()

    assert "offset" not in captured_params[0]
    assert captured_params[1]["offset"] == "abc123"


def test_failure_on_a_later_page_raises_not_returns_earlier_pages(monkeypatch):
    """I3: the single most important pagination case -- a failure partway
    through must raise, never return whatever pages succeeded before it."""
    from channel_vetting.airtable import do_not_contact

    page1 = _page([{"fields": {FIELD_URL: "https://www.youtube.com/@ChannelOne"}}], offset="o1")
    page2 = _page([{"fields": {FIELD_URL: "https://www.youtube.com/@ChannelTwo"}}], offset="o2")
    responses = [page1, page2, _Resp(500)]

    monkeypatch.setattr(do_not_contact.HTTP, "get", lambda *a, **k: responses.pop(0))
    monkeypatch.setattr(do_not_contact.time, "sleep", lambda s: None)

    with pytest.raises(do_not_contact.BlocklistUnavailable):
        do_not_contact.fetch_blocklist()


def test_request_params_use_field_ids_and_returnFieldsByFieldId(monkeypatch):
    """I4: without this, deleting or typo'ing returnFieldsByFieldId, or
    changing a fld... constant, would pass every other test."""
    from channel_vetting.airtable import do_not_contact

    captured = {}
    records = [{"fields": {FIELD_URL: "https://www.youtube.com/@Someone"}}]

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["params"] = params
        return _page(records)

    monkeypatch.setattr(do_not_contact.HTTP, "get", fake_get)
    monkeypatch.setattr(do_not_contact.time, "sleep", lambda s: None)

    do_not_contact.fetch_blocklist()

    params = captured["params"]
    assert params["returnFieldsByFieldId"] == "true"
    assert set(params["fields[]"]) == {FIELD_NAME, FIELD_URL, FIELD_EMAIL}


def test_match_accepts_a_full_url_as_the_handle_input(monkeypatch):
    """I5: the index side normalizes URLs via normalize_handle(); the
    lookup side must accept the same shapes (a full URL, not just a bare
    handle) since the next task may pass a URL or snippet.customUrl
    straight through."""
    from channel_vetting.airtable import do_not_contact

    records = [{"fields": {FIELD_URL: "https://www.youtube.com/@LinusTechTips"}}]
    monkeypatch.setattr(do_not_contact.HTTP, "get", lambda *a, **k: _page(records))
    monkeypatch.setattr(do_not_contact.time, "sleep", lambda s: None)

    bl = do_not_contact.fetch_blocklist()
    assert bl.match(handle="https://www.youtube.com/@LinusTechTips")
    assert bl.match(handle="youtube.com/@LinusTechTips/videos")
    assert bl.match(handle="@LinusTechTips")
    assert bl.match(handle="linustechtips")


def test_huge_error_body_is_truncated_not_reported_in_full(monkeypatch, caplog):
    """The non-200 path reports the body through http_client.safe_body(),
    so an Airtable error that echoes a whole rejected page back cannot
    dump unbounded text into the run log (or into the abort message
    pipeline.py logs verbatim). It must still fail closed."""
    from channel_vetting.airtable import do_not_contact
    from channel_vetting.core.http_client import safe_body

    huge = "x" * 20_000
    monkeypatch.setattr(do_not_contact.HTTP, "get", lambda *a, **k: _Resp(500, text=huge))

    caplog.set_level(logging.DEBUG, logger="do_not_contact")
    with pytest.raises(do_not_contact.BlocklistUnavailable) as excinfo:
        do_not_contact.fetch_blocklist()

    message = str(excinfo.value)
    # Fail-closed contract intact, AND the body is bounded.
    assert huge not in message
    assert "truncated" in message
    assert len(message) < len(huge)
    assert safe_body(_Resp(500, text=huge)) in message
    # Nothing logged the full body on the way out either.
    assert not any(huge in record.getMessage() for record in caplog.records)


def test_auth_failure_body_is_withheld_entirely(monkeypatch):
    """safe_body() reports nothing but the status for 401/403 — that body
    is noise attached to the one status most likely to get pasted into a
    ticket. The abort itself is unchanged."""
    from channel_vetting.airtable import do_not_contact

    monkeypatch.setattr(
        do_not_contact.HTTP, "get",
        lambda *a, **k: _Resp(403, text="token 'patXXXX' lacks scope data.records:read"),
    )

    with pytest.raises(do_not_contact.BlocklistUnavailable) as excinfo:
        do_not_contact.fetch_blocklist()

    message = str(excinfo.value)
    assert "patXXXX" not in message
    assert "403" in message
    assert "withheld" in message
