"""
Hunter.io Domain Search integration: a last-resort email-finder fallback.

Only called when our own free extraction (a repeated email across a
channel's videos, or a single mention in its About description) finds
nothing — Hunter is a metered third-party service, so this is meant to
fill genuine coverage gaps, not to be the primary source.
"""
import logging

import requests

from config import HUNTER_API_KEY

logger = logging.getLogger(__name__)

HUNTER_API_BASE_URL = "https://api.hunter.io/v2"


def find_domain_email(domain: str) -> str:
    """
    Query Hunter's Domain Search endpoint for a known email at `domain`.
    Returns the highest-confidence email found, or "" if none/on error/if
    HUNTER_API_KEY isn't configured. Costs one Hunter search credit per
    call — only call this once our free extraction methods have already
    come up empty for a channel.
    """
    if not HUNTER_API_KEY or not domain:
        return ""

    params = {"domain": domain, "api_key": HUNTER_API_KEY, "limit": 5}
    try:
        resp = requests.get(f"{HUNTER_API_BASE_URL}/domain-search", params=params, timeout=30)
    except requests.RequestException as e:
        logger.warning("Hunter.io request failed for domain '%s': %s", domain, e)
        return ""

    if resp.status_code != 200:
        logger.warning("Hunter.io domain-search failed for '%s': %s %s", domain, resp.status_code, resp.text)
        return ""

    emails = resp.json().get("data", {}).get("emails", [])
    if not emails:
        return ""

    # Hunter already returns results ordered by its own confidence score.
    return emails[0].get("value", "")
