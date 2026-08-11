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

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import YOUTUBE_API_KEY

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
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "PATCH", "DELETE"})

# Airtable's documented 429 cooldown is ~30s; give it a little room.
POST_RETRY_STATUSES = (429,)
POST_RETRY_ATTEMPTS = 4
POST_RETRY_WAIT_SECONDS = 32.0


def _make_session() -> requests.Session:
    retry = Retry(
        total=RETRY_TOTAL,
        connect=RETRY_TOTAL,
        read=RETRY_TOTAL,
        backoff_factor=BACKOFF_FACTOR,
        status_forcelist=RETRY_STATUSES,
        allowed_methods=IDEMPOTENT_METHODS,
        respect_retry_after_header=True,
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

# Guarded so importing this module never explodes when the key is unset —
# discovery.py raises its own clear "YOUTUBE_API_KEY is not set" error, and
# the test suite imports these modules without a populated .env.
if YOUTUBE_API_KEY:
    YOUTUBE.headers["X-goog-api-key"] = YOUTUBE_API_KEY


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
    # letting a bad header stall the run.
    if value <= 0 or value > 300:
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
