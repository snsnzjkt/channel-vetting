"""
Modash integration: creator-database email lookup keyed on the YouTube
channel ID.

Why this exists alongside hunter_client.py — they fail in opposite ways:

  Hunter is a B2B tool keyed on a *company domain*, so it can only reach
  creators who run a business website, and that domain has to be guessed
  out of description text (the source of the sponsor false-positives
  documented in enrichment.DOMAIN_SEARCH_BLOCKLIST). A creator whose
  contact address is a plain gmail one is structurally unreachable.

  Modash is keyed on the channel ID we already have, and returns whatever
  contact the creator published — gmail included. No guessing, no domain
  required.

Metered: 1 credit per successful profile report. Nothing here is called
automatically by the pipeline; modash_backfill.py drives it explicitly so
credit spend is always a deliberate act.
"""
import logging

import requests

from config import MODASH_API_KEY, MODASH_API_BASE_URL

logger = logging.getLogger(__name__)

# Distinguishes "Modash has no email for this creator" from "Modash has
# never heard of this channel" — a miss for the first reason means the
# data genuinely isn't there, while a run full of the second means the
# identifier format is wrong and the batch should be stopped, not
# continued. Callers branch on these rather than on a bare "".
FOUND = "found"
NO_EMAIL_ON_FILE = "no_email_on_file"
NOT_IN_DATABASE = "not_in_database"
ERROR = "error"


def _headers() -> dict:
    return {"Authorization": f"Bearer {MODASH_API_KEY}"}


def get_account_info() -> dict | None:
    """
    Fetch the account's remaining credit balance and rate limits.

    Free (does not consume a credit), so callers should always check this
    before starting a batch rather than discovering an empty balance
    partway through.

    Returns a dict with 'credits', 'raw_requests' and 'rate_limit', or
    None on error / if MODASH_API_KEY isn't configured.
    """
    if not MODASH_API_KEY:
        logger.error("MODASH_API_KEY is not set — cannot query Modash.")
        return None

    try:
        resp = requests.get(f"{MODASH_API_BASE_URL}/user/info", headers=_headers(), timeout=30)
    except requests.RequestException as e:
        logger.error("Modash /user/info request failed: %s", e)
        return None

    if resp.status_code != 200:
        logger.error("Modash /user/info failed: %s %s", resp.status_code, resp.text[:300])
        return None

    body = resp.json()
    # The payload has been seen both bare and wrapped in a "result"
    # envelope depending on endpoint, so tolerate either shape.
    data = body.get("result", body)
    billing = data.get("billing", {}) or {}
    limits = data.get("rateLimits", {}) or {}
    return {
        "credits": billing.get("credits"),
        "raw_requests": billing.get("rawRequests"),
        "rate_limit": limits.get("discoveryRatelimit"),
        "raw": data,
    }


def find_channel_email(channel_id: str) -> tuple[str, str]:
    """
    Look up a YouTube channel's published contact email.

    `channel_id` is a UC... channel ID or an @handle — Modash accepts
    either as the profile identifier.

    Costs 1 credit per successful report. Returns (status, email) where
    status is one of the module constants above; email is "" unless
    status is FOUND.
    """
    if not MODASH_API_KEY or not channel_id:
        return ERROR, ""

    url = f"{MODASH_API_BASE_URL}/youtube/profile/{channel_id}/report"
    try:
        resp = requests.get(url, headers=_headers(), timeout=45)
    except requests.RequestException as e:
        logger.warning("Modash report request failed for %s: %s", channel_id, e)
        return ERROR, ""

    if resp.status_code == 404:
        return NOT_IN_DATABASE, ""
    if resp.status_code != 200:
        logger.warning("Modash report failed for %s: %s %s", channel_id, resp.status_code, resp.text[:300])
        return ERROR, ""

    body = resp.json()
    # Modash signals some failures with HTTP 200 + error:true in the body,
    # so a 200 alone isn't proof of success.
    if body.get("error"):
        code = body.get("code", "")
        if code in ("not_found", "influencer_not_found"):
            return NOT_IN_DATABASE, ""
        logger.warning("Modash report error for %s: %s", channel_id, body.get("message", code))
        return ERROR, ""

    profile = (body.get("profile") or {}).get("profile") or body.get("profile") or {}
    contacts = profile.get("contacts") or []
    for contact in contacts:
        if (contact.get("type") or "").lower() == "email":
            value = (contact.get("value") or "").strip()
            if value:
                return FOUND, value

    return NO_EMAIL_ON_FILE, ""
