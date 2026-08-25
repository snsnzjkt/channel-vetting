"""
The outreach sender: the one entry point that turns an Approved prospect into a
sent, logged email.

Run `channel-vetting-outreach` for a dry run (the default) — or
`python -m channel_vetting.outreach.sender`, which is what CI uses and does not
need pip's script directory on PATH. `--send` is required to actually deliver,
and even then `OUTREACH_DEMO_MODE` redirects every message to
`OUTREACH_DEMO_RECIPIENT` until someone deliberately turns it off.

Two independent gates, on purpose
---------------------------------
`--dry-run` lives on the command line, one copied README command away from
being overridden. Demo mode lives in the environment, so leaving it takes an
edit someone has to justify. The send path must clear BOTH.

Order of operations, and why
----------------------------
Every free refusal happens before anything is written or sent:

    preflight (templates, creds, footer) -> exit 1, nothing claimed
    read the daily budget from the ledger -> exit 1 if unreadable
    fetch blocklist                      -> exit 1 if unreachable (fail closed)
    acquire lease                        -> exit 1 if held
    per prospect:
        render                          -> skip on TemplateError
        blocklist re-check              -> skip
        CLAIM (ledger write)            -> skip if refused
        SEND                            -> settle Sent / NotSent / MaybeSent
        mark Contacted                  -> best effort; the ledger is the guard

Exit codes are a contract (`pipeline.py` set the precedent: a scheduled run that
did nothing must never be reported as green):

    0  ran and sent at least one message, or a dry run rendered at least one
    1  aborted before doing any work (creds, footer, lease, blocklist, ledger)
    2  ran but produced nothing — there WAS work and none of it got done.
       An empty queue returns 0, not 2: this workflow is manual-only, so a dry
       run when nobody has queued anything is a normal answer, and painting it
       red trains people to ignore the X.

The lease is taken LAST, after the budget and blocklist reads, so a run that is
going to be refused never holds it. The cost is that a losing run spends those
reads before standing down; moving the lease first would need the blocklist
fetch inside the lease's finally, or a fail-closed abort would leak the lease
for the full stale threshold. Noted as a follow-up, not done here.
"""
import argparse
import logging
import os
from channel_vetting.core.paths import data_path
import socket
import signal
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from channel_vetting.outreach import templates as templates
from channel_vetting.config import (
    GMAIL_CREDENTIALS_B64,
    GMAIL_SENDER_EMAIL,
    OUTREACH_LEASE_STALE_MINUTES,
    OUTREACH_DAILY_CAP,
    OUTREACH_DEMO_MODE,
    OUTREACH_DEMO_RECIPIENT,
    OUTREACH_FOOTER_TEXT,
    OUTREACH_MAX_PER_RUN,
    OUTREACH_SLEEP_SECONDS,
    OUTREACH_STRANDED_AFTER_MINUTES,
    OUTREACH_UNSUBSCRIBE_URL,
    STATUS_CONTACTED,
)
from channel_vetting.airtable.do_not_contact import (
    BlocklistUnavailable,
    fetch_blocklist,
)
from channel_vetting.enrichment.channels import EMAIL_PATTERN
from channel_vetting.outreach.mailer import (
    DemoModeError,
    MailerError,
    build_message,
    from_config,
    recipient_for,
)
from channel_vetting.discovery.niches import NICHES
from channel_vetting.airtable.outreach_store import (
    AirtableLedgerStore,
    AirtableLeaseStore,
    get_queued_prospects,
    mark_contacted,
)
from channel_vetting.outreach.ledger import (
    LedgerUnavailable,
    acquire_lease,
    release_lease,
    RunBudget,
    claim,
    find_stranded_claims,
    remaining_daily_budget,
    settle_maybe_sent,
    settle_not_sent,
    settle_sent,
)
from channel_vetting.core.prospect_day import today_iso
from channel_vetting.core.text_safety import csv_unsafe

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

PREVIEW_DIR = Path(data_path("outreach_preview"))

EXIT_OK = 0
EXIT_ABORTED = 1
EXIT_NOTHING_DONE = 2

# Set by the SIGINT handler. Checked at candidate boundaries only — never
# mid claim->send->settle, because an interrupt landing there strands a row in
# `Claimed`, which means "this email MAY have been delivered" and costs a human
# a trip to the sent-mail folder to resolve.
_INTERRUPTED = False


def _handle_sigint(signum, frame):
    global _INTERRUPTED
    if _INTERRUPTED:
        # Second Ctrl-C: the operator means it. May strand one claim.
        logger.warning("Second interrupt — exiting now. Run --reconcile.")
        raise KeyboardInterrupt
    _INTERRUPTED = True
    logger.warning("Interrupt received — finishing the current message, then stopping.")


def redact(email: str) -> str:
    """
    `j***@domain.tld`. Recipient addresses are creator PII and CI logs are
    retained for 90 days, for data subjects in zones this pipeline deliberately
    targets. Full addresses appear only in `--dry-run`, which is local.
    """
    if "@" not in (email or ""):
        return "?"
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


def preflight(send_mode: bool) -> list[str]:
    """
    Return a list of blocking problems. Empty means safe to proceed.

    Checked BEFORE the lease and before the blocklist, because these are free
    and local: failing here costs nothing, while failing after a claim strands
    a ledger row.
    """
    problems = []
    for niche in NICHES:
        if niche not in templates.TEMPLATES:
            problems.append(
                f"no email template for niche {niche!r} — add one to "
                f"outreach.templates.TEMPLATES or the niche cannot be contacted"
            )
    if send_mode:
        if not OUTREACH_FOOTER_TEXT:
            problems.append(
                "OUTREACH_FOOTER_TEXT is not set. CAN-SPAM requires a physical "
                "postal address on every commercial message. Fix: set it in .env "
                "or as a CI secret."
            )
        if not OUTREACH_UNSUBSCRIBE_URL:
            problems.append(
                "OUTREACH_UNSUBSCRIBE_URL is not set. CAN-SPAM (and PECR in the "
                "UK/EU, which the search zones target) requires a working opt-out. "
                "Fix: set it in .env or as a CI secret."
            )
        if OUTREACH_DEMO_MODE and not OUTREACH_DEMO_RECIPIENT:
            problems.append(
                "OUTREACH_DEMO_MODE is on but OUTREACH_DEMO_RECIPIENT is unset. "
                "Refusing to send: with no redirect target the only way to proceed "
                "would be to email the real prospect."
            )
    return problems


def render_for(row: dict, niche: str) -> dict:
    """Render one prospect's email, or raise TemplateError."""
    fields = row.get("fields", row)
    return templates.render(
        niche,
        channel_name=fields.get("Channel Name", ""),
        channel_url=fields.get("Channel URL", ""),
        channel_id=fields.get("Channel ID", ""),
        footer_text=OUTREACH_FOOTER_TEXT,
        unsubscribe_url=OUTREACH_UNSUBSCRIBE_URL,
    )


def write_preview(campaign: str, channel_id: str, rendered: dict, *, to: str, sender: str) -> Path:
    """
    Write a dry run's message to disk as RFC-822.

    A `.eml` opens in any mail client, so the operator can read the actual
    message with this creator's name in it — which is the question they are
    really being asked before authorising a send. A recipient COUNT does not
    answer it, and the repo's own precedent (scripts/backfill/cleanup_external_duplicates.py)
    is to print everything before asking for confirmation.
    """
    out_dir = PREVIEW_DIR / campaign
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{channel_id or 'unknown'}.eml"
    msg = build_message(
        sender=sender, to=to, subject=rendered["subject"],
        text=rendered["text"], html=rendered["html"],
    )
    path.write_bytes(msg.as_bytes())
    return path


@dataclass
class Summary:
    """
    Per-run counters. Printed on every exit path, including an interrupt and an
    unhandled exception — see run()'s finally.

    A dataclass, not a hand-rolled __init__: `print()` is the only real
    behaviour here (it branches on mode and has a warning path), and a Counter
    makes the increment a one-liner instead of a method.
    """

    sent: int = 0
    not_sent: int = 0
    maybe_sent: int = 0
    rendered: int = 0
    contacted_patch_failures: int = 0
    skipped: Counter = field(default_factory=Counter)

    def print(self, *, campaign: str, mode: str, remaining: int, stranded: int) -> None:
        logger.info("--- Outreach run summary ---")
        logger.info("Campaign: %s   Mode: %s", campaign, mode)
        if mode == "dry-run":
            logger.info("Rendered:        %d  (previews in %s/)", self.rendered, PREVIEW_DIR)
        else:
            logger.info("Sent:            %d", self.sent)
            logger.info("NotSent:         %d  (provably undelivered; --retry-failed eligible)", self.not_sent)
            logger.info("MaybeSent:       %d  (UNKNOWN outcome; never auto-retried)", self.maybe_sent)
        for reason, n in sorted(self.skipped.items(), key=lambda kv: -kv[1]):
            logger.info("Skipped (%s): %d", reason, n)
        if self.contacted_patch_failures:
            logger.warning(
                "%d row(s) sent but not marked Contacted. The ledger still says Sent, "
                "so they will NOT be re-sent.", self.contacted_patch_failures,
            )
        logger.info("Daily budget remaining: %d", remaining)
        if stranded:
            logger.warning(
                "%d stranded claim(s) MAY have been delivered. Run: "
                "channel-vetting-outreach --reconcile", stranded,
            )


def run(args) -> int:
    mode = "send" if args.send else "dry-run"
    campaign = args.campaign or today_iso()
    summary = Summary()

    problems = preflight(args.send)
    if problems:
        for p in problems:
            logger.error("ABORTING: %s", p)
        return EXIT_ABORTED

    ledger = AirtableLedgerStore()

    # --reconcile / --settle are reporting/repair paths: no lease, no sending.
    if args.reconcile:
        stranded = find_stranded_claims(ledger, OUTREACH_STRANDED_AFTER_MINUTES)
        if not stranded:
            logger.info("No stranded claims.")
            return EXIT_OK
        logger.warning("%d stranded claim(s) — these MAY have been delivered:", len(stranded))
        for row in stranded:
            f = row.get("fields", {})
            logger.warning(
                "  %s  claimed %s  ->  channel-vetting-outreach "
                "--settle %s --state sent|notsent",
                f.get("Idempotency Key"), f.get("Claimed At"), f.get("Idempotency Key"),
            )
        return EXIT_OK

    if args.settle:
        if not args.state:
            logger.error("--settle requires --state sent|notsent.")
            return EXIT_ABORTED
        rows = ledger.find_by_key(args.settle)
        if not rows:
            logger.error("No ledger row for key %r.", args.settle)
            return EXIT_ABORTED
        record_id = rows[0]["record_id"]
        note = f"settled by hand via --settle --state {args.state}"
        if args.state == "sent":
            ok = settle_sent(ledger, record_id, provider_message_id="")
        else:
            ok = settle_not_sent(ledger, record_id, error=note)
        logger.info("Settled %s as %s: %s", args.settle, args.state, "ok" if ok else "FAILED")
        return EXIT_OK if ok else EXIT_ABORTED

    if args.check_setup:
        # Validates what the workflow step's comment CLAIMS it validates. It used
        # to return before the mailer was ever constructed, so it reported
        # "credentials present" having checked nothing but the templates — a
        # misleading green on a repo with no footer and no credentials.
        problems = []
        try:
            AirtableLeaseStore().read() is not None or problems.append(
                "Outreach Lock table has no readable row; outreach cannot start."
            )
            ledger.count_claimed_on(today_iso())
            for niche, cfg in NICHES.items():
                get_queued_prospects(cfg["table_name"])
        except Exception as e:
            problems.append(f"Airtable schema/permission check failed: {e}")
        if OUTREACH_SENDER_CONFIGURED := bool(GMAIL_SENDER_EMAIL and GMAIL_CREDENTIALS_B64):
            try:
                from_config(
                    sender=GMAIL_SENDER_EMAIL, credentials_b64=GMAIL_CREDENTIALS_B64,
                    demo_mode=OUTREACH_DEMO_MODE, demo_recipient=OUTREACH_DEMO_RECIPIENT,
                )
            except MailerError as e:
                problems.append(f"Gmail credentials: {e}")
        for p in problems:
            logger.error("CHECK FAILED: %s", p)
        logger.info(
            "Checked: templates, Airtable schema + lock row%s.",
            ", Gmail credentials" if OUTREACH_SENDER_CONFIGURED
            else " (Gmail credentials not configured — --send would refuse)",
        )
        return EXIT_ABORTED if problems else EXIT_OK

    mailer = None
    if args.send:
        try:
            mailer = from_config(
                sender=GMAIL_SENDER_EMAIL,
                credentials_b64=GMAIL_CREDENTIALS_B64,
                demo_mode=OUTREACH_DEMO_MODE,
                demo_recipient=OUTREACH_DEMO_RECIPIENT,
            )
        except MailerError as e:
            logger.error("ABORTING: %s", e)
            return EXIT_ABORTED

    # --- Nothing queued? Stop here, cheaply. ------------------------------------
    # A cron makes IDLE runs the common case, and an idle run used to cost ~22
    # Airtable requests before discovering there was nothing to do — 14 of them
    # the blocklist, which is uncached by design and re-fetched every run.
    # Reading the queue first costs 2 and answers the question.
    #
    # Skipping the blocklist here does NOT weaken the fail-closed contract: it
    # exists to prevent SENDS, and with an empty queue there are none. It is
    # still fetched before any send below. Same for the lease — an idle run that
    # never sends has no reason to contend for it, which also stops a frequent
    # cron from competing with a real run for the base's 5 req/s.
    #
    # The stranded scan still runs. A claim left behind by a run that died must
    # be surfaced even on a day nobody queues anything, or it is invisible for
    # exactly as long as the queue stays empty.
    try:
        queued_total = sum(
            len(get_queued_prospects(cfg["table_name"]))
            for niche, cfg in NICHES.items()
            if not args.niche or niche in args.niche
        )
    except LedgerUnavailable as e:
        logger.error("ABORTING: could not read the outreach queue: %s", e)
        return EXIT_ABORTED

    if queued_total == 0:
        stranded = len(find_stranded_claims(ledger, OUTREACH_STRANDED_AFTER_MINUTES))
        logger.info(
            "Nothing queued — no prospect has `Send Requested At` set. "
            "Stopping before the blocklist fetch and the lease (saved ~20 Airtable "
            "requests). Stranded claims: %d.", stranded,
        )
        return EXIT_OK

    try:
        budget_remaining = remaining_daily_budget(ledger, today_iso(), OUTREACH_DAILY_CAP)
    except LedgerUnavailable as e:
        logger.error(
            "ABORTING: could not read today's send count: %s. Refusing to assume a "
            "full budget — that is the one direction that overspends.", e,
        )
        return EXIT_ABORTED

    try:
        blocklist = fetch_blocklist()
    except BlocklistUnavailable as e:
        logger.error(
            "ABORTING: DO NOT CONTACT unavailable: %s. Refusing to send without "
            "the suppression list — proceeding could contact someone who asked "
            "not to be.", e,
        )
        return EXIT_ABORTED

    # `is None`, not `or`: an explicit --limit 0 is falsy, so `or` silently turned
    # the safest-looking value into the default and sent ten real emails.
    per_run = OUTREACH_MAX_PER_RUN if args.limit is None else args.limit
    budget = RunBudget(remaining=min(budget_remaining, per_run))
    logger.info(
        "Campaign %s | mode %s | budget %d (daily remaining %d)",
        campaign, mode, budget.remaining, budget_remaining,
    )
    if OUTREACH_DEMO_MODE:
        logger.warning(
            "DEMO MODE: every message is redirected to %s. No creator will receive "
            "anything.", OUTREACH_DEMO_RECIPIENT or "<unset — sending is refused>",
        )

    signal.signal(signal.SIGINT, _handle_sigint)

    # LAYER 2 of the concurrency design, and the only one that can see a
    # hand-run `channel-vetting-outreach` overlapping with the scheduled CI job
    # — the GitHub Actions `concurrency: outreach` group serialises CI against CI and
    # is blind to a laptop. Advisory, not a mutex (Airtable has no conditional
    # writes), which is exactly why it is a SECOND line rather than the only
    # one. Acquired after every free refusal so a rejected run never holds it.
    lease_store = AirtableLeaseStore()
    lease_holder = f"{socket.gethostname()}:{os.getpid()}"
    lease = acquire_lease(
        lease_store, holder=lease_holder,
        stale_after_minutes=OUTREACH_LEASE_STALE_MINUTES,
    )
    if not lease.acquired:
        logger.error(
            "ABORTING: another outreach run holds the lease (%s). %s",
            lease.holder or "unknown", lease.detail,
        )
        return EXIT_ABORTED

    queue_was_empty = False
    try:
        queue_was_empty = _send_phase(
            args, ledger, mailer, blocklist, budget, summary, campaign
        )
    finally:
        # The stranded scan lives in the finally WITH the summary that reports
        # it. It was inside the try, so an exception left it at 0 and the summary
        # cheerfully printed "0 stranded" in precisely the scenario where a claim
        # is most likely stranded mid-flight.
        try:
            stranded = len(find_stranded_claims(ledger, OUTREACH_STRANDED_AFTER_MINUTES))
        except Exception:  # reporting must not mask the original failure
            stranded = 0
        # Both in a finally: an unhandled exception must not swallow the record
        # of irreversible outbound email, and must not leave the lease held
        # until the stale threshold expires.
        summary.print(campaign=campaign, mode=mode, remaining=budget.remaining, stranded=stranded)
        release_lease(lease_store, holder=lease_holder)

    if summary.sent or summary.rendered:
        return EXIT_OK
    if queue_was_empty:
        # Nobody has stamped `Send Requested At`. That is a normal answer to a
        # manual dispatch, not a fault — and this workflow is manual-only with no
        # cron, so returning non-zero here would paint a routine dry run red and
        # train people to ignore the X. pipeline.py's non-zero-on-nothing is
        # justified by being SCHEDULED, where silence means broken.
        logger.info("Queue was empty — nothing to do.")
        return EXIT_OK
    return EXIT_NOTHING_DONE


def _send_phase(args, ledger, mailer, blocklist, budget, summary, campaign) -> bool:
    """
    The per-niche, per-prospect loop. Extracted so run() can wrap it in the
    lease. Returns True when NO niche had anything queued, which run() uses to
    tell "nothing to do" from "there was work and none of it got done".
    """
    total_queued = 0
    for niche, cfg in NICHES.items():
        if args.niche and niche not in args.niche:
            continue
        if _INTERRUPTED or budget.remaining <= 0:
            break

        try:
            queued = get_queued_prospects(cfg["table_name"])
        except LedgerUnavailable as e:
            # Skip the NICHE, not the run, and never fall back to "send
            # everything" — the same contract run_niche() uses for a failed cap
            # read. A niche we cannot read is a niche we do not contact.
            logger.error("Skipping %s: could not read its queue: %s", niche, e)
            summary.skipped["queue_unreadable"] += 1
            continue

        total_queued += len(queued)
        logger.info("Niche: %s — %d queued", niche, len(queued))

        for row in queued:
            if _INTERRUPTED or budget.remaining <= 0:
                break
            fields = row.get("fields", {})
            channel_id = fields.get("Channel ID", "")
            real_email = csv_unsafe(fields.get("Email", ""))

            try:
                rendered = render_for(row, niche)
            except templates.TemplateError as e:
                # Never send on a raised template: a half-rendered or
                # unsafely-linked email is worse than a skipped prospect.
                logger.warning("Skipping %s: %s", channel_id or "?", e)
                summary.skipped["render_failed"] += 1
                continue

            if not EMAIL_PATTERN.fullmatch(real_email):
                # fullmatch, not search: `search` would store "contact us at
                # a@b.com today" verbatim. csv_unsafe above is what stops a
                # legitimate `+promo@studio.com` failing here for a leading
                # apostrophe the encoder added.
                logger.warning("Skipping %s: unusable email", channel_id or "?")
                summary.skipped["bad_email"] += 1
                continue

            if blocklist.match(
                handle=fields.get("Handle", ""),
                email=real_email,
                # csv_unsafe like the email above: a stored "'-Bob AV" would
                # never match a DNC entry for "-Bob AV", and NAME IS THE ONLY
                # KEY for 139 of 1329 blocklist rows (10.5%) that carry neither
                # a handle nor an email.
                name=csv_unsafe(fields.get("Channel Name", "")),
            ):
                # Re-checked at SEND time on all three keys, not just the one
                # vetting used: a creator blocked between vetting and outreach
                # must not be contacted, and a false negative here is the harm
                # the whole list exists to prevent.
                logger.info("Skipping %s: DO NOT CONTACT", channel_id or "?")
                summary.skipped["blocklisted"] += 1
                continue

            if not args.send:
                # Ask the real rule rather than reconstructing it: hardcoding the
                # demo address here showed it even when demo mode was OFF,
                # misrepresenting what a --send run would do on the exact
                # artefact a human reads to authorise the send.
                try:
                    to = recipient_for(
                        real_email,
                        demo_mode=OUTREACH_DEMO_MODE,
                        demo_recipient=OUTREACH_DEMO_RECIPIENT,
                    )
                except DemoModeError as e:
                    logger.error("ABORTING preview: %s", e)
                    raise
                path = write_preview(
                    campaign, channel_id, rendered,
                    to=to, sender=GMAIL_SENDER_EMAIL or "<sender unset>",
                )
                logger.info("Rendered %s -> %s", fields.get("Channel Name", "?"), path)
                summary.rendered += 1
                budget.record(real_email)
                continue

            granted = claim(
                ledger,
                channel_id=channel_id,
                campaign=campaign,
                niche=niche,
                recipient_email=real_email,
                qualification=fields.get("Qualification", ""),
                channel_name=fields.get("Channel Name", ""),
                template_version=rendered["template_version"],
                budget=budget,
                allow_retry=args.retry_failed,
                allow_recontact=args.allow_recontact,
            )
            if not granted.granted:
                logger.info("Skipping %s: %s %s", channel_id or "?", granted.reason, granted.detail)
                summary.skipped[granted.reason] += 1
                continue

            try:
                message_id = mailer.send(
                    to=real_email,
                    subject=rendered["subject"],
                    text=rendered["text"],
                    html=rendered["html"],
                )
            except MailerError as e:
                if e.provably_not_sent:
                    settle_not_sent(ledger, granted.record_id, error=str(e))
                    summary.not_sent += 1
                    logger.error("NotSent %s: %s", redact(real_email), e)
                else:
                    # UNKNOWN, not failed. Never auto-retried.
                    settle_maybe_sent(ledger, granted.record_id, error=str(e))
                    summary.maybe_sent += 1
                    logger.error("MaybeSent %s (may have been delivered): %s", redact(real_email), e)
                continue

            settle_sent(ledger, granted.record_id, provider_message_id=message_id)
            summary.sent += 1
            logger.info("Sent to %s (msg %s)", redact(real_email), message_id or "?")

            # Consequence, not the guard. If this PATCH fails the ledger still
            # says Sent, so the next run still skips the row.
            if not mark_contacted(cfg["table_name"], row.get("record_id", "")):
                summary.contacted_patch_failures += 1

            time.sleep(OUTREACH_SLEEP_SECONDS)

    return total_queued == 0



def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Send outreach email to Approved, Qualified prospects.",
        epilog=(
            "Dry run is the default. --send is required to deliver, and "
            "OUTREACH_DEMO_MODE still redirects every message until it is "
            "deliberately turned off."
        ),
    )
    # Mutually exclusive so `--dry-run --send` is rejected by argparse with a
    # clear message, rather than leaving the precedence to whoever reads the
    # code last.
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="Render and report without sending (the default).")
    mode.add_argument("--send", action="store_true",
                      help="Actually deliver. Requires credentials and the legal footer.")
    p.add_argument("--limit", type=int, default=None,
                   help=f"Max sends this run (default OUTREACH_MAX_PER_RUN={OUTREACH_MAX_PER_RUN}). "
                        "Bounded by the per-prospect-day cap regardless.")
    p.add_argument("--niche", action="append", default=None,
                   help="Limit to one niche; repeatable. Default: all.")
    p.add_argument("--campaign", default=None,
                   help="Campaign LABEL. Not the duplicate guard — that is keyed on channel.")
    p.add_argument("--check-setup", action="store_true",
                   help="Validate configuration and exit without touching the ledger.")
    p.add_argument("--reconcile", action="store_true",
                   help="List claims that may have been delivered but were never settled.")
    p.add_argument("--settle", metavar="KEY", default=None,
                   help="Resolve one stranded claim by idempotency key. Use with --state.")
    p.add_argument("--state", choices=["sent", "notsent"], default=None,
                   help="The outcome to record for --settle, after checking the sent-mail folder.")
    p.add_argument("--retry-failed", action="store_true",
                   help="Re-attempt NotSent rows. Never touches MaybeSent.")
    p.add_argument("--allow-recontact", action="store_true",
                   help="Bypass the ever-sent guard. You own the consequences.")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        logger.warning("Interrupted. Run --reconcile to check for stranded claims.")
        return EXIT_ABORTED


if __name__ == "__main__":
    sys.exit(main())
