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

from iso_time import parse_iso_utc
from scoring import QUALIFIED

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

# There is deliberately NO set of "blocking" states here. classify_existing()
# below decides that with an ordered if-chain, and a flat set cannot express
# what the chain does: each state maps to a distinct REASON_*, and the order is
# load-bearing (Sent outranks MaybeSent outranks Claimed). A BLOCKING_STATES
# set sat here and was read by nothing — the dangerous kind of dead code, since
# adding a state to it looks like blocking that state while changing no
# behaviour. Add new blocking states to the chain, not to a set.

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

# --- Who may be emailed at all -----------------------------------------------
# Only the pipeline's own "Qualified" verdict is emailable. `New Channel` and
# the legacy `Below View Minimum` are flagged for a HUMAN to look at, not for
# the sender to pick up — that is the whole point of the separate flagged
# budget. A reviewer who wants to approach a flagged channel does it through
# the base's existing manual outreach tables.
#
# The primary gate is the server-side filter in `get_queued_prospects()`; this
# constant is the defence-in-depth check, because that filter is a hand-built
# `filterByFormula` on a hand-maintained view and a typo there fails OPEN.
# Imported, NOT re-spelled: this is the exact string scoring.qualify() writes
# into the Airtable single-select, and main.py already imports the constant
# rather than typing it. Two copies of an option name on a hand-maintained
# schema is how you get `Canada` and `canada` — and the copy that would fail
# open is this one, since the gate here is the defence-in-depth behind a
# hand-built filterByFormula. scoring.py imports only `math`, so no cycle.
SENDABLE_QUALIFICATION = QUALIFIED
REASON_NOT_QUALIFIED = "not_qualified"

# --- Demo mode lives in mailer.py --------------------------------------------
# `resolve_recipient()` and `DemoModeError` were here, and that was the wrong
# layer: this module never touches the mailer, so the gate was only safe by
# CONVENTION — nothing stopped a caller doing `mailer.send(to=row["Email"])`
# and skipping it. `Mailer.send()` now takes the prospect's real address and
# applies the redirect itself, so a caller that forgets the gate cannot be
# written. The ledger still records the REAL address, which is why the two
# concerns are separable at all.

# --- Follow-up ("respam") refusal reasons ------------------------------------
REASON_NO_PRIOR_SEND = "no_prior_send"
REASON_REPLIED = "replied"
REASON_REPLY_STATE_UNKNOWN = "reply_state_unknown"
REASON_TOO_SOON = "too_soon"
REASON_MAX_TOUCHES = "max_touches_reached"
REASON_NOT_REQUESTED = "followup_not_requested"

# `Reply State` values. A follow-up is only ever sent to a NON-replier, so this
# vocabulary is load-bearing rather than reporting.
REPLY_NONE = "No Reply"
REPLY_REPLIED = "Replied"
REPLY_INTERESTED = "Interested"
REPLY_DECLINED = "Declined"
REPLY_STATES = frozenset({REPLY_NONE, REPLY_REPLIED, REPLY_INTERESTED, REPLY_DECLINED})


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


def _parse_utc(value: str):
    """
    Parse a timestamp read back off an Airtable dateTime field, or return None.

    NOT "one of our own stamps" — that wording was the bug. `_utc_now_iso()`
    above writes `%Y-%m-%dT%H:%M:%SZ`, but the value round-trips through an
    Airtable dateTime and comes back with milliseconds (`...T12:00:00.000Z`),
    which a strict `strptime` on the write format rejects. So this could not
    read what it had just written, and the failure was silent in the refuse
    direction: `followup_eligibility()` rejected every follow-up with "prior
    send has no readable timestamp", making OUTREACH_RESPAM_MIN_DAYS
    unreachable, and `_lease_is_stale()` returned False at any age, so a
    stranded lease never aged out and needed clearing by hand.

    Delegates to `iso_time.parse_iso_utc` so this rule has exactly one
    implementation — see that module for the full history.

    Still returns None rather than raising, and every caller still treats None
    as "cannot prove anything from this": for a lease that means "assume it is
    live", for a follow-up "assume not enough time has passed". Both lean away
    from acting, which is why the bug above was safe as well as invisible.
    """
    return parse_iso_utc(value)


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


@dataclass(frozen=True)
class FollowUpVerdict:
    """
    Whether a channel may receive a FOLLOW-UP (the "respam" button).

    This is the only sanctioned way past the ever-sent guard. `claim()` will
    accept `allow_recontact=True` from anywhere, which is a footgun, so the
    rule is: **a scheduled run may only pass `allow_recontact=True` when it is
    holding an `eligible` verdict from this function.** An operator using
    `--allow-recontact` by hand is overriding on purpose and owns the result.
    """
    eligible: bool
    reason: str
    touch_number: int = 0
    last_sent_at: str = ""
    next_campaign: str = ""
    detail: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        return self.eligible


def followup_eligibility(
    store: LedgerStore,
    *,
    channel_id: str,
    qualification: str,
    reply_state: str,
    followup_requested: bool,
    campaign_prefix: str,
    min_days_since_send: int,
    max_touches: int,
    clock=None,
) -> FollowUpVerdict:
    """
    Decide whether a non-replier may be emailed a second time.

    Every precondition below must hold. They are checked in order of how badly
    it reads to get them wrong, and none of them is inferred:

      1. `followup_requested` — a human pressed the button. Follow-ups are
         never automatic; nothing here goes hunting for people to re-email.
      2. `Qualification == "Qualified"` — same rule as a first touch.
      3. A prior `Sent` row exists. Without proof of touch 1 this is not a
         follow-up, it is a first touch trying to skip the ever-sent guard.
      4. `Reply State == "No Reply"` — EXACTLY that. A blank, an unrecognised
         value, or anything else refuses. This deliberately inverts the
         pipeline's usual "absent data never disqualifies" rule, for the same
         reason the English-language gate does: the action is only defined for
         a non-replier, and a blank cannot establish that. Emailing someone who
         already replied is worse than not emailing them at all.
      5. Enough time has passed since the LAST send (`min_days_since_send`).
      6. Touch count is under `max_touches`. Prior `Sent` rows ARE the touch
         count, so this is bounded by evidence rather than by a counter someone
         can reset. Without it "respam" has no natural end, and a follow-up
         cadence with no ceiling is indistinguishable from spam — which the
         name of the button says out loud.

    Returns a verdict carrying `next_campaign`, a NEW label, so the follow-up
    gets its own idempotency key and its own ledger row. That is what campaign
    labels are for; the ever-sent guard stays keyed on the channel.

    NOTE this function cannot see the suppression list. DO NOT CONTACT is
    re-checked at send time on handle+email+name, as for a first touch, and a
    creator who asked to be left alone must never reach this path.
    """
    if not followup_requested:
        return FollowUpVerdict(False, REASON_NOT_REQUESTED)

    if qualification != SENDABLE_QUALIFICATION:
        return FollowUpVerdict(
            False, REASON_NOT_QUALIFIED,
            detail=f"Qualification is {qualification!r}, not {SENDABLE_QUALIFICATION!r}",
        )

    prior = store.find_sent_for_channel(channel_id)
    if not prior:
        return FollowUpVerdict(
            False, REASON_NO_PRIOR_SEND,
            detail="no Sent row — a follow-up requires a completed first touch",
        )

    if reply_state not in REPLY_STATES:
        return FollowUpVerdict(
            False, REASON_REPLY_STATE_UNKNOWN,
            detail=f"Reply State {reply_state!r} is blank or unrecognised — "
                   "triage the inbox before following up",
        )
    if reply_state != REPLY_NONE:
        return FollowUpVerdict(
            False, REASON_REPLIED,
            detail=f"Reply State is {reply_state!r} — do not re-email a responder",
        )

    touches = len(prior)
    if touches >= max_touches:
        return FollowUpVerdict(
            False, REASON_MAX_TOUCHES, touch_number=touches,
            detail=f"{touches} send(s) already, ceiling is {max_touches}",
        )

    # The LAST send, not the first — otherwise a second follow-up could go out
    # the day after the first one on the strength of touch 1's age.
    # max(), not sorted()[-1]: `prior` is already known non-empty (the
    # no-prior-send guard returned above), so there is no empty case, and a
    # reader does not have to check the sort direction to see which end wins.
    last_sent_at = max(
        (r.get("fields", {}) or {}).get("Settled At")
        or (r.get("fields", {}) or {}).get("Claimed At")
        or ""
        for r in prior
    )
    last_sent = _parse_utc(last_sent_at)
    if last_sent is None:
        return FollowUpVerdict(
            False, REASON_TOO_SOON, touch_number=touches, last_sent_at=last_sent_at,
            detail="prior send has no readable timestamp — cannot prove enough time passed",
        )

    now = (clock or (lambda: datetime.now(timezone.utc)))()
    age_days = (now - last_sent).days
    if age_days < min_days_since_send:
        return FollowUpVerdict(
            False, REASON_TOO_SOON, touch_number=touches, last_sent_at=last_sent_at,
            detail=f"last send was {age_days}d ago, minimum is {min_days_since_send}d",
        )

    return FollowUpVerdict(
        True, REASON_GRANTED,
        touch_number=touches + 1,
        last_sent_at=last_sent_at,
        next_campaign=f"{campaign_prefix}-followup{touches + 1}",
        detail=f"{age_days}d since touch {touches}",
    )


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
    qualification: str,
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

    # Defence in depth behind the server-side filter. `qualification` is passed
    # in rather than defaulted, so every call site has to state it and a new
    # caller cannot inherit a permissive default.
    key = build_key(channel_id, campaign)
    if qualification != SENDABLE_QUALIFICATION:
        return ClaimResult(
            False, REASON_NOT_QUALIFIED, key,
            detail=f"Qualification is {qualification!r}, not {SENDABLE_QUALIFICATION!r}",
        )

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
    # Read ONCE and reuse. Re-reading to recover the retry target cost a second
    # round trip against Airtable's 5-req/s PER-BASE limit (shared with human
    # editors and other automations), and opened a window in which a concurrent
    # write could make the row we PATCH a different one from the row we
    # classified.
    existing = store.find_by_key(key)
    blocked, reason, detail = classify_existing(existing)
    if blocked and not (reason == REASON_PREVIOUSLY_NOT_SENT and allow_retry):
        return ClaimResult(False, reason, key, detail=detail)
    # Past the early return, `blocked` already implies the retry case, and
    # classify_existing() returns not-blocked for an empty list — so `blocked`
    # also implies `existing` is non-empty. Restating either would be dead.
    # Retry PATCHes the existing row back to Claimed; it must never POST a
    # second row under the same key, which is how a ledger grows two histories
    # for one prospect.
    retrying_record_id = existing[0].get("record_id") if blocked else None

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
    since = _parse_utc(since_iso)
    if since is None:
        # A holder with no readable timestamp cannot be aged out on evidence.
        # Treat it as live: refusing to start is recoverable, double-sending is
        # not.
        if since_iso:
            logger.warning("Unparseable lease timestamp %r — treating the lease as live.", since_iso)
        return False
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    return now - since > timedelta(minutes=stale_after_minutes)
