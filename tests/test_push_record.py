"""
push_record() must not let a re-push of an already-tracked channel
destroy a human reviewer's Status or Notes.

Failure scenario this pins: get_existing_channel_ids() returns a partial
set (a paginated read cut short by a transient error), so an
already-tracked channel looks "fresh", gets rediscovered and
re-enriched, and reaches push_record() with a fresh record dict
(Status=DEFAULT_STATUS, Notes=""). Since the channel already exists,
push_record() PATCHes it — and until this fix, sent the whole dict,
silently reverting the reviewer's Status (e.g. "Contacted" -> "New") and
erasing their Notes. See IMPORTANT 2 in the fix-wave review.
"""


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = "error body"

    def json(self):
        return self._payload


def _full_record(channel_id="UC1"):
    return {
        "Channel Name": "Chan",
        "Channel URL": f"https://www.youtube.com/channel/{channel_id}",
        "Channel ID": channel_id,
        "Status": "New",
        "Notes": "",
        "Date Added": "2026-08-09",
    }


def test_update_strips_status_and_notes_by_default(monkeypatch):
    import airtable_client

    monkeypatch.setattr(airtable_client, "channel_exists", lambda table, cid: "recExisting")

    captured = {}

    def fake_patch(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return _Resp(200)

    monkeypatch.setattr(airtable_client.requests, "patch", fake_patch)
    monkeypatch.setattr(
        airtable_client.requests, "post",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must PATCH an existing record, not POST")),
    )

    ok = airtable_client.push_record("tblFake", _full_record())

    assert ok is True
    fields = captured["json"]["fields"]
    assert "Status" not in fields
    assert "Notes" not in fields
    # Everything else must still go through untouched.
    assert fields["Channel Name"] == "Chan"
    assert fields["Date Added"] == "2026-08-09"


def test_create_sends_status_and_notes_as_given(monkeypatch):
    """A brand-new record has nothing to preserve — Status/Notes defaults
    must reach Airtable on the initial POST."""
    import airtable_client

    monkeypatch.setattr(airtable_client, "channel_exists", lambda table, cid: None)

    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return _Resp(201)

    monkeypatch.setattr(airtable_client.requests, "post", fake_post)
    monkeypatch.setattr(
        airtable_client.requests, "patch",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must POST a new record, not PATCH")),
    )

    ok = airtable_client.push_record("tblFake", _full_record())

    assert ok is True
    fields = captured["json"]["fields"]
    assert fields["Status"] == "New"
    assert fields["Notes"] == ""


def test_update_with_explicit_opt_in_can_still_change_status(monkeypatch):
    """audit_blocklist.py's --mark deliberately changes Status on an
    existing record — overwrite_status_and_notes=True must let that
    through rather than being silently stripped."""
    import airtable_client

    monkeypatch.setattr(airtable_client, "channel_exists", lambda table, cid: "recExisting")

    captured = {}

    def fake_patch(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return _Resp(200)

    monkeypatch.setattr(airtable_client.requests, "patch", fake_patch)

    ok = airtable_client.push_record(
        "tblFake",
        {"Channel ID": "UC1", "Status": "Do Not Contact"},
        overwrite_status_and_notes=True,
    )

    assert ok is True
    assert captured["json"]["fields"] == {"Channel ID": "UC1", "Status": "Do Not Contact"}


def test_update_without_status_or_notes_in_the_record_is_unaffected(monkeypatch):
    """backfill_missing_emails.py only ever sends {Channel ID, Email} on
    an update — stripping Status/Notes must be a no-op when neither key
    is present in the first place."""
    import airtable_client

    monkeypatch.setattr(airtable_client, "channel_exists", lambda table, cid: "recExisting")

    captured = {}

    def fake_patch(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return _Resp(200)

    monkeypatch.setattr(airtable_client.requests, "patch", fake_patch)

    airtable_client.push_record("tblFake", {"Channel ID": "UC1", "Email": "a@b.com"})

    assert captured["json"]["fields"] == {"Channel ID": "UC1", "Email": "a@b.com"}
