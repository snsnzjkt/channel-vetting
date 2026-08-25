"""
scripts/audit/audit_blocklist.py must never write to Airtable without --mark, and its
marking payload must be exactly {Channel ID, Status} so reviewer Notes
survive (see the docstring in pipeline.py's push_record). It must also never
let typecast=True silently mint a brand-new "Status" option — --mark
must abort unless the target option is confirmed to exist, or the human
explicitly opts in.
"""
import sys

import pytest

@pytest.fixture(autouse=True)
def _fake_tables(monkeypatch):
    """
    Pin TABLES to fake table IDs for every test in this file.

    audit_blocklist.TABLES is built from AIRTABLE_TABLE_* at import, so on a
    machine with no .env it is {"Home Theater": None, "Lifestyle Sofa": None}
    and main()'s `if not table_name: continue` skips every niche — push_record
    is never called and the SystemExit paths never run, so four tests here
    failed for a reason that had nothing to do with the behaviour they pin.
    A unit test must not need a real table name, and a fresh clone must be able
    to run the suite.
    """
    from scripts.audit import audit_blocklist

    monkeypatch.setattr(
        audit_blocklist, "TABLES",
        {"Home Theater": "tblFAKE_HOME_THEATER", "Lifestyle Sofa": "tblFAKE_LIFESTYLE"},
    )




class _AlwaysHitBlocklist:
    def match(self, handle="", email="", name=""):
        return "handle test-hit"


class _NeverHitBlocklist:
    def match(self, handle="", email="", name=""):
        return ""


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = "error body"

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


_RECORD = {
    "fields": {
        "Channel ID": "UC1",
        "Channel Name": "Chan",
        "Channel URL": "https://www.youtube.com/@chan",
    }
}


def _records_page():
    return _Resp(200, {"records": [_RECORD]})


def test_report_only_never_calls_push_record(monkeypatch):
    from scripts.audit import audit_blocklist

    monkeypatch.setattr(audit_blocklist, "fetch_blocklist", lambda: _AlwaysHitBlocklist())
    monkeypatch.setattr(audit_blocklist.HTTP, "get", lambda *a, **k: _records_page())
    monkeypatch.setattr(audit_blocklist.time, "sleep", lambda s: None)
    monkeypatch.setattr(audit_blocklist, "push_record", lambda *a, **k: pytest.fail("must not write without --mark"))
    monkeypatch.setattr(sys, "argv", ["scripts/audit/audit_blocklist.py"])

    audit_blocklist.main()


def test_no_hits_never_calls_push_record_even_with_mark(monkeypatch):
    from scripts.audit import audit_blocklist

    monkeypatch.setattr(audit_blocklist, "fetch_blocklist", lambda: _NeverHitBlocklist())
    monkeypatch.setattr(audit_blocklist.HTTP, "get", lambda *a, **k: _records_page())
    monkeypatch.setattr(audit_blocklist.time, "sleep", lambda s: None)
    monkeypatch.setattr(audit_blocklist, "push_record", lambda *a, **k: pytest.fail("no hits — nothing to mark"))
    monkeypatch.setattr(audit_blocklist, "_status_option_exists", lambda table_name, records: True)
    monkeypatch.setattr(sys, "argv", ["scripts/audit/audit_blocklist.py", "--mark"])

    audit_blocklist.main()


def test_mark_sends_only_channel_id_and_status(monkeypatch):
    from scripts.audit import audit_blocklist

    captured = {}

    def fake_push(table_name, record, **kwargs):
        captured["record"] = record
        captured["kwargs"] = kwargs
        return True

    monkeypatch.setattr(audit_blocklist, "fetch_blocklist", lambda: _AlwaysHitBlocklist())
    monkeypatch.setattr(audit_blocklist.HTTP, "get", lambda *a, **k: _records_page())
    monkeypatch.setattr(audit_blocklist.time, "sleep", lambda s: None)
    monkeypatch.setattr(audit_blocklist, "push_record", fake_push)
    monkeypatch.setattr(audit_blocklist, "_status_option_exists", lambda table_name, records: True)
    monkeypatch.setattr(sys, "argv", ["scripts/audit/audit_blocklist.py", "--mark"])

    audit_blocklist.main()

    assert captured["record"] == {"Channel ID": "UC1", "Status": audit_blocklist.MARK_STATUS}
    # Deliberately opts in to overwriting Status — this script's whole
    # purpose is to change it — but must not silently touch Notes (it's
    # simply never included in the payload above).
    assert captured["kwargs"] == {"overwrite_status_and_notes": True}


def test_mark_aborts_when_status_option_confirmed_missing(monkeypatch):
    from scripts.audit import audit_blocklist

    monkeypatch.setattr(audit_blocklist, "fetch_blocklist", lambda: _AlwaysHitBlocklist())
    monkeypatch.setattr(audit_blocklist.HTTP, "get", lambda *a, **k: _records_page())
    monkeypatch.setattr(audit_blocklist.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        audit_blocklist, "push_record",
        lambda *a, **k: pytest.fail("must not write when the Status option is confirmed missing"),
    )
    monkeypatch.setattr(audit_blocklist, "_status_option_exists", lambda table_name, records: False)
    monkeypatch.setattr(sys, "argv", ["scripts/audit/audit_blocklist.py", "--mark"])

    with pytest.raises(SystemExit):
        audit_blocklist.main()


def test_mark_aborts_when_status_option_unknown_without_opt_in(monkeypatch):
    """Schema read unavailable (403) and no existing row proves the
    option exists — must abort rather than risk typecast minting a new
    Status option."""
    from scripts.audit import audit_blocklist

    monkeypatch.setattr(audit_blocklist, "fetch_blocklist", lambda: _AlwaysHitBlocklist())
    monkeypatch.setattr(audit_blocklist.HTTP, "get", lambda *a, **k: _records_page())
    monkeypatch.setattr(audit_blocklist.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        audit_blocklist, "push_record",
        lambda *a, **k: pytest.fail("must not write without explicit opt-in when unconfirmed"),
    )
    monkeypatch.setattr(audit_blocklist, "_status_option_exists", lambda table_name, records: None)
    monkeypatch.setattr(sys, "argv", ["scripts/audit/audit_blocklist.py", "--mark"])

    with pytest.raises(SystemExit):
        audit_blocklist.main()


def test_mark_proceeds_when_unknown_but_explicitly_opted_in(monkeypatch):
    from scripts.audit import audit_blocklist

    captured = {}

    monkeypatch.setattr(audit_blocklist, "fetch_blocklist", lambda: _AlwaysHitBlocklist())
    monkeypatch.setattr(audit_blocklist.HTTP, "get", lambda *a, **k: _records_page())
    monkeypatch.setattr(audit_blocklist.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        audit_blocklist, "push_record",
        lambda table_name, record, **kwargs: captured.setdefault("record", record) or True,
    )
    monkeypatch.setattr(audit_blocklist, "_status_option_exists", lambda table_name, records: None)
    monkeypatch.setattr(sys, "argv", ["scripts/audit/audit_blocklist.py", "--mark", "--yes-create-status-option"])

    audit_blocklist.main()

    assert captured["record"] == {"Channel ID": "UC1", "Status": audit_blocklist.MARK_STATUS}


def test_status_option_exists_reads_schema_first(monkeypatch):
    from scripts.audit import audit_blocklist

    def fake_get(url, headers=None, timeout=None, params=None):
        assert "/meta/bases/" in url
        return _Resp(200, {
            "tables": [
                {
                    "id": "tblFake",
                    "fields": [
                        {"name": "Status", "options": {"choices": [{"name": "New"}, {"name": audit_blocklist.MARK_STATUS}]}},
                    ],
                }
            ]
        })

    monkeypatch.setattr(audit_blocklist.HTTP, "get", fake_get)

    assert audit_blocklist._status_option_exists("tblFake", []) is True


def test_status_option_exists_falls_back_to_records_on_403(monkeypatch):
    from scripts.audit import audit_blocklist

    monkeypatch.setattr(audit_blocklist.HTTP, "get", lambda *a, **k: _Resp(403))

    records_with_status = [{"fields": {"Status": audit_blocklist.MARK_STATUS}}]
    assert audit_blocklist._status_option_exists("tblFake", records_with_status) is True

    records_without_status = [{"fields": {"Status": "New"}}]
    assert audit_blocklist._status_option_exists("tblFake", records_without_status) is None
