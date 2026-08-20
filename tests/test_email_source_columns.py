"""
Where an address came from is recorded, and a BLANK cell explains itself.

Context: the chain has five steps of very different trustworthiness, and
resolve_email_with_source already reported which one fired — process_candidate
then threw it away into `_email_source`. So every Email cell looked equally
authoritative and an empty one explained nothing.

The reviewer-facing question that forced this: some rows "show as Other or have
no email". "Other" is the vendor's own `email_type`, which was never stored, and
a blank Email had no recorded reason. Verified live 2026-08-20 against the 8
email-less rows in the base: influencers.club answers "not found" for 7 of them
and "invalid or expired" for 1 — genuinely absent data, not a mapping bug, and
exactly what these columns make legible.
"""
import main
from influencers import InfluencersClient, null_client


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


# --- the vendor's own label -------------------------------------------------


def test_the_vendor_email_type_is_captured():
    client = InfluencersClient()
    email = client._email_from_response(
        _Resp(200, {"credits_cost": 0.2,
                    "result": {"email": "a@b.com", "email_type": "personal_email"}}),
        "UC1",
    )
    assert email == "a@b.com"
    assert client.last_email_type == "personal_email"


def test_the_email_type_other_is_carried_verbatim():
    """'Other' is the value a reviewer sees in the vendor dashboard and asks
    about, so it must reach the table unchanged rather than being normalised."""
    client = InfluencersClient()
    client._email_from_response(
        _Resp(200, {"result": {"email": "a@b.com", "email_type": "other"}}), "UC1"
    )
    assert client.last_email_type == "other"


def test_a_missing_email_type_is_empty_not_none():
    """The value goes into an Airtable cell; None would be written as "None"."""
    client = InfluencersClient()
    client._email_from_response(_Resp(200, {"result": {"email": "a@b.com"}}), "UC1")
    assert client.last_email_type == ""


def test_the_type_is_kept_even_when_our_own_screen_rejects_the_address():
    """Captured before the fullmatch/blocklist screens, so a rejected address
    still reports what the vendor called it."""
    client = InfluencersClient()
    assert client._email_from_response(
        _Resp(200, {"result": {"email": "not an address", "email_type": "other"}}), "UC1"
    ) == ""
    assert client.last_email_type == "other"


# --- why the vendor declined -----------------------------------------------


def test_a_not_found_decline_is_classified(monkeypatch):
    client = InfluencersClient()
    monkeypatch.setattr(client, "_send", lambda cid: _Resp(
        400, {"error": "Email is required but not found for this creator."}))
    assert client.find_email("UC1") == ""
    assert client.last_email_note == "not_found"


def test_an_expired_decline_is_classified_differently(monkeypatch):
    """The live HGTV row: an address exists but no longer validates. Worth
    telling apart from 'nobody has this address'."""
    client = InfluencersClient()
    monkeypatch.setattr(client, "_send", lambda cid: _Resp(
        400, {"error": "Email for this creator is invalid or expired."}))
    assert client.find_email("UC1") == ""
    assert client.last_email_note == "invalid_or_expired"


def test_unrecognised_vendor_wording_degrades_to_declined(monkeypatch):
    """The vendor owns that prose. A reword must blur the code, never splinter
    the column into free text."""
    client = InfluencersClient()
    monkeypatch.setattr(client, "_send", lambda cid: _Resp(400, {"error": "nope"}))
    client.find_email("UC1")
    assert client.last_email_note == "declined"


def test_state_never_describes_a_previous_channel(monkeypatch):
    """Cleared at the top of find_email, before any early return."""
    client = InfluencersClient()
    monkeypatch.setattr(client, "_send", lambda cid: _Resp(
        200, {"result": {"email": "a@b.com", "email_type": "other"}}))
    client.find_email("UC1")
    assert client.last_email_type == "other"

    client._active = False          # step disabled mid-run
    assert client.find_email("UC2") == ""
    assert client.last_email_type == ""
    assert client.last_email_note == ""


# --- what lands in the table ----------------------------------------------


def test_the_miss_note_prefers_the_vendors_reason(monkeypatch):
    client = InfluencersClient()
    client._last_email_note = "not_found"
    assert main._email_miss_note(client) == "none found (not_found)"


def test_the_miss_note_still_says_something_with_no_vendor_reason():
    """An empty Email Source beside an empty Email is the ambiguity these
    columns exist to remove."""
    assert main._email_miss_note(null_client()) == "none found (all 5 steps ran)"


def test_the_miss_note_tolerates_a_client_without_the_attribute():
    """browser_email's null scraper and any older stand-in must not crash it."""
    assert main._email_miss_note(object()) == "none found (all 5 steps ran)"


# --- process_candidate wiring ---------------------------------------------
#
# Reuses test_csv_injection's stub shapes rather than re-deriving them, so a
# change to the enrichment contract breaks one set of stubs and not two.

from tests.test_csv_injection import _NullBlocklist, _stub_performance, _stub_stats
from search_zones import ZONE_CORE


class _Enricher:
    """Stands in for InfluencersClient with a known label."""
    def __init__(self, email_type="other", note=""):
        self.last_email_type = email_type
        self.last_email_note = note


def _record(monkeypatch, *, email, source, columns, enricher=None):
    monkeypatch.setattr(main, "get_channel_stats", lambda cid: _stub_stats())
    monkeypatch.setattr(main, "get_recent_video_performance",
                        lambda cid, pl: _stub_performance())
    monkeypatch.setattr(main, "channel_age_months", lambda p: 100)
    monkeypatch.setattr(main, "resolve_email_with_source",
                        lambda *a, **k: (email, source, None))
    monkeypatch.setattr(main, "table_has_field", lambda table, field: field in columns)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    record, _ = main.process_candidate(
        {"channel_id": "UC1", "channel_title": "Chan", "matched_keywords": []},
        {}, _NullBlocklist(),
        {"min_avg_views": 10_000, "min_channel_age_months": None,
         "allowed_country_codes": ZONE_CORE, "table_name": "tbl"},
        None, enricher or _Enricher(),
    )
    assert record is not None
    return record


def test_the_email_source_is_written_when_the_column_exists(monkeypatch):
    record = _record(monkeypatch, email="a@b.com",
                     source=main.EMAIL_SOURCE_REPEATED, columns={"Email Source"})
    assert record["Email Source"] == main.EMAIL_SOURCE_REPEATED


def test_nothing_is_written_when_the_column_does_not_exist_yet(monkeypatch):
    """push_record rejects the WHOLE record for one unknown field, so an absent
    column must be a no-op rather than an outage — the Handle rule."""
    record = _record(monkeypatch, email="a@b.com",
                     source=main.EMAIL_SOURCE_REPEATED, columns=set())
    assert "Email Source" not in record
    assert "Email Type" not in record


def test_the_email_type_is_written_only_for_the_influencers_step(monkeypatch):
    """The other four sources have no concept of a type, so it stays blank for
    them rather than being guessed at."""
    vendor = _record(monkeypatch, email="a@b.com",
                     source=main.EMAIL_SOURCE_INFLUENCERS,
                     columns={"Email Type"}, enricher=_Enricher("other"))
    assert vendor["Email Type"] == "other"

    scraped = _record(monkeypatch, email="a@b.com",
                      source=main.EMAIL_SOURCE_BROWSER,
                      columns={"Email Type"}, enricher=_Enricher("other"))
    assert scraped["Email Type"] == ""


def test_a_blank_email_records_why(monkeypatch):
    """The whole point: an empty Email cell must not sit beside an empty
    Email Source."""
    record = _record(monkeypatch, email="", source="",
                     columns={"Email Source"}, enricher=_Enricher(note="not_found"))
    assert record["Email"] == ""
    assert record["Email Source"] == "none found (not_found)"
