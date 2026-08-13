"""
The outreach send ledger — the only thing standing between an approved
prospect and a DUPLICATE cold email.

Read this before changing anything here. The first draft of this design
(OUTREACH_PLAN.md rev 1) was reviewed and found not to work, and the reasons
are the reasons this module looks the way it does.

Why a plain "claim then send" is not enough
-------------------------------------------
The obvious design is: send the email, then set Status = Contacted. There is
a window between the two. If the process dies, or the Airtable PATCH fails
(429, 5xx, network), the email is ALREADY SENT and the row still reads
Approved — so the next run sends it again. That is the same failure shape
`http_client.IDEMPOTENT_METHODS` already guards against for POSTs: the retry
hurts in exactly the case it was meant to help.

So the claim is written BEFORE the mailer is touched. But that alone is still
not enough, because:

**Airtable has no unique constraints.** A "create if absent" claim is
necessarily read-then-write (the same shape as
`airtable_client.push_record()`), so two concurrent runs interleave:

    run A: find_by_key -> 0 rows
    run B: find_by_key -> 0 rows
    run A: create_claim              run B: create_claim
    run A: SEND  ------------>       run B: SEND  ------------>   TWO EMAILS

Nothing collides. A *third*, later run notices two rows — after the damage.
Detecting a duplicate is not the same as preventing one.

The four layers, in order
------------------------
1. **One entry point.** No Airtable button, no webhook: a single scheduled
   sender. (An Airtable button *field* renders per row, so a reviewer clicking
   it on row 14 would fire a run that emails everyone else.)
2. **A startup lease** (`acquire_lease`) — a single-row lock claimed by PATCH,
   which is addressed at a known record id and therefore safe for the session
   retry adapter to repeat.
3. **Claim-verify-send** (`claim`) — create the claim, then RE-READ every row
   for that key. If more than one exists, the row with the lexicographically
   higher Airtable record id aborts before sending. A deterministic tiebreak
   means both processes reach the same verdict without talking to each other,
   and neither sends on ambiguity.
4. **A campaign-independent ever-sent guard** — the question is "has this
   channel EVER been Sent?", not "has it been sent under this campaign".

Layer 4 exists because a campaign-scoped key silently re-opens the hole. If
the campaign label defaults to something like `<niche>-<YYYY-MM>`, every key
changes on the 1st of the month and every previously-emailed prospect becomes
claimable again. Campaign is a LABEL here, not the guard.

What this module does NOT promise
---------------------------------
`claim()` narrows the concurrent-duplicate window; it does not close it.
If run A's verify read completes before run B's claim write is visible, A sees
only itself and proceeds. That residual window is why layers 1 and 2 exist and
are not optional garnish — do not delete the lease on the grounds that the
verify "handles it". If you need a hard guarantee, the answer is a real mutex
outside Airtable, not a cleverer read.

Ambiguity is never resolved in favour of sending
------------------------------------------------
`SEND_STATES` has four values, not three, because a 5xx or a read timeout from
the mail provider means UNKNOWN, not "not delivered" — the message may have
been accepted and the response lost. `influencers.py` already reasons this way
about money (only `ConnectTimeout` provably predates the send); this is the
same rule applied to something that cannot be refunded at all. Only
`STATE_NOT_SENT` is retryable, and `classify_send_failure()` (T2) is what
decides which failures earn it.

This module is deliberately storage-agnostic: it takes a `LedgerStore` (see the
protocol below) so the protocol above can be tested against an in-memory fake,
including the interleaving that broke rev 1. The Airtable-backed store lives in
`outreach_airtable.py`. Note that store MUST send `typecast=False` on the
`Send State` single-select: `typecast=True` silently CREATES a missing option,
so a typo would mint `Snet` and drop rows out of the reviewer's saved views.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

logger = logging.getLogger(__name__)


# --- Send states -------------------------------------------------------------
# Four, not three. See the module docstring: the third and fourth exist because
# "the send failed" and "we don't know whether the send happened" must not share
# a state, and only the former may ever be retried.
STATE_CLAIMED = "Claimed"        # claim written, mailer not yet called
STATE_SENT = "Sent"              # provider confirmed, message id recorded
STATE_NOT_SENT = "NotSent"       # PROVABLY not delivered — the only retryable state
STATE_MAYBE_SENT = "MaybeSent"   # unknown outcome — never auto-retried

SEND_STATES = frozenset({STATE_CLAIMED, STATE_SENT, STATE_NOT_SENT, STATE_MAYBE_SENT})

# States that mean "do not send again without a human deciding to".
BLOCKING_STATES = frozenset({STATE_SENT, STATE_MAYBE_SENT, STATE_CLAIMED})

# Refusal reasons. Surfaced verbatim in the run summary, so they are stable
# identifiers rather than prose.
REASON_GRANTED = "granted"
REASON_NO_CHANNEL_ID = "no_channel_id"
REASON_ALREADY_SENT = "already_sent"
REASON_MAYBE_SENT = "maybe_sent"
REASON_IN_FLIGHT = "in_flight"
REASON_PREVIOUSLY_NOT_SENT = "previously_not_sent"
REASON_UNKNOWN_STATE = "unknown_state"
REASON_OVER_DAILY_CAP = "over_daily_cap"
REASON_DUPLICATE_ADDRESS = "duplicate_address"
REASON_CLAIM_WRITE_FAILED = "claim_write_failed"
REASON_LOST_VERIFY = "lost_verify"

# Written to the losing claim's `Verify Result` so a human reading the ledger
# can tell a tiebreak loss from a genuine send failure.
VERIFY_LOST_TIEBREAK = "lost-tiebreak"


class LedgerUnavailable(RuntimeError):
    """
    Raised when a ledger read that a safety decision depends on cannot be
    completed.

    Deliberately breaks the log-and-return-falsy convention used elsewhere,
    for the same reason `airtable_client.AirtableReadError` does: a silent
    empty result here reads as "nothing sent today" and hands out a full
    daily budget, which is the one direction that overspends. Callers must
    abort the run rather than assume a fresh budget.
    """


@dataclass(frozen=True)
class ClaimResult:
    """Outcome of a claim attempt. `granted` is the only permission to send."""
    granted: bool
    reason: str
    key: str
    record_id: str | None = None
    detail: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        return self.granted


class LedgerStore(Protocol):
    """
    The storage seam. Implemented against Airtable in `outreach_airtable.py`
    and against a dict in the tests.

    Every read here may raise `LedgerUnavailable`; none of them may return a
    partial result silently. A half-read ledger is indistinguishable from an
    empty one, and an empty one authorises sending.
    """

    def find_by_key(self, key: str) -> list[dict]:
        """Every ledger row carrying `key`. Each dict: {"record_id", "fields"}."""

    def find_sent_for_channel(self, channel_id: str) -> list[dict]:
        """Every row for `channel_id` whose Send State is Sent, any campaign."""

    def create_claim(self, fields: dict) -> str | None:
        """POST a claim row. Returns its record id, or None on failure."""

    def patch(self, record_id: str, fields: dict) -> bool:
        """PATCH an existing ledger row."""

    def count_claimed_on(self, prospect_day: str) -> int:
        """Rows whose Claimed At falls on `prospect_day`. Raises on failure."""

    def find_stranded(self, cutoff_utc_iso: str) -> list[dict]:
        """Rows still Claimed whose Claimed At predates `cutoff_utc_iso`."""


def _utc_now_iso(clock=None) -> str:
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_key(channel_id: str, campaign: str) -> str:
    """
    The idempotency key.

    Channel ID rather than email because it is the stable identity the rest of
    the pipeline dedupes on, and because two channels can legitimately share an
    agency address. Campaign is included so a DELIBERATE second campaign is
    expressible — but it is NOT the duplicate guard; see `claim()`'s ever-sent
    check and the module docstring.
    """
    return f"{channel_id}:{campaign}"


def classify_existing(rows: list[dict]) -> tuple[bool, str, str]:
    """
    Decide whether existing ledger rows for a key permit a new send.

    Returns (blocked, reason, detail). Precedence matters and is ordered by
    how expensive it is to be wrong:

      Sent        -> already done; never send again
      MaybeSent   -> may have been delivered; never risk a second
      Claimed     -> another run is mid-flight
      unknown     -> a value outside SEND_STATES (a hand-typo in the ledger,
                     or a `typecast=True` regression that minted an option).
                     Treated as MaybeSent, NOT as absent — an unreadable
                     state must never be the reason an email goes out.
      NotSent     -> provably not delivered; retryable, but only on request
    """
    states = [(r.get("fields", {}) or {}).get("Send State") for r in rows]
    if not states:
        return False, REASON_GRANTED, ""

    unknown = [s for s in states if s not in SEND_STATES]
    if unknown:
        return True, REASON_UNKNOWN_STATE, f"unrecognised Send State {unknown!r}"
    if STATE_SENT in states:
        return True, REASON_ALREADY_SENT, ""
    if STATE_MAYBE_SENT in states:
        return True, REASON_MAYBE_SENT, ""
    if STATE_CLAIMED in states:
        return True, REASON_IN_FLIGHT, ""
    # Everything left is NotSent.
    return True, REASON_PREVIOUSLY_NOT_SENT, ""


@dataclass
class RunBudget:
    """
    Per-run send budget and in-run recipient set.

    The daily figure is read from the ledger, not tracked locally, for the same
    reason `airtable_client.count_added_today()` reads Airtable: a per-RUN cap
    is not a cap. Five runs before lunch at 50 each is 250 emails.

    `seen_addresses` is not an optimisation. A channel can legitimately be
    tracked in BOTH niche tables, and the key is deliberately per-channel, so
    an agency fronting five approved channels would otherwise receive five
    near-identical cold emails inside one run. That protects deliverability
    more than the inter-send pacing does.
    """
    remaining: int
    seen_addresses: set[str] = field(default_factory=set)

    def normalise(self, email: str) -> str:
        return (email or "").strip().casefold()

    def already_addressed(self, email: str) -> bool:
        return self.normalise(email) in self.seen_addresses

    def record(self, email: str) -> None:
        self.seen_addresses.add(self.normalise(email))
        self.remaining -= 1


def remaining_daily_budget(store: LedgerStore, prospect_day: str, daily_cap: int) -> int:
    """
    How many more sends today's budget allows.

    Propagates `LedgerUnavailable` deliberately — see that exception's
    docstring. Never returns `daily_cap` as a fallback.
    """
    already = store.count_claimed_on(prospect_day)
    remaining = max(0, daily_cap - already)
    logger.info(
        "Outreach daily budget for %s: %d already claimed, %d of %d remaining.",
        prospect_day, already, remaining, daily_cap,
    )
    return remaining


def claim(
    store: LedgerStore,
    *,
    channel_id: str,
    campaign: str,
    niche: str,
    recipient_email: str,
    channel_name: str = "",
    template_version: str = "",
    budget: RunBudget | None = None,
    allow_retry: bool = False,
    allow_recontact: bool = False,
    clock=None,
    settle=None,
) -> ClaimResult:
    """
    Attempt to claim the right to send one email. `granted=True` is the ONLY
    permission to call the mailer.

    Order is load-bearing: every free refusal happens before the claim row is
    written, so a refused candidate leaves no ledger litter, and the two reads
    that could authorise a send (ever-sent, existing-key) happen before the
    write rather than after it.

    `settle` is an injectable sleep used between the claim write and the verify
    read, so tests don't wait on Airtable's write visibility.
    """
    if not channel_id:
        # An empty channel id would build the key ":campaign", which collides
        # with every other empty-id row — the first would claim and the rest
        # would be skipped as "already sent".
        return ClaimResult(False, REASON_NO_CHANNEL_ID, key="", detail="empty Channel ID")

    key = build_key(channel_id, campaign)

    # --- Guard 1: ever sent, any campaign. Campaign-independent on purpose. ---
    if not allow_recontact:
        prior = store.find_sent_for_channel(channel_id)
        if prior:
            campaigns = sorted(
                {(r.get("fields", {}) or {}).get("Campaign", "?") for r in prior}
            )
            return ClaimResult(
                False, REASON_ALREADY_SENT, key,
                detail=f"already Sent under campaign(s) {', '.join(campaigns)}",
            )

    # --- Guard 2: existing rows for this exact key. ---
    blocked, reason, detail = classify_existing(store.find_by_key(key))
    if blocked and not (reason == REASON_PREVIOUSLY_NOT_SENT and allow_retry):
        return ClaimResult(False, reason, key, detail=detail)
    retrying_record_id = None
    if blocked and reason == REASON_PREVIOUSLY_NOT_SENT:
        # Retry PATCHes the existing row back to Claimed. It must never POST a
        # second row under the same key — that is how a ledger grows two
        # histories for one prospect.
        existing = store.find_by_key(key)
        retrying_record_id = existing[0].get("record_id") if existing else None

    # --- Guard 3: budget and per-run address dedupe. Both free. ---
    if budget is not None:
        if budget.remaining <= 0:
            return ClaimResult(False, REASON_OVER_DAILY_CAP, key)
        if budget.already_addressed(recipient_email):
            return ClaimResult(
                False, REASON_DUPLICATE_ADDRESS, key,
                detail=f"{recipient_email} already addressed this run",
            )

    now_iso = _utc_now_iso(clock)
    claim_fields = {
        "Idempotency Key": key,
        "Channel ID": channel_id,
        "Channel Name": channel_name,
        "Niche": niche,
        "Campaign": campaign,
        "Recipient Email": recipient_email,
        "Send State": STATE_CLAIMED,
        "Claimed At": now_iso,
        "Template Version": template_version,
    }

    if retrying_record_id:
        if not store.patch(retrying_record_id, {"Send State": STATE_CLAIMED, "Claimed At": now_iso}):
            return ClaimResult(False, REASON_CLAIM_WRITE_FAILED, key, detail="retry PATCH failed")
        record_id = retrying_record_id
    else:
        record_id = store.create_claim(claim_fields)
        if not record_id:
            # The write may or may not have landed. Do NOT send: re-read on the
            # next run will treat whatever is there as authoritative.
            return ClaimResult(False, REASON_CLAIM_WRITE_FAILED, key)

    # --- Verify: did anyone else claim the same key? ---
    if settle:
        settle()
    rows = store.find_by_key(key)
    contenders = sorted(r.get("record_id", "") for r in rows if r.get("record_id"))
    if len(contenders) > 1 and record_id != contenders[0]:
        # Lexicographic tiebreak: the lowest record id wins. Deterministic, so
        # both processes reach the same verdict without coordinating, and the
        # loser aborts BEFORE sending. Marked NotSent because nothing was sent
        # — which is also true, and keeps it retryable behind the ever-sent
        # guard rather than looking like an in-flight claim forever.
        store.patch(record_id, {
            "Send State": STATE_NOT_SENT,
            "Settled At": _utc_now_iso(clock),
            "Verify Result": VERIFY_LOST_TIEBREAK,
        })
        logger.warning(
            "Concurrent claim detected for %s: %d rows, keeping %s, standing down %s.",
            key, len(contenders), contenders[0], record_id,
        )
        return ClaimResult(
            False, REASON_LOST_VERIFY, key, record_id=record_id,
            detail=f"lost tiebreak to {contenders[0]}",
        )

    if budget is not None:
        budget.record(recipient_email)
    return ClaimResult(True, REASON_GRANTED, key, record_id=record_id)


def settle_sent(store: LedgerStore, record_id: str, *, provider_message_id: str, clock=None) -> bool:
    """Record a confirmed send. The message id is the delivery receipt."""
    return store.patch(record_id, {
        "Send State": STATE_SENT,
        "Settled At": _utc_now_iso(clock),
        "Provider Message ID": provider_message_id,
    })


def settle_not_sent(store: LedgerStore, record_id: str, *, error: str, clock=None) -> bool:
    """
    Record a PROVABLY undelivered send — retryable.

    Only call this for outcomes that prove the message never left: a connect
    error, a 4xx rejection of the message, or a pre-flight render/validation
    failure. For a 5xx, a read timeout, a dropped connection, or an unknown
    exception, call `settle_maybe_sent()` instead. Getting this wrong turns
    `--retry-failed` into a duplicate-send path.
    """
    return store.patch(record_id, {
        "Send State": STATE_NOT_SENT,
        "Settled At": _utc_now_iso(clock),
        "Error": error,
    })


def settle_maybe_sent(store: LedgerStore, record_id: str, *, error: str, clock=None) -> bool:
    """
    Record an outcome we cannot resolve. Never auto-retried; surfaced by
    `find_stranded()`/`--reconcile` for a human to settle by hand after
    checking the sent-mail folder.
    """
    return store.patch(record_id, {
        "Send State": STATE_MAYBE_SENT,
        "Settled At": _utc_now_iso(clock),
        "Error": error,
    })


def stranded_cutoff_iso(stranded_after_minutes: int, clock=None) -> str:
    """UTC timestamp before which a still-Claimed row counts as stranded."""
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    return (now - timedelta(minutes=stranded_after_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_stranded_claims(store: LedgerStore, stranded_after_minutes: int, clock=None) -> list[dict]:
    """
    Rows left Claimed past the threshold — i.e. a run died between claim and
    settle, so the email MAY have gone out.

    Reported, never auto-resolved. The run summary prints an explicit
    `--settle` command per row so the fix is a copy-paste rather than a
    hand-edit of the single-select the guard reads.
    """
    cutoff = stranded_cutoff_iso(stranded_after_minutes, clock)
    stranded = store.find_stranded(cutoff)
    if stranded:
        logger.warning(
            "%d outreach claim(s) stranded before %s — these MAY have been delivered. "
            "Run --reconcile; do not assume they failed.",
            len(stranded), cutoff,
        )
    return stranded


# --- Lease -------------------------------------------------------------------

LEASE_FREE = ""


class LeaseStore(Protocol):
    """Single-row lock. `read()` returns {"record_id", "fields"} or None."""

    def read(self) -> dict | None: ...

    def patch(self, record_id: str, fields: dict) -> bool: ...


@dataclass(frozen=True)
class LeaseResult:
    acquired: bool
    holder: str = ""
    detail: str = ""


def acquire_lease(
    store: LeaseStore,
    *,
    holder: str,
    stale_after_minutes: int,
    clock=None,
) -> LeaseResult:
    """
    Take the outreach lease, or refuse to start.

    PATCH rather than POST on purpose: it is addressed at a known record id, so
    the session-level retry adapter can safely repeat it — a retry converges on
    the same end state instead of minting a second lock row.

    A lease older than `stale_after_minutes` is treated as abandoned and taken
    over, because otherwise a single killed run locks outreach out forever. That
    take-over is logged loudly: it is also the signature of two runners
    genuinely overlapping, which is what layer 3 exists to catch.
    """
    row = store.read()
    if row is None:
        return LeaseResult(False, detail="lease row unreadable — refusing to start")

    fields = row.get("fields", {}) or {}
    current = fields.get("Holder") or LEASE_FREE
    since = fields.get("Acquired At") or ""

    if current and current != holder:
        if _lease_is_stale(since, stale_after_minutes, clock):
            logger.warning(
                "Taking over a STALE outreach lease held by %r since %s. If another "
                "runner is in fact alive, claim-verify-send is now the only guard.",
                current, since,
            )
        else:
            return LeaseResult(False, holder=current, detail=f"held by {current} since {since}")

    if not store.patch(row["record_id"], {"Holder": holder, "Acquired At": _utc_now_iso(clock)}):
        return LeaseResult(False, detail="could not write the lease")
    return LeaseResult(True, holder=holder)


def release_lease(store: LeaseStore, *, holder: str, clock=None) -> bool:
    """
    Release the lease. Call from a `finally`, so a crash does not hold it —
    though `stale_after_minutes` is the backstop for when even that fails.

    Only clears the lease if we still hold it: releasing someone else's lease
    after a stale take-over would hand a live runner's lock away.
    """
    row = store.read()
    if row is None:
        logger.error("Could not read the outreach lease to release it.")
        return False
    if (row.get("fields", {}) or {}).get("Holder") not in (holder, LEASE_FREE, None):
        logger.warning("Not releasing the outreach lease — it is held by someone else now.")
        return False
    return store.patch(row["record_id"], {"Holder": LEASE_FREE, "Acquired At": None})


def _lease_is_stale(since_iso: str, stale_after_minutes: int, clock=None) -> bool:
    if not since_iso:
        # A holder with no timestamp cannot be aged out on evidence. Treat it as
        # live: refusing to start is recoverable, double-sending is not.
        return False
    try:
        since = datetime.strptime(since_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        logger.warning("Unparseable lease timestamp %r — treating the lease as live.", since_iso)
        return False
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    return now - since > timedelta(minutes=stale_after_minutes)
