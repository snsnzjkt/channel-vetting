"""
Gmail transport, and the last place a real creator's address can be swapped for
the demo mailbox.

The demo redirect lives HERE, not in the caller
-----------------------------------------------
`resolve_recipient()` used to be something the caller invoked before calling
the mailer. That made the safe path a convention: nothing structurally stopped
a future `outreach.py` from doing `mailer.send(to=row["Email"])` and skipping
the gate entirely. The reviewed fix is that `send()` takes the PROSPECT'S REAL
address and applies the redirect itself, so a caller that forgets the gate
cannot be written. Demo state is bound at construction, not passed per call,
for the same reason: a per-call flag is a per-call opportunity to omit it.

Credentials are REQUIRED, not soft-disabled
--------------------------------------------
`null_scraper()` and the inert `InfluencersClient` are the right contract for
OPTIONAL enrichment steps — a miss costs some email coverage. Applied to a
mailer it would be actively harmful: the run would claim N ledger rows, fail N
sends, and leave a batch to reconcile having delivered nothing. `from_config()`
raises unless it can actually send, and `--dry-run` never constructs one.

Transport
---------
`google-auth` mints the bearer token; the request goes through
`http_client.GMAIL`, a requests Session, so the autouse guard in
tests/conftest.py covers it. See that session's comment for why its retry
policy is the strictest in the codebase.
"""
import base64
import json
import logging
from email.message import EmailMessage

import requests

from http_client import GMAIL as HTTP, safe_body

logger = logging.getLogger(__name__)

GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
GMAIL_SCOPES = ("https://www.googleapis.com/auth/gmail.send",)


class MailerError(RuntimeError):
    """Transport failure. `provably_not_sent` decides whether a retry is safe."""

    def __init__(self, message: str, *, provably_not_sent: bool):
        super().__init__(message)
        # The single most consequential bit in this module. True ONLY when the
        # message demonstrably never left: a connect error, a 4xx rejection, a
        # pre-flight failure. A 5xx or a read timeout is UNKNOWN — the message
        # may have been accepted and the response lost — and calling that
        # "failed" is what turns --retry-failed into a duplicate-send path.
        self.provably_not_sent = provably_not_sent


class DemoModeError(RuntimeError):
    """
    Raised when a send would reach a real creator while the project is still in
    demo mode. An exception, not a skip: a misconfigured demo run must stop the
    whole run loudly rather than quietly email one person and carry on.
    """


def _header_safe(value: str) -> str:
    """Strip the characters that could start a new header line. See build_message."""
    return (value or "").replace("\r", "").replace("\n", "").replace("\x00", "")


def build_message(*, sender: str, to: str, subject: str, text: str, html: str) -> EmailMessage:
    """
    Assemble a multipart/alternative message.

    `EmailMessage` rather than string formatting because it owns header
    encoding. `outreach_templates` already strips CR/LF/NUL from every
    substituted value, but that is the render boundary; this is the assembly
    boundary, and header injection is worth stopping at both — a value here
    could come from config, or from a future caller that never went through a
    template.

    Headers are stripped rather than allowed to raise. Python's email policy
    raises `ValueError` on a linefeed in a header, which is a *safe* refusal but
    the wrong shape for this pipeline: it would escape `send()` as something the
    caller does not catch and kill the whole run over one bad prospect. This
    repo's convention is that one bad record is skipped, not fatal — see
    `airtable_client`'s log-and-continue and `enrichment`'s return-None. So the
    value is neutralised here and the run carries on.
    """
    msg = EmailMessage()
    msg["From"] = _header_safe(sender)
    msg["To"] = _header_safe(to)
    msg["Subject"] = _header_safe(subject)
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


def recipient_for(real_email: str, *, demo_mode: bool, demo_recipient: str) -> str:
    """
    The address a message is ACTUALLY delivered to.

    Module-level so the DRY-RUN preview can ask the same question. No `Mailer`
    exists in dry-run mode (deliberately — `from_config()` raises without
    credentials), and the preview writer was reconstructing the decision by
    hand as `OUTREACH_DEMO_RECIPIENT or "<unset>"`. That showed the demo
    address even when demo mode was OFF, misrepresenting what a `--send` run
    would do — on the exact artefact a human reads to authorise the send.

    A missing redirect target RAISES rather than falling back to the real
    address. That fallback is the obvious convenience and it is exactly the bug
    the gate exists to prevent, so it is unrepresentable rather than merely
    discouraged.
    """
    if not demo_mode:
        return real_email
    if not demo_recipient:
        raise DemoModeError(
            "OUTREACH_DEMO_MODE is on but OUTREACH_DEMO_RECIPIENT is unset. "
            "Refusing to send: with no redirect target the only way to proceed "
            "would be to email the real prospect. Set OUTREACH_DEMO_RECIPIENT "
            "to a mailbox you own, or turn demo mode off deliberately once the "
            "OUTREACH_PLAN.md prerequisites are signed off."
        )
    return demo_recipient


class Mailer:
    """Sends mail. Owns the demo redirect; see the module docstring."""

    def __init__(self, *, sender: str, credentials, demo_mode: bool, demo_recipient: str):
        self.sender = sender
        self._credentials = credentials
        self.demo_mode = demo_mode
        self.demo_recipient = demo_recipient

    def resolve_recipient(self, real_email: str) -> str:
        """Delegates to `recipient_for` so there is ONE implementation of the rule."""
        return recipient_for(
            real_email, demo_mode=self.demo_mode, demo_recipient=self.demo_recipient
        )

    def send(self, *, to: str, subject: str, text: str, html: str) -> str:
        """
        Send one message. `to` is the PROSPECT'S REAL address — the redirect is
        applied here, so no caller can bypass it.

        Returns the provider message id. Raises `MailerError` carrying
        `provably_not_sent`, which is what the ledger uses to decide between
        `NotSent` (retryable) and `MaybeSent` (never auto-retried).
        """
        envelope_to = self.resolve_recipient(to)
        if self.demo_mode:
            logger.info("DEMO MODE: redirecting mail for %s to %s", to, envelope_to)

        msg = build_message(
            sender=self.sender, to=envelope_to, subject=subject, text=text, html=html
        )
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

        try:
            token = self._token()
        except Exception as e:  # credential refresh, not a send
            raise MailerError(f"Gmail credential refresh failed: {e}", provably_not_sent=True) from e

        try:
            resp = HTTP.post(
                GMAIL_SEND_URL,
                headers={"Authorization": f"Bearer {token}"},
                json={"raw": raw},
                timeout=30,
            )
        except requests.ConnectTimeout as e:
            # The ONLY transport failure that provably predates the send. Same
            # reasoning influencers.py applies to money, applied to something
            # that cannot be refunded at all.
            raise MailerError(f"Gmail connect timeout: {e}", provably_not_sent=True) from e
        except requests.RequestException as e:
            # Includes ReadTimeout and a connection dropped mid-response: the
            # request WAS sent and the answer was lost, so delivery is unknown.
            raise MailerError(f"Gmail request failed: {e}", provably_not_sent=False) from e

        if resp.status_code in (200, 201):
            try:
                return resp.json().get("id", "")
            except ValueError:
                # Gmail accepted it; we just cannot read the receipt. Delivered
                # without a message id is still delivered.
                logger.warning("Gmail returned unparseable JSON on a 2xx; treating as sent.")
                return ""

        if 400 <= resp.status_code < 500:
            # The message was rejected unprocessed — malformed address, bad
            # scope, quota denial. Nothing was delivered.
            raise MailerError(
                f"Gmail rejected the message: {resp.status_code} {safe_body(resp)}",
                provably_not_sent=True,
            )
        raise MailerError(
            f"Gmail server error: {resp.status_code} {safe_body(resp)}",
            provably_not_sent=False,
        )

    def _token(self) -> str:
        creds = self._credentials
        if not getattr(creds, "valid", False):
            from google.auth.transport.requests import Request

            creds.refresh(Request())
        return creds.token


def from_config(*, sender: str, credentials_b64: str, demo_mode: bool, demo_recipient: str) -> Mailer:
    """
    Build a Mailer, or RAISE. There is no inert variant — see the module
    docstring for why soft-disable is the wrong contract for a mailer.

    Credentials arrive base64-encoded rather than as a path because CI cannot
    supply a file, and a service-account private key should not be written to a
    runner's filesystem where any later step can read it.
    """
    if not sender:
        raise MailerError("GMAIL_SENDER_EMAIL is not set; --send cannot start.",
                          provably_not_sent=True)
    if not credentials_b64:
        raise MailerError("GMAIL_CREDENTIALS_B64 is not set; --send cannot start.",
                          provably_not_sent=True)
    try:
        from google.oauth2 import service_account

        info = json.loads(base64.b64decode(credentials_b64))
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=list(GMAIL_SCOPES)
        ).with_subject(sender)
    except Exception as e:
        raise MailerError(f"Gmail credentials could not be loaded: {e}",
                          provably_not_sent=True) from e

    return Mailer(
        sender=sender, credentials=creds,
        demo_mode=demo_mode, demo_recipient=demo_recipient,
    )
