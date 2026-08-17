"""
Tests for `outreach_ledger.py` — the duplicate-cold-email guard.

The first test in this file is the one that matters most. The reviewed plan's
first draft claimed that a claim-then-send protocol keyed on an "Idempotency
Key" would make two concurrent runs "collide -> skip". Airtable has no unique
constraints, so it does not: the claim is read-then-write and two runs
interleave straight through it. `test_interleaved_claims_send_exactly_once`
reproduces that interleaving and pins the fix.

The fake store below models the ONE property of Airtable that makes this hard:
a read reflects the state at the moment it was issued, and another writer can
land between that read and your own write.
"""
import pytest

import outreach_ledger as L


class FakeLedgerStore:
    """In-memory LedgerStore with a hook for interleaving another writer."""

    def __init__(self):
        self.rows: list[dict] = []
        self._seq = 0
        # Fires DURING find_by_key, after the result is snapshotted but before
        # it is returned — i.e. "someone else wrote after we read". One-shot.
        self.on_find_by_key = None
        self.create_should_fail = False
        self.patch_should_fail = False
        self.count_raises = False
        self.claimed_today = 0
        self.creates = 0
        self.patches: list[tuple[str, dict]] = []
        # When set, the verify read (every find_by_key after the first per key)
        # returns [] — modelling Airtable write-visibility lag. See
        # test_documented_limitation_stale_verify_reads_can_double_send.
        self.stale_verify = False
        self._key_reads: dict[str, int] = {}

    def _new_id(self) -> str:
        self._seq += 1
        return f"rec{self._seq:03d}"

    def add(self, key, state, *, channel_id="UC_a", campaign="c1", claimed_at="2026-08-14T09:00:00Z"):
        row = {
            "record_id": self._new_id(),
            "fields": {
                "Idempotency Key": key,
                "Channel ID": channel_id,
                "Campaign": campaign,
                "Send State": state,
                "Claimed At": claimed_at,
            },
        }
        self.rows.append(row)
        return row

    # --- LedgerStore ---
    def find_by_key(self, key):
        self._key_reads[key] = self._key_reads.get(key, 0) + 1
        snapshot = [r for r in self.rows if r["fields"].get("Idempotency Key") == key]
        if self.stale_verify and self._key_reads[key] > 1:
            snapshot = []
        hook, self.on_find_by_key = self.on_find_by_key, None
        if hook:
            hook()
        return snapshot

    def find_sent_for_channel(self, channel_id):
        return [
            r for r in self.rows
            if r["fields"].get("Channel ID") == channel_id
            and r["fields"].get("Send State") == L.STATE_SENT
        ]

    def create_claim(self, fields):
        self.creates += 1
        if self.create_should_fail:
            return None
        row = {"record_id": self._new_id(), "fields": dict(fields)}
        self.rows.append(row)
        return row["record_id"]

    def patch(self, record_id, fields):
        self.patches.append((record_id, dict(fields)))
        if self.patch_should_fail:
            return False
        for r in self.rows:
            if r["record_id"] == record_id:
                r["fields"].update({k: v for k, v in fields.items() if v is not None})
                return True
        return False

    def count_claimed_on(self, prospect_day):
        if self.count_raises:
            raise L.LedgerUnavailable(f"count_claimed_on({prospect_day}) failed")
        return self.claimed_today

    def find_stranded(self, cutoff_utc_iso):
        return [
            r for r in self.rows
            if r["fields"].get("Send State") == L.STATE_CLAIMED
            and (r["fields"].get("Claimed At") or "") < cutoff_utc_iso
        ]


def _claim(store, **kw):
    base = dict(
        channel_id="UC_a",
        campaign="ht-2026-08",
        niche="Home Theater",
        recipient_email="creator@example.com",
        # Stated explicitly at every call site on purpose — there is no
        # permissive default for the send-permission gate.
        qualification="Qualified",
    )
    base.update(kw)
    return L.claim(store, **base)


# --- The interleaving that broke rev 1 --------------------------------------

def test_interleaved_claims_send_exactly_once():
    """
    Run B's whole claim lands between run A's key read and A's own write.

    Both runs believe the key is free when they read it — which is exactly what
    Airtable's lack of a unique constraint permits. Only one may be granted
    permission to send.
    """
    store = FakeLedgerStore()
    grants = []

    def run_b():
        grants.append(_claim(store))

    store.on_find_by_key = run_b
    grants.append(_claim(store))

    granted = [g for g in grants if g.granted]
    assert len(granted) == 1, f"expected exactly one send, got {[ (g.granted, g.reason) for g in grants ]}"

    # Both wrote a claim row — that is unavoidable. The point is that the loser
    # stood down BEFORE sending, and said why.
    assert store.creates == 2
    loser = [g for g in grants if not g.granted][0]
    assert loser.reason == L.REASON_LOST_VERIFY
    assert L.VERIFY_LOST_TIEBREAK in str(store.patches)


def test_tiebreak_is_deterministic_lowest_record_id_wins():
    store = FakeLedgerStore()
    outcomes = []
    store.on_find_by_key = lambda: outcomes.append(_claim(store))
    outcomes.append(_claim(store))

    winner = [o for o in outcomes if o.granted][0]
    all_ids = sorted(r["record_id"] for r in store.rows)
    assert winner.record_id == all_ids[0]


def test_verify_read_can_be_delayed_for_write_visibility():
    """
    The real store needs a beat between the claim write and the verify read,
    or the verify cannot see a competing row. That delay is injectable so the
    suite doesn't wait on it.
    """
    store = FakeLedgerStore()
    calls = []
    assert _claim(store, settle=lambda: calls.append(1)).granted
    assert calls == [1], "the verify read must be preceded by the settle delay"


def test_documented_limitation_stale_verify_reads_can_double_send():
    """
    DOCUMENTED LIMITATION, pinned on purpose.

    claim-verify-send NARROWS the concurrent-duplicate window; it does not
    close it. If both runs' verify reads complete before the other's claim
    write is visible, neither sees a contender and both send.

    This test exists so that nobody deletes the startup lease or the
    single-entry-point rule on the theory that the verify "handles
    concurrency". It does not. If this test ever starts failing because the
    verify became a hard guarantee, delete it deliberately — do not weaken
    the other layers to make it pass.
    """
    store = FakeLedgerStore()
    store.stale_verify = True
    grants = []
    store.on_find_by_key = lambda: grants.append(_claim(store))
    grants.append(_claim(store))

    assert len([g for g in grants if g.granted]) == 2, (
        "with invisible writes both runs proceed — this is why acquire_lease() exists"
    )


def test_claim_write_failure_never_sends():
    store = FakeLedgerStore()
    store.create_should_fail = True
    result = _claim(store)
    assert not result.granted
    assert result.reason == L.REASON_CLAIM_WRITE_FAILED


# --- The ever-sent guard (campaign-independence) ----------------------------

def test_prior_send_under_a_different_campaign_blocks():
    """
    The month-rollover case. A campaign-scoped key changes on the 1st; the
    guard must not.
    """
    store = FakeLedgerStore()
    store.add("UC_a:ht-2026-07", L.STATE_SENT, campaign="ht-2026-07")

    result = _claim(store, campaign="ht-2026-08")
    assert not result.granted
    assert result.reason == L.REASON_ALREADY_SENT
    assert "ht-2026-07" in result.detail
    assert store.creates == 0, "must refuse before writing a claim row"


def test_allow_recontact_bypasses_the_ever_sent_guard():
    store = FakeLedgerStore()
    store.add("UC_a:ht-2026-07", L.STATE_SENT, campaign="ht-2026-07")
    assert _claim(store, campaign="ht-2026-08", allow_recontact=True).granted


# --- Existing-key states ----------------------------------------------------

@pytest.mark.parametrize(
    "state,expected_reason",
    [
        (L.STATE_SENT, L.REASON_ALREADY_SENT),
        (L.STATE_MAYBE_SENT, L.REASON_MAYBE_SENT),
        (L.STATE_CLAIMED, L.REASON_IN_FLIGHT),
        (L.STATE_NOT_SENT, L.REASON_PREVIOUSLY_NOT_SENT),
    ],
)
def test_existing_key_blocks_by_state(state, expected_reason):
    store = FakeLedgerStore()
    store.add("UC_a:ht-2026-08", state)
    # Sent rows also trip guard 1; force this test onto guard 2 for that case.
    result = _claim(store, allow_recontact=True)
    assert not result.granted
    assert result.reason == expected_reason
    assert store.creates == 0


def test_maybe_sent_is_never_retryable():
    """A lost response may have been delivered. --retry-failed must not touch it."""
    store = FakeLedgerStore()
    store.add("UC_a:ht-2026-08", L.STATE_MAYBE_SENT)
    result = _claim(store, allow_retry=True)
    assert not result.granted
    assert result.reason == L.REASON_MAYBE_SENT


def test_unknown_send_state_is_treated_as_maybe_sent_not_absent():
    """
    A hand-typo in the ledger (or a `typecast=True` regression minting an
    option) must never be the reason an email goes out.
    """
    store = FakeLedgerStore()
    store.add("UC_a:ht-2026-08", "Snet")
    result = _claim(store, allow_retry=True, allow_recontact=True)
    assert not result.granted
    assert result.reason == L.REASON_UNKNOWN_STATE
    assert "Snet" in result.detail


def test_retry_patches_the_existing_row_and_never_creates_a_second():
    store = FakeLedgerStore()
    row = store.add("UC_a:ht-2026-08", L.STATE_NOT_SENT)

    result = _claim(store, allow_retry=True)
    assert result.granted
    assert result.record_id == row["record_id"]
    assert store.creates == 0, "retry must PATCH, not POST a second ledger row"
    assert len([r for r in store.rows if r["fields"]["Idempotency Key"] == "UC_a:ht-2026-08"]) == 1


def test_retry_reads_the_key_once_for_the_guard_not_twice():
    """
    Regression: claim() re-read find_by_key to recover the retry target, which
    cost a second round trip against Airtable's 5-req/s PER-BASE limit (shared
    with human editors) and opened a window where a concurrent write could make
    the row we PATCH a different one from the row we classified.

    Two reads total is correct: one for the guard, one for the post-write
    verify. A third means the guard read was duplicated.
    """
    store = FakeLedgerStore()
    store.add("UC_a:ht-2026-08", L.STATE_NOT_SENT)
    reads = []
    real = store.find_by_key
    store.find_by_key = lambda k: (reads.append(k), real(k))[1]

    assert _claim(store, allow_retry=True).granted
    assert len(reads) == 2, f"expected guard + verify, got {len(reads)} reads"


def test_retry_patch_failure_never_sends():
    store = FakeLedgerStore()
    store.add("UC_a:ht-2026-08", L.STATE_NOT_SENT)
    store.patch_should_fail = True
    result = _claim(store, allow_retry=True)
    assert not result.granted
    assert result.reason == L.REASON_CLAIM_WRITE_FAILED


# --- Keying edge cases ------------------------------------------------------

def test_empty_channel_id_is_refused_before_keying():
    """Otherwise the key is ':campaign' and every such row collides."""
    store = FakeLedgerStore()
    result = _claim(store, channel_id="")
    assert not result.granted
    assert result.reason == L.REASON_NO_CHANNEL_ID
    assert store.creates == 0


def test_build_key_shape():
    assert L.build_key("UC_x", "ls-2026-08") == "UC_x:ls-2026-08"


# --- Budget and address dedupe ---------------------------------------------

def test_daily_budget_read_failure_raises_rather_than_assuming_a_full_budget():
    store = FakeLedgerStore()
    store.count_raises = True
    with pytest.raises(L.LedgerUnavailable):
        L.remaining_daily_budget(store, "2026-08-14", 10)


def test_daily_budget_subtracts_what_the_ledger_already_holds():
    store = FakeLedgerStore()
    store.claimed_today = 7
    assert L.remaining_daily_budget(store, "2026-08-14", 10) == 3


def test_daily_budget_never_goes_negative():
    store = FakeLedgerStore()
    store.claimed_today = 99
    assert L.remaining_daily_budget(store, "2026-08-14", 10) == 0


def test_exhausted_budget_refuses_before_writing():
    store = FakeLedgerStore()
    result = _claim(store, budget=L.RunBudget(remaining=0))
    assert not result.granted
    assert result.reason == L.REASON_OVER_DAILY_CAP
    assert store.creates == 0


def test_shared_agency_address_is_emailed_once_per_run():
    """
    Five approved channels behind one agency address must not produce five
    near-identical cold emails in a single run.
    """
    store = FakeLedgerStore()
    budget = L.RunBudget(remaining=10)
    granted = [
        _claim(store, channel_id=f"UC_{i}", recipient_email="agency@example.com", budget=budget).granted
        for i in range(5)
    ]
    assert granted.count(True) == 1
    assert granted.count(False) == 4


def test_address_dedupe_is_case_and_whitespace_insensitive():
    store = FakeLedgerStore()
    budget = L.RunBudget(remaining=10)
    assert _claim(store, channel_id="UC_1", recipient_email="A@Example.com", budget=budget).granted
    second = _claim(store, channel_id="UC_2", recipient_email="  a@example.com ", budget=budget)
    assert not second.granted
    assert second.reason == L.REASON_DUPLICATE_ADDRESS


def test_budget_only_decrements_on_a_granted_claim():
    store = FakeLedgerStore()
    budget = L.RunBudget(remaining=2)
    store.add("UC_a:ht-2026-08", L.STATE_SENT)
    _claim(store, budget=budget)          # refused
    assert budget.remaining == 2
    _claim(store, channel_id="UC_b", budget=budget)   # granted
    assert budget.remaining == 1


# --- Settling ---------------------------------------------------------------

def test_settle_sent_records_the_message_id():
    store = FakeLedgerStore()
    row = store.add("UC_a:ht-2026-08", L.STATE_CLAIMED)
    assert L.settle_sent(store, row["record_id"], provider_message_id="msg-1")
    assert row["fields"]["Send State"] == L.STATE_SENT
    assert row["fields"]["Provider Message ID"] == "msg-1"
    assert row["fields"]["Settled At"]


def test_settle_not_sent_is_retryable_and_maybe_sent_is_not():
    store = FakeLedgerStore()
    a = store.add("UC_a:ht-2026-08", L.STATE_CLAIMED)
    b = store.add("UC_b:ht-2026-08", L.STATE_CLAIMED, channel_id="UC_b")

    L.settle_not_sent(store, a["record_id"], error="550 rejected")
    L.settle_maybe_sent(store, b["record_id"], error="ReadTimeout")

    assert a["fields"]["Send State"] == L.STATE_NOT_SENT
    assert b["fields"]["Send State"] == L.STATE_MAYBE_SENT

    blocked_a, reason_a, _ = L.classify_existing([a])
    blocked_b, reason_b, _ = L.classify_existing([b])
    assert reason_a == L.REASON_PREVIOUSLY_NOT_SENT   # eligible for --retry-failed
    assert reason_b == L.REASON_MAYBE_SENT            # never retried
    assert blocked_a and blocked_b


def test_classify_existing_empty_is_not_blocked():
    assert L.classify_existing([]) == (False, L.REASON_GRANTED, "")


def test_sent_wins_precedence_over_every_other_state():
    rows = [
        {"fields": {"Send State": L.STATE_NOT_SENT}},
        {"fields": {"Send State": L.STATE_SENT}},
        {"fields": {"Send State": L.STATE_CLAIMED}},
    ]
    assert L.classify_existing(rows)[1] == L.REASON_ALREADY_SENT


# --- Stranded claims --------------------------------------------------------

def test_stranded_claims_are_reported_not_resolved():
    store = FakeLedgerStore()
    old = store.add("UC_a:ht-2026-08", L.STATE_CLAIMED, claimed_at="2026-08-14T09:00:00Z")
    store.add("UC_b:ht-2026-08", L.STATE_CLAIMED, channel_id="UC_b", claimed_at="2026-08-14T11:59:00Z")
    store.add("UC_c:ht-2026-08", L.STATE_SENT, channel_id="UC_c", claimed_at="2026-08-14T09:00:00Z")

    from datetime import datetime, timezone
    now = lambda: datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)

    stranded = L.find_stranded_claims(store, 60, clock=now)
    ids = [r["record_id"] for r in stranded]
    assert ids == [old["record_id"]], "only Claimed rows past the threshold"
    assert store.patches == [], "reconcile must not auto-settle anything"


def test_stranded_cutoff_subtracts_the_threshold():
    from datetime import datetime, timezone
    now = lambda: datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
    assert L.stranded_cutoff_iso(90, clock=now) == "2026-08-14T10:30:00Z"


# --- Lease ------------------------------------------------------------------

class FakeLeaseStore:
    def __init__(self, holder="", acquired_at=None, readable=True):
        self.readable = readable
        self.patch_should_fail = False
        self.row = {
            "record_id": "recLEASE",
            "fields": {"Holder": holder, "Acquired At": acquired_at},
        }

    def read(self):
        return self.row if self.readable else None

    def patch(self, record_id, fields):
        if self.patch_should_fail:
            return False
        self.row["fields"].update(fields)
        return True


def _now(h=12):
    from datetime import datetime, timezone
    return lambda: datetime(2026, 8, 14, h, 0, 0, tzinfo=timezone.utc)


def test_lease_acquired_when_free():
    store = FakeLeaseStore()
    assert L.acquire_lease(store, holder="run-1", stale_after_minutes=60, clock=_now()).acquired
    assert store.row["fields"]["Holder"] == "run-1"


def test_lease_refused_when_held_by_a_live_runner():
    store = FakeLeaseStore(holder="run-other", acquired_at="2026-08-14T11:45:00Z")
    result = L.acquire_lease(store, holder="run-1", stale_after_minutes=60, clock=_now())
    assert not result.acquired
    assert result.holder == "run-other"
    assert store.row["fields"]["Holder"] == "run-other", "must not steal a live lease"


def test_stale_lease_is_taken_over():
    store = FakeLeaseStore(holder="run-dead", acquired_at="2026-08-14T09:00:00Z")
    assert L.acquire_lease(store, holder="run-1", stale_after_minutes=60, clock=_now()).acquired
    assert store.row["fields"]["Holder"] == "run-1"


def test_unreadable_lease_refuses_to_start():
    """Fail closed: an unreadable lock is not an absent one."""
    store = FakeLeaseStore(readable=False)
    assert not L.acquire_lease(store, holder="run-1", stale_after_minutes=60, clock=_now()).acquired


def test_holder_without_a_timestamp_is_treated_as_live():
    store = FakeLeaseStore(holder="run-other", acquired_at=None)
    assert not L.acquire_lease(store, holder="run-1", stale_after_minutes=60, clock=_now()).acquired


def test_unparseable_lease_timestamp_is_treated_as_live():
    store = FakeLeaseStore(holder="run-other", acquired_at="not-a-date")
    assert not L.acquire_lease(store, holder="run-1", stale_after_minutes=60, clock=_now()).acquired


def test_lease_write_failure_is_not_an_acquisition():
    store = FakeLeaseStore()
    store.patch_should_fail = True
    assert not L.acquire_lease(store, holder="run-1", stale_after_minutes=60, clock=_now()).acquired


def test_release_clears_our_own_lease():
    store = FakeLeaseStore(holder="run-1", acquired_at="2026-08-14T11:59:00Z")
    assert L.release_lease(store, holder="run-1", clock=_now())
    assert store.row["fields"]["Holder"] == L.LEASE_FREE


def test_release_does_not_clear_someone_elses_lease():
    """After a stale take-over, the original runner must not free the new holder."""
    store = FakeLeaseStore(holder="run-2", acquired_at="2026-08-14T11:59:00Z")
    assert not L.release_lease(store, holder="run-1", clock=_now())
    assert store.row["fields"]["Holder"] == "run-2"


# --- Only "Qualified" may be emailed ----------------------------------------

@pytest.mark.parametrize("qualification", ["New Channel", "Below View Minimum", "", None, "qualified"])
def test_only_qualified_prospects_can_be_claimed(qualification):
    """
    The flagged buckets exist for a human to look at, not for the sender to
    pick up. Note "qualified" lowercase is also refused — the match is exact,
    because the live single-select is hand-maintained and has drifted before
    (the base has both `Canada` and `canada`).
    """
    store = FakeLedgerStore()
    result = _claim(store, qualification=qualification)
    assert not result.granted
    assert result.reason == L.REASON_NOT_QUALIFIED
    assert store.creates == 0, "must refuse before writing a claim row"


def test_qualified_prospect_is_claimable():
    store = FakeLedgerStore()
    assert _claim(store, qualification="Qualified").granted


def test_qualification_has_no_default_so_callers_must_state_it():
    """A permissive default on a send-permission gate fails open."""
    store = FakeLedgerStore()
    with pytest.raises(TypeError):
        L.claim(
            store, channel_id="UC_a", campaign="c", niche="n",
            recipient_email="a@b.com",
        )


# --- Follow-up / "respam" ---------------------------------------------------

def _sent_row(store, channel_id="UC_a", settled_at="2026-05-01T09:00:00Z", campaign="ht-2026-05"):
    row = store.add(f"{channel_id}:{campaign}", L.STATE_SENT, channel_id=channel_id, campaign=campaign)
    row["fields"]["Settled At"] = settled_at
    return row


def _followup(store, **kw):
    base = dict(
        channel_id="UC_a",
        qualification="Qualified",
        reply_state=L.REPLY_NONE,
        followup_requested=True,
        campaign_prefix="ht",
        min_days_since_send=90,
        max_touches=2,
        clock=_now_on("2026-08-14"),
    )
    base.update(kw)
    return L.followup_eligibility(store, **base)


def _now_on(day):
    from datetime import datetime, timezone
    y, m, d = (int(x) for x in day.split("-"))
    return lambda: datetime(y, m, d, 12, 0, 0, tzinfo=timezone.utc)


def test_followup_is_eligible_after_the_waiting_period():
    store = FakeLedgerStore()
    _sent_row(store, settled_at="2026-05-01T09:00:00Z")   # 105 days before 2026-08-14
    v = _followup(store)
    assert v.eligible
    assert v.touch_number == 2
    assert v.next_campaign == "ht-followup2"
    assert v.last_sent_at == "2026-05-01T09:00:00Z"


def test_followup_requires_a_human_to_ask():
    """Nothing goes hunting for people to re-email."""
    store = FakeLedgerStore()
    _sent_row(store)
    v = _followup(store, followup_requested=False)
    assert not v.eligible
    assert v.reason == L.REASON_NOT_REQUESTED


def test_followup_requires_qualified():
    store = FakeLedgerStore()
    _sent_row(store)
    v = _followup(store, qualification="New Channel")
    assert not v.eligible
    assert v.reason == L.REASON_NOT_QUALIFIED


def test_followup_requires_proof_of_a_first_touch():
    """Otherwise this is a first touch trying to skip the ever-sent guard."""
    store = FakeLedgerStore()
    v = _followup(store)
    assert not v.eligible
    assert v.reason == L.REASON_NO_PRIOR_SEND


@pytest.mark.parametrize("reply", [L.REPLY_REPLIED, L.REPLY_INTERESTED, L.REPLY_DECLINED])
def test_followup_never_re_emails_someone_who_replied(reply):
    store = FakeLedgerStore()
    _sent_row(store)
    v = _followup(store, reply_state=reply)
    assert not v.eligible
    assert v.reason == L.REASON_REPLIED


@pytest.mark.parametrize("reply", ["", None, "no reply", "Unknown", "None"])
def test_blank_or_unrecognised_reply_state_refuses(reply):
    """
    Deliberately inverts "absent data never disqualifies". The action is only
    defined for a non-replier and a blank cannot establish that.
    """
    store = FakeLedgerStore()
    _sent_row(store)
    v = _followup(store, reply_state=reply)
    assert not v.eligible
    assert v.reason == L.REASON_REPLY_STATE_UNKNOWN


def test_followup_refused_before_the_waiting_period():
    store = FakeLedgerStore()
    _sent_row(store, settled_at="2026-08-01T09:00:00Z")   # 13 days
    v = _followup(store)
    assert not v.eligible
    assert v.reason == L.REASON_TOO_SOON
    assert "13d ago" in v.detail


def test_followup_ages_from_the_LAST_send_not_the_first():
    """
    Otherwise a second follow-up rides out the day after the first, on the
    strength of touch 1's age.
    """
    store = FakeLedgerStore()
    _sent_row(store, settled_at="2026-01-01T09:00:00Z", campaign="ht-2026-01")
    _sent_row(store, settled_at="2026-08-10T09:00:00Z", campaign="ht-followup2")
    v = _followup(store, max_touches=5)
    assert not v.eligible
    assert v.reason == L.REASON_TOO_SOON


def test_followup_is_bounded_by_max_touches():
    """Respam with no ceiling is just spam. Prior Sent rows ARE the counter."""
    store = FakeLedgerStore()
    _sent_row(store, settled_at="2026-01-01T09:00:00Z", campaign="ht-2026-01")
    _sent_row(store, settled_at="2026-04-01T09:00:00Z", campaign="ht-followup2")
    v = _followup(store, max_touches=2)
    assert not v.eligible
    assert v.reason == L.REASON_MAX_TOUCHES
    assert v.touch_number == 2


def test_followup_refuses_when_the_prior_send_has_no_readable_timestamp():
    store = FakeLedgerStore()
    row = _sent_row(store)
    row["fields"]["Settled At"] = "not-a-date"
    row["fields"]["Claimed At"] = "also-not-a-date"
    v = _followup(store)
    assert not v.eligible
    assert v.reason == L.REASON_TOO_SOON


def test_followup_falls_back_to_claimed_at_when_settled_at_is_missing():
    store = FakeLedgerStore()
    row = _sent_row(store)
    del row["fields"]["Settled At"]
    row["fields"]["Claimed At"] = "2026-05-01T09:00:00Z"
    assert _followup(store).eligible


def test_an_eligible_followup_claims_under_a_new_key():
    """
    The follow-up gets its own campaign, so its idempotency key differs and it
    lands as a SECOND ledger row — the audit trail shows both touches.
    """
    store = FakeLedgerStore()
    _sent_row(store, settled_at="2026-05-01T09:00:00Z")
    v = _followup(store)
    assert v.eligible

    # Without the verdict's bypass, the ever-sent guard correctly refuses.
    blocked = _claim(store, campaign=v.next_campaign)
    assert not blocked.granted
    assert blocked.reason == L.REASON_ALREADY_SENT

    granted = _claim(store, campaign=v.next_campaign, allow_recontact=True)
    assert granted.granted
    assert granted.key == "UC_a:ht-followup2"
    assert granted.key != "UC_a:ht-2026-05"


def test_reply_states_vocabulary_is_exactly_four():
    assert L.REPLY_STATES == {"No Reply", "Replied", "Interested", "Declined"}


# --- State vocabulary -------------------------------------------------------

def test_send_states_are_exactly_four():
    """
    The store writes these with typecast=False, so this set must match the
    Airtable single-select options exactly. Adding a fifth without adding the
    option first means every claim 422s.
    """
    assert L.SEND_STATES == {"Claimed", "Sent", "NotSent", "MaybeSent"}


def test_only_not_sent_is_retryable():
    retryable = {
        s for s in L.SEND_STATES
        if L.classify_existing([{"fields": {"Send State": s}}])[1] == L.REASON_PREVIOUSLY_NOT_SENT
    }
    assert retryable == {L.STATE_NOT_SENT}
