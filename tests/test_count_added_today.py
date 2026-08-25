"""count_added_today must raise on read failure, never return 0.

HTTP is mocked on `airtable.client.HTTP` (the shared retrying session from
core/http_client.py); `airtable.client.requests` survives only as the source of
`requests.RequestException`.
"""
import pytest


class _Resp:
    def __init__(self, status_code, payload=None, text="error body"):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.headers = {}

    def json(self):
        return self._payload


def test_counts_records_across_pages(monkeypatch):
    from channel_vetting.airtable import client

    pages = [
        _Resp(200, {"records": [{"id": "r1"}, {"id": "r2"}], "offset": "next"}),
        _Resp(200, {"records": [{"id": "r3"}]}),
    ]
    monkeypatch.setattr(client.HTTP, "get", lambda *a, **k: pages.pop(0))
    monkeypatch.setattr(client.time, "sleep", lambda s: None)

    assert client.count_added_today("tblFake") == 3


def test_raises_on_non_200(monkeypatch):
    from channel_vetting.airtable import client

    monkeypatch.setattr(client.HTTP, "get", lambda *a, **k: _Resp(500))

    with pytest.raises(client.AirtableReadError):
        client.count_added_today("tblFake")


def test_raises_on_request_exception(monkeypatch):
    from channel_vetting.airtable import client

    def boom(*a, **k):
        raise client.requests.RequestException("network down")

    monkeypatch.setattr(client.HTTP, "get", boom)

    with pytest.raises(client.AirtableReadError):
        client.count_added_today("tblFake")


def test_qualification_filter_is_included(monkeypatch):
    from channel_vetting.airtable import client

    captured = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["params"] = params
        return _Resp(200, {"records": []})

    monkeypatch.setattr(client.HTTP, "get", fake_get)
    client.count_added_today("tblFake", qualification="Qualified")

    assert "Qualified" in captured["params"]["filterByFormula"]


def test_qualification_with_an_apostrophe_is_escaped(monkeypatch):
    """Qualification option names come out of a hand-edited Airtable
    schema. An unescaped apostrophe closes the formula string early, and
    Airtable answers a malformed formula with a 422 — which here raises
    AirtableReadError and costs the niche its whole run. See
    _quote_formula_value().
    """
    from channel_vetting.airtable import client

    captured = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["params"] = params
        return _Resp(200, {"records": []})

    monkeypatch.setattr(client.HTTP, "get", fake_get)
    client.count_added_today("tblFake", qualification="Editor's Pick")

    formula = captured["params"]["filterByFormula"]
    assert "{Qualification} = 'Editor\\'s Pick'" in formula
    # Only the delimiters survive stripping the escape sequences: two for
    # the DATESTR literal, two for the Qualification literal.
    assert formula.replace("\\\\", "").replace("\\'", "").count("'") == 4


def test_read_failure_does_not_put_the_whole_body_in_the_error(monkeypatch):
    """The AirtableReadError message is what run_niche() logs, so it is a
    log site in all but name — safe_body() has to bound it too."""
    from channel_vetting.airtable import client

    huge = "y" * 10_000
    monkeypatch.setattr(client.HTTP, "get", lambda *a, **k: _Resp(500, text=huge))

    with pytest.raises(client.AirtableReadError) as exc:
        client.count_added_today("tblFake")

    message = str(exc.value)
    assert huge not in message
    assert len(message) < 1_000
    assert "500" in message
