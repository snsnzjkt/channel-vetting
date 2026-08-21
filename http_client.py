"""
One shared, retrying HTTP session per upstream API.

Why this module exists: every call site in this pipeline used to be a bare
`requests.get()`, which meant a 429 was fatal in a *different* way at each
one — `push_record()` returned False and silently dropped a prospect the
pipeline had already spent quota enriching, `get_existing_channel_ids()`
raised and aborted the whole run, `discover_channels_by_keyword()` logged
and broke out of its loop. None of them retried.

That mattered because being well-behaved is not sufficient here:

- **Airtable's limit is 5 requests/second per BASE**, not per token. This
  base has nine tables with human maintainers and other automations
  against it, so a colleague pasting 500 rows into an outreach table can
  429 this pipeline no matter how politely it paces itself. On a 429
  Airtable also requires roughly a 30-second cooldown, which is why
  `respect_retry_after_header` below is load-bearing rather than decorative.
- **YouTube returns 403 `rateLimitExceeded`/`userRateLimitExceeded`** for
  its per-minute limits, which are separate from the daily 10,000-unit
  quota, plus ordinary 500/503s.

The API key travels as a HEADER, never as a `key=` query parameter
--------------------------------------------------------------------
`requests` embeds the full URL in its exception messages:

    ConnectionError: HTTPSConnectionPool(host='www.googleapis.com', ...):
      Max retries exceeded with url: /youtube/v3/search?...&key=AIzaSy...

With the key in the query string, any unhandled network error prints the
live credential into stdout — which in CI is the Actions log, retained for
90 days. GitHub's secret masking would redact it only because the key
happens to be a registered secret; that is a backstop, not a control, and
it does nothing for a local run. Google accepts `X-goog-api-key` for the
same request, so the key is set ONCE here and the URL becomes safe to log.
Do not reintroduce `"key": YOUTUBE_API_KEY` into any `params` dict.
"""
import logging
import math

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import YOUTUBE_API_KEY, INFLUENCERS_API_KEY, GEMINI_MAX_RETRIES

logger = logging.getLogger(__name__)

# 5 attempts with backoff_factor=1.5 sleeps roughly 0s, 3s, 6s, 12s, 24s
# between tries — about 45 seconds of patience in the worst case. That is
# deliberately longer than Airtable's ~30-second 429 cooldown, so a single
# rate-limit event is absorbed rather than merely survived.
RETRY_TOTAL = 5
BACKOFF_FACTOR = 1.5
RETRY_STATUSES = (429, 500, 502, 503, 504)

# POST is deliberately ABSENT from this set.
#
# A retried GET/PATCH/DELETE is harmless: PATCH and DELETE are addressed at
# a known record ID, so repeating one converges on the same end state. A
# retried POST does not — if Airtable creates the record and *then* the
# response is lost to a 502, retrying posts it a second time and creates a
# duplicate row, defeating push_record()'s documented "Never creates
# duplicates" guarantee in exactly the situation the retry was meant to help.
#
# A 429 is the one status where POST is provably safe to repeat, because it
# means Airtable rejected the request without processing it. That narrow
# case is handled explicitly by post_with_rate_limit_retry() below rather
# than by widening this set.
#
# Scope of that reasoning: it is about POSTs that CREATE something, not about
# POST as a verb. INFLUENCERS_RETRY_METHODS below deliberately adds POST for
# the influencers.club session, whose only POST is a lookup that creates
# nothing — see the comment there. This set stays as-is and that one derives
# from it, so a future edit here still reaches both.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "PATCH", "DELETE"})

# Airtable's documented 429 cooldown is ~30s; give it a little room.
POST_RETRY_STATUSES = (429,)
POST_RETRY_ATTEMPTS = 4
POST_RETRY_WAIT_SECONDS = 32.0


def _make_session(
    *,
    retry_statuses=RETRY_STATUSES,
    allowed_methods=IDEMPOTENT_METHODS,
    respect_retry_after=True,
    read_retries=RETRY_TOTAL,
    total=RETRY_TOTAL,
) -> requests.Session:
    """
    `total` was added for the GEMINI session (2026-08-21), which needs a retry
    budget of its own rather than the module-wide RETRY_TOTAL: every request it
    makes is metered against a free-tier daily allowance, so "how many times may
    we re-send this" is a per-vendor policy question, not a global one. Existing
    callers omit it and are unchanged.
    """
    retry = Retry(
        total=total,
        connect=total,
        read=read_retries,
        backoff_factor=BACKOFF_FACTOR,
        status_forcelist=retry_statuses,
        allowed_methods=allowed_methods,
        respect_retry_after_header=respect_retry_after,
        # Hand the final failed response back to the caller instead of
        # raising urllib3.exceptions.MaxRetryError. Every call site in this
        # pipeline already branches on `resp.status_code != 200` and logs
        # the body, and that reporting is more useful than a stack trace.
        raise_on_status=False,
    )
    session = requests.Session()
    # pool_maxsize covers the handful of concurrent hosts in play (Airtable,
    # googleapis) with room to spare. Connection reuse is the free win here:
    # the pipeline made ~200 calls per run, each paying a fresh TLS handshake.
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


AIRTABLE = _make_session()
YOUTUBE = _make_session()

# Gmail send. The retry policy here is the STRICTEST in this module, because a
# repeated request costs a duplicate EMAIL — the one failure in this pipeline
# that cannot be deleted, refunded, or apologised away after the fact.
#
#   - POST is excluded (IDEMPOTENT_METHODS), like AIRTABLE. Sending is the only
#     thing this session does, so in practice nothing retries at all. That is
#     intended: `outreach_ledger` classifies an ambiguous outcome as MaybeSent
#     and hands it to a human, which is strictly better than a machine deciding
#     to try again.
#   - `read_retries=0`, like INFLUENCERS and for a sharper version of the same
#     reason: a read retry means the request WAS sent and the response was lost,
#     so repeating it may deliver a second copy. INFLUENCERS avoids buying a
#     second credit; here it avoids emailing a creator twice.
#   - `respect_retry_after_header=False`, like INFLUENCERS: urllib3 sleeps the
#     header verbatim with no ceiling, which would park a run inside the adapter
#     where neither the daily cap nor the claim lease can see it.
#
# Deliberately NOT `google-api-python-client`: that uses httplib2, which the
# autouse guard in tests/conftest.py (patched at `HTTPAdapter.send`, the
# `requests` chokepoint) cannot see. A missed mock would have emailed a real
# creator from a test run. Going through a requests Session means the existing
# guard covers the mailer for free, and `safe_body()` applies to its errors.
GMAIL = _make_session(
    allowed_methods=IDEMPOTENT_METHODS,
    respect_retry_after=False,
    read_retries=0,
)

# 429 is deliberately ABSENT from this session's retry set.
#
# influencers.club overloads one status onto two conditions that need
# opposite responses, and tells them apart by whether the body carries a
# `retry_after` field:
#
#   with retry_after    -> the per-minute rate limit (300 req/min). Clears
#                          on its own; retrying is correct.
#   without retry_after -> the fair-use credit cap. It resets at
#                          subscription renewal, so retrying "provides no
#                          benefit" (their docs) — it cannot succeed today.
#
# `retry_after` is read from the documented body field OR the standard
# Retry-After header, because misreading a plain rate limit as the cap would
# disable the step for the whole run over a 30-second wait.
#
# Leaving 429 in the forcelist would make the adapter spend ~45s of backoff
# on a cap that a month of backoff wouldn't clear, and it would do that for
# EVERY remaining candidate in the run. Since the two cases are only
# distinguishable from the response BODY, which the adapter never inspects,
# the split has to happen at the call site: influencers.py handles both and
# trips a circuit breaker on the cap.
INFLUENCERS_RETRY_STATUSES = (500, 502, 503, 504)

# POST *is* retried on this session, unlike every other one here, and the
# reason IDEMPOTENT_METHODS excludes it does not apply: the exclusion exists
# because a retried Airtable POST that succeeded-but-lost-its-response
# creates a duplicate ROW. influencers.club's enrich endpoint is a lookup —
# it creates nothing, so repeating it converges on the same answer. Billing
# is per successful result, and a 5xx is not one, so a retried 5xx cannot
# double-charge either.
#
# This is not optional politeness: the endpoint is only ever reached by POST,
# so leaving POST out of the allowed set would make INFLUENCERS_RETRY_STATUSES
# above dead configuration — the 5xx retry would silently never happen.
INFLUENCERS_RETRY_METHODS = IDEMPOTENT_METHODS | {"POST"}
# Retry-After is deliberately NOT honoured on this session.
#
# urllib3 2.x sleeps the header's value verbatim (`Retry.sleep_for_retry`
# calls `time.sleep(retry_after)` with no ceiling — verified against the
# pinned 2.7.0), and this session retries 5xx. So a 503 carrying
# `Retry-After: 86400` would park the run inside the adapter for a day,
# where neither the lookup budget nor the failure breaker can see it, because
# find_email() has not been handed a response yet.
#
# Nothing is lost by refusing it: the only status whose cooldown this vendor
# actually asks us to honour is 429, and that is handled explicitly in
# influencers.py — where the wait IS capped (MAX_RATE_LIMIT_WAIT_SECONDS)
# and the value is parsed defensively. 5xx retries fall back to the
# exponential backoff above, which urllib3 caps on its own.
#
# AIRTABLE keeps respect_retry_after_header=True: its ~30s 429 cooldown is
# documented, load-bearing, and comes from a service we trust.
#
# READ retries are disabled on this session, and that is the other half of
# the POST decision above. The three retry kinds are not equally safe once
# money is involved:
#
#   connect -> the connection was never established, so the vendor never
#              processed the request and never billed. Safe to retry.
#   status  -> the vendor answered 5xx, so it returned no result and billed
#              nothing (billing is per successful result). Safe to retry.
#   read    -> the request WAS sent and the response was lost. The vendor
#              may have completed the lookup and charged a credit; we simply
#              never saw it. Retrying spends a SECOND credit for one answer,
#              and the budget only ever sees the final response, so the
#              overspend is invisible.
#
# This is the money-shaped version of the duplicate-row rule at the top of
# this file: the failure mode a retry is supposed to help is exactly the one
# where repeating it does damage. A lost response therefore surfaces to
# influencers.py as a RequestException, which returns "" and counts toward
# the outage breaker — costing one address instead of an unbounded number of
# duplicate charges.
INFLUENCERS = _make_session(
    retry_statuses=INFLUENCERS_RETRY_STATUSES,
    allowed_methods=INFLUENCERS_RETRY_METHODS,
    respect_retry_after=False,
    read_retries=0,
)

# Guarded so importing this module never explodes when the key is unset —
# discovery.py raises its own clear "YOUTUBE_API_KEY is not set" error, and
# the test suite imports these modules without a populated .env.
if YOUTUBE_API_KEY:
    YOUTUBE.headers["X-goog-api-key"] = YOUTUBE_API_KEY

# Same reasoning as the YouTube key: a credential in a query string is a
# credential printed into any unhandled network error's message, and in CI
# that message lands in a log retained for 90 days.
if INFLUENCERS_API_KEY:
    INFLUENCERS.headers["Authorization"] = f"Bearer {INFLUENCERS_API_KEY}"


# Gemini (relevance verification). The retry policy here is the second-strictest
# in this module, and every deviation from the factory defaults is load-bearing:
#
# - 429 is DELIBERATELY ABSENT from the forcelist, unlike RETRY_STATUSES. A 429
#   here is the free-tier wall, and retrying it is precisely the behaviour this
#   integration must not have — the caller classifies the QuotaFailure body into
#   a per-minute pause or a per-day latch instead. Backing off into a wall is how
#   an unattended run spends its whole budget discovering the same fact 5 times.
#
# - POST is added to allowed_methods. `:generateContent` is reached ONLY by POST,
#   so leaving it out would make GEMINI_RETRY_STATUSES below dead configuration
#   and the 5xx retry would silently never happen — the same trap documented for
#   INFLUENCERS above. This is not optional politeness.
#
# - respect_retry_after=False. urllib3 sleeps the header verbatim with no
#   ceiling, so a 503 carrying `Retry-After: 86400` would park the run inside the
#   adapter for a day, invisibly, against the workflow's own timeout-minutes.
#
# - read_retries=0. A read retry means the request WAS SENT, the free-tier
#   request was already consumed, and we simply never saw the response. Retrying
#   spends a second request that the ledger's post-response accounting never
#   sees, which is the one direction that overspends a quota.
GEMINI_RETRY_STATUSES = (500, 502, 503, 504)
GEMINI_RETRY_METHODS = IDEMPOTENT_METHODS | {"POST"}

GEMINI = _make_session(
    retry_statuses=GEMINI_RETRY_STATUSES,
    allowed_methods=GEMINI_RETRY_METHODS,
    respect_retry_after=False,
    read_retries=0,
    total=GEMINI_MAX_RETRIES,
)


def post_with_rate_limit_retry(url: str, *, sleep=None, **kwargs) -> requests.Response:
    """
    POST to Airtable, retrying ONLY on the statuses in POST_RETRY_STATUSES
    (429), which mean the request was rejected without being processed.

    Exists because POST is excluded from the session-level retry set — see
    IDEMPOTENT_METHODS above for why retrying a POST on a 5xx risks a
    duplicate row. A 429 carries no such ambiguity.

    `sleep` is injectable so tests don't actually wait 32 seconds.
    """
    if sleep is None:
        import time

        sleep = time.sleep

    resp = AIRTABLE.post(url, **kwargs)
    for attempt in range(1, POST_RETRY_ATTEMPTS):
        if resp.status_code not in POST_RETRY_STATUSES:
            return resp
        wait = _retry_after_seconds(resp, POST_RETRY_WAIT_SECONDS)
        logger.warning(
            "Airtable rate-limited a POST (%s); waiting %.1fs before retry %d/%d.",
            resp.status_code, wait, attempt, POST_RETRY_ATTEMPTS - 1,
        )
        sleep(wait)
        resp = AIRTABLE.post(url, **kwargs)
    return resp


def _retry_after_seconds(resp: requests.Response, default: float) -> float:
    """Honour a Retry-After header when the server sends a sane one."""
    raw = resp.headers.get("Retry-After", "")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    # Ignore nonsense (negative, or an implausibly long hold) rather than
    # letting a bad header stall the run. isfinite() is checked FIRST and is
    # load-bearing, not defensive padding: float("nan") parses cleanly and
    # then fails every comparison (`nan <= 0` is False, `nan > 300` is False),
    # so without this guard a `Retry-After: nan` header would fall straight
    # through to `return value` and reach time.sleep(nan) in
    # post_with_rate_limit_retry(), which raises ValueError. Nothing between
    # there and run() catches it, so one malformed header would kill the whole
    # run — the same trap influencers._wait_seconds() guards against.
    if not math.isfinite(value) or value <= 0 or value > 300:
        return default
    return value


def safe_body(resp: requests.Response, limit: int = 500) -> str:
    """
    A response body trimmed to `limit` chars for logging.

    Two reasons this is not just `resp.text`. Bodies are unbounded, and an
    Airtable validation error can echo an entire rejected record into the
    log. And on an auth failure the body is pure noise attached to the one
    status code that most invites someone to paste a log into a ticket, so
    401/403 report nothing but their status.
    """
    if resp.status_code in (401, 403):
        return "<body withheld: auth failure>"
    text = resp.text or ""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [{len(text)} bytes total, truncated]"
