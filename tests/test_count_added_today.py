"""count_added_today must raise on read failure, never return 0."""
import pytest


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = "error body"

    def json(self):
        return self._payload


def test_counts_records_across_pages(monkeypatch):
    import airtable_client

    pages = [
        _Resp(200, {"records": [{"id": "r1"}, {"id": "r2"}], "offset": "next"}),
        _Resp(200, {"records": [{"id": "r3"}]}),
    ]
    monkeypatch.setattr(airtable_client.requests, "get", lambda *a, **k: pages.pop(0))
    monkeypatch.setattr(airtable_client.time, "sleep", lambda s: None)

    assert airtable_client.count_added_today("tblFake") == 3


def test_raises_on_non_200(monkeypatch):
    import airtable_client

    monkeypatch.setattr(airtable_client.requests, "get", lambda *a, **k: _Resp(500))

    with pytest.raises(airtable_client.AirtableReadError):
        airtable_client.count_added_today("tblFake")


def test_raises_on_request_exception(monkeypatch):
    import airtable_client

    def boom(*a, **k):
        raise airtable_client.requests.RequestException("network down")

    monkeypatch.setattr(airtable_client.requests, "get", boom)

    with pytest.raises(airtable_client.AirtableReadError):
        airtable_client.count_added_today("tblFake")


def test_qualification_filter_is_included(monkeypatch):
    import airtable_client

    captured = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["params"] = params
        return _Resp(200, {"records": []})

    monkeypatch.setattr(airtable_client.requests, "get", fake_get)
    airtable_client.count_added_today("tblFake", qualification="Qualified")

    assert "Qualified" in captured["params"]["filterByFormula"]
