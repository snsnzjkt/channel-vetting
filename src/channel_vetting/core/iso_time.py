"""
The ONE tolerant ISO 8601 parse in this codebase.

Imports NOTHING from this project (same rule as `core/text_safety.py`) so any
module can use it without a dependency cycle — `outreach/ledger.py` is
deliberately storage-agnostic and testable without Airtable, so it must not
have to import `enrichment/channels.py` (and its `http_client`/`config` chain) just to
read a timestamp.

**Why one home.** Two modules independently implemented this rule with a
strict `datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")`, and both were wrong
in the same way:

1. `enrichment.calc_upload_frequency` raised `ValueError` on any
   `videoPublishedAt` carrying fractional seconds or a `+00:00` offset.
   Nothing between it and `run()` catches that, so one odd timestamp ended a
   whole pipeline run with the quota already spent.
2. `outreach.ledger._parse_utc` returned None for the same shapes — and its
   docstring claimed it parsed "one of our own stamps", which was the bug in
   one sentence. The ledger WRITES `%Y-%m-%dT%H:%M:%SZ`, but the value makes a
   round trip through an Airtable dateTime field and comes back as
   `...T12:00:00.000Z`. It could not read what it had just written.

That failure was silent and permanent: `followup_eligibility()` refused every
follow-up with "prior send has no readable timestamp", so
`OUTREACH_RESPAM_MIN_DAYS` was unreachable, and `_lease_is_stale()` returned
False for any age, so a stranded outreach lease never aged out and had to be
cleared by hand. Both fail CLOSED, which is why nothing alarmed.

**The rule.** Missing or unreadable input is UNKNOWN, never a verdict. This
function returns None and lets each caller apply its own policy — an unknown
channel age does not disqualify a candidate, while an unknown lease age is
treated as live. The parse does not need two implementations to support two
policies; that conflation is what let the two copies drift.

Callers must not re-implement this. `tests/test_iso_time.py` pins the shapes,
and `tests/test_upload_cadence.py` pins that `calc_upload_frequency` still
routes through it.
"""
from datetime import datetime, timezone


def parse_iso_utc(value: str | None) -> datetime | None:
    """
    An ISO 8601 timestamp as a tz-AWARE UTC datetime, or None when it is
    missing or unreadable.

    Accepts everything the two producers in play actually emit: YouTube's
    trailing-`Z` form, Airtable's `.000Z` millisecond form, an explicit
    `+00:00` offset, and a bare `YYYY-MM-DD` date.

    Always returns an aware datetime. A bare date (or any offsetless value)
    parses tz-NAIVE, and it is coerced to UTC here so no caller can hit an
    aware/naive TypeError subtracting it from `datetime.now(timezone.utc)` —
    that coercion belongs with the parse, not repeated at each call site.
    """
    if not value:
        return None
    try:
        # fromisoformat handles fractional seconds and explicit offsets, which
        # a strptime format string does not. The replace() is only for the
        # trailing "Z", which it did not accept before Python 3.11 and which
        # costs nothing to keep supporting explicitly.
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
