"""
The follow-up category decision. PURE — no I/O, no clock of its own.

WHY THIS DELEGATES INSTEAD OF DECIDING
--------------------------------------
`ledger.followup_eligibility()` already encodes the shared question — may this
creator be re-contacted? — with six preconditions and tested refusal reasons. An
earlier version of this package re-derived the date floor and the touch count
inside `legacy.free_screen()`, which meant TWO implementations of that question
in one repo. `ledger.py:136-143` names the failure mode exactly: "Two copies of
an option name on a hand-maintained schema is how you get `Canada` and
`canada`" — and here the two copies could disagree about whether someone may be
emailed.

So this module MAPS `FollowUpVerdict.reason` onto a category. It does not
recompute age, touch count or reply state. Four of the reasons already correspond
1:1 to a category:

    REASON_NO_PRIOR_SEND        -> No Prior Send
    REASON_MAX_TOUCHES          -> Touch Limit Reached
    REASON_REPLIED              -> Already Replied
    REASON_REPLY_STATE_UNKNOWN  -> Reply Unknown
    REASON_TOO_SOON             -> Not Yet Eligible
    REASON_NOT_QUALIFIED        -> No Longer Relevant

The legacy-only screens (suppression, a missing address, an unkeyable handle, a
deleted channel) are layered AROUND that call, because they are facts about a
hand-maintained 2023 table that the ledger has no vocabulary for.

ORDER IS THE SAFETY PROPERTY
----------------------------
`ledger.classify_existing()` documents its own precedence as "ordered by how
expensive it is to be wrong", and the same applies here. A row that is BOTH
suppressed and inactive must read as suppressed: `DNC Blocked` is a decision
somebody made, `Inactive Channel` is a reviewable bucket, and filing a
suppression as a review item hides it.

`Follow-Up Needed` is reachable ONLY as the final return, after every refusal has
been ruled out. A category function that assigned buckets by positive test would
let "no signal" fall through to eligible, which inverts the repo's own rule that
absent data cannot establish a fact (`ledger.py:349`).
"""
from dataclasses import dataclass

from channel_vetting.outreach.ledger import (
    REASON_GRANTED,
    REASON_MAX_TOUCHES,
    REASON_NOT_QUALIFIED,
    REASON_NOT_REQUESTED,
    REASON_NO_PRIOR_SEND,
    REASON_REPLIED,
    REASON_REPLY_STATE_UNKNOWN,
    REASON_TOO_SOON,
)

# --- The vocabulary ----------------------------------------------------------
# ONE definition, read by the writer AND by the page builder. Two copies of
# these strings is how a filter typo silently empties a page: config.py:96-99
# records that a mistyped filter value returns HTTP 200 with ZERO rows and no
# error at all, measured live. Written with typecast=False so a value not in
# this tuple 422s loudly instead of minting a new select option.
CAT_DNC_BLOCKED = "DNC Blocked"
CAT_NO_EMAIL = "No Email"
CAT_UNRESOLVABLE = "Unresolvable"
CAT_INACTIVE = "Inactive Channel"
CAT_NO_PRIOR_SEND = "No Prior Send"
CAT_ALREADY_REPLIED = "Already Replied"
CAT_TOUCH_LIMIT = "Touch Limit Reached"
CAT_REPLY_UNKNOWN = "Reply Unknown"
CAT_ACTIVITY_UNKNOWN = "Activity Unknown"
CAT_NOT_YET = "Not Yet Eligible"
CAT_RELEVANCE_UNKNOWN = "Relevance Unknown"
CAT_NOT_RELEVANT = "No Longer Relevant"
CAT_FOLLOW_UP = "Follow-Up Needed"

# In refusal order. The page builder walks this, so a category with no page is a
# test failure rather than an invisible row.
CATEGORIES = (
    CAT_DNC_BLOCKED,
    CAT_NO_EMAIL,
    CAT_UNRESOLVABLE,
    CAT_INACTIVE,
    CAT_NO_PRIOR_SEND,
    CAT_ALREADY_REPLIED,
    CAT_TOUCH_LIMIT,
    CAT_REPLY_UNKNOWN,
    CAT_ACTIVITY_UNKNOWN,
    CAT_NOT_YET,
    CAT_RELEVANCE_UNKNOWN,
    CAT_NOT_RELEVANT,
    CAT_FOLLOW_UP,
)

# Only this one may be actioned. Named so a page filter and a send guard cannot
# drift apart.
ACTIONABLE = CAT_FOLLOW_UP

# Terminal: a later run may never demote these back toward Follow-Up Needed
# without a human clearing the field. Idempotence is not enough — the INPUTS
# change every run (a sweep completes, a verdict arrives, a read half-fails), so
# the property that matters is MONOTONICITY in the safe direction. Without this
# latch, a half-failed reply read on Tuesday re-opens someone who was filed as
# Already Replied on Monday.
TERMINAL = frozenset({CAT_DNC_BLOCKED, CAT_ALREADY_REPLIED, CAT_TOUCH_LIMIT})

# Still awaiting evidence. These are the ONLY rows worth spending a paid signal
# on — everything else has already been refused for free, and re-probing a
# suppressed or touch-limited creator buys nothing.
UNDECIDED = frozenset({CAT_REPLY_UNKNOWN, CAT_ACTIVITY_UNKNOWN, CAT_RELEVANCE_UNKNOWN})

# followup_eligibility()'s reason -> category. Explicit table rather than an
# if-chain so an unmapped reason is a visible KeyError at the boundary instead of
# a silent fall-through to eligible.
REASON_TO_CATEGORY = {
    REASON_NO_PRIOR_SEND: CAT_NO_PRIOR_SEND,
    REASON_MAX_TOUCHES: CAT_TOUCH_LIMIT,
    REASON_REPLIED: CAT_ALREADY_REPLIED,
    REASON_REPLY_STATE_UNKNOWN: CAT_REPLY_UNKNOWN,
    REASON_TOO_SOON: CAT_NOT_YET,
    REASON_NOT_QUALIFIED: CAT_NOT_RELEVANT,
}


@dataclass(frozen=True)
class Signals:
    """
    Everything the ledger cannot know, gathered by the callers in `legacy.py`
    and `activity.py`.

    `channel_alive` and `relevant` are THREE-state on purpose. None means "not
    established", and it must never collapse into either True or False:
    `enrichment.days_since_last_upload()` documents the same rule for its own
    None, and `pipeline.py:740` honours it for discovery where "unknown -> keep
    the lead" is safe. For a follow-up the safe direction is the opposite, so
    None routes to its own bucket rather than to a verdict.
    """
    dnc_hit: str = ""
    has_email: bool = True
    handle: str = ""
    reply_known: bool = True
    channel_alive: bool | None = None
    relevant: bool | None = None
    relevance_detail: str = ""


def categorize(verdict, signals: Signals, *, previous: str = "") -> tuple[str, str]:
    """
    (category, reason). `verdict` is a `ledger.FollowUpVerdict`.

    `previous` is the category already stored on the row, if any. It exists only
    to enforce the TERMINAL latch: a row that was filed as suppressed, replied
    or touch-limited stays there.

    The reason string is the operator's ONLY channel — it is what renders in
    `Follow-Up Reason` on a page — so each one states the fact, and where an
    action exists, names it. `Relevance Detail` in the README is the shape being
    followed here.
    """
    if previous in TERMINAL:
        return previous, f"held: {previous} is terminal until a human clears it"

    # --- legacy-only refusals, cheapest and most decisive first --------------
    if signals.dnc_hit:
        return CAT_DNC_BLOCKED, (
            f"matched DO NOT CONTACT by {signals.dnc_hit}. "
            "This is correct — no action needed.")

    if not signals.has_email:
        return CAT_NO_EMAIL, (
            "no address on the row. Needs an email lookup or a non-email channel.")

    if not signals.handle:
        return CAT_UNRESOLVABLE, (
            "Link is not a youtube.com/@handle URL, so the channel cannot be "
            "keyed or checked. Fix the Link to re-include this row.")

    if signals.channel_alive is False:
        return CAT_INACTIVE, (
            "channel no longer resolves on YouTube (deleted, private or "
            "terminated). Do not contact.")

    # --- the shared rule, delegated -----------------------------------------
    if not verdict.eligible:
        if verdict.reason == REASON_NOT_REQUESTED:
            # Triage never asks a human first — that flag is the SEND path's
            # gate, not the categoriser's. A caller that leaves it false is
            # asking the wrong question, so say so rather than bucket it.
            raise ValueError(
                "followup_eligibility() was called with followup_requested=False; "
                "the categoriser must pass True and let the SEND path apply the "
                "human-request gate")
        try:
            cat = REASON_TO_CATEGORY[verdict.reason]
        except KeyError:
            raise ValueError(
                f"unmapped FollowUpVerdict reason {verdict.reason!r}; add it to "
                "REASON_TO_CATEGORY rather than letting it fall through"
            ) from None
        return cat, verdict.detail or verdict.reason

    # --- eligible on the shared rule; the unknowns decide the rest ----------
    # Reply state is a SIGNAL here, not a ledger precondition, and the ordering
    # is why. `followup_eligibility()` checks reply state at rule 4, BEFORE the
    # touch ceiling (ledger.py:399) and the age floor (:425) — correct for a
    # SEND, where a blank must refuse before anything else is considered. But for
    # triage it means a population with no Reply State column at all (the legacy
    # tables have none) short-circuits at rule 4 and every row lands in Reply
    # Unknown, collapsing the 990 touch-limited rows and every date bucket into
    # one. Measured: that is exactly what happened on the first wiring.
    #
    # So the caller passes REPLY_NONE to the ledger to get the age and touch
    # verdict, and passes reply_known=False here. That is not a loosening: this
    # refusal still fires, just after the two facts the ledger CAN establish. The
    # SEND path is untouched and still passes the real field, so a blank there
    # refuses at rule 4 as designed.
    if not signals.reply_known:
        return CAT_REPLY_UNKNOWN, (
            "no reply history has been read for this creator, so 'did not reply' "
            "is unproven rather than false. To clear: set Reply State on the row.")

    if signals.channel_alive is None:
        return CAT_ACTIVITY_UNKNOWN, (
            "activity not checked yet — this is not a judgement about the "
            "channel. Runs when the dead-channel sweep reaches this row.")

    if signals.relevant is None:
        return CAT_RELEVANCE_UNKNOWN, (
            "no relevance signal available. Gemini relevance is disabled "
            "pending the criteria rewrite, so only the text screen has run.")

    if signals.relevant is False:
        return CAT_NOT_RELEVANT, (
            signals.relevance_detail
            or "does not match current collection criteria. Flag for re-check if wrong.")

    return CAT_FOLLOW_UP, (
        f"{verdict.detail}; channel resolves; passes the text relevance screen"
    ).strip("; ")
