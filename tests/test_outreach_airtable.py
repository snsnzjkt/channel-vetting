"""
Tests for the Airtable-backed ledger store and the demo-mode gate.

Two themes here, and both are about failing in the safe direction:

1. The reads that can AUTHORISE a send (find_by_key, find_sent_for_channel,
   count_claimed_on) must RAISE on failure, never return empty. An empty result
   from them means "never contacted, go ahead", so a failed read that returned
   [] would be a wrong answer rather than a missing one — and the failure mode
   is a duplicate cold email.

2. Demo mode must make emailing a real creator unrepresentable, not merely
   discouraged, while the project is pre-launch.
"""
import pytest

from channel_vetting.airtable import outreach_store as OA
from channel_vetting.outreach import ledger as L
from channel_vetting.outreach import mailer as M


class _Resp:
    def __init__(self, status_code=200, payload=None, bad_json=False):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"records": []}
        self._bad_json = bad_json
        self.text = "error body"
        self.headers = {}

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._payload


def _record(rid, **fields):
    return {"id": rid, "fields": fields}


@pytest.fixture
def store():
    return OA.AirtableLedgerStore(table_name="tblTEST")


# --- Reads that gate a send must RAISE, never return empty ------------------

@pytest.mark.parametrize(
    "method,args",
    [
        ("find_by_key", ("UC_a:c1",)),
        ("find_sent_for_channel", ("UC_a",)),
        ("count_claimed_on", ("2026-08-14",)),
    ],
)
@pytest.mark.parametrize(
    "resp_kw",
    [
        {"status_code": 500},
        {"status_code": 429},
        {"status_code": 422},
        {"bad_json": True},
    ],
    ids=["500", "429", "422", "unparseable-200"],
)
def test_send_gating_reads_raise_on_failure(store, monkeypatch, method, args, resp_kw):
    monkeypatch.setattr(OA.HTTP, "get", lambda *a, **k: _Resp(**resp_kw))
    with pytest.raises(L.LedgerUnavailable):
        getattr(store, method)(*args)


def test_send_gating_reads_raise_on_transport_error(store, monkeypatch):
    import requests

    def boom(*a, **k):
        raise requests.ConnectionError("network down")

    monkeypatch.setattr(OA.HTTP, "get", boom)
    with pytest.raises(L.LedgerUnavailable):
        store.find_by_key("UC_a:c1")


def test_a_failed_read_never_looks_like_never_contacted(store, monkeypatch):
    """
    The whole point, stated as a test: if this returned [] on a 500, claim()
    would read "no prior send" and authorise a duplicate email.
    """
    monkeypatch.setattr(OA.HTTP, "get", lambda *a, **k: _Resp(status_code=500))
    fake_ledger = store
    with pytest.raises(L.LedgerUnavailable):
        L.claim(
            fake_ledger, channel_id="UC_a", campaign="c1", niche="Home Theater",
            recipient_email="a@b.com", qualification="Qualified",
        )


# --- find_stranded is reporting only, so it fails SOFT -----------------------

def test_find_stranded_fails_soft(store, monkeypatch):
    """An empty result here under-reports a problem; it cannot authorise a send."""
    monkeypatch.setattr(OA.HTTP, "get", lambda *a, **k: _Resp(status_code=500))
    assert store.find_stranded("2026-08-14T11:00:00Z") == []


# --- Pagination --------------------------------------------------------------

def test_reads_follow_the_offset_cursor(store, monkeypatch):
    pages = [
        _Resp(payload={"records": [_record("rec1")], "offset": "o1"}),
        _Resp(payload={"records": [_record("rec2")]}),
    ]
    seen = []

    def fake_get(url, **kwargs):
        seen.append(kwargs["params"].get("offset"))
        return pages[len(seen) - 1]

    monkeypatch.setattr(OA.HTTP, "get", fake_get)
    rows = store.find_by_key("k")
    assert [r["record_id"] for r in rows] == ["rec1", "rec2"]
    assert seen == [None, "o1"], "second page must carry the offset"


# --- Writes must not typecast ------------------------------------------------

def test_claim_post_sends_typecast_false(store, monkeypatch):
    """
    typecast=True silently CREATES a missing single-select option, so a typo
    would mint a fifth Send State the guard has no rule for.
    """
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs["json"])
        return _Resp(status_code=200, payload={"id": "recNEW"})

    monkeypatch.setattr(OA, "post_with_rate_limit_retry", fake_post)
    assert store.create_claim({"Send State": "Claimed"}) == "recNEW"
    assert captured["typecast"] is False


def test_patch_sends_typecast_false(store, monkeypatch):
    captured = {}

    def fake_patch(url, **kwargs):
        captured.update(kwargs["json"])
        return _Resp(status_code=200)

    monkeypatch.setattr(OA.HTTP, "patch", fake_patch)
    assert store.patch("rec1", {"Send State": "Sent"})
    assert captured["typecast"] is False


def test_claim_uses_the_429_only_post_helper_not_a_plain_post(store, monkeypatch):
    """
    A general POST retry after a lost 5xx would create a SECOND claim row for
    one send. Only 429 — rejected unprocessed — is safe to repeat.
    """
    called = []
    monkeypatch.setattr(
        OA, "post_with_rate_limit_retry",
        lambda url, **k: called.append("helper") or _Resp(payload={"id": "r"}),
    )

    def forbidden(*a, **k):
        raise AssertionError("must not use a plain session POST for the claim")

    monkeypatch.setattr(OA.HTTP, "post", forbidden)
    store.create_claim({"a": 1})
    assert called == ["helper"]


# --- Claim write failures return None, so the caller does not send -----------

@pytest.mark.parametrize("resp_kw", [{"status_code": 500}, {"status_code": 422}, {"bad_json": True}])
def test_claim_write_failure_returns_none(store, monkeypatch, resp_kw):
    monkeypatch.setattr(OA, "post_with_rate_limit_retry", lambda url, **k: _Resp(**resp_kw))
    assert store.create_claim({"a": 1}) is None


def test_claim_write_transport_error_returns_none(store, monkeypatch):
    import requests

    def boom(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(OA, "post_with_rate_limit_retry", boom)
    assert store.create_claim({"a": 1}) is None


# --- The daily cap must be counted in PROSPECT days, not UTC -----------------

def test_daily_count_converts_to_the_prospect_day_zone(store, monkeypatch):
    """
    Claimed At is UTC but the cap is denominated in prospect days. Without
    SET_TIMEZONE the budget rolls over 4-5 hours early for Toronto — the same
    silent clock desync the quota zone and prospect zone were separated to avoid.
    """
    captured = {}

    def fake_get(url, **kwargs):
        captured.update(kwargs["params"])
        return _Resp(payload={"records": [_record("r1"), _record("r2")]})

    monkeypatch.setattr(OA.HTTP, "get", fake_get)
    assert store.count_claimed_on("2026-08-14") == 2
    formula = captured["filterByFormula"]
    assert "SET_TIMEZONE" in formula, "must not compare raw UTC dates"
    assert OA.PROSPECT_DAY_TZ in formula


def test_formula_values_are_quoted(store, monkeypatch):
    """A stray apostrophe would malform the formula -> 422 -> a wrong answer."""
    captured = {}
    monkeypatch.setattr(
        OA.HTTP, "get",
        lambda url, **k: (captured.update(k["params"]), _Resp())[1],
    )
    store.find_by_key("UC_o'brien:c1")
    assert "\\'" in captured["filterByFormula"]


def test_ever_sent_query_is_campaign_independent(store, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        OA.HTTP, "get",
        lambda url, **k: (captured.update(k["params"]), _Resp())[1],
    )
    store.find_sent_for_channel("UC_a")
    formula = captured["filterByFormula"]
    assert "Channel ID" in formula and "Sent" in formula
    assert "Campaign" not in formula, "the guard must not be scoped to a campaign"


# --- Lease store -------------------------------------------------------------

def test_unreadable_lock_returns_none_so_the_run_refuses_to_start(monkeypatch):
    lease = OA.AirtableLeaseStore(table_name="tblLOCK")
    monkeypatch.setattr(OA.HTTP, "get", lambda *a, **k: _Resp(status_code=500))
    assert lease.read() is None
    assert not L.acquire_lease(lease, holder="run-1", stale_after_minutes=60).acquired


def test_empty_lock_table_returns_none_rather_than_creating_a_row(monkeypatch):
    """
    Creating the row here would race exactly like the lock it guards, so an
    empty table is a loud refusal instead.
    """
    lease = OA.AirtableLeaseStore(table_name="tblLOCK")
    monkeypatch.setattr(OA.HTTP, "get", lambda *a, **k: _Resp(payload={"records": []}))
    monkeypatch.setattr(
        OA, "post_with_rate_limit_retry",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not create a lock row")),
    )
    assert lease.read() is None


def test_lease_read_returns_the_single_row(monkeypatch):
    lease = OA.AirtableLeaseStore(table_name="tblLOCK")
    monkeypatch.setattr(
        OA.HTTP, "get",
        lambda *a, **k: _Resp(payload={"records": [_record("recLOCK", Holder="", **{"Lock Name": "outreach"})]}),
    )
    row = lease.read()
    assert row["record_id"] == "recLOCK"


# --- Demo mode: emailing a real creator must be unrepresentable --------------

def _demo_mailer(real_email, *, demo_mode, demo_recipient):
    """The redirect now lives inside the Mailer, so it cannot be bypassed."""
    m = M.Mailer(sender="s@x.test", credentials=None,
                 demo_mode=demo_mode, demo_recipient=demo_recipient)
    return m.resolve_recipient(real_email)


def test_demo_mode_redirects_every_message_to_the_test_mailbox():
    got = _demo_mailer(
        "realcreator@example.com", demo_mode=True, demo_recipient="me@mine.test"
    )
    assert got == "me@mine.test"


def test_demo_mode_without_a_recipient_raises_rather_than_falling_back():
    """
    The fallback-to-real-address is the obvious convenience and is exactly the
    bug that would email a creator during a demo. It must not be reachable.
    """
    with pytest.raises(M.DemoModeError) as exc:
        _demo_mailer("realcreator@example.com", demo_mode=True, demo_recipient="")
    assert "real prospect" in str(exc.value)


def test_live_mode_uses_the_real_address():
    got = _demo_mailer(
        "realcreator@example.com", demo_mode=False, demo_recipient="me@mine.test"
    )
    assert got == "realcreator@example.com"


@pytest.mark.parametrize("raw", [None, "", "true", "TRUE", "yes", "0", "no", "off", "fasle"])
def test_only_the_literal_false_disables_a_default_true_flag(raw, monkeypatch):
    """
    The fail-safe direction, pinned.

    Note "off", "0" and "no" all leave the flag ON — that reads wrong until you
    see the asymmetry it buys: a half-configured environment, or a typo like
    "fasle", stays safe. A default=False flag parses the opposite way, and the
    two want opposite behaviour for the same reason: each defaults toward the
    harmless outcome for its own feature. A browser step that fails to start
    costs email coverage; a demo gate that fails to start emails real creators.
    """
    from channel_vetting import config

    if raw is None:
        monkeypatch.delenv("PROBE_FLAG", raising=False)
    else:
        monkeypatch.setenv("PROBE_FLAG", raw)
    assert config.env_flag("PROBE_FLAG", default=True) is True


@pytest.mark.parametrize("raw", ["false", "False", "FALSE", " FALSE "])
def test_a_default_true_flag_is_disabled_only_deliberately(raw, monkeypatch):
    from channel_vetting import config

    monkeypatch.setenv("PROBE_FLAG", raw)
    assert config.env_flag("PROBE_FLAG", default=True) is False


@pytest.mark.parametrize(
    "raw,expected",
    [("true", True), ("TRUE", True), (" true ", True), ("false", False), ("", False), ("yes", False)],
)
def test_a_default_false_flag_requires_the_literal_true(raw, expected, monkeypatch):
    from channel_vetting import config

    monkeypatch.setenv("PROBE_FLAG", raw)
    assert config.env_flag("PROBE_FLAG", default=False) is expected


# --- The queue selector ------------------------------------------------------

def _queue_formula(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        OA.HTTP, "get",
        lambda url, **k: (captured.update(k["params"]), _Resp())[1],
    )
    OA.get_queued_prospects("tblX")
    return captured["filterByFormula"]


def test_queue_never_uses_BLANK_for_the_dateTime(monkeypatch):
    """
    REGRESSION, measured live 2026-08-14 on the real base with
    `Send Requested At` freshly created and empty on every row:

        {Send Requested At} != BLANK()   -> 47 rows   (ALL of them)
        {Send Requested At} != ""        ->  0 rows   (correct)

    So `!= BLANK()` MATCHES an empty dateTime. Had it shipped, the selector
    would have returned every Approved+Qualified row as "queued" and a --send
    run would have emailed 47 creators nobody queued. Demo mode would have
    redirected them all, which is exactly why that gate defaults on and is a
    separate switch from --dry-run.

    Asserted against the BUILT FORMULA, not the module source — the source
    explains the bug and would match a naive substring check on its own
    explanation.
    """
    assert "BLANK()" not in _queue_formula(monkeypatch), (
        "Airtable's != BLANK() does not exclude an empty date/dateTime. "
        'Use {Field} != "" instead.'
    )


def test_queue_requires_all_five_conditions(monkeypatch):
    formula = _queue_formula(monkeypatch)
    assert "{Qualification} = 'Qualified'" in formula
    assert "{Status} = 'Approved'" in formula
    assert '{Send Requested At} != ""' in formula
    assert "{Email} != ''" in formula
    assert "{Outreach Ineligible Reason} = ''" in formula


@pytest.mark.parametrize("excluded", ["New", "Reviewing", "Rejected", "Contacted"])
def test_queue_excludes_every_non_approved_status_by_construction(monkeypatch, excluded):
    """Not by omission — the formula pins Approved, so the rest cannot appear."""
    assert f"'{excluded}'" not in _queue_formula(monkeypatch)


def test_queue_is_ordered_oldest_request_first(store, monkeypatch):
    """A row that has waited must not be starved by a fresher one every run."""
    monkeypatch.setattr(
        OA.HTTP, "get",
        lambda url, **k: _Resp(payload={"records": [
            _record("recNEW", **{"Send Requested At": "2026-08-14T12:00:00.000Z"}),
            _record("recOLD", **{"Send Requested At": "2026-08-01T09:00:00.000Z"}),
        ]}),
    )
    rows = OA.get_queued_prospects("tblX")
    assert [r["record_id"] for r in rows] == ["recOLD", "recNEW"]


def test_queue_read_failure_raises_rather_than_reporting_an_empty_queue(store, monkeypatch):
    monkeypatch.setattr(OA.HTTP, "get", lambda *a, **k: _Resp(status_code=500))
    with pytest.raises(L.LedgerUnavailable):
        OA.get_queued_prospects("tblX")
