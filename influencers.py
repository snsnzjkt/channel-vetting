"""
Step 4 of the email chain: resolves a YouTube channel ID to a validated
contact address via influencers.club's enrich-by-handle endpoint.

Why it sits ahead of the browser step (now step 5): this is ONE HTTP call
where `browser_email.py` spends up to four page loads per candidate (the
creator's landing page, its /contact, a Facebook /about, each with its own
30s navigation timeout), and it works from a datacenter IP, which the
browser path measurably does not — GitHub-hosted runners sit in Azure
ranges YouTube challenges hard, so on CI the browser step is close to a
no-op. The browser stays as the last resort because it reads the creator's
OWN site, a genuinely different source from a creator-data platform's
index, and it recovered 4 addresses across 18 email-less rows.

Cost model, which is what makes the ordering safe: every request sends
`email_required: "must_have"`, and the vendor charges nothing for an empty
result or a failed validation under that setting. A miss is free, so
running this before the browser costs nothing on the channels the browser
would have caught. A hit is 0.2 credits.

Everything here fails SOFT — a bad key, an outage, a malformed body, a
tripped credit cap all return "" and let the chain continue. One channel is
never worth ending a run over, which is the same contract
`browser_email.find_email()` honours.
"""
import logging
import math
import time

import requests

from config import (
    INFLUENCERS_API_KEY,
    INFLUENCERS_BASE_URL,
    INFLUENCERS_EMAIL_REQUIRED,
    INFLUENCERS_ENRICH_PATH,
    INFLUENCERS_MAX_LOOKUPS_PER_RUN,
)
from enrichment import EMAIL_PATTERN, is_blocklisted_email
from http_client import INFLUENCERS as HTTP, safe_body

logger = logging.getLogger(__name__)

# The vendor's own identifier for this pipeline's platform. Their `handle`
# field accepts a username, a profile URL, or a YouTube channel ID — the
# channel ID is what process_candidate() already holds and the only one of
# the three that is stable across a creator renaming themselves.
PLATFORM_YOUTUBE = "youtube"

REQUEST_TIMEOUT_SECONDS = 30


class InfluencersClient:
    """
    A per-run enrichment client.

    Instance state, not module state, for two reasons: the lookup budget and
    the circuit breaker both describe ONE run, and a module-level counter
    would leak between tests in a suite that imports this once.

    Construct with `enabled=False` (or via `null_client()`) for an inert
    client that always returns "" — the same shape as
    `browser_email.null_scraper()`, so `main.py` treats a missing API key
    and a disabled browser identically.

    Deliberately takes NO api_key. The credential is bolted to the shared
    session once at import (`http_client.INFLUENCERS`), exactly as the
    YouTube key is, so a per-client key would be accepted and then silently
    ignored — an object advertising injectable auth it does not have.
    `from_config()` is where "is there a key at all" is decided.
    """

    # Consecutive unbillable failures before the step gives up for the run.
    # 3 is enough to ride out a blip while bounding an outage to three
    # candidates instead of every remaining one.
    MAX_CONSECUTIVE_FAILURES = 3

    # The vendor's limit is per-minute, so a cooldown is seconds, not
    # minutes. The ceiling stops a bad or hostile header from parking the
    # run for an hour; the default covers a 429 that names no wait at all.
    DEFAULT_RATE_LIMIT_WAIT_SECONDS = 30.0
    MAX_RATE_LIMIT_WAIT_SECONDS = 90.0

    def __init__(self, enabled=True, max_lookups=None, sleep=None):
        # One flag, not two. An earlier version split "configured off" from
        # "cap reached" so a log could tell them apart, but each condition
        # is already logged where it is DETECTED, and nothing ever read the
        # two apart — so the split was state carrying no information.
        self._active = enabled
        self._max_lookups = (
            INFLUENCERS_MAX_LOOKUPS_PER_RUN if max_lookups is None else max_lookups
        )
        self._lookups_spent = 0
        # Credits as the VENDOR reports them, when it does. Separate from
        # the budget counter on purpose: the budget bounds billable events
        # (and must also count the ones whose response never arrived, which
        # by definition carry no reported cost), while this is the exact
        # money figure for reporting. Don't merge them.
        self._credits_reported = 0.0
        self._consecutive_failures = 0
        # Injectable so tests don't actually wait out a cooldown, matching
        # http_client.post_with_rate_limit_retry()'s convention.
        self._sleep = sleep if sleep is not None else time.sleep

    @property
    def enabled(self) -> bool:
        """True if this client can still spend a lookup."""
        return self._active and self._lookups_spent < self._max_lookups

    @property
    def lookups_spent(self) -> int:
        """Billable events this run: addresses returned, plus requests whose
        response was lost and so may have been billed. An upper bound on
        credits — see `credits_reported` for the vendor's own figure."""
        return self._lookups_spent

    @property
    def credits_reported(self) -> float:
        """
        Credits as the vendor reported them in `credits_cost`.

        Not authoritative on its own: a lost response is billable but
        carries no reported cost, so this can UNDER-count exactly where
        `lookups_spent` over-counts. Reported alongside it, never instead.
        """
        return self._credits_reported

    @classmethod
    def from_config(cls) -> "InfluencersClient":
        """The client main.py uses, or an inert one when no key is set."""
        if not INFLUENCERS_API_KEY:
            logger.info(
                "INFLUENCERS_API_KEY is not set — email chain step 4 "
                "(influencers.club) is disabled for this run."
            )
            return cls(enabled=False)
        return cls()

    def find_email(self, channel_id: str) -> str:
        """
        Return a validated contact address for `channel_id`, or "".

        Never raises. Never spends a lookup once the budget is exhausted or
        the credit cap has been reported.
        """
        if not channel_id or not self.enabled:
            return ""

        resp = self._send(channel_id)
        if resp is None:
            return ""

        if resp.status_code != 200:
            logger.warning(
                "influencers.club returned %s for %s: %s",
                resp.status_code, channel_id, safe_body(resp),
            )
            # A 5xx has already cost ~45s of adapter backoff plus up to five
            # 30s timeouts before reaching here. Repeating that for every
            # remaining candidate during an outage is hours of wall clock
            # for zero addresses — on CI, billed runner minutes that can eat
            # the job timeout before any records are pushed.
            if resp.status_code >= 500:
                self._record_failure(str(resp.status_code))
            else:
                # A 4xx is the vendor answering — this channel is simply
                # unknown to it. Not an outage, so it must also CLEAR the
                # streak, or an intermittent 503 either side of a 404 would
                # read as three-in-a-row and disable a working step.
                self._consecutive_failures = 0
            return ""

        # A response that parsed is proof the vendor is reachable again.
        self._consecutive_failures = 0

        return self._email_from_response(resp, channel_id)

    def _record_billable(self) -> None:
        """
        Count one credit and disable the step once the budget is used up.

        Called when the VENDOR returned an address, which is the moment it
        bills — deliberately not when we decide to keep one. Our `fullmatch`
        and blocklist screens are this pipeline's policy applied after the
        fact; rejecting an address does not refund it. Counting post-screen
        would undercount real spend and let a run of blocklisted results
        overrun INFLUENCERS_MAX_LOOKUPS_PER_RUN.
        """
        self._lookups_spent += 1
        if self._lookups_spent >= self._max_lookups:
            logger.warning(
                "influencers.club credit budget (%d) reached — step 4 is "
                "disabled for the rest of this run.",
                self._max_lookups,
            )

    def _send(self, channel_id: str):
        """
        POST, waiting out ONE per-minute rate limit if the vendor asks for it.

        Returns a non-429 response, or None when the request failed outright,
        the credit cap tripped, or a second 429 said the cooldown outlasts a
        single wait.

        The wait is the point. The rate limit is account-wide and lasts a
        minute, so simply returning "" on a 429 does not skip ONE candidate —
        it silently drops the address for every candidate processed during
        the cooldown, which is the whole reason this step exists. Sleeping is
        cheap here: this is a batch job with no latency budget, and the
        adapter already sleeps ~45s absorbing an Airtable 429.
        """
        for attempt in range(2):
            resp = self._post(channel_id)
            if resp is None:
                self._record_failure("unreachable")
                return None

            if resp.status_code != 429:
                return resp

            # The vendor answered, so it is reachable — whichever kind of 429
            # this is, it is not the outage the failure streak tracks.
            self._consecutive_failures = 0

            wait = self._rate_limit_wait(resp)
            if wait is None:
                return None  # the credit cap; the breaker is already tripped

            if attempt:
                logger.warning(
                    "influencers.club rate-limited %s twice — skipping step 4 "
                    "for this channel rather than waiting again.", channel_id,
                )
                return None

            logger.info(
                "influencers.club rate-limited; waiting %.1fs before one retry.",
                wait,
            )
            self._sleep(wait)

        return None

    def _post(self, channel_id: str):
        """The HTTP call, with every transport failure flattened to None."""
        payload = {
            "handle": channel_id,
            "platform": PLATFORM_YOUTUBE,
            "email_required": INFLUENCERS_EMAIL_REQUIRED,
        }
        try:
            return HTTP.post(
                f"{INFLUENCERS_BASE_URL}{INFLUENCERS_ENRICH_PATH}",
                json=payload,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            # By the time this surfaces the adapter has already spent ~45s
            # of retries on 5xx, so it means genuinely unreachable.
            logger.warning(
                "influencers.club request failed for %s: %s", channel_id, exc
            )
            # A failure is not automatically a FREE failure, and treating it
            # as one leaves the credit cap bypassable: a lost response may
            # have been completed and billed by the vendor, and it would
            # increment only the outage breaker — which any later success
            # resets. Alternating lost-billable responses with ordinary
            # misses could then spend without limit while `lookups_spent`
            # stayed put.
            #
            # So charge anything that might have reached the vendor.
            # ConnectTimeout is the ONLY exemption, because it is the only
            # failure that provably predates the send: the connection was
            # never established, so the request was never processed.
            #
            # Deliberately NOT the broader `ConnectionError`. That class is
            # a transport wrapper — it also carries urllib3 ProtocolError,
            # i.e. a connection dropped MID-RESPONSE, which happens after
            # the vendor has already done the work and billed. Exempting the
            # whole family would reopen the bypass this exists to close.
            # Unknown failures charge: over-counting is the safe direction
            # for a spend ceiling.
            if not isinstance(exc, requests.exceptions.ConnectTimeout):
                self._record_billable()
            return None

    def _record_failure(self, reason: str) -> None:
        """
        Count an unbillable failure and give up for the run after enough of
        them in a row.

        Deliberately CONSECUTIVE, not cumulative: a vendor that answers
        every third request is degraded but still worth asking, while three
        misses in a row is an outage. The counter resets on any response
        that parses, so a single blip never disables the step — which is the
        contract `test_request_exception_is_survived` pins.
        """
        self._consecutive_failures += 1
        if self._consecutive_failures < self.MAX_CONSECUTIVE_FAILURES:
            return

        self._active = False
        logger.error(
            "influencers.club failed %d times in a row (last: %s) — email chain "
            "step 4 is disabled for the rest of this run. Each attempt costs "
            "~45s of retries and up to five 30s timeouts, so continuing would "
            "spend the run's wall clock on an unreachable vendor.",
            self._consecutive_failures, reason,
        )

    def _rate_limit_wait(self, resp):
        """
        Tell the two meanings of 429 apart, returning how long to wait.

        Returns the wait in seconds for the per-minute rate limit, or None
        for the fair-use credit cap (having tripped the breaker).

        A `retry_after` means the per-minute limit, which clears on its own,
        so the caller waits it out and retries. Its absence means the credit
        cap, which does not clear until subscription renewal, so every
        further request this run is guaranteed to fail: trip the breaker and
        stop asking.

        The signal is read from the response BODY (documented) **or** the
        standard `Retry-After` HEADER. Checking only the body would read a
        perfectly ordinary rate limit sent the conventional way as a credit
        cap, and disable step 4 for the whole run over a 30-second wait.
        The header is the safer of the two to trust here because getting
        this wrong is asymmetric: a cap misread as a rate limit costs a few
        wasted requests, while a rate limit misread as a cap costs every
        remaining address in the run.
        """
        body = self._json_or_none(resp)
        retry_after = (body or {}).get("retry_after")
        if retry_after is None:
            retry_after = resp.headers.get("Retry-After")

        if retry_after is not None:
            return self._wait_seconds(retry_after)

        self._active = False
        logger.error(
            "influencers.club fair-use credit cap reached — email chain step 4 "
            "is disabled for the rest of this run. It resets at subscription "
            "renewal, so retrying today cannot help. Response: %s",
            safe_body(resp),
        )
        return None

    @classmethod
    def _wait_seconds(cls, raw) -> float:
        """
        A sane sleep from the vendor's `retry_after`, whatever shape it is.

        The field is documented but its type is not, and it can arrive as an
        int, a float or a string (the `Retry-After` header is always a
        string). An unparseable or absurd value falls back to the default
        rather than stalling the run — the same defensive read
        `http_client._retry_after_seconds()` applies to Airtable.
        """
        try:
            wait = float(raw)
        except (TypeError, ValueError):
            return cls.DEFAULT_RATE_LIMIT_WAIT_SECONDS
        # isfinite BEFORE the comparisons, not after. float("NaN") parses
        # cleanly, then fails EVERY comparison — `nan <= 0` is False and
        # `min(nan, cap)` returns nan — so it would sail through both guards
        # into time.sleep(nan), which raises ValueError. Nothing between
        # find_email() and run() catches that, so one malformed header would
        # end the whole run. float("inf") is caught by the min(), but is
        # rejected here too rather than relying on that.
        if not math.isfinite(wait) or wait <= 0:
            return cls.DEFAULT_RATE_LIMIT_WAIT_SECONDS
        return min(wait, cls.MAX_RATE_LIMIT_WAIT_SECONDS)

    def _email_from_response(self, resp, channel_id: str) -> str:
        """
        Pull the address out of a 200 and screen it.

        A 200 with no address is the NORMAL outcome under `must_have` — the
        vendor had no validated email and charged nothing for saying so — so
        it is logged at debug, not as a failure.
        """
        body = self._json_or_none(resp)
        if body is None:
            logger.warning(
                "influencers.club returned a non-JSON 200 for %s: %s",
                channel_id, safe_body(resp),
            )
            return ""

        # Recorded before the result is inspected, and regardless of whether
        # we end up keeping the address: it is what the vendor says it
        # charged, so our screening verdict has no bearing on it. Read
        # defensively — an absent or non-numeric value simply contributes
        # nothing rather than breaking the run.
        cost = body.get("credits_cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            if math.isfinite(cost) and cost > 0:
                self._credits_reported += float(cost)

        result = body.get("result")
        if not isinstance(result, dict):
            logger.debug("influencers.club returned no result for %s", channel_id)
            return ""

        email = result.get("email")
        email = email.strip() if isinstance(email, str) else ""
        if not email:
            # The normal `must_have` miss, and it is FREE — so it must not
            # touch the budget. Since step 4 only runs for channels the free
            # steps missed, misses are the common case; billing them would
            # turn a credit budget into a request cap and disable the step
            # having spent nothing.
            logger.debug("influencers.club found no email for %s", channel_id)
            return ""

        # The vendor returned an address, so the credit is spent NOW —
        # before the screens below, which can still reject it. See
        # _record_billable().
        self._record_billable()

        # Shape-check the address rather than trusting the label on it. This
        # value is written to an Airtable cell a human runs outreach from,
        # and `fullmatch` is deliberate: EMAIL_PATTERN.search() would happily
        # accept "contact us at a@b.com today" and store the sentence.
        if not EMAIL_PATTERN.fullmatch(email):
            logger.warning(
                "influencers.club returned an unparseable email for %s — discarding.",
                channel_id,
            )
            return ""

        # The same screen scraped addresses get, through the same helper —
        # a sponsor/agency/platform domain is not the creator's address no
        # matter which source produced it, and being vendor-"validated"
        # buys no exemption. Routed through enrichment so the rule lives
        # next to the list rather than being spelled out again here.
        if is_blocklisted_email(email):
            logger.info(
                "influencers.club returned a blocklisted domain for %s — discarding.",
                channel_id,
            )
            return ""

        return email

    @staticmethod
    def _json_or_none(resp):
        """
        `resp.json()` guarded for the same reason enrichment._json_or_none()
        guards it, returning None unless the body is a JSON **object**.

        Note it is deliberately STRICTER than that one, which returns
        whatever parsed — so don't read the two as interchangeable.

        A 200 is not a promise of JSON — a proxy or captive-portal
        interstitial serves HTML with one — and requests' JSONDecodeError
        subclasses RequestException, so it LOOKS covered by the guard in
        _post() while actually being raised outside it.

        The isinstance check is the second half of that guard and matters
        just as much: valid JSON is not necessarily an object. A body of
        `"rate limited"` or `[]` parses fine and then raises AttributeError
        on `.get()` — an exception no caller up through run() catches, so a
        single malformed response would abort the entire run and break this
        module's "never raises" contract.
        """
        try:
            body = resp.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) else None


def null_client() -> InfluencersClient:
    """An inert client, for runs with step 4 turned off."""
    return InfluencersClient(enabled=False)
