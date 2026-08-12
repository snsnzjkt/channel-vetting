"""
Discovery source: influencers.club's creator-search endpoint, a replacement
for discovery.py's YouTube search.list.

Why switch. search.list returns whatever ranks for a keyword and ranks by
the matched VIDEO, so the pipeline's CHANNEL-level gates (English, an
allowed country, a subscriber/view floor, a real long-form catalogue) throw
most of it away — measured ~15% survival. This endpoint filters on those
same criteria SERVER-SIDE (profile_language, location, number_of_subscribers,
topics), so a much larger fraction of what it returns can become a row.

Cost model, which is the whole reason exclude_handles below is load-bearing
rather than an optimisation: discovery costs REAL MONEY, not free YouTube
quota — 0.01 credits per creator RETURNED (verified live 2026-08-13). A
creator already tracked in our base that a broad query would re-return is
0.01 spent on a row we already have. exclude_handles makes the vendor drop
those creators server-side, so it never returns — and never bills — them.
Filtering them out locally AFTER the response would not save the credit; the
billing already happened.

What it returns. Each account is {user_id, profile:{username, full_name,
followers, engagement_percent}} — the @handle (`username`), NOT the YouTube
UC… channel ID the rest of the pipeline is keyed on. The caller bridges
handle -> channel_id via the YouTube API (channels.list?forHandle), the same
1-unit call get_channel_stats already makes, so discovery adds no YouTube
quota beyond what enrichment already spends.

Everything here fails SOFT — a bad key, an outage, a malformed body, a
tripped credit ceiling all return the candidates gathered so far (often none)
and let run_niche move on. One bad page is never worth ending a run over,
the same contract influencers.py and browser_email.py honour.
"""
import logging

import requests

from config import (
    API_SLEEP_SECONDS,
    INFLUENCERS_API_KEY,
    INFLUENCERS_BASE_URL,
    INFLUENCERS_DISCOVERY_PATH,
    INFLUENCERS_MAX_DISCOVERY_CREDITS_PER_RUN,
    INFLUENCERS_MAX_EXCLUDE_HANDLES,
)
from enrichment import normalize_handle
from http_client import INFLUENCERS as HTTP, safe_body

logger = logging.getLogger(__name__)

PLATFORM_YOUTUBE = "youtube"
REQUEST_TIMEOUT_SECONDS = 45
# The API caps a page at 50 results; asking for more is rejected.
PAGE_LIMIT = 50
# Relevancy is the only sort that surfaces on-niche creators first; it only
# supports descending order (verified). number_of_followers/engagement are
# available but bias toward big accounts regardless of fit.
DEFAULT_SORT = {"sort_by": "relevancy", "sort_order": "desc"}


class InfluencerDiscovery:
    """
    A per-run discovery client.

    Instance state, not module state, for the same reasons as
    InfluencersClient: the credit budget describes ONE run, and a
    module-level counter would leak between tests.

    Construct with `enabled=False` (or via `null_discovery()`) for an inert
    client that always returns [] — the same soft-disable contract as
    null_client()/null_scraper(), so a missing API key turns discovery off
    without special-casing at the call site.

    Takes NO api_key: the credential is bolted to the shared INFLUENCERS
    session at import, exactly as InfluencersClient relies on. from_config()
    decides only whether a key exists at all.
    """

    def __init__(self, enabled=True, max_credits=None, sleep=None):
        self._active = enabled
        self._max_credits = (
            INFLUENCERS_MAX_DISCOVERY_CREDITS_PER_RUN if max_credits is None else max_credits
        )
        # Credits as the vendor reports them in each response's credits_cost —
        # the authoritative spend figure, accumulated so a run can print what
        # discovery actually cost.
        self._credits_spent = 0.0
        # The vendor's own remaining-balance figure from the last response, for
        # reporting; None until a response carries one.
        self._credits_left_reported = None
        import time
        self._sleep = sleep if sleep is not None else time.sleep

    @property
    def enabled(self) -> bool:
        """True if this client can still spend on discovery."""
        return self._active and self._credits_spent < self._max_credits

    @property
    def credits_spent(self) -> float:
        """Credits the vendor reported charging for discovery this run."""
        return self._credits_spent

    @property
    def credits_left_reported(self):
        """The account's remaining balance from the last response, or None."""
        return self._credits_left_reported

    @classmethod
    def from_config(cls) -> "InfluencerDiscovery":
        """The client run_niche uses, or an inert one when no key is set."""
        if not INFLUENCERS_API_KEY:
            logger.info(
                "INFLUENCERS_API_KEY is not set — influencers.club discovery is "
                "disabled for this run."
            )
            return cls(enabled=False)
        return cls()

    def discover(
        self,
        *,
        filters: dict,
        target: int,
        exclude_handles=(),
        platform: str = PLATFORM_YOUTUBE,
        sort: dict | None = None,
        source_label: str = "influencers.club discovery",
        page_cap: int = 40,
    ) -> list[dict]:
        """
        Return up to `target` candidate creators matching `filters`, paginating
        the discovery endpoint and excluding `exclude_handles` server-side.

        Each candidate is a dict shaped for the pipeline's downstream steps:

            {"handle": <normalized, no '@'>, "channel_title": <full_name>,
             "influencers_user_id": <vendor id>, "subscriber_count": <int|None>,
             "engagement_percent": <float|None>, "matched_keywords": [source_label]}

        Note there is no "channel_id" — discovery returns the @handle, and the
        caller resolves it to a UC… id via the YouTube API before the rest of
        process_candidate runs.

        Never raises. Stops early when `target` is met, the credit ceiling is
        reached, the result set is exhausted, or a page fails — returning
        whatever was gathered so far.
        """
        if not self.enabled or target <= 0:
            return []

        body_filters = dict(filters)
        exclude = self._prepare_exclude(exclude_handles)
        if exclude:
            body_filters["exclude_handles"] = exclude

        sort = sort or DEFAULT_SORT
        # Keyed by normalized handle so the same creator returned on two pages
        # (pagination is not always perfectly stable) is not double-counted.
        candidates: dict[str, dict] = {}
        page = 0

        while len(candidates) < target and page < page_cap:
            if self._credits_spent >= self._max_credits:
                logger.warning(
                    "influencers.club discovery credit ceiling (%.2f) reached — "
                    "stopping discovery for this niche.",
                    self._max_credits,
                )
                break

            payload = {
                "platform": platform,
                "paging": {"limit": PAGE_LIMIT, "page": page},
                "sort": sort,
                "filters": body_filters,
            }
            resp = self._post(payload)
            if resp is None:
                break  # fail-soft: keep what we have, let the caller move on

            accounts, total, cost, credits_left = self._parse(resp)
            # credits_cost is what the vendor billed for THIS page, charged
            # whether or not we end up using every account — so count it now.
            self._credits_spent += cost
            if credits_left is not None:
                self._credits_left_reported = credits_left

            if not accounts:
                break

            for account in accounts:
                candidate = self._to_candidate(account, source_label)
                if candidate and candidate["handle"] not in candidates:
                    candidates[candidate["handle"]] = candidate

            page += 1
            # Supply exhausted: a short page means no more results, and once
            # we have paged past `total` there is nothing left to ask for.
            if len(accounts) < PAGE_LIMIT or page * PAGE_LIMIT >= total:
                break

            self._sleep(API_SLEEP_SECONDS)

        result = list(candidates.values())[:target]
        logger.info(
            "influencers.club discovery: %d candidate(s) over %d page(s), "
            "%.2f credits spent (%s reported remaining).",
            len(result), page, self._credits_spent,
            self._credits_left_reported if self._credits_left_reported is not None else "?",
        )
        return result

    def _prepare_exclude(self, exclude_handles) -> list[str]:
        """
        Normalize, dedupe, and cap the exclusion set.

        Accepts either RAW handles/URLs ("@Foo", "youtube.com/@Foo") or the
        ALREADY-BARE handles the blocklist and external-dedupe indexes store
        ("foo"). normalize_handle() only recognises the raw forms (it requires
        a literal "@"), so the bare case falls back to a plain strip/lower —
        the same two-step normalization do_not_contact.Blocklist.match() uses,
        which is exactly why both must agree here. Drops anything that
        normalizes to "" (a legacy /c/ or /user/ channel with no handle can't
        be matched this way).

        The vendor caps exclude_handles at INFLUENCERS_MAX_EXCLUDE_HANDLES
        (10,000). Over that it TRUNCATES with a loud warning rather than
        letting the request 400 — a partial exclusion still saves most of the
        spend, and a silent 400 would disable discovery entirely. A base
        larger than the cap is the signal to wire the persistent server-side
        exclusion list (see the module docstring).
        """
        normalized = {
            normalize_handle(h) or (h or "").strip().lstrip("@").lower()
            for h in exclude_handles
        }
        normalized.discard("")
        ordered = sorted(normalized)
        if len(ordered) > INFLUENCERS_MAX_EXCLUDE_HANDLES:
            logger.warning(
                "Exclusion set has %d handles, over the %d per-request cap — "
                "sending the first %d. Some already-tracked creators may be "
                "re-returned (and billed at 0.01 each); wire the persistent "
                "exclusion list to cover the whole base.",
                len(ordered), INFLUENCERS_MAX_EXCLUDE_HANDLES,
                INFLUENCERS_MAX_EXCLUDE_HANDLES,
            )
            ordered = ordered[:INFLUENCERS_MAX_EXCLUDE_HANDLES]
        return ordered

    def _post(self, payload: dict):
        """The HTTP call, with every failure flattened to None (fail-soft)."""
        url = f"{INFLUENCERS_BASE_URL}{INFLUENCERS_DISCOVERY_PATH}"
        try:
            resp = HTTP.post(url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            logger.warning("influencers.club discovery request failed: %s", exc)
            return None
        if resp.status_code != 200:
            logger.warning(
                "influencers.club discovery returned %s: %s",
                resp.status_code, safe_body(resp),
            )
            return None
        return resp

    def _parse(self, resp):
        """
        Pull (accounts, total, credits_cost, credits_left) out of a 200.

        A 200 is not a promise of JSON (a proxy interstitial can serve HTML
        with one), and requests' JSONDecodeError subclasses RequestException,
        so it is guarded here rather than trusted. Missing/malformed numeric
        fields degrade to safe defaults (empty page, 0 cost) rather than
        raising through the run.
        """
        try:
            body = resp.json()
        except ValueError:
            logger.warning(
                "influencers.club discovery returned a non-JSON 200: %s", safe_body(resp)
            )
            return [], 0, 0.0, None
        if not isinstance(body, dict):
            return [], 0, 0.0, None

        accounts = body.get("accounts")
        accounts = accounts if isinstance(accounts, list) else []
        total = body.get("total")
        total = total if isinstance(total, int) else len(accounts)
        cost = body.get("credits_cost")
        cost = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else 0.0
        left = body.get("credits_left")
        left = float(left) if isinstance(left, (int, float)) and not isinstance(left, bool) else None
        return accounts, total, cost, left

    def _to_candidate(self, account, source_label: str):
        """One account -> a pipeline candidate dict, or None to skip it."""
        if not isinstance(account, dict):
            return None
        profile = account.get("profile")
        if not isinstance(profile, dict):
            return None
        handle = normalize_handle(profile.get("username", ""))
        if not handle:
            # No @handle means we can neither dedupe nor resolve it to a
            # channel ID — drop it rather than carry an unusable candidate.
            return None
        followers = profile.get("followers")
        engagement = profile.get("engagement_percent")
        return {
            "handle": handle,
            "channel_title": profile.get("full_name") or "",
            "influencers_user_id": account.get("user_id"),
            "subscriber_count": followers if isinstance(followers, int) else None,
            "engagement_percent": (
                float(engagement) if isinstance(engagement, (int, float)) and not isinstance(engagement, bool) else None
            ),
            "matched_keywords": [source_label],
        }


def null_discovery() -> InfluencerDiscovery:
    """An inert discovery client, for runs with the switch turned off."""
    return InfluencerDiscovery(enabled=False)
