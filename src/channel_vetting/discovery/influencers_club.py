"""
Discovery source: influencers.club's creator-search endpoint, a replacement
for discovery/youtube_search.py's YouTube search.list.

Why switch. search.list returns whatever ranks for a keyword and ranks by
the matched VIDEO, so the pipeline's CHANNEL-level gates (English, an
allowed country, a subscriber/view floor, a real long-form catalogue) throw
most of it away — measured ~15% survival. This endpoint can filter on some of
those criteria SERVER-SIDE, so a larger fraction of what it returns can
become a row.

Which filters are actually sent, as of 2026-08-14 (see NICHES in pipeline.py):

  - SENT: profile_language, gender, ai_search, number_of_subscribers,
    keywords_not_in_description.
  - `location` is supported by the vendor and is NOT sent yet. That is a gap,
    not a decision: the search-zone check is a hard discard gate, so every
    out-of-zone creator returned is 0.01 credits spent on a row that can never
    exist. Wiring it needs a live probe of the field name and country format
    first, the same bar gender and keywords_not_in_description were held to.
  - `topics` is NOT sent ON PURPOSE. The yt-topics taxonomy has no leaf for
    "home" or "furniture", and pinning topics to Movies/Technology (Home
    Theater) or Fashion/Tourism (Lifestyle Sofa) would EXCLUDE exactly the
    furniture / homebody / house-tour creators both briefs are aimed at, which
    YouTube files under Lifestyle. Relevance rides on the ai_search semantic
    query instead. Do not "fix" this gap; it will narrow the funnel.

An earlier version of this docstring listed location and topics as though both
were already in use. They were not, and conflating the two is how the
deliberate omission gets undone by accident.

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

Only the IDENTIFIERS are carried forward. The vendor's `followers` and
`engagement_percent` are read here and deliberately dropped: statistics come
from the YouTube Data API v3, which is the only source the gates, the scores
and the Airtable columns are allowed to reflect. See _to_candidate.

Everything here fails SOFT — a bad key, an outage, a malformed body, a
tripped credit ceiling all return the candidates gathered so far (often none)
and let run_niche move on. One bad page is never worth ending a run over,
the same contract enrichment/email_influencers.py and enrichment/email_browser.py honour.
"""
import logging

import requests

from channel_vetting.config import (
    API_SLEEP_SECONDS,
    INFLUENCERS_API_KEY,
    INFLUENCERS_BASE_URL,
    INFLUENCERS_DISCOVERY_PATH,
    INFLUENCERS_MAX_DISCOVERY_CREDITS_PER_RUN,
    INFLUENCERS_MAX_EXCLUDE_HANDLES,
)
from channel_vetting.budget.credit_tracker import (
    KIND_DISCOVERY,
    can_afford,
    can_afford_handles,
    record_spend,
    record_vendor_balance,
)
from channel_vetting.enrichment.channels import normalize_handle
from channel_vetting.core.http_client import INFLUENCERS as HTTP, safe_body

logger = logging.getLogger(__name__)

PLATFORM_YOUTUBE = "youtube"
REQUEST_TIMEOUT_SECONDS = 45
# The API caps a page at 50 results; asking for more is rejected.
PAGE_LIMIT = 50

CREDITS_PER_CREATOR = 0.01


def page_cost_credits() -> float:
    """
    Worst-case credit cost of the next page, for projecting against the ceilings
    before buying it (see credit_tracker.can_afford).

    Worst case: a short final page bills less. The ledger always records the
    vendor's own `credits_cost`, never this estimate. A function rather than a
    constant so tests can shrink PAGE_LIMIT.
    """
    return PAGE_LIMIT * CREDITS_PER_CREATOR


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
        # Creators the vendor actually RETURNED, i.e. the set it charged for.
        # Reported next to credits_spent so a run summary shows the ratio that
        # matters — creators bought against rows produced. The 2026-08-13 waste
        # bug was only noticed because a credit figure happened to sit next to a
        # row count in one log line; this makes that comparison deliberate.
        self._creators_billed = 0
        # The vendor's own remaining-balance figure from the last response, for
        # reporting; None until a response carries one.
        self._credits_left_reported = None
        import time
        self._sleep = sleep if sleep is not None else time.sleep

    @property
    def enabled(self) -> bool:
        """
        True if this client can still buy AT LEAST ONE MORE PAGE.

        Projected, matching discover()'s loop check, because the two must not be
        able to disagree. With a bare `spent < max` this said True at 5.9 of a
        6.0 ceiling — and run_niche reads it as `use_discovery` (pipeline.py:1556),
        which then sets `remaining_keywords = []` (pipeline.py:1582) and abandons the
        search.list fallback. discover() would immediately refuse the page, so
        the niche got NO discovery at all: worse than either the overshoot or the
        clean stop.
        """
        # The handle allowance is checked here as well as inside discover()'s
        # loop, and the placement is deliberate. run_niche reads this property
        # as `use_discovery`; when it is False for a discovery_source="both"
        # niche the free YouTube keyword loop keeps its full keyword list and
        # simply fills the headroom instead (pipeline.py). So an exhausted
        # allowance degrades to "the free source does the whole job" rather
        # than to a niche that quietly produces nothing.
        #
        # Costs one ledger read per call, and this is called once per niche and
        # once at the top of discover() — not per page.
        return (
            self._active
            and self._credits_spent + page_cost_credits() <= self._max_credits
            and can_afford_handles(PAGE_LIMIT, "discovery")
        )

    @property
    def credits_spent(self) -> float:
        """Credits the vendor reported charging for discovery this run."""
        return self._credits_spent

    @property
    def creators_billed(self) -> int:
        """How many creators the vendor returned (and therefore charged for)."""
        return self._creators_billed

    @property
    def credits_left_reported(self):
        """The account's remaining balance from the last response, or None."""
        return self._credits_left_reported

    @classmethod
    def from_config(cls, max_credits=None) -> "InfluencerDiscovery":
        """
        The client run_niche uses, or an inert one when no key is set.

        `max_credits` overrides INFLUENCERS_MAX_DISCOVERY_CREDITS_PER_RUN for
        this run — pipeline.py passes the much tighter --test ceiling through it.
        None keeps the configured default (see __init__).
        """
        if not INFLUENCERS_API_KEY:
            logger.info(
                "INFLUENCERS_API_KEY is not set — influencers.club discovery is "
                "disabled for this run."
            )
            return cls(enabled=False)
        return cls(max_credits=max_credits)

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
        Return candidate creators matching `filters`, paginating the discovery
        endpoint and excluding `exclude_handles` server-side.

        `target` is a FLOOR that decides whether to fetch another page, not a
        limit on what comes back: the return includes every creator the vendor
        billed for, which is usually MORE than `target` (pages are 50). See the
        comment at the return statement for why trimming here was a money leak.

        Each candidate is a dict shaped for the pipeline's downstream steps:

            {"handle": <normalized, no '@'>, "channel_title": <full_name>,
             "influencers_user_id": <vendor id>, "matched_keywords": [source_label]}

        Identifiers only — no statistics. See _to_candidate for why.

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
            # Both ceilings are PROJECTED against the page's price — see
            # credit_tracker.can_afford for why a ceiling checked without the
            # price is not a ceiling.
            page_cost = page_cost_credits()
            if self._credits_spent + page_cost > self._max_credits:
                logger.warning(
                    "influencers.club per-run discovery ceiling (%.2f) would be "
                    "exceeded by another page (%.3f spent, page costs up to %.2f) "
                    "— stopping discovery for this niche.",
                    self._max_credits, self._credits_spent, page_cost,
                )
                break

            # The PERSISTENT limits, which the per-run one above cannot see: the
            # day, the month, and the vendor's own reported balance. False also
            # covers an unreadable ledger, since both answers are "stop paying".
            if not can_afford(page_cost, source_label):
                self._active = False
                break

            # The vendor's OTHER meter. Checked separately from can_afford
            # because handles are not credits: an account can sit well inside
            # every credit ceiling and still be over its fair-use handle
            # allowance, which is exactly the state the 2026-09-01 email
            # reported. Projects a FULL page (PAGE_LIMIT) rather than the
            # short page this might turn out to be — same reasoning as
            # page_cost_credits above, since the ceiling has to hold against
            # the worst case, not the typical one.
            if not can_afford_handles(PAGE_LIMIT, source_label):
                self._active = False
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
            # Persisted at the same moment and for the same reason. A failed
            # write stops discovery: continuing would authorise every later page
            # against a total the ledger no longer reflects.
            #
            # `handles` is len(accounts), NOT a figure derived from `cost`. The
            # two agree at the observed 0.01/creator rate, but the handle meter
            # is the vendor's and the rate is our measurement — if the rate ever
            # changes, a derived count would drift silently against the exact
            # limit it is meant to defend.
            if not record_spend(
                cost, kind=KIND_DISCOVERY, detail=source_label,
                handles=len(accounts),
            ):
                self._active = False
            if credits_left is not None:
                self._credits_left_reported = credits_left
                # The vendor's own balance outranks our estimated ceilings, and
                # the email step never sees a discovery response — so persist it
                # for both to read. Previously this value reached a log line only.
                record_vendor_balance(credits_left)

            if not accounts:
                break

            # Counted off `accounts`, the set the vendor returned and billed —
            # NOT off the candidates that survive _to_candidate below, which
            # drops handle-less accounts we were still charged for.
            self._creators_billed += len(accounts)

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

        # EVERY candidate the vendor billed for is returned — deliberately NOT
        # trimmed to `target` (changed 2026-08-14).
        #
        # The vendor bills 0.01 per creator RETURNED and its minimum page is 50,
        # so one page costs 0.5 credits for 50 creators no matter how few the
        # caller asked for. Trimming to `target` here threw away creators that
        # were already paid for — and because the caller only ever saw the
        # trimmed list, the discarded ones never entered its `seen_handles`, so
        # they were not in the next round's `exclude_handles` and the vendor
        # returned and BILLED them again. A live run spent 16 credits re-buying
        # the same page ~32 times to write one row.
        #
        # `target` now decides only whether to fetch ANOTHER PAGE (the loop
        # condition above); it is not a slice on results already bought. The
        # caller is responsible for examining no more than it has headroom for —
        # see _run_discovery_rounds' backlog, which is what keeps the extra
        # candidates from turning a money saving into a YouTube-quota blow-up.
        result = list(candidates.values())
        logger.info(
            "influencers.club discovery: %d candidate(s) over %d page(s), "
            "%.2f credits spent (%s reported remaining).",
            len(result), page, self._credits_spent,
            self._credits_left_reported if self._credits_left_reported is not None else "?",
        )
        return result

    def probe(self, filters: dict, limit: int = 1, *, source_label="pool probe"):
        """
        One measurement request, billed THROUGH THE LEDGER.

        Exists because the measurement scripts used to call `_post` directly,
        and `_post` is only the HTTP call — `can_afford` and `record_spend` live
        inside `discover()`'s loop. So every ablation run spent real vendor
        credits that `credit_log.json` never saw and that no day or month
        ceiling was ever checked against. At limit=1 that was ~0.1 credits per
        run and invisible; at the limit=20 a variant sweep needs it is ~2.0,
        about 20% of the day cap, entirely off the books. The ledger's whole
        premise is that no spend escapes it.

        Returns (accounts, total) — or (None, None) when the request failed or
        the spend was refused, so a caller can tell "no results" apart from
        "never asked". Unlike discover() this does NOT paginate, does NOT
        dedupe, and does NOT convert to candidates: it is for measuring, and
        the raw account dicts are what a precision read needs.
        """
        # `_active`, NOT `enabled`. `enabled` answers "can this client buy a
        # whole PAGE?" — it projects PAGE_LIMIT creators and a full page's
        # credits. A probe buys `limit` creators, often 1-3, so gating it on the
        # page-sized projection refuses cheap probes whenever fewer than 50
        # handles remain: the last 49 handles of an allowance became unusable
        # for measurement even though a limit=1 probe fits in them comfortably.
        # `_active` is the part that actually means "this client is still
        # alive"; the exact pricing follows immediately below and is what
        # enforces both ceilings for this call.
        if not self._active:
            return None, None
        # The vendor bills per creator RETURNED, so a limit=N request costs at
        # most N * 0.01 — much cheaper than discover()'s whole-page projection,
        # and worth pricing exactly since the point of a probe is to be cheap.
        cost_estimate = limit * CREDITS_PER_CREATOR
        if self._credits_spent + cost_estimate > self._max_credits:
            logger.warning(
                "probe would exceed the per-run ceiling (%.2f spent of %.2f, "
                "probe costs up to %.2f) — refusing.",
                self._credits_spent, self._max_credits, cost_estimate,
            )
            return None, None
        if not can_afford(cost_estimate, source_label):
            self._active = False
            return None, None
        # Probes spend from the fair-use meter exactly like a real page does,
        # and they run off the normal schedule — a variant sweep is precisely
        # the unplanned spend a period cap is for. `limit` is the worst case
        # here, not PAGE_LIMIT, since probe() asks for a short page on purpose.
        if not can_afford_handles(limit, source_label):
            self._active = False
            return None, None

        resp = self._post({
            "platform": "youtube",
            "paging": {"limit": limit, "page": 0},
            "sort": DEFAULT_SORT,
            "filters": dict(filters),
        })
        if resp is None:
            return None, None

        accounts, total, cost, credits_left = self._parse(resp)
        self._credits_spent += cost
        if not record_spend(
            cost, kind=KIND_DISCOVERY, detail=source_label, handles=len(accounts),
        ):
            self._active = False
        if credits_left is not None:
            self._credits_left_reported = credits_left
            record_vendor_balance(credits_left)
        self._creators_billed += len(accounts)
        return accounts, total

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
        """
        One account -> a pipeline candidate dict, or None to skip it.

        IDENTIFIERS ONLY — deliberately no statistics (changed 2026-08-14).

        The response also carries `profile.followers` and
        `profile.engagement_percent`, and this used to copy both onto the
        candidate. Nothing ever read them: every number the pipeline gates on,
        scores with, or writes to Airtable comes from the YouTube Data API v3
        via get_channel_stats() / get_recent_video_performance(). So they were
        dead fields that LOOKED authoritative while riding along through
        process_candidate — the same trap as the BLOCKING_STATES constant
        removed from outreach/ledger.py, where a future reader wiring up
        `candidate["subscriber_count"]` would land vendor data in a column
        labelled with a YouTube-verified figure, and no test would fail.

        Statistics are YouTube's to report. The vendor's own follower count is
        still used for the SERVER-SIDE `number_of_subscribers` discovery filter,
        which is the right use: it decides what we pay to look at, never what we
        believe about a channel. test_influencer_discovery pins the absence.

        `channel_title` stays. It is an identifier, not a statistic, and DO NOT
        CONTACT checkpoint 1 matches on it before any quota is spent.
        """
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
        return {
            "handle": handle,
            "channel_title": profile.get("full_name") or "",
            "influencers_user_id": account.get("user_id"),
            "matched_keywords": [source_label],
        }


def null_discovery() -> InfluencerDiscovery:
    """An inert discovery client, for runs with the switch turned off."""
    return InfluencerDiscovery(enabled=False)
