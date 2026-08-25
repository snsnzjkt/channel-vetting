"""
Airtable-backed implementations of `outreach_ledger`'s storage protocols.

This is the seam between the concurrency logic (which is storage-agnostic and
tested against an in-memory fake) and the real `Outreach Log` / `Outreach Lock`
tables.

Why this is NOT built on `airtable.client.push_record()`
--------------------------------------------------------
`push_record()` is hard-keyed on "Channel ID" and PATCHes the existing row when
it finds one (`airtable/client.py:266-271`). Pointed at the ledger it would
find the PREVIOUS campaign's row for the same channel and overwrite it —
destroying the send history the duplicate guard depends on, silently, while
appearing to work. The ledger needs append-mostly semantics and lookups by a
different key, so it gets its own narrow functions.

Reads here RAISE rather than returning empty
--------------------------------------------
`find_by_key()` and `find_sent_for_channel()` are the two reads that can
AUTHORISE a send. An empty result from them means "never contacted, go ahead".
So a failed read that returned `[]` would not be a missing answer, it would be
a WRONG one, and the failure mode is a duplicate cold email. Both raise
`LedgerUnavailable`, as does the daily-cap count, for the same reason
`airtable.client.count_added_today()` raises: failing open is the one direction
that overspends. `find_stranded()` is reporting only and may fail soft.

Writes send `typecast=False`
----------------------------
The opposite of `push_record()`, deliberately. `typecast=True` silently CREATES
a missing single-select option, so a typo would mint `Snet` as a fifth
`Send State` and the guard would read a value it has no rule for. This base has
already been bitten by that: its hand-maintained selects contain `Canada` AND
`canada`, `Sweden` AND `Sweeden`, and a `Location` option whose name is
literally "Location". Here an unknown option must 422 loudly instead.
"""
import logging
import time

# Kept for `requests.RequestException` only — calls go through the shared
# session, never through module-level `requests.get()`.
import requests

from channel_vetting.airtable.client import _base_url, _headers, _quote_formula_value
from channel_vetting.config import (
    AIRTABLE_TABLE_OUTREACH_LOCK,
    AIRTABLE_TABLE_OUTREACH_LOG,
    API_SLEEP_SECONDS,
    PROSPECT_DAY_TZ,
    STATUS_APPROVED,
    STATUS_CONTACTED,
)
from channel_vetting.core.http_client import (
    AIRTABLE as HTTP,
    post_with_rate_limit_retry,
    safe_body,
)
from channel_vetting.outreach.ledger import SENDABLE_QUALIFICATION, LedgerUnavailable

logger = logging.getLogger(__name__)

PAGE_SIZE = 100


def _rows(payload: dict) -> list[dict]:
    """Airtable records -> the {record_id, fields} shape the protocols use."""
    return [
        {"record_id": r.get("id", ""), "fields": r.get("fields", {}) or {}}
        for r in payload.get("records", [])
    ]


def _select(table: str, formula: str, *, what: str, fields: list[str] | None = None) -> list[dict]:
    """
    Paginated server-side select. ALWAYS raises `LedgerUnavailable` on failure.

    There is deliberately no `soft` switch. It existed, and it tripled the
    error handling for one caller: three near-identical
    `if soft: log; return out` / `else: raise` pairs, which a fourth failure
    mode would have to remember to branch the same way — silently becoming
    raise-only for a caller documented as fail-soft. `find_stranded()` absorbs
    the exception itself instead, which cannot drift.

    Paces between pages with `API_SLEEP_SECONDS`, like every other paginator in
    this repo (`airtable_client`, `external_dedupe`, `do_not_contact`).
    `airtable_client`'s header explains at length that pacing and retry are
    BOTH required and that dropping either half is not a simplification —
    Airtable's 5 req/s is per BASE, and this base is shared with human editors.
    This module is the one holding a mail credential and a claim lease while it
    runs, so a self-inflicted 429 storm is the worst place to save a sleep.

    `fields` narrows the payload. `count_claimed_on` in particular only needs a
    row COUNT, so pulling whole ledger rows to call len() is pure waste once
    the daily cap is raised past a page.
    """
    out: list[dict] = []
    offset = None
    while True:
        params: dict = {"filterByFormula": formula, "pageSize": PAGE_SIZE}
        if fields:
            params["fields[]"] = fields
        if offset:
            params["offset"] = offset
        try:
            resp = HTTP.get(_base_url(table), headers=_headers(), params=params, timeout=30)
        except requests.RequestException as e:
            raise LedgerUnavailable(f"{what} request failed: {e}") from e

        if resp.status_code != 200:
            raise LedgerUnavailable(f"{what} failed: {resp.status_code} {safe_body(resp)}")

        try:
            payload = resp.json()
        except ValueError as e:
            # A 200 is not a promise of JSON — a proxy interstitial is HTML.
            raise LedgerUnavailable(f"{what} returned unparseable JSON: {e}") from e

        out.extend(_rows(payload))
        offset = payload.get("offset")
        if not offset:
            return out
        time.sleep(API_SLEEP_SECONDS)


def _patch(table: str, record_id: str, fields: dict, *, what: str) -> bool:
    """
    PATCH one row. Addressed at a known record id, so the session's retry
    adapter can safely repeat it — a retry converges on the same end state
    rather than creating anything.

    Shared by both stores so the `typecast=False` invariant lives in ONE
    literal. It was in three, and it is load-bearing: `typecast=True` silently
    CREATES a missing single-select option, so a typo would mint a fifth
    `Send State` the guard has no rule for. A future store that copies the
    wrong neighbour would have silently got the unsafe default.
    """
    try:
        resp = HTTP.patch(
            f"{_base_url(table)}/{record_id}",
            headers=_headers(),
            json={"fields": fields, "typecast": False},
            timeout=30,
        )
    except requests.RequestException as e:
        logger.error("%s PATCH %s failed: %s", what, record_id, e)
        return False
    if resp.status_code != 200:
        logger.error(
            "%s PATCH %s rejected: %s %s", what, record_id, resp.status_code, safe_body(resp)
        )
        return False
    return True


def get_queued_prospects(table_name: str) -> list[dict]:
    """
    The rows a run may contact, read server-side. Raises `LedgerUnavailable`.

    Five conditions, ALL required, and the order they are written in is the
    order they matter:

      1. `Qualification = 'Qualified'` — only the pipeline's own verdict is
         emailable. `New Channel` and the legacy `Below View Minimum` are
         flagged for a HUMAN to look at, which is the entire reason the flagged
         budget is separate. A reviewer who wants to approach a flagged channel
         uses the base's manual outreach tables.
      2. `Status = 'Approved'` — the explicit human decision.
      3. `Send Requested At` is set — the queue-then-schedule step that replaced
         an Airtable button. A button *field* renders per row, so clicking it on
         row 14 would have fired a run that emailed everyone else; stamping a
         date queues exactly one row and leaves a window to de-queue it.
      4. `Email` is non-empty — nothing to send to otherwise.
      5. `Outreach Ineligible Reason` is empty — a row a previous run already
         skipped for cause does not silently re-enter the queue.

    There is no default-include path: `New`, `Reviewing`, `Rejected` and
    `Contacted` are all excluded by construction rather than by omission.

    This raises rather than returning `[]` for the same reason the ledger reads
    do — except inverted, and worth being explicit about: an empty result here
    means "nothing to send", which is SAFE. It still raises, because a silent
    partial page would under-send without anyone noticing, and a caller that
    cannot tell "nobody is queued" from "the read broke" will report a green
    run either way.
    """
    # `!= ""` for the dateTime, NOT `!= BLANK()`. This is not a style choice and
    # it is not interchangeable — MEASURED LIVE 2026-08-14 on the real base, with
    # `Send Requested At` freshly created and therefore empty on every row:
    #
    #     {Send Requested At} != BLANK()   -> 47 rows   (ALL of them)
    #     {Send Requested At} != ""        ->  0 rows   (correct)
    #
    # So `!= BLANK()` matches an empty dateTime. Had this shipped, the queue
    # selector would have returned every Approved+Qualified row as "queued" and a
    # --send run would have emailed 47 creators that no human ever queued. Demo
    # mode would have caught it — every message redirects — which is precisely
    # the reason that gate defaults on and is a separate switch from --dry-run.
    #
    # `= ''` on the two TEXT fields below is fine and is verified by the same
    # measurement; the quirk is specific to the date/dateTime comparison.
    formula = (
        "AND("
        f"{{Qualification}} = '{_quote_formula_value(SENDABLE_QUALIFICATION)}',"
        " {Status} = '" + STATUS_APPROVED + "',"
        ' {Send Requested At} != "",'
        " {Email} != '',"
        " {Outreach Ineligible Reason} = ''"
        ")"
    )
    rows = _select(table_name, formula, what=f"get_queued_prospects({table_name})")
    # Oldest request first, so a row that has been waiting does not get starved
    # by a fresher one every run.
    rows.sort(key=lambda r: (r.get("fields", {}) or {}).get("Send Requested At") or "")
    # No `limit` parameter: sends are already bounded by RunBudget and the
    # loop's remaining-check, and truncating the QUEUE READ would starve the
    # oldest-first ordering this sort just established. A parameter kept alive
    # only by the test written for it is the dead code CLAUDE.md warns about.
    return rows


def get_followup_queue(table_name: str) -> list[dict]:
    """
    Prospect rows a human has TICKED for a follow-up, read server-side.

    This is `get_queued_prospects()`'s sibling for touch 2, and the differences
    are the interesting part:

      * it reads `Follow-up Requested At`, not `Send Requested At`. Two separate
        stamps, so a first-touch queue and a follow-up queue can never be
        confused for one another, and de-queueing one does not touch the other.

      * it does NOT require `Status = 'Approved'`. After touch 1 the row reads
        `Contacted`, and filtering on either value would be wrong: `Approved`
        excludes every genuine follow-up candidate, and `Contacted` excludes a
        row whose `mark_contacted()` PATCH failed after a successful send — a
        documented case, see that function. The authoritative evidence of a
        first touch is a `Sent` ledger row, which `followup_eligibility()`
        precondition 3 checks. The ledger is the truth; `Status` is a
        consequence.

      * it requires `Reply State = 'No Reply'` EXACTLY. A blank or an
        unrecognised value is excluded here and refused again by
        `followup_eligibility()` rule 4. Defence in depth, the same shape as
        `SENDABLE_QUALIFICATION` being both a formula clause and a constant
        check: this filter is a hand-built `filterByFormula` and a typo in one
        fails OPEN.

    `!= ""` on the dateTime, NOT `!= BLANK()`. Measured live 2026-08-14 on this
    base: `!= BLANK()` matched all 47 rows on an empty dateTime where `!= ""`
    correctly matched 0. Had that shipped in `get_queued_prospects()` it would
    have emailed 47 creators nobody queued.

    Raises `LedgerUnavailable` rather than returning a partial page, for the same
    reason `get_queued_prospects()` does.
    """
    formula = (
        "AND("
        f"{{Qualification}} = '{_quote_formula_value(SENDABLE_QUALIFICATION)}',"
        ' {Follow-up Requested At} != "",'
        " {Email} != '',"
        " {Outreach Ineligible Reason} = '',"
        f" {{Reply State}} = '{_quote_formula_value(REPLY_NONE)}'"
        ")"
    )
    rows = _select(table_name, formula, what=f"get_followup_queue({table_name})")
    # Oldest request first, matching get_queued_prospects: a row that has been
    # waiting must not be starved by a fresher tick every run.
    rows.sort(key=lambda r: (r.get("fields", {}) or {}).get("Follow-up Requested At") or "")
    return rows


def mark_contacted(table_name: str, record_id: str) -> bool:
    """
    Flip a prospect row to Contacted after a successful send.

    Lives here, next to `get_queued_prospects()`, because this module owns
    access to the niche prospect tables — the read and the write of one table
    belong together. It was `outreach/sender.py` reaching for a private `_patch`
    across a module boundary, which put the `typecast=False` invariant's one
    literal behind an underscore that then had an outside caller.

    A CONSEQUENCE, not the guard. If this fails the Outreach Log still says
    `Sent`, so the next run still skips the row — which is the whole reason the
    duplicate guard was built on the ledger rather than on this field.

    `typecast=False` matters here specifically: `scripts/audit/audit_blocklist.py` writes a
    Status through `push_record` with `typecast=True` and needs a whole
    `_status_option_exists()` preflight to compensate. Do not copy that one.
    """
    if not record_id:
        return False
    return _patch(table_name, record_id, {"Status": STATUS_CONTACTED}, what="Prospect row")


class AirtableLedgerStore:
    """`outreach.ledger.LedgerStore` against the global Outreach Log table."""

    def __init__(self, table_name: str = AIRTABLE_TABLE_OUTREACH_LOG):
        self.table = table_name

    def find_by_key(self, key: str) -> list[dict]:
        """Every row for an idempotency key. Raises — see the module docstring."""
        formula = f"{{Idempotency Key}} = '{_quote_formula_value(key)}'"
        return _select(self.table, formula, what=f"find_by_key({key})")

    def find_sent_for_channel(self, channel_id: str) -> list[dict]:
        """
        Every Sent row for a channel, ANY campaign — the ever-sent guard.

        Campaign-independent on purpose: a campaign-scoped question would let a
        month-derived label reset the guard on the 1st.
        """
        formula = (
            f"AND({{Channel ID}} = '{_quote_formula_value(channel_id)}',"
            f" {{Send State}} = 'Sent')"
        )
        return _select(self.table, formula, what=f"find_sent_for_channel({channel_id})")

    def create_claim(self, fields: dict) -> str | None:
        """
        POST a claim row. Returns its record id, or None on failure.

        Uses `post_with_rate_limit_retry`, which retries ONLY on 429 — the one
        status that means Airtable rejected the request without processing it.
        A general POST retry could create a second claim row for one send.
        """
        payload = {"fields": fields, "typecast": False}
        try:
            resp = post_with_rate_limit_retry(
                _base_url(self.table), headers=_headers(), json=payload, timeout=30
            )
        except requests.RequestException as e:
            # The write may or may not have landed. The caller does NOT send;
            # the next run re-reads and treats whatever is there as truth.
            logger.error("Outreach Log claim POST failed: %s", e)
            return None

        if resp.status_code not in (200, 201):
            logger.error(
                "Outreach Log claim POST rejected: %s %s", resp.status_code, safe_body(resp)
            )
            return None
        try:
            return resp.json().get("id")
        except ValueError:
            logger.error("Outreach Log claim POST returned unparseable JSON.")
            return None

    def patch(self, record_id: str, fields: dict) -> bool:
        """PATCH a ledger row. See the module-level `_patch`."""
        return _patch(self.table, record_id, fields, what="Outreach Log")

    def count_claimed_on(self, prospect_day: str) -> int:
        """
        Claims made on a given PROSPECT day. Raises on failure.

        `Claimed At` is a UTC dateTime but the cap is denominated in prospect
        days (`PROSPECT_DAY_TZ`, currently America/Toronto), so the comparison
        MUST convert. A plain `DATESTR({Claimed At})` would compare UTC dates
        and the budget would roll over at the wrong moment — 4-5 hours early
        for Toronto, which is the same class of silent clock desync that
        `quota_tracker`'s Pacific day and the prospect day were separated to
        avoid. SET_TIMEZONE is what makes the two agree.

        VERIFIED LIVE 2026-08-14 against the real base: a probe row stamped
        `2026-08-14T20:31:19Z` was counted for prospect day `2026-08-14`.
        That is worth stating because the failure mode is not a wrong number —
        an unsupported formula 422s, `_select` raises `LedgerUnavailable`, and
        every outreach run aborts at the cap check. Re-probe if the formula or
        the timezone value changes.
        """
        formula = (
            f"DATESTR(SET_TIMEZONE({{Claimed At}}, '{PROSPECT_DAY_TZ}'))"
            f" = '{_quote_formula_value(prospect_day)}'"
        )
        # Only the row COUNT matters, so ask for one small field instead of whole
        # ledger rows. Free today at a cap of 10; it stops being free the moment
        # the cap is raised past a page.
        rows = _select(
            self.table, formula,
            what=f"count_claimed_on({prospect_day})",
            fields=["Idempotency Key"],
        )
        return len(rows)

    def find_stranded(self, cutoff_utc_iso: str) -> list[dict]:
        """
        Rows still `Claimed` from before the cutoff — a run died between claim
        and settle, so these MAY have been delivered.

        Reporting only, so this one fails SOFT: an empty result here cannot
        authorise a send, it only under-reports a problem that --reconcile will
        surface on the next run.

        VERIFIED LIVE 2026-08-14: `IS_BEFORE` does coerce our
        `%Y-%m-%dT%H:%M:%SZ` string — a probe row matched a future cutoff and
        was correctly excluded by a 2020 one. Worth pinning because the failure
        here is SILENT: if the coercion stopped working this would report zero
        stranded claims forever, and a run that died between claim and settle
        would leave a possibly-delivered email that nobody is told about.
        """
        formula = (
            f"AND({{Send State}} = 'Claimed',"
            f" IS_BEFORE({{Claimed At}}, '{_quote_formula_value(cutoff_utc_iso)}'))"
        )
        try:
            return _select(self.table, formula, what="find_stranded")
        except LedgerUnavailable as e:
            # Absorbed HERE rather than via a `soft=` switch inside _select().
            # That switch tripled the error handling for this one caller, and a
            # fourth failure mode added to _select() would have had to remember
            # to branch the same way — silently turning this caller raise-only.
            # A try/except at the one place that wants it cannot drift.
            logger.error("find_stranded failed (reporting only, run continues): %s", e)
            return []


class AirtableLeaseStore:
    """
    `outreach.ledger.LeaseStore` against the single-row Outreach Lock table.

    ADVISORY ONLY. Airtable has no conditional writes, so acquire_lease() is
    itself read-then-write and two runs can both read "free". Real
    serialisation is the CI concurrency group; this exists to catch a hand-run
    `channel-vetting-outreach` overlapping with a scheduled one, which CI
    cannot see.
    """

    def __init__(self, table_name: str = AIRTABLE_TABLE_OUTREACH_LOCK):
        self.table = table_name

    def read(self) -> dict | None:
        """
        The single lock row, or None if it cannot be read.

        None makes `acquire_lease()` refuse to start. That is fail-closed and
        intended: a lock we cannot read is not the same as a lock that is free,
        and treating it as free is how two runs start at once.
        """
        try:
            resp = HTTP.get(
                _base_url(self.table), headers=_headers(),
                params={"pageSize": 1}, timeout=30,
            )
        except requests.RequestException as e:
            logger.error("Outreach Lock read failed: %s", e)
            return None
        if resp.status_code != 200:
            logger.error("Outreach Lock read rejected: %s %s", resp.status_code, safe_body(resp))
            return None
        try:
            rows = _rows(resp.json())
        except ValueError:
            logger.error("Outreach Lock read returned unparseable JSON.")
            return None
        if not rows:
            # The seeded row was deleted. acquire_lease() never creates one —
            # recreating it here would race exactly like the lock it guards.
            logger.error(
                "Outreach Lock table is EMPTY. Recreate the single row with "
                "Lock Name = 'outreach' and Holder blank; outreach cannot run "
                "without it."
            )
            return None
        return rows[0]

    def patch(self, record_id: str, fields: dict) -> bool:
        """PATCH the lock row. See the module-level `_patch`."""
        return _patch(self.table, record_id, fields, what="Outreach Lock")
