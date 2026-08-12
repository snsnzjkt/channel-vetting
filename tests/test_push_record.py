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

The second half of this file pins the transport contract around that
guarantee: the formula that decides PATCH-vs-POST must survive an
apostrophe (a malformed formula routes a duplicate row straight past the
PATCH path), a rate-limited POST must be retried rather than dropped, and
an error body must not be logged unbounded.

All HTTP is mocked on `airtable_client.HTTP` — the shared retrying session
from http_client.py — not on `airtable_client.requests`, which the module
now imports only for `requests.RequestException`.
"""
import logging
import math


class _Resp:
    def __init__(self, status_code, payload=None, text="error body", headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        # post_with_rate_limit_retry() reads Retry-After off a 429, so the
        # stub needs a headers mapping even when it's empty.
        self.headers = headers or {}

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

    monkeypatch.setattr(airtable_client.HTTP, "patch", fake_patch)
    monkeypatch.setattr(
        airtable_client, "post_with_rate_limit_retry",
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

    monkeypatch.setattr(airtable_client, "post_with_rate_limit_retry", fake_post)
    monkeypatch.setattr(
        airtable_client.HTTP, "patch",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must POST a new record, not PATCH")),
    )

    ok = airtable_client.push_record("tblFake", _full_record())

    assert ok is True
    fields = captured["json"]["fields"]
    assert fields["Status"] == "New"
    assert fields["Notes"] == ""


def test_create_goes_through_the_rate_limit_aware_post_helper(monkeypatch):
    """A new record must NOT be sent via the session's plain .post().

    The session's retry adapter deliberately excludes POST (a retry after a
    lost 5xx response duplicates the row), so the only sanctioned POST path
    is post_with_rate_limit_retry(). Patching HTTP.post to explode pins that
    push_record() never regresses to the bare session call.
    """
    import airtable_client

    monkeypatch.setattr(airtable_client, "channel_exists", lambda table, cid: None)
    monkeypatch.setattr(
        airtable_client.HTTP, "post",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("POST must go through post_with_rate_limit_retry, not HTTP.post")
        ),
    )
    monkeypatch.setattr(airtable_client, "post_with_rate_limit_retry", lambda *a, **k: _Resp(201))

    assert airtable_client.push_record("tblFake", _full_record()) is True


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

    monkeypatch.setattr(airtable_client.HTTP, "patch", fake_patch)

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

    monkeypatch.setattr(airtable_client.HTTP, "patch", fake_patch)

    airtable_client.push_record("tblFake", {"Channel ID": "UC1", "Email": "a@b.com"})

    assert captured["json"]["fields"] == {"Channel ID": "UC1", "Email": "a@b.com"}


# --------------------------------------------------------------------------
# filterByFormula escaping — the duplicate-row bug
# --------------------------------------------------------------------------

def _unescaped_quote_count(formula: str) -> int:
    """Count the single quotes that actually delimit strings.

    Escaped pairs (\\' and \\\\) are removed first, so what's left is only
    the real delimiters. A well-formed formula with one string literal has
    exactly two.
    """
    return formula.replace("\\\\", "").replace("\\'", "").count("'")


def test_quote_formula_value_escapes_an_apostrophe():
    import airtable_client

    # Returns the INNER text only — the caller supplies the surrounding
    # quotes — so the expected value carries no delimiters of its own.
    assert airtable_client._quote_formula_value("O'Brien AV") == "O\\'Brien AV"


def test_quote_formula_value_escapes_backslashes_before_quotes():
    """Order matters: escaping quotes first would then double the
    backslashes this function just introduced."""
    import airtable_client

    assert airtable_client._quote_formula_value("a\\b") == "a\\\\b"
    assert airtable_client._quote_formula_value("a\\'b") == "a\\\\\\'b"


def test_quote_formula_value_leaves_ordinary_values_alone():
    import airtable_client

    assert airtable_client._quote_formula_value("UCabc123") == "UCabc123"


def test_channel_exists_sends_a_well_formed_formula_for_a_quoted_id(monkeypatch):
    """A channel_id containing an apostrophe must not close the formula
    string early — Airtable answers a malformed formula with a 422."""
    import airtable_client

    captured = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["params"] = params
        return _Resp(200, {"records": [{"id": "recFound"}]})

    monkeypatch.setattr(airtable_client.HTTP, "get", fake_get)

    assert airtable_client.channel_exists("tblFake", "UC O'Brien") == "recFound"

    formula = captured["params"]["filterByFormula"]
    assert formula == "{Channel ID} = 'UC O\\'Brien'"
    # Two delimiters, not four: the value's own quote is escaped, so the
    # string literal opens and closes exactly once.
    assert _unescaped_quote_count(formula) == 2


def test_quoted_channel_id_does_not_fall_through_to_the_duplicating_post(monkeypatch):
    """The consequence, not just the syntax.

    channel_exists() fails soft (logs, returns None), and None means "no
    existing record" — so a formula Airtable rejects sends push_record()
    down the POST branch and creates a SECOND row for a channel that
    already has one, breaking the "Never creates duplicates" guarantee and
    stamping fresh Status/Notes defaults the PATCH path would have stripped.

    channel_exists() is deliberately NOT stubbed here: this drives the real
    formula construction end to end.
    """
    import airtable_client

    captured = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["formula"] = params["filterByFormula"]
        # Airtable answers a well-formed formula with the existing row.
        return _Resp(200, {"records": [{"id": "recExisting"}]})

    def fake_patch(url, headers=None, json=None, timeout=None):
        captured["patched_url"] = url
        return _Resp(200)

    monkeypatch.setattr(airtable_client.HTTP, "get", fake_get)
    monkeypatch.setattr(airtable_client.HTTP, "patch", fake_patch)
    monkeypatch.setattr(
        airtable_client, "post_with_rate_limit_retry",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("a quoted Channel ID must still resolve to a PATCH — a POST here is a duplicate row")
        ),
    )

    ok = airtable_client.push_record("tblFake", _full_record("UC O'Brien"))

    assert ok is True
    assert captured["patched_url"].endswith("/recExisting")
    assert _unescaped_quote_count(captured["formula"]) == 2


# --------------------------------------------------------------------------
# Rate-limit retry on the POST path
# --------------------------------------------------------------------------

def test_push_record_retries_a_rate_limited_post_and_succeeds(monkeypatch):
    """A 429 means Airtable rejected the request WITHOUT processing it, so
    repeating the POST cannot duplicate a row — and must not cost the
    pipeline a prospect it already spent enrichment quota on.

    This drives the real post_with_rate_limit_retry() (only `sleep` is
    injected, so the test doesn't wait 32 seconds) rather than a fake, so
    the retry loop itself is under test.
    """
    import airtable_client
    import http_client

    monkeypatch.setattr(airtable_client, "channel_exists", lambda table, cid: None)

    posts = []
    responses = [_Resp(429), _Resp(201)]

    def fake_session_post(url, **kwargs):
        posts.append(url)
        return responses.pop(0)

    monkeypatch.setattr(http_client.AIRTABLE, "post", fake_session_post)

    slept = []
    monkeypatch.setattr(
        airtable_client, "post_with_rate_limit_retry",
        lambda url, **kwargs: http_client.post_with_rate_limit_retry(url, sleep=slept.append, **kwargs),
    )

    assert airtable_client.push_record("tblFake", _full_record()) is True
    assert len(posts) == 2, "the 429 should have been retried exactly once"
    assert slept == [http_client.POST_RETRY_WAIT_SECONDS]


def test_push_record_honours_retry_after_on_a_429(monkeypatch):
    """Airtable's ~30s cooldown is advisory in the header; a sane value
    there beats the hardcoded default."""
    import airtable_client
    import http_client

    monkeypatch.setattr(airtable_client, "channel_exists", lambda table, cid: None)

    responses = [_Resp(429, headers={"Retry-After": "5"}), _Resp(201)]
    monkeypatch.setattr(http_client.AIRTABLE, "post", lambda url, **k: responses.pop(0))

    slept = []
    monkeypatch.setattr(
        airtable_client, "post_with_rate_limit_retry",
        lambda url, **kwargs: http_client.post_with_rate_limit_retry(url, sleep=slept.append, **kwargs),
    )

    assert airtable_client.push_record("tblFake", _full_record()) is True
    assert slept == [5.0]


def test_retry_after_rejects_nan_and_infinity():
    """A `Retry-After: nan` header parses cleanly through float() and then
    fails every comparison (`nan <= 0` and `nan > 300` are both False), so
    without an isfinite() guard it reaches time.sleep(nan) and raises
    ValueError out of a path nothing above run() catches. inf is caught by
    the >300 bound but is rejected here too rather than relying on that."""
    import http_client

    default = 32.0
    for bad in ("nan", "NaN", "inf", "-inf", "infinity"):
        got = http_client._retry_after_seconds(_Resp(429, headers={"Retry-After": bad}), default)
        assert got == default, f"{bad!r} should fall back to the default, got {got!r}"
        assert math.isfinite(got)


def test_retry_after_still_reads_a_sane_value():
    """The guard must not regress the happy path: a plausible numeric header
    is still honoured over the default."""
    import http_client

    assert http_client._retry_after_seconds(_Resp(429, headers={"Retry-After": "12"}), 32.0) == 12.0


def test_push_record_survives_a_nan_retry_after(monkeypatch):
    """End-to-end: a 429 carrying `Retry-After: nan` must not crash the run.
    Before the isfinite() guard, post_with_rate_limit_retry() called
    time.sleep(nan) and the ValueError unwound through push_record() ->
    run_niche() -> run(), killing every remaining niche over one bad header."""
    import airtable_client
    import http_client

    monkeypatch.setattr(airtable_client, "channel_exists", lambda table, cid: None)

    responses = [_Resp(429, headers={"Retry-After": "nan"}), _Resp(201)]
    monkeypatch.setattr(http_client.AIRTABLE, "post", lambda url, **k: responses.pop(0))

    slept = []
    monkeypatch.setattr(
        airtable_client, "post_with_rate_limit_retry",
        lambda url, **kwargs: http_client.post_with_rate_limit_retry(url, sleep=slept.append, **kwargs),
    )

    assert airtable_client.push_record("tblFake", _full_record()) is True
    # Fell back to the hardcoded default instead of sleeping nan.
    assert slept == [http_client.POST_RETRY_WAIT_SECONDS]


def test_push_record_does_not_retry_a_500_post(monkeypatch):
    """The other half of the contract: a 5xx is ambiguous — Airtable may
    have created the row before the response was lost — so the POST is
    reported as failed rather than repeated. One attempt, no duplicate."""
    import airtable_client
    import http_client

    monkeypatch.setattr(airtable_client, "channel_exists", lambda table, cid: None)

    posts = []

    def fake_session_post(url, **kwargs):
        posts.append(url)
        return _Resp(500)

    monkeypatch.setattr(http_client.AIRTABLE, "post", fake_session_post)
    monkeypatch.setattr(
        airtable_client, "post_with_rate_limit_retry",
        lambda url, **kwargs: http_client.post_with_rate_limit_retry(
            url, sleep=lambda s: None, **kwargs
        ),
    )

    assert airtable_client.push_record("tblFake", _full_record()) is False
    assert len(posts) == 1, "a 5xx POST must never be repeated — that is how duplicates appear"


# --------------------------------------------------------------------------
# Logged bodies are bounded
# --------------------------------------------------------------------------

def test_push_record_does_not_log_an_unbounded_error_body(caplog, monkeypatch):
    """An Airtable validation error echoes the whole rejected record back,
    so `resp.text` in a log call is unbounded by construction. safe_body()
    caps it; this pins that push_record() actually routes through it."""
    import airtable_client

    monkeypatch.setattr(airtable_client, "channel_exists", lambda table, cid: None)
    huge = "x" * 10_000
    monkeypatch.setattr(
        airtable_client, "post_with_rate_limit_retry",
        lambda *a, **k: _Resp(500, text=huge),
    )

    with caplog.at_level(logging.ERROR, logger="airtable_client"):
        ok = airtable_client.push_record("tblFake", _full_record())

    assert ok is False
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert huge not in logged
    assert len(logged) < 1_000, f"a 10KB body reached the log ({len(logged)} chars)"
    assert "truncated" in logged


def test_auth_failure_body_is_withheld_from_the_log(caplog, monkeypatch):
    """A 403 body is pure noise on the status most likely to be pasted into
    a ticket, so it is withheld entirely rather than truncated."""
    import airtable_client

    monkeypatch.setattr(airtable_client, "channel_exists", lambda table, cid: None)
    monkeypatch.setattr(
        airtable_client, "post_with_rate_limit_retry",
        lambda *a, **k: _Resp(403, text='{"error":{"type":"INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND"}}'),
    )

    with caplog.at_level(logging.ERROR, logger="airtable_client"):
        assert airtable_client.push_record("tblFake", _full_record()) is False

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "INVALID_PERMISSIONS" not in logged
    assert "withheld" in logged
    # The status code itself still has to be there — that's the diagnostic.
    assert "403" in logged
