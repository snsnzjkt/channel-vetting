"""
get_existing_channel_ids() must raise on read failure, never return a
partial set silently. This set is the pipeline's only pre-filter AND its
only exclude_ids source for discovery — a silent partial result (e.g. a
429 on page 7 of 14) would make already-tracked channels look "fresh",
get re-enriched, and get re-pushed via push_record's PATCH path, which
reverts a reviewer's Status and erases their Notes. See IMPORTANT 2 in
the fix-wave review.
"""
import pytest


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = "error body"

    def json(self):
        return self._payload


def test_collects_ids_across_pages(monkeypatch):
    import airtable_client

    pages = [
        _Resp(200, {"records": [{"fields": {"Channel ID": "UC1"}}], "offset": "next"}),
        _Resp(200, {"records": [{"fields": {"Channel ID": "UC2"}}]}),
    ]
    monkeypatch.setattr(airtable_client.requests, "get", lambda *a, **k: pages.pop(0))
    monkeypatch.setattr(airtable_client.time, "sleep", lambda s: None)

    assert airtable_client.get_existing_channel_ids("tblFake") == {"UC1", "UC2"}


def test_raises_on_non_200_partway_through_pagination(monkeypatch):
    """The core regression: a 429 on page 2 must not return the partial
    set collected from page 1."""
    import airtable_client

    pages = [
        _Resp(200, {"records": [{"fields": {"Channel ID": "UC1"}}], "offset": "next"}),
        _Resp(429),
    ]
    monkeypatch.setattr(airtable_client.requests, "get", lambda *a, **k: pages.pop(0))
    monkeypatch.setattr(airtable_client.time, "sleep", lambda s: None)

    with pytest.raises(airtable_client.AirtableReadError):
        airtable_client.get_existing_channel_ids("tblFake")


def test_raises_on_request_exception(monkeypatch):
    import airtable_client

    def boom(*a, **k):
        raise airtable_client.requests.RequestException("network down")

    monkeypatch.setattr(airtable_client.requests, "get", boom)

    with pytest.raises(airtable_client.AirtableReadError):
        airtable_client.get_existing_channel_ids("tblFake")
