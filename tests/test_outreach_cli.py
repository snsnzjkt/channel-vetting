"""
Tests for the outreach entry point and the mail transport.

The theme is the same as everywhere else in this feature: the interesting
assertions are about what does NOT happen. A dry run must write nothing, a
missing legal footer must stop a send before anything is claimed, and an
ambiguous transport outcome must never be classified as retryable.
"""
import base64

import pytest
import requests

import mailer as M
import outreach
# The REAL classes, not lookalike stubs: a bare Blocklist() never matches
# (all three indexes default to empty sets) and LeaseResult already has
# exactly the fields a stub would declare. Using them means these tests pin
# the real shapes instead of quietly diverging from them.
from do_not_contact import Blocklist
from outreach_ledger import LeaseResult


# --- Argument parsing --------------------------------------------------------

def test_dry_run_and_send_are_mutually_exclusive():
    """
    Rejected by argparse rather than resolved by precedence — leaving "which
    wins" to whoever reads the code last is how a dry run becomes a send.
    """
    with pytest.raises(SystemExit):
        outreach.build_parser().parse_args(["--dry-run", "--send"])


def test_dry_run_is_the_default():
    args = outreach.build_parser().parse_args([])
    assert args.send is False


def test_niche_is_repeatable():
    args = outreach.build_parser().parse_args(["--niche", "Home Theater", "--niche", "Lifestyle Sofa"])
    assert args.niche == ["Home Theater", "Lifestyle Sofa"]


# --- Preflight ---------------------------------------------------------------

def test_dry_run_does_not_require_the_legal_footer():
    """Rendering a preview is how you DECIDE the footer wording."""
    assert outreach.preflight(send_mode=False) == []


def test_send_requires_the_can_spam_footer(monkeypatch):
    monkeypatch.setattr(outreach, "OUTREACH_FOOTER_TEXT", "")
    monkeypatch.setattr(outreach, "OUTREACH_UNSUBSCRIBE_URL", "")
    problems = outreach.preflight(send_mode=True)
    assert any("CAN-SPAM" in p for p in problems)
    assert any("opt-out" in p for p in problems)


def test_send_requires_a_demo_recipient_while_demo_mode_is_on(monkeypatch):
    monkeypatch.setattr(outreach, "OUTREACH_FOOTER_TEXT", "addr")
    monkeypatch.setattr(outreach, "OUTREACH_UNSUBSCRIBE_URL", "https://x.test/u")
    monkeypatch.setattr(outreach, "OUTREACH_DEMO_MODE", True)
    monkeypatch.setattr(outreach, "OUTREACH_DEMO_RECIPIENT", "")
    assert any("DEMO" in p for p in outreach.preflight(send_mode=True))


def test_a_niche_without_a_template_is_caught_before_any_send(monkeypatch):
    """Better here than mid-run, after other prospects were already claimed."""
    monkeypatch.setattr(outreach, "NICHES", {"Kitchen Islands": {}})
    assert any("no email template" in p for p in outreach.preflight(send_mode=False))


def test_preflight_problems_abort_before_touching_the_ledger(monkeypatch):
    monkeypatch.setattr(outreach, "NICHES", {"Kitchen Islands": {}})

    def boom(*a, **k):
        raise AssertionError("must not construct a store when preflight failed")

    monkeypatch.setattr(outreach, "AirtableLedgerStore", boom)
    args = outreach.build_parser().parse_args([])
    assert outreach.run(args) == outreach.EXIT_ABORTED


# --- Redaction ---------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [("jane@example.com", "j***@example.com"), ("a@b.co", "a***@b.co"), ("nonsense", "?"), ("", "?")],
)
def test_recipient_addresses_are_redacted_for_logs(raw, expected):
    """Creator PII in CI logs retained 90 days, for EU/UK data subjects."""
    assert outreach.redact(raw) == expected


# --- The demo redirect, which now lives in the Mailer ------------------------

def _mailer(demo_mode=True, demo_recipient="me@mine.test"):
    return M.Mailer(sender="s@x.test", credentials=None,
                    demo_mode=demo_mode, demo_recipient=demo_recipient)


def test_demo_mode_redirects_every_message():
    assert _mailer().resolve_recipient("realcreator@example.com") == "me@mine.test"


def test_demo_mode_without_a_target_raises_rather_than_falling_back():
    with pytest.raises(M.DemoModeError) as exc:
        _mailer(demo_recipient="").resolve_recipient("realcreator@example.com")
    assert "real prospect" in str(exc.value)


def test_live_mode_uses_the_real_address():
    assert _mailer(demo_mode=False).resolve_recipient("real@example.com") == "real@example.com"


def test_send_applies_the_redirect_itself_so_a_caller_cannot_bypass_it(monkeypatch):
    """
    The reason the gate moved out of the ledger: `send()` takes the PROSPECT'S
    real address and redirects internally, so a caller that forgets the gate
    cannot be written.
    """
    captured = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"id": "msg-1"}

    def fake_post(url, **kwargs):
        raw = kwargs["json"]["raw"]
        captured["msg"] = base64.urlsafe_b64decode(raw).decode("utf-8", "replace")
        return _Resp()

    m = _mailer()
    monkeypatch.setattr(m, "_token", lambda: "tok")
    monkeypatch.setattr(M.HTTP, "post", fake_post)

    assert m.send(to="realcreator@example.com", subject="S", text="T", html="<p>H</p>") == "msg-1"
    assert "To: me@mine.test" in captured["msg"]
    assert "realcreator@example.com" not in captured["msg"]


# --- Transport failure classification ----------------------------------------

def _sending_mailer(monkeypatch, raiser=None, status=None):
    m = _mailer(demo_mode=False)
    monkeypatch.setattr(m, "_token", lambda: "tok")

    class _Resp:
        status_code = status
        text = "body"
        headers = {}

        def json(self):
            return {}

    if raiser:
        monkeypatch.setattr(M.HTTP, "post", lambda *a, **k: (_ for _ in ()).throw(raiser))
    else:
        monkeypatch.setattr(M.HTTP, "post", lambda *a, **k: _Resp())
    return m


def test_connect_timeout_is_provably_not_sent(monkeypatch):
    """The only transport failure that provably predates the send."""
    m = _sending_mailer(monkeypatch, raiser=requests.ConnectTimeout("no route"))
    with pytest.raises(M.MailerError) as exc:
        m.send(to="a@b.test", subject="S", text="T", html="H")
    assert exc.value.provably_not_sent is True


@pytest.mark.parametrize(
    "exc",
    [requests.ReadTimeout("lost"), requests.ConnectionError("dropped mid-response")],
)
def test_a_lost_response_is_NOT_classified_as_not_sent(monkeypatch, exc):
    """
    The message may have been accepted and the answer lost. Calling this
    "failed" is what turns --retry-failed into a duplicate-send path.
    """
    m = _sending_mailer(monkeypatch, raiser=exc)
    with pytest.raises(M.MailerError) as e:
        m.send(to="a@b.test", subject="S", text="T", html="H")
    assert e.value.provably_not_sent is False


@pytest.mark.parametrize("status", [400, 401, 403, 429])
def test_a_4xx_rejection_is_provably_not_sent(monkeypatch, status):
    m = _sending_mailer(monkeypatch, status=status)
    with pytest.raises(M.MailerError) as e:
        m.send(to="a@b.test", subject="S", text="T", html="H")
    assert e.value.provably_not_sent is True


@pytest.mark.parametrize("status", [500, 502, 503])
def test_a_5xx_is_ambiguous_not_failed(monkeypatch, status):
    m = _sending_mailer(monkeypatch, status=status)
    with pytest.raises(M.MailerError) as e:
        m.send(to="a@b.test", subject="S", text="T", html="H")
    assert e.value.provably_not_sent is False


# --- Credentials are required, not soft-disabled -----------------------------

@pytest.mark.parametrize(
    "sender,creds", [("", "abc"), ("s@x.test", ""), ("", "")],
)
def test_missing_credentials_raise_rather_than_returning_an_inert_mailer(sender, creds):
    """
    Soft-disable is the contract for OPTIONAL enrichment. Applied to a mailer
    it would claim N ledger rows, fail N sends, and leave a batch to reconcile
    having delivered nothing.
    """
    with pytest.raises(M.MailerError):
        M.from_config(sender=sender, credentials_b64=creds,
                      demo_mode=True, demo_recipient="me@mine.test")


def test_unparseable_credentials_are_provably_not_sent():
    with pytest.raises(M.MailerError) as e:
        M.from_config(sender="s@x.test", credentials_b64="not-base64-json",
                      demo_mode=True, demo_recipient="me@mine.test")
    assert e.value.provably_not_sent is True


# --- Message assembly --------------------------------------------------------

def test_build_message_is_multipart_alternative_with_both_parts():
    msg = M.build_message(sender="s@x.test", to="t@x.test", subject="S",
                          text="plain body", html="<p>html body</p>")
    assert msg.get_content_type() == "multipart/alternative"
    types = {p.get_content_type() for p in msg.iter_parts()}
    assert types == {"text/plain", "text/html"}


@pytest.mark.parametrize("field", ["subject", "to", "sender"])
def test_header_injection_is_neutralised_without_killing_the_run(field):
    """
    Python's email policy RAISES ValueError on a linefeed in a header — a safe
    refusal, but the wrong shape here: it would escape send() as something the
    caller does not catch and kill the whole run over one bad prospect. This
    repo skips a bad record, it does not abort. So the value is stripped and
    assembly still succeeds.
    """
    kwargs = dict(sender="s@x.test", to="t@x.test", subject="Hi", text="t", html="<p>h</p>")
    kwargs[field] = "X\r\nBcc: evil@attacker.tld"

    msg = M.build_message(**kwargs)  # must NOT raise

    raw = msg.as_bytes().decode("utf-8", "replace")
    assert "\nBcc: evil@attacker.tld" not in raw
    assert "\r\n\r\nBcc:" not in raw


def test_header_safe_strips_only_the_dangerous_characters():
    assert M._header_safe("Hi\r\nthere\x00") == "Hithere"
    assert M._header_safe("Bane Tech") == "Bane Tech"


# --- Gmail session policy ----------------------------------------------------

def test_gmail_session_never_retries_a_post():
    """A retried send is a duplicate EMAIL — the one thing that cannot be undone."""
    from http_client import GMAIL

    adapter = GMAIL.get_adapter("https://gmail.googleapis.com/")
    assert "POST" not in adapter.max_retries.allowed_methods


def test_gmail_session_does_not_retry_reads():
    """A read retry means the request was sent and the answer lost."""
    from http_client import GMAIL

    adapter = GMAIL.get_adapter("https://gmail.googleapis.com/")
    assert adapter.max_retries.read == 0


def test_gmail_session_ignores_retry_after():
    """urllib3 sleeps the header verbatim, with no ceiling, inside the adapter."""
    from http_client import GMAIL

    adapter = GMAIL.get_adapter("https://gmail.googleapis.com/")
    assert adapter.max_retries.respect_retry_after_header is False


# --- Regressions from the milestone-2 review ---------------------------------

def _budget_for(monkeypatch, argv, daily_remaining=10):
    """Run run() with everything stubbed and capture the RunBudget it built."""
    captured = {}
    monkeypatch.setattr(outreach, "AirtableLeaseStore", lambda: "LEASE_STORE")
    monkeypatch.setattr(outreach, "acquire_lease", lambda store, **k: LeaseResult(True))
    monkeypatch.setattr(outreach, "release_lease", lambda store, **k: True)
    _stub_run_deps(monkeypatch)
    # AFTER _stub_run_deps, which also stubs this — order matters.
    monkeypatch.setattr(outreach, "remaining_daily_budget", lambda *a, **k: daily_remaining)
    monkeypatch.setattr(
        outreach, "_send_phase",
        lambda args, ledger, mailer, blocklist, budget, summary, campaign:
            captured.setdefault("remaining", budget.remaining),
    )
    outreach.run(outreach.build_parser().parse_args(argv))
    return captured["remaining"]


def test_limit_zero_means_zero_not_the_default(monkeypatch):
    """
    REGRESSION: `args.limit or OUTREACH_MAX_PER_RUN` treated an explicit
    --limit 0 as unset, so the safest-looking flag sent ten real emails.

    Asserted on the budget run() actually BUILDS, not on the arithmetic
    recomputed here — an earlier version of this test duplicated the expression
    and therefore passed against the bug it was written to catch.
    """
    assert _budget_for(monkeypatch, ["--limit", "0"]) == 0


def test_limit_unset_falls_back_to_the_configured_default(monkeypatch):
    from config import OUTREACH_DAILY_CAP, OUTREACH_MAX_PER_RUN

    # _stub_run_deps reports a daily remaining of 10, and the run takes the min.
    assert _budget_for(monkeypatch, []) == min(10, OUTREACH_MAX_PER_RUN, OUTREACH_DAILY_CAP)


def test_limit_is_capped_by_the_daily_remaining(monkeypatch):
    """A per-run limit must never exceed what the prospect-day budget allows."""
    assert _budget_for(monkeypatch, ["--limit", "99"], daily_remaining=3) == 3


def test_settle_exists_because_reconcile_tells_the_operator_to_run_it():
    """
    REGRESSION: --reconcile printed `--settle <key> --state sent|notsent`, which
    exited 2 with `unrecognized arguments`. The operator's only fallback was
    hand-editing the Send State single-select — the exact drift typecast=False
    exists to prevent, where a typo mints `Snet` and blocks the channel forever.
    """
    args = outreach.build_parser().parse_args(["--settle", "UC_a:c1", "--state", "sent"])
    assert args.settle == "UC_a:c1"
    assert args.state == "sent"


def test_settle_rejects_an_unknown_state():
    with pytest.raises(SystemExit):
        outreach.build_parser().parse_args(["--settle", "k", "--state", "maybe"])


def test_the_lease_is_actually_acquired_and_released(monkeypatch):
    """
    REGRESSION: AirtableLeaseStore was imported and never used, so layer 2 of
    the concurrency design was absent while the docstring claimed it ran. It is
    the ONLY mechanism that can see a hand-run outreach overlapping with the
    scheduled CI job — the Actions concurrency group is blind to a laptop.
    """
    calls = []
    monkeypatch.setattr(outreach, "AirtableLeaseStore", lambda: "LEASE_STORE")
    monkeypatch.setattr(outreach, "acquire_lease",
                        lambda store, **k: calls.append(("acquire", k["holder"])) or LeaseResult(True))
    monkeypatch.setattr(outreach, "release_lease",
                        lambda store, **k: calls.append(("release", k["holder"])))
    _stub_run_deps(monkeypatch)

    outreach.run(outreach.build_parser().parse_args([]))

    assert [c[0] for c in calls] == ["acquire", "release"]
    assert calls[0][1] == calls[1][1], "must release the lease it acquired, not another holder's"


def test_a_held_lease_aborts_before_any_send(monkeypatch):
    monkeypatch.setattr(outreach, "AirtableLeaseStore", lambda: "LEASE_STORE")
    monkeypatch.setattr(outreach, "acquire_lease",
                        lambda store, **k: LeaseResult(False, holder="other-run"))
    monkeypatch.setattr(outreach, "release_lease", lambda store, **k: True)

    def forbidden(*a, **k):
        raise AssertionError("must not read the queue when the lease is held")

    _stub_run_deps(monkeypatch, get_queued=forbidden)
    assert outreach.run(outreach.build_parser().parse_args([])) == outreach.EXIT_ABORTED


def test_summary_and_release_survive_an_unhandled_exception(monkeypatch, caplog):
    """
    REGRESSION: summary.print() sat on the happy path, so an exception mid-run
    discarded the record of irreversible outbound email AND left the lease held
    until the stale threshold expired.
    """
    released = []
    monkeypatch.setattr(outreach, "AirtableLeaseStore", lambda: "LEASE_STORE")
    monkeypatch.setattr(outreach, "acquire_lease", lambda store, **k: LeaseResult(True))
    monkeypatch.setattr(outreach, "release_lease", lambda store, **k: released.append(1))
    _stub_run_deps(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("something unexpected mid-run")

    monkeypatch.setattr(outreach, "_send_phase", boom)

    with caplog.at_level("INFO"):
        with pytest.raises(RuntimeError):
            outreach.run(outreach.build_parser().parse_args([]))

    assert released == [1], "the lease must be released even on an exception"
    assert "Outreach run summary" in caplog.text, "the summary must survive an exception"


def _stub_run_deps(monkeypatch, get_queued=None):
    """Neutralise every network dependency of run() so the wiring can be tested."""
    monkeypatch.setattr(outreach, "AirtableLedgerStore", lambda: "LEDGER")
    monkeypatch.setattr(outreach, "fetch_blocklist", lambda: Blocklist())
    monkeypatch.setattr(outreach, "remaining_daily_budget", lambda *a, **k: 10)
    monkeypatch.setattr(outreach, "find_stranded_claims", lambda *a, **k: [])
    monkeypatch.setattr(outreach, "get_queued_prospects", get_queued or (lambda *a, **k: []))




# --- Blocklist name normalisation --------------------------------------------

def _run_send_phase(monkeypatch, row, blocklist, write_preview=None,
                    demo_mode=True, demo_recipient="demo@example.test",
                    footer_text="Example Co, 1 Test St, Testville",
                    unsubscribe_url="https://example.test/unsub"):
    """
    Drive _send_phase over one row in dry-run mode and report what happened.

    EVERYTHING this helper patches is a PARAMETER, deliberately. FOUR separate
    bugs came from ambient state here: three from this helper patching
    unconditionally and silently overriding whatever the caller had set first,
    and one from NOT patching enough — the footer pair was left reading config,
    so a developer configuring a mailto: opt-out turned four tests red. If you
    add another patch here, add it as a parameter; if you find a config value
    this helper does not pin, pin it.

    `demo_mode`/`demo_recipient` are pinned rather than read from config so the
    suite does not depend on the developer's .env: the dry-run path asks
    recipient_for(), which RAISES when demo mode is on with no redirect target,
    so a clean checkout failed here while a configured machine passed.
    """
    summary = outreach.Summary()
    from outreach_ledger import RunBudget

    monkeypatch.setattr(outreach, "NICHES", {"Home Theater": {"table_name": "tblX"}})
    monkeypatch.setattr(outreach, "get_queued_prospects", lambda *a, **k: [row])
    monkeypatch.setattr(outreach, "write_preview",
                        write_preview or (lambda *a, **k: "preview.eml"))
    monkeypatch.setattr(outreach, "OUTREACH_DEMO_MODE", demo_mode)
    monkeypatch.setattr(outreach, "OUTREACH_DEMO_RECIPIENT", demo_recipient)
    # The footer pair is pinned for the same reason as the demo pair: read from
    # config, these tests inherit whatever the developer's .env holds. That bit
    # concretely — configuring a mailto: opt-out locally made four tests here
    # fail against a validator that only accepted http(s), which is a fact about
    # the machine, not about the behaviour under test.
    monkeypatch.setattr(outreach, "OUTREACH_FOOTER_TEXT", footer_text)
    monkeypatch.setattr(outreach, "OUTREACH_UNSUBSCRIBE_URL", unsubscribe_url)
    args = outreach.build_parser().parse_args([])
    outreach._send_phase(
        args, ledger="LEDGER", mailer=None, blocklist=blocklist,
        budget=RunBudget(remaining=5), summary=summary, campaign="c1",
    )
    return summary


def _row(**overrides):
    fields = {
        "Channel Name": "Bane Tech",
        "Channel URL": "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv",
        "Channel ID": "UCabcdefghijklmnopqrstuv",
        "Email": "creator@example.com",
        "Qualification": "Qualified",
        "Handle": "@banetech",
    }
    fields.update(overrides)
    return {"record_id": "rec1", "fields": fields}


def test_a_csv_safe_prefixed_name_still_matches_the_blocklist(monkeypatch):
    """
    REGRESSION: `Channel Name` was read raw while `Email` went through
    csv_unsafe(). A creator named "-Bob AV" is STORED as "'-Bob AV", so a DNC
    entry for "-Bob AV" never matched — and name is the ONLY key for 10.5% of
    the live blocklist. do_not_contact.py states the costs are asymmetric: a
    false positive is one lost lead, a false negative is the harm the list
    exists to prevent.
    """
    # Name-only, like the 139 of 1329 live DNC rows (10.5%) that carry neither
    # a handle nor an email. Blocklist casefolds its index, so this is the real
    # normalisation, not a stub reimplementation of it.
    blocklist = Blocklist(names={"-bob av"})
    summary = _run_send_phase(monkeypatch, _row(**{"Channel Name": "'-Bob AV"}), blocklist)

    assert summary.skipped.get("blocklisted") == 1, (
        "the stored csv_safe apostrophe must be undone before matching: a raw "
        "\"'-Bob AV\" casefolds to \"'-bob av\" and misses the index entry"
    )
    assert summary.rendered == 0, "a blocklisted creator must not be rendered or sent"


def test_an_unblocked_name_is_still_rendered(monkeypatch):
    blocklist = Blocklist(names={"someone-else"})
    summary = _run_send_phase(monkeypatch, _row(), blocklist)
    assert summary.rendered == 1
    assert not summary.skipped


# --- Simplify-pass behaviour changes -----------------------------------------

def test_an_empty_queue_exits_zero_not_two(monkeypatch):
    """
    This workflow is manual-only with no cron, so a dry run when nobody has
    stamped `Send Requested At` is a NORMAL answer. Returning non-zero painted
    it red and trained people to ignore the X. main.py's non-zero-on-nothing is
    justified by being SCHEDULED, where silence means broken.
    """
    monkeypatch.setattr(outreach, "AirtableLeaseStore", lambda: "LEASE_STORE")
    monkeypatch.setattr(outreach, "acquire_lease", lambda store, **k: LeaseResult(True))
    monkeypatch.setattr(outreach, "release_lease", lambda store, **k: True)
    _stub_run_deps(monkeypatch)
    assert outreach.run(outreach.build_parser().parse_args([])) == outreach.EXIT_OK


def test_work_that_all_got_skipped_still_exits_two(monkeypatch):
    """`2` is reserved for 'there WAS work and none of it got done'."""
    monkeypatch.setattr(outreach, "AirtableLeaseStore", lambda: "LEASE_STORE")
    monkeypatch.setattr(outreach, "acquire_lease", lambda store, **k: LeaseResult(True))
    monkeypatch.setattr(outreach, "release_lease", lambda store, **k: True)
    _stub_run_deps(monkeypatch)
    # One queued row that every gate rejects (no template for this niche).
    monkeypatch.setattr(outreach, "NICHES", {"Kitchen Islands": {"table_name": "tblX"}})
    monkeypatch.setattr(outreach, "get_queued_prospects", lambda *a, **k: [_row()])
    monkeypatch.setattr(outreach, "preflight", lambda send_mode: [])
    assert outreach.run(outreach.build_parser().parse_args([])) == outreach.EXIT_NOTHING_DONE


def test_the_preview_shows_the_real_address_when_demo_mode_is_off(monkeypatch):
    """
    REGRESSION: the dry-run path hardcoded `OUTREACH_DEMO_RECIPIENT or "<unset>"`,
    so a preview showed the demo address even with demo mode OFF —
    misrepresenting what --send would do, on the exact artefact a human reads to
    authorise the send. It now asks the same rule the mailer uses.
    """
    captured = {}
    _run_send_phase(
        monkeypatch, _row(**{"Email": "real@creator.test"}), Blocklist(),
        write_preview=lambda *a, **k: captured.update(to=k["to"]) or "p.eml",
        demo_mode=False, demo_recipient="demo@mine.test",
    )
    assert captured["to"] == "real@creator.test"


def test_the_preview_shows_the_demo_address_when_demo_mode_is_on(monkeypatch):
    captured = {}
    _run_send_phase(
        monkeypatch, _row(**{"Email": "real@creator.test"}), Blocklist(),
        write_preview=lambda *a, **k: captured.update(to=k["to"]) or "p.eml",
        demo_mode=True, demo_recipient="demo@mine.test",
    )
    assert captured["to"] == "demo@mine.test"


def test_the_stranded_scan_is_reported_even_when_the_send_phase_raises(monkeypatch, caplog):
    """
    REGRESSION: the scan was inside the try while the summary was in the finally,
    so an exception left it at 0 and the summary printed "0 stranded" in exactly
    the scenario where a claim is most likely stranded mid-flight.
    """
    monkeypatch.setattr(outreach, "AirtableLeaseStore", lambda: "LEASE_STORE")
    monkeypatch.setattr(outreach, "acquire_lease", lambda store, **k: LeaseResult(True))
    monkeypatch.setattr(outreach, "release_lease", lambda store, **k: True)
    _stub_run_deps(monkeypatch)
    monkeypatch.setattr(outreach, "find_stranded_claims", lambda *a, **k: ["rec1", "rec2"])
    monkeypatch.setattr(outreach, "_send_phase",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("mid-run")))

    with caplog.at_level("WARNING"):
        with pytest.raises(RuntimeError):
            outreach.run(outreach.build_parser().parse_args([]))
    assert "2 stranded claim(s)" in caplog.text


def test_status_literals_come_from_config_not_string_duplicates():
    """
    A typo in the WRITE ("Contacted") 422s loudly under typecast=False. A typo in
    the QUERY ("Aproved") returns HTTP 200 with ZERO rows and no error at all —
    measured live — so the run reports an empty queue and silently sends nothing.
    """
    import inspect

    import config
    import outreach_airtable as OA

    # The formula must carry the CONFIGURED value, so renaming the option in
    # config changes the query rather than leaving a stale literal behind.
    src = inspect.getsource(OA.get_queued_prospects)
    assert "STATUS_APPROVED" in src, "the query must interpolate the constant"
    assert "STATUS_CONTACTED" in inspect.getsource(OA.mark_contacted)
    assert config.STATUS_APPROVED == "Approved"
    assert config.STATUS_CONTACTED == "Contacted"
