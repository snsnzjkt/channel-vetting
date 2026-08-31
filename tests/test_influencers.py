"""
Pins email chain step 4 (influencers.club).

The behaviours that matter here are the ones that cost money or lose a
prospect: the two meanings of 429, the lookup budget, and the screening a
returned address has to survive before it reaches an Airtable cell a human
runs outreach from.
"""
import json
import os
from unittest import mock

import pytest
import requests

from channel_vetting.enrichment import email_influencers
from channel_vetting import pipeline
from channel_vetting.enrichment.email_influencers import InfluencersClient, null_client


class FakeResponse:
    """Enough of requests.Response for the client's paths."""

    def __init__(self, status_code=200, payload=None, text=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload or {})
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture
def slept():
    """Records cooldown waits instead of taking them."""
    return []


@pytest.fixture
def client(slept):
    return InfluencersClient(sleep=slept.append)


def _mock_post(monkeypatch, response, calls=None):
    def fake_post(url, **kwargs):
        if calls is not None:
            calls.append((url, kwargs))
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(email_influencers.HTTP, "post", fake_post)


def test_returns_validated_email(monkeypatch, client):
    _mock_post(monkeypatch, FakeResponse(payload={"result": {"email": "hi@creator.com"}}))
    assert client.find_email("UC123") == "hi@creator.com"


def test_sends_channel_id_and_must_have(monkeypatch, client):
    calls = []
    _mock_post(monkeypatch, FakeResponse(payload={"result": {"email": "a@b.com"}}), calls)

    client.find_email("UC_the_channel")

    _, kwargs = calls[0]
    body = kwargs["json"]
    assert body["handle"] == "UC_the_channel"
    assert body["platform"] == "youtube"
    # must_have is what makes a miss free — a regression to "preferred"
    # silently starts billing for unvalidated addresses.
    assert body["email_required"] == "must_have"


def test_strips_surrounding_whitespace(monkeypatch, client):
    _mock_post(monkeypatch, FakeResponse(payload={"result": {"email": "  hi@creator.com \n"}}))
    assert client.find_email("UC123") == "hi@creator.com"


@pytest.mark.parametrize("payload", [
    {"result": {"email": ""}},
    {"result": {"email": None}},
    {"result": {}},
    {"result": None},
    {},
])
def test_absent_email_is_empty_string_not_an_error(monkeypatch, client, payload):
    """A 200 with no address is the normal must_have outcome, and free."""
    _mock_post(monkeypatch, FakeResponse(payload=payload))
    assert client.find_email("UC123") == ""


def test_blocklisted_domain_is_discarded(monkeypatch, client):
    """A sponsor/platform domain is not the creator's address, whatever
    source produced it — the same screen scraped addresses get."""
    _mock_post(monkeypatch, FakeResponse(payload={"result": {"email": "team@patreon.com"}}))
    assert client.find_email("UC123") == ""


def test_freemail_is_kept(monkeypatch, client):
    """gmail is the single most common creator contact domain — folding
    freemail into the blocklist was a real bug once and must not return."""
    _mock_post(monkeypatch, FakeResponse(payload={"result": {"email": "creator@gmail.com"}}))
    assert client.find_email("UC123") == "creator@gmail.com"


def test_unparseable_email_is_discarded(monkeypatch, client):
    """fullmatch, not search: a sentence containing an address must not be
    stored as the address."""
    _mock_post(
        monkeypatch,
        FakeResponse(payload={"result": {"email": "contact us at a@b.com today"}}),
    )
    assert client.find_email("UC123") == ""


def test_non_json_200_is_survived(monkeypatch, client):
    """A captive-portal interstitial serves HTML with a 200."""
    _mock_post(monkeypatch, FakeResponse(payload=None, text="<html>nope</html>"))
    assert client.find_email("UC123") == ""


def test_request_exception_is_survived(monkeypatch, client):
    _mock_post(monkeypatch, requests.RequestException("unreachable"))
    assert client.find_email("UC123") == ""
    assert client.enabled, "a transport blip must not disable the whole run"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500])
def test_error_status_returns_empty(monkeypatch, client, status):
    _mock_post(monkeypatch, FakeResponse(status_code=status, payload={"error": "nope"}))
    assert client.find_email("UC123") == ""


# --- the two meanings of 429 ------------------------------------------------

def test_rate_limit_429_does_not_disable_the_client(monkeypatch, client):
    """With retry_after present it is the per-minute limit, which clears on
    its own — the next candidate must still be tried."""
    _mock_post(
        monkeypatch,
        FakeResponse(status_code=429, payload={"error": "slow down", "retry_after": 30}),
    )
    assert client.find_email("UC123") == ""
    assert client.enabled


def test_credit_cap_429_trips_the_breaker(monkeypatch, client):
    """Without retry_after it is the fair-use cap, which does not clear
    until renewal — every further request this run would be wasted."""
    _mock_post(
        monkeypatch,
        FakeResponse(status_code=429, payload={"error": "credit limit reached"}),
    )
    assert client.find_email("UC123") == ""
    assert not client.enabled


def test_credit_cap_stops_further_requests(monkeypatch, client):
    calls = []
    _mock_post(
        monkeypatch,
        FakeResponse(status_code=429, payload={"error": "credit limit reached"}),
        calls,
    )

    client.find_email("UC1")
    client.find_email("UC2")
    client.find_email("UC3")

    assert len(calls) == 1, "the breaker must stop the client from asking again"


def test_rate_limit_header_is_not_mistaken_for_the_cap(monkeypatch, client):
    """The per-minute limit sent the conventional way — a Retry-After
    HEADER with no body field — must not disable step 4 for the run."""
    _mock_post(
        monkeypatch,
        FakeResponse(
            status_code=429,
            payload={"error": "too many requests"},
            headers={"Retry-After": "30"},
        ),
    )
    assert client.find_email("UC123") == ""
    assert client.enabled


def test_a_rate_limit_is_waited_out_and_retried(monkeypatch, client, slept):
    """The limit is account-wide and lasts a minute, so returning "" on a 429
    doesn't skip ONE candidate — it drops the address for every candidate
    processed during the cooldown."""
    responses = [
        FakeResponse(status_code=429, payload={"error": "slow", "retry_after": 12}),
        FakeResponse(payload={"result": {"email": "hi@creator.com"}}),
    ]
    monkeypatch.setattr(email_influencers.HTTP, "post", lambda url, **kw: responses.pop(0))

    assert client.find_email("UC123") == "hi@creator.com"
    assert slept == [12.0], "the vendor's cooldown must actually be waited out"


def test_a_second_rate_limit_gives_up_rather_than_waiting_again(monkeypatch, client, slept):
    _mock_post(
        monkeypatch,
        FakeResponse(status_code=429, payload={"error": "slow", "retry_after": 5}),
    )
    assert client.find_email("UC123") == ""
    assert len(slept) == 1, "one wait per channel, not an unbounded loop"
    assert client.enabled, "a busy minute is not an outage"


@pytest.mark.parametrize("raw,expected", [
    (12, 12.0),
    ("12", 12.0),
    (12.5, 12.5),
    ("garbage", InfluencersClient.DEFAULT_RATE_LIMIT_WAIT_SECONDS),
    (None, InfluencersClient.DEFAULT_RATE_LIMIT_WAIT_SECONDS),
    (-5, InfluencersClient.DEFAULT_RATE_LIMIT_WAIT_SECONDS),
    (99999, InfluencersClient.MAX_RATE_LIMIT_WAIT_SECONDS),
    # "NaN" and "inf" parse cleanly through float() and then fail every
    # comparison — nan <= 0 is False and min(nan, cap) returns nan — so
    # without an isfinite() check they reach time.sleep() and raise
    # ValueError, which nothing up through run() catches.
    ("NaN", InfluencersClient.DEFAULT_RATE_LIMIT_WAIT_SECONDS),
    ("nan", InfluencersClient.DEFAULT_RATE_LIMIT_WAIT_SECONDS),
    (float("nan"), InfluencersClient.DEFAULT_RATE_LIMIT_WAIT_SECONDS),
    # inf would also be clamped by the min(), but isfinite() rejects it
    # first — a vendor sending "infinity" means something is broken, so the
    # default is a better read of intent than a 90-second park.
    ("inf", InfluencersClient.DEFAULT_RATE_LIMIT_WAIT_SECONDS),
    ("-inf", InfluencersClient.DEFAULT_RATE_LIMIT_WAIT_SECONDS),
])
def test_retry_after_is_read_defensively(raw, expected):
    """The field's type is undocumented, and the header form is always a
    string. A hostile or broken value must not park the run."""
    assert InfluencersClient._wait_seconds(raw) == expected


def test_a_nan_retry_after_does_not_kill_the_run(monkeypatch, client, slept):
    """End-to-end on the escape path: a NaN cooldown must degrade to the
    default wait, not raise ValueError out of find_email()."""
    responses = [
        FakeResponse(status_code=429, payload={"error": "slow"},
                     headers={"Retry-After": "NaN"}),
        FakeResponse(payload={"result": {"email": "hi@creator.com"}}),
    ]
    monkeypatch.setattr(email_influencers.HTTP, "post", lambda url, **kw: responses.pop(0))

    assert client.find_email("UC123") == "hi@creator.com"
    assert slept == [InfluencersClient.DEFAULT_RATE_LIMIT_WAIT_SECONDS]


def test_influencers_session_ignores_retry_after_headers():
    """urllib3 sleeps a Retry-After verbatim with no ceiling, and this
    session retries 5xx — so a 503 with `Retry-After: 86400` would park the
    run inside the adapter, where neither the budget nor the breaker can see
    it. AIRTABLE must keep honouring it; its 429 cooldown is load-bearing."""
    from channel_vetting.core import http_client

    assert http_client.INFLUENCERS.adapters["https://"].max_retries.respect_retry_after_header is False
    assert http_client.AIRTABLE.adapters["https://"].max_retries.respect_retry_after_header is True


def test_cap_429_costs_no_lookup(monkeypatch, client):
    """The budget bounds CREDIT spend, and a 429 is never billable."""
    _mock_post(
        monkeypatch,
        FakeResponse(status_code=429, payload={"error": "credit limit reached"}),
    )
    client.find_email("UC1")
    assert client.lookups_spent == 0


# --- a failing vendor must not consume the run's budget ---------------------

@pytest.mark.parametrize("response", [
    FakeResponse(status_code=500, payload={"error": "boom"}),
    FakeResponse(status_code=503, payload={"error": "later"}),
    FakeResponse(status_code=429, payload={"error": "slow", "retry_after": 5}),
    # ConnectTimeout specifically — the only failure that provably predates
    # the send, so the vendor never processed or billed it. A generic
    # RequestException (or a plain ConnectionError, which also wraps a
    # mid-response drop) is NOT here on purpose: those may have been billed,
    # and they are charged (see test_a_lost_response_is_charged_to_the_budget).
    requests.exceptions.ConnectTimeout("no route"),
])
def test_unbillable_outcomes_do_not_burn_the_budget(monkeypatch, response):
    """A vendor blip must not exhaust a 100-lookup allowance having spent
    no money — the budget exists to bound credits, not requests."""
    client = InfluencersClient(max_lookups=2, sleep=lambda s: None)
    _mock_post(monkeypatch, response)

    for i in range(5):
        client.find_email(f"UC{i}")

    assert client.lookups_spent == 0


# --- an unreachable vendor must not eat the run's wall clock ----------------

@pytest.mark.parametrize("response", [
    FakeResponse(status_code=503, payload={"error": "later"}),
    requests.RequestException("unreachable"),
])
def test_repeated_failures_disable_the_step(monkeypatch, response):
    """Each attempt costs ~45s of adapter backoff plus up to five 30s
    timeouts. Repeating that per candidate through an outage is hours of
    wall clock for zero addresses."""
    calls = []
    client = InfluencersClient(sleep=lambda s: None)
    _mock_post(monkeypatch, response, calls)

    for i in range(10):
        client.find_email(f"UC{i}")

    assert len(calls) == InfluencersClient.MAX_CONSECUTIVE_FAILURES
    assert not client.enabled


def test_a_success_resets_the_failure_streak(monkeypatch, client):
    """CONSECUTIVE, not cumulative — a vendor answering intermittently is
    degraded but still worth asking."""
    responses = [
        FakeResponse(status_code=503, payload={"error": "boom"}),
        FakeResponse(status_code=503, payload={"error": "boom"}),
        FakeResponse(payload={"result": {"email": "hi@creator.com"}}),
        FakeResponse(status_code=503, payload={"error": "boom"}),
        FakeResponse(status_code=503, payload={"error": "boom"}),
    ]
    monkeypatch.setattr(email_influencers.HTTP, "post", lambda url, **kw: responses.pop(0))

    for i in range(5):
        client.find_email(f"UC{i}")

    assert client.enabled, "two failures either side of a success is not an outage"


def test_a_4xx_does_not_count_toward_the_outage_breaker(monkeypatch, client):
    """A 400/404 is the vendor answering — the channel is just unknown to
    it. Only a 5xx or an unreachable host means 'stop asking'."""
    _mock_post(monkeypatch, FakeResponse(status_code=404, payload={"error": "no"}))

    for i in range(10):
        client.find_email(f"UC{i}")

    assert client.enabled


@pytest.mark.parametrize("interruption", [
    FakeResponse(status_code=404, payload={"error": "unknown channel"}),
    FakeResponse(status_code=429, payload={"error": "slow", "retry_after": 1}),
])
def test_any_vendor_answer_clears_the_failure_streak(monkeypatch, slept, interruption):
    """`503, 503, <answer>, 503, 503` is not three-in-a-row. Only a response
    that never arrives counts toward an outage, so any answer — even an
    error one — has to reset the streak or a working vendor gets disabled."""
    boom = FakeResponse(status_code=503, payload={"error": "boom"})
    responses = [boom, boom, interruption, boom, boom]
    client = InfluencersClient(sleep=slept.append)
    monkeypatch.setattr(email_influencers.HTTP, "post", lambda url, **kw: responses.pop(0))

    for i in range(4):
        client.find_email(f"UC{i}")

    assert client.enabled


# --- a malformed body must not abort the run --------------------------------

@pytest.mark.parametrize("payload", ["just a string", [1, 2, 3], 42])
def test_non_object_json_is_survived(monkeypatch, client, payload):
    """Valid JSON is not necessarily an object. `.get()` on a str/list
    raises AttributeError, which nothing up through run() catches."""
    _mock_post(monkeypatch, FakeResponse(payload=payload))
    assert client.find_email("UC123") == ""


@pytest.mark.parametrize("payload", ["just a string", [1, 2, 3]])
def test_non_object_json_on_a_429_is_survived(monkeypatch, client, payload):
    """The same trap on the rate-limit path, which also calls .get()."""
    _mock_post(monkeypatch, FakeResponse(status_code=429, payload=payload))
    assert client.find_email("UC123") == ""


# --- budget -----------------------------------------------------------------

def test_credit_budget_is_enforced(monkeypatch):
    """The budget stops the step once that many addresses have been BILLED."""
    calls = []
    client = InfluencersClient(max_lookups=2, sleep=lambda s: None)
    _mock_post(monkeypatch, FakeResponse(payload={"result": {"email": "a@b.com"}}), calls)

    for i in range(5):
        client.find_email(f"UC{i}")

    assert len(calls) == 2
    assert not client.enabled


def test_lookups_spent_counts_billable_hits(monkeypatch, client):
    _mock_post(monkeypatch, FakeResponse(payload={"result": {"email": "a@b.com"}}))
    client.find_email("UC1")
    client.find_email("UC2")
    assert client.lookups_spent == 2


def test_free_misses_never_exhaust_the_budget(monkeypatch):
    """The bug this pins: under `must_have` an empty 200 is FREE, and step 4
    only runs for channels the free steps missed — so misses are the common
    case. Counting them would turn a credit budget into a request cap and
    silently disable the step having spent nothing."""
    calls = []
    client = InfluencersClient(max_lookups=2, sleep=lambda s: None)
    _mock_post(monkeypatch, FakeResponse(payload={"result": {}}), calls)

    for i in range(20):
        client.find_email(f"UC{i}")

    assert len(calls) == 20
    assert client.lookups_spent == 0
    assert client.enabled


@pytest.mark.parametrize("email", [
    "x@patreon.com",                 # rejected by the domain blocklist
    "contact us at a@b.com today",   # rejected by the fullmatch screen
])
def test_a_discarded_address_still_costs_a_credit(monkeypatch, client, email):
    """The vendor bills for RETURNING an address. Our blocklist and
    fullmatch screens are this pipeline's policy applied afterwards, and
    rejecting an address does not refund it — so counting post-screen would
    undercount real spend and let a run of blocklisted results overrun the
    budget."""
    _mock_post(monkeypatch, FakeResponse(payload={"result": {"email": email}}))

    assert client.find_email("UC1") == ""
    assert client.lookups_spent == 1


def test_the_budget_stops_a_run_of_discarded_addresses(monkeypatch):
    """The overrun the above prevents, end to end."""
    calls = []
    client = InfluencersClient(max_lookups=2, sleep=lambda s: None)
    _mock_post(
        monkeypatch,
        FakeResponse(payload={"result": {"email": "spam@patreon.com"}}),
        calls,
    )

    for i in range(10):
        client.find_email(f"UC{i}")

    assert len(calls) == 2
    assert not client.enabled


# --- disabled clients -------------------------------------------------------

def test_null_client_makes_no_request(monkeypatch):
    calls = []
    _mock_post(monkeypatch, FakeResponse(payload={"result": {"email": "a@b.com"}}), calls)

    assert null_client().find_email("UC123") == ""
    assert not calls


def test_from_config_is_inert_without_a_key(monkeypatch):
    """The production factory, on the configuration CI ships by default."""
    calls = []
    _mock_post(monkeypatch, FakeResponse(payload={"result": {"email": "a@b.com"}}), calls)
    monkeypatch.setattr(email_influencers, "INFLUENCERS_API_KEY", "")

    client = InfluencersClient.from_config()
    assert not client.enabled
    assert client.find_email("UC123") == ""
    assert not calls


def test_from_config_is_live_with_a_key(monkeypatch):
    monkeypatch.setattr(email_influencers, "INFLUENCERS_API_KEY", "a-key")
    assert InfluencersClient.from_config().enabled


def test_blank_channel_id_makes_no_request(monkeypatch, client):
    calls = []
    _mock_post(monkeypatch, FakeResponse(payload={"result": {"email": "a@b.com"}}), calls)

    assert client.find_email("") == ""
    assert not calls
    assert client.lookups_spent == 0


# --- chain placement --------------------------------------------------------

class StubEmailSource:
    def __init__(self, email=""):
        self.email = email
        self.calls = []

    def find_email(self, channel_id):
        self.calls.append(channel_id)
        return self.email

    def find_contact(self, channel_id, need_email=True):
        # Used when this stub stands in for the browser scraper (step 5). An
        # address implies the link list wasn't empty; no address leaves
        # presence unknown (None) here — these tests don't exercise the
        # empty-link-list case.
        #
        # need_email=False is the link-list-only mode, which the chain also
        # calls once an EARLIER step found the address — so it must not hand
        # back an email, or a step-1 hit would be mislabelled as step 5.
        self.calls.append(channel_id)
        if not need_email:
            return "", (True if self.email else None)
        return self.email, (True if self.email else None)


def _stats(channel_id="UC123", business_email=""):
    return {
        "channel_id": channel_id,
        "business_email": business_email,
        "uploads_playlist_id": "UU123",
    }


def _performance(repeated_email=""):
    return {
        "repeated_email": repeated_email,
        "next_page_token": "",
        "video_descriptions": [],
    }


@pytest.fixture
def no_deep_scan(monkeypatch):
    """Step 3 costs quota; stub it out so these tests isolate steps 4 and 5."""
    monkeypatch.setattr(pipeline, "scan_older_videos_for_email", lambda *a, **k: "")


def test_step_4_runs_before_the_browser(no_deep_scan):
    enricher = StubEmailSource("api@creator.com")
    scraper = StubEmailSource("browser@creator.com")

    email, source, _ = pipeline.resolve_email_with_source(
        _stats(), _performance(), scraper, enricher
    )

    assert email == "api@creator.com"
    assert source == pipeline.EMAIL_SOURCE_INFLUENCERS
    # The browser IS consulted — but only for the link list (need_email=False),
    # never for an address. Step 4's answer stands, and the source label proves
    # the browser didn't supply it. See resolve_email_with_source for why the
    # link list has to be read even when the address is already in hand.
    assert scraper.calls == ["UC123"]


def test_browser_still_runs_when_step_4_misses(no_deep_scan):
    enricher = StubEmailSource("")
    scraper = StubEmailSource("browser@creator.com")

    email, source, _ = pipeline.resolve_email_with_source(
        _stats(), _performance(), scraper, enricher
    )

    assert email == "browser@creator.com"
    assert source == pipeline.EMAIL_SOURCE_BROWSER
    assert enricher.calls == ["UC123"]


def test_free_steps_still_win_over_step_4(no_deep_scan):
    """Steps 1-2 are free and stronger; step 4 must never be paid for when
    an address is already known."""
    enricher = StubEmailSource("api@creator.com")

    email, source, _ = pipeline.resolve_email_with_source(
        _stats(business_email="about@creator.com"), _performance(), None, enricher
    )

    assert email == "about@creator.com"
    assert source == pipeline.EMAIL_SOURCE_ABOUT
    assert not enricher.calls


def test_repeated_email_still_wins_over_step_4(no_deep_scan):
    enricher = StubEmailSource("api@creator.com")

    email, source, _ = pipeline.resolve_email_with_source(
        _stats(), _performance(repeated_email="repeat@creator.com"), None, enricher
    )

    assert email == "repeat@creator.com"
    assert source == pipeline.EMAIL_SOURCE_REPEATED
    assert not enricher.calls


def test_chain_without_an_enricher_is_unchanged(no_deep_scan):
    """Existing callers that pass no enricher keep the old behaviour."""
    scraper = StubEmailSource("browser@creator.com")

    email, source, _ = pipeline.resolve_email_with_source(_stats(), _performance(), scraper)

    assert email == "browser@creator.com"
    assert source == pipeline.EMAIL_SOURCE_BROWSER


def test_post_is_retryable_on_the_influencers_session():
    """INFLUENCERS_RETRY_STATUSES is dead configuration unless POST is in
    the allowed set — the endpoint is only ever reached by POST. Pinned
    because the omission is silent: 5xx retries simply never happen."""
    from channel_vetting.core import http_client

    retry = http_client.INFLUENCERS.adapters["https://"].max_retries
    assert retry.is_retry("POST", 503) is True
    # ...and 429 is still excluded, because its two meanings are only
    # distinguishable from the body, which the adapter never sees.
    assert retry.is_retry("POST", 429) is False


def test_vendor_reported_credits_are_accumulated(monkeypatch, client):
    """The vendor returns its own `credits_cost`, which is exact where
    lookups_spent is only an upper bound."""
    _mock_post(monkeypatch, FakeResponse(payload={
        "result": {"email": "a@b.com"}, "credits_cost": 0.2,
    }))

    client.find_email("UC1")
    client.find_email("UC2")

    assert client.credits_reported == pytest.approx(0.4)
    assert client.lookups_spent == 2


def test_reported_credits_count_even_when_we_discard_the_address(monkeypatch, client):
    """Our screening verdict has no bearing on what the vendor charged."""
    _mock_post(monkeypatch, FakeResponse(payload={
        "result": {"email": "x@patreon.com"}, "credits_cost": 0.2,
    }))

    assert client.find_email("UC1") == ""
    assert client.credits_reported == pytest.approx(0.2)


@pytest.mark.parametrize("cost", [None, "0.2", True, float("nan"), float("inf"), -1, 0])
def test_a_bad_credits_cost_contributes_nothing(monkeypatch, client, cost):
    """An absent or nonsense value must not break the run or corrupt the
    total — `True` is int-like in Python and must not count as 1 credit."""
    _mock_post(monkeypatch, FakeResponse(payload={
        "result": {"email": "a@b.com"}, "credits_cost": cost,
    }))

    assert client.find_email("UC1") == "a@b.com"
    assert client.credits_reported == 0.0
    assert client.lookups_spent == 1, "the budget still counts the billable event"


def test_a_lost_response_is_charged_to_the_budget(monkeypatch, client):
    """A read timeout means the request was SENT and the answer lost — the
    vendor may have completed and billed it. Leaving it uncounted makes the
    cap bypassable, since it would touch only the outage breaker and any
    later success resets that."""
    _mock_post(monkeypatch, requests.exceptions.ReadTimeout("lost"))

    assert client.find_email("UC1") == ""
    assert client.lookups_spent == 1


def test_a_connect_timeout_is_not_charged(monkeypatch, client):
    """The one failure that provably reached nobody: the connection was
    never established, so the vendor never processed or billed it."""
    _mock_post(monkeypatch, requests.exceptions.ConnectTimeout("no route"))

    assert client.find_email("UC1") == ""
    assert client.lookups_spent == 0


def test_a_bare_connection_error_is_still_charged(monkeypatch, client):
    """ConnectionError is a broad transport wrapper — it also carries a
    connection dropped MID-RESPONSE, which happens after the vendor has done
    the work and billed. Exempting the whole family reopens the bypass."""
    _mock_post(monkeypatch, requests.exceptions.ConnectionError("reset by peer"))

    assert client.find_email("UC1") == ""
    assert client.lookups_spent == 1


def test_alternating_lost_responses_and_misses_still_hit_the_cap(monkeypatch):
    """The bypass this closes: the outage breaker alone cannot bound spend,
    because an ordinary miss in between resets it."""
    miss = FakeResponse(payload={"result": {}})
    lost = requests.exceptions.ReadTimeout("lost")
    script = [lost, miss, lost, miss, lost, miss, lost, miss]
    client = InfluencersClient(max_lookups=2, sleep=lambda s: None)

    def fake_post(url, **kwargs):
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(email_influencers.HTTP, "post", fake_post)

    for i in range(8):
        client.find_email(f"UC{i}")

    assert client.lookups_spent == 2
    assert not client.enabled, "the credit cap must bound spend on its own"
    assert len(script) > 0, "the client must have stopped before the script ran out"


def test_influencers_session_does_not_retry_a_lost_response():
    """A read timeout means the request WAS sent and the response was lost —
    the vendor may have completed the lookup and charged for it. Retrying
    spends a second credit for one answer, and the budget only sees the
    final response, so the overspend would be invisible. Connect and status
    retries stay on: neither can have been billed."""
    from channel_vetting.core import http_client

    retry = http_client.INFLUENCERS.adapters["https://"].max_retries
    assert retry.read == 0, "a lost response must not be re-POSTed"
    assert retry.connect == http_client.RETRY_TOTAL
    assert retry.is_retry("POST", 503) is True


def test_airtable_session_still_refuses_to_retry_post():
    """The influencers session widening its allowed methods must not have
    widened Airtable's — a retried POST there creates a duplicate row."""
    from channel_vetting.core import http_client

    retry = http_client.AIRTABLE.adapters["https://"].max_retries
    assert retry.is_retry("POST", 503) is False


# The "every source label is distinct" invariant is pinned once, in
# tests/test_resolve_email_source.py::test_every_source_label_is_distinct,
# which now includes EMAIL_SOURCE_INFLUENCERS. A second copy here would be
# the same guarantee asserted in two places — and the way one of them
# quietly stops covering the full set.


# --- the per-run email CREDIT ceiling (added 2026-09-01) -------------------


def test_the_email_step_is_bounded_by_credits_not_just_requests():
    """
    The default cap must be the CREDIT ceiling, not the 100-request one.

    INFLUENCERS_MAX_LOOKUPS_PER_RUN bounds requests, so its real cost is
    100 x EMAIL_COST_CREDITS = 20 credits/run — double
    INFLUENCERS_MAX_CREDITS_PER_DAY. It never fired only because the view
    floors were strict enough that few channels reached step 4. Lowering
    MIN_AVG_VIEWS on 2026-09-01 removed that accidental protection, so this
    asserts money is what bounds the step now.
    """
    from channel_vetting.config import (
        INFLUENCERS_MAX_EMAIL_CREDITS_PER_RUN,
        INFLUENCERS_MAX_LOOKUPS_PER_RUN,
    )
    from channel_vetting.enrichment.email_influencers import (
        EMAIL_COST_CREDITS,
        InfluencersClient,
    )

    client = InfluencersClient()
    ceiling = client._max_lookups * EMAIL_COST_CREDITS

    assert ceiling <= INFLUENCERS_MAX_EMAIL_CREDITS_PER_RUN
    # ...and the credit ceiling is the one actually binding, not the request cap.
    assert client._max_lookups < INFLUENCERS_MAX_LOOKUPS_PER_RUN


def test_the_email_ceiling_holds_spend_at_its_pre_change_level():
    """
    Measured email spend per run BEFORE the 2026-09-01 view-floor change was
    2.40 credits (08-25), 0.60 (08-26), 0.00 (08-27), 1.00 (08-28). The
    operator's requirement when lowering the floors was that credit spend not
    increase, so the ceiling must sit at that 2.40 high-water mark.

    A regression here means the criteria change started costing money, which is
    the one thing it was required not to do.
    """
    from channel_vetting.enrichment.email_influencers import (
        EMAIL_COST_CREDITS,
        InfluencersClient,
    )

    PRE_CHANGE_HIGH_WATER = 2.40
    client = InfluencersClient()
    assert client._max_lookups * EMAIL_COST_CREDITS <= PRE_CHANGE_HIGH_WATER + 1e-9


def test_the_email_ceiling_is_env_tunable():
    """Retuning is a secret change, not a deploy — same as the view floors."""
    import importlib

    from channel_vetting import config
    from channel_vetting.enrichment import email_influencers

    with mock.patch.dict(
        os.environ, {"INFLUENCERS_MAX_EMAIL_CREDITS_PER_RUN": "1.0"}, clear=False
    ):
        importlib.reload(config)
        importlib.reload(email_influencers)
        try:
            client = email_influencers.InfluencersClient()
            # 1.0 / 0.2 == 5 lookups
            assert client._max_lookups == 5
        finally:
            importlib.reload(config)
            importlib.reload(email_influencers)
