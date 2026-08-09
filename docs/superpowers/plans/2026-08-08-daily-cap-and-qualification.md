# Daily Cap, Qualification Gate, and DO NOT CONTACT Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cap the vetting pipeline at 40 qualified prospects per niche per day, flag below-criteria channels for separate review, enforce the DO NOT CONTACT blocklist, and drop Hunter/Modash in favour of the free scraper chain plus CloakBrowser.

**Architecture:** All changes converge on the per-candidate path in `main.py`. New leaf modules (`do_not_contact.py`, `browser_email.py`) are pure additions with no dependencies on each other; `scoring.qualify()` and `airtable_client.count_added_today()` extend existing modules in place. `main.py` is rewired last, once every part it consumes exists.

**Tech Stack:** Python 3.12, `requests`, `pandas`, `python-dotenv`, `cloakbrowser`, YouTube Data API v3, Airtable REST API, `pytest` (added by Task 1).

## Global Constraints

- Never hardcode secrets; everything loads from `.env` via `config.py`.
- Every API call site carries a comment noting its quota cost.
- Enrichment functions return `None` and log for inaccessible channels — never raise.
- `airtable_client.py` logs and returns falsy on API errors rather than raising. **Task 3 introduces one deliberate exception** — see that task.
- Scoring weights/thresholds are named constants at the top of `scoring.py`.
- `airtable_client.py` functions take `table_name` as an explicit first argument. No global "the table".
- "Upload Frequency" is a **text** field in Airtable — send it as a formatted string.
- **`EMAIL_DOMAIN_BLOCKLIST` (= `THIRD_PARTY_DOMAINS`) must never contain freemail domains.** Merging them was a real bug that discarded every `@gmail.com` match (53% of collected addresses).
- Do **not** run `git commit` in this repo. Each task's commit step prints the message for the user to run themselves.
- Quota bucketing in `quota_tracker.py` stays Pacific Time. Do not change it.

## Resolved deviations from the spec

The spec was written before the CI workflow was examined. All three are now decided.

1. **Prospect-day timezone → `America/Toronto`.** The spec says `Date Added` uses `date.today()` (local system time). But `.github/workflows/channel-vetting.yml` runs on GitHub Actions at 09:00 UTC, where "local" is **UTC**, while the dev machine is **UTC+8** and head office is **Toronto**. Three different answers to "what day is it". Pinned to `America/Toronto` because the cap models the reviewing team's working day, not the machine's.

2. **CloakBrowser in CI ships off by default.** The scheduled workflow gains full CloakBrowser support (Task 12) but `USE_CLOAKBROWSER` defaults to `false`. GitHub-hosted runners use Azure datacenter IP ranges that YouTube challenges aggressively, and CloakBrowser patches *fingerprints*, not IP reputation. It gets proven via manual `workflow_dispatch` before the cron run depends on it.

3. **Blocklist reads by field ID, not field name.** `external_dedupe.py` requests fields by name. The DO NOT CONTACT table is manually maintained, so a rename would silently empty the blocklist — the exact failure this work exists to prevent. Task 6 uses `returnFieldsByFieldId=true` with verified IDs instead.

## CloakBrowser: two integration paths, deliberately separate

`cloakbrowser-mcp` is an **npm** package (v1.10.0) that bridges CloakBrowser to the Playwright MCP protocol. MCP is a protocol for *LLM agents* to call tools — the server needs an MCP client, which the `python main.py` cron job does not have and should not grow. Driving it from CI would mean installing Node, spawning the server, and writing a JSON-RPC client in Python to reach a browser the Python package already drives in-process.

| Path | Mechanism | Task |
|---|---|---|
| Pipeline / CI | Python `cloakbrowser` `launch()` directly | 7, 12 |
| Claude Code and other agents | `cloakbrowser-mcp` via `.mcp.json` | 11 |

Both are implemented. Neither depends on the other.

**License key is optional throughout.** `cloakbrowser/license.py` reads `CLOAKBROWSER_LICENSE_KEY` to unlock a Pro binary; unset, it runs in no-key mode. Every place the key appears is optional, so the pipeline runs identically with or without one.

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `requirements.txt`, `pytest.ini`, `tests/` | Test scaffolding | 1 |
| `hunter_client.py`, `modash_client.py`, `modash_backfill.py` | **Deleted** | 2 |
| `config.py` | Caps, timezone, toggles; Hunter/Modash keys removed | 2, 3 |
| `prospect_day.py` | Single source of truth for "what day is it" | 3 |
| `airtable_client.py` | `count_added_today()` | 3 |
| `enrichment.py` | `published_at`; Hunter-only helpers removed | 2, 4 |
| `scoring.py` | `qualify()` and its thresholds | 5 |
| `do_not_contact.py` | Blocklist index and matching | 6 |
| `browser_email.py` | CloakBrowser session + About-page scrape | 7 |
| `main.py` | `NICHES` criteria, capped `run_niche`, rewired `resolve_email` | 2, 5, 8 |
| `discovery.py` | Early-stop params | 8 |
| `audit_blocklist.py` | One-off audit of existing rows | 9 |
| `.mcp.json` | CloakBrowser for MCP clients (agents only) | 11 |
| `.github/workflows/channel-vetting.yml` | Secrets, toggles, Chromium deps and cache | 2, 8, 12 |
| `.env.example` | Documented optional variables | 2, 12 |
| `CLAUDE.md`, `README.md` | Docs | 2, 10, 11 |

---

### Task 1: Test scaffolding

No tests exist and `pytest` is absent from `requirements.txt` (it happens to be installed globally as 8.3.3, but CI installs only from the file). Every later task is TDD, so this comes first.

Note `cloakbrowser_test.py` matches pytest's default `*_test.py` collection glob. It is a CLI smoke script, not a test — importing it is harmless but collecting it is confusing. `pytest.ini` restricts collection to `tests/`.

**Files:**
- Modify: `requirements.txt`
- Create: `pytest.ini`
- Create: `tests/__init__.py` (empty)
- Create: `tests/test_scaffolding.py`

**Interfaces:**
- Consumes: nothing
- Produces: a working `pytest` command that later tasks extend

- [ ] **Step 1: Add pytest to requirements**

Append to `requirements.txt`:

```
pytest==8.3.3
```

- [ ] **Step 2: Create `pytest.ini`**

```ini
[pytest]
testpaths = tests
python_files = test_*.py
addopts = -v
```

- [ ] **Step 3: Create `tests/__init__.py`**

Empty file.

- [ ] **Step 4: Write a scaffolding test**

`tests/test_scaffolding.py`:

```python
"""Confirms the test harness collects and that project modules import."""


def test_harness_runs():
    assert True


def test_config_imports():
    import config

    assert hasattr(config, "QUOTA_CEILING")
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest`
Expected: 2 passed. If `test_config_imports` fails on a missing `.env`, that is a real problem — `config.py` must import cleanly without one.

- [ ] **Step 6: Commit**

```bash
git add requirements.txt pytest.ini tests/
```
Message for the user to commit:
```
test: add pytest scaffolding

No test harness existed. Restricts collection to tests/ so the
cloakbrowser_test.py CLI script isn't picked up by the *_test.py glob.
```

---

### Task 2: Remove Hunter and Modash

Done early: it deletes code that later tasks would otherwise have to reason around, and shrinks `resolve_email()` before Task 7 modifies it.

**Files:**
- Delete: `hunter_client.py`, `modash_client.py`, `modash_backfill.py`
- Modify: `config.py`, `main.py:24-43,95-122`, `enrichment.py:105-140`, `backfill_missing_emails.py`, `.env`, `.env.example`, `.github/workflows/channel-vetting.yml`

**Interfaces:**
- Consumes: nothing
- Produces: `resolve_email(stats: dict, performance: dict) -> str` — the `use_hunter` parameter is gone. Task 7 adds a `browser` parameter.

- [ ] **Step 1: Write the failing test**

`tests/test_no_paid_lookups.py`:

```python
"""The paid email-lookup integrations are gone and stay gone."""
import importlib

import pytest


@pytest.mark.parametrize("module_name", ["hunter_client", "modash_client", "modash_backfill"])
def test_paid_modules_are_removed(module_name):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module_name)


def test_config_has_no_paid_keys():
    import config

    for attr in ("HUNTER_API_KEY", "MODASH_API_KEY", "MODASH_API_BASE_URL"):
        assert not hasattr(config, attr), f"{attr} should be removed from config"


def test_resolve_email_has_no_hunter_param():
    import inspect

    import main

    assert "use_hunter" not in inspect.signature(main.resolve_email).parameters


def test_email_blocklist_still_excludes_freemail():
    """Removing Hunter deletes DOMAIN_SEARCH_BLOCKLIST, never EMAIL_DOMAIN_BLOCKLIST."""
    import enrichment

    assert "gmail.com" in enrichment.FREEMAIL_DOMAINS
    assert "gmail.com" not in enrichment.EMAIL_DOMAIN_BLOCKLIST
    assert not enrichment.is_blocklisted_email_domain("gmail.com")
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `python -m pytest tests/test_no_paid_lookups.py`
Expected: FAIL — the modules still import and `use_hunter` is still a parameter.

- [ ] **Step 3: Delete the three modules**

```bash
git rm hunter_client.py modash_client.py modash_backfill.py
```

- [ ] **Step 4: Clean `config.py`**

Delete the entire `# --- Hunter.io (optional) ---` block (lines 27-31) and the `# --- Modash (optional) ---` block (lines 33-40).

- [ ] **Step 5: Clean `main.py`**

Remove `extract_candidate_domain` from the `enrichment` import list (line 29) and delete the `from hunter_client import find_domain_email` line (line 31).

Replace `resolve_email()` (lines 95-122) with:

```python
def resolve_email(stats: dict, performance: dict) -> str:
    """
    Email fallback chain: an address repeated across several of the
    channel's recent video descriptions (strongest signal), then a single
    mention in the channel's own About description.

    Both steps are free and use data already fetched during enrichment.
    """
    return performance.get("repeated_email") or stats.get("business_email", "")
```

- [ ] **Step 6: Clean `enrichment.py`**

Delete `extract_candidate_domain()` (lines 125-140) and the `DOMAIN_SEARCH_BLOCKLIST` assignment (lines 109-110), plus their now-orphaned comments.

**Keep `THIRD_PARTY_DOMAINS`, `FREEMAIL_DOMAINS`, `EMAIL_DOMAIN_BLOCKLIST`, and `is_blocklisted_email_domain()` exactly as they are.** `EMAIL_DOMAIN_BLOCKLIST` screens scraped addresses and is still live; `FREEMAIL_DOMAINS` is still used by `backfill_missing_emails.py` reporting. Update the comments on lines 88 and 95-97 so they no longer reference Hunter.

- [ ] **Step 7: Clean `backfill_missing_emails.py`**

Delete the `--with-hunter` argument block (lines 163-168) and the `use_hunter = args.with_hunter` line. Change the `resolve_email` call on line 125 from `resolve_email(stats, performance, use_hunter=False)` to `resolve_email(stats, performance)`. Delete the `"Hunter domain search"` branch (lines 137-138) so the `else` falls through to `"About description"`. Replace the two-line banner print with:

```python
    print(f"CloakBrowser fallback: {'ENABLED' if args.use_cloakbrowser else 'DISABLED'}\n")
```

Update the module docstring to drop the Hunter sentence.

- [ ] **Step 8: Clean the workflow and env files**

In `.github/workflows/channel-vetting.yml`, delete both `HUNTER_API_KEY` lines (56, 68) and their preceding `# Optional` comments (55, 67).

Remove `HUNTER_API_KEY` and `MODASH_API_KEY` from `.env` and `.env.example`.

- [ ] **Step 9: Update `CLAUDE.md`**

Replace the "Email" fallback-chain bullet (lines 108-121) with a two-step chain, and delete the two-pass backfill bullet (lines 122-140) and the Modash blocklist bullet (lines 141-144). In the two-blocklist section (145-155), delete the `DOMAIN_SEARCH_BLOCKLIST` half but **keep the freemail warning attached to `EMAIL_DOMAIN_BLOCKLIST`**. Remove `hunter_client.py`, `modash_client.py`, and `modash_backfill.py` from the project-structure tree (lines 25-27).

- [ ] **Step 10: Run the tests**

Run: `python -m pytest`
Expected: all pass.

- [ ] **Step 11: Commit**

```bash
git add -A
```
Message:
```
refactor: remove Hunter.io and Modash integrations

Sticking with the free scraper chain (repeated video email -> About
description). Deletes hunter_client, modash_client, modash_backfill and
the Hunter-only DOMAIN_SEARCH_BLOCKLIST / extract_candidate_domain.

EMAIL_DOMAIN_BLOCKLIST is deliberately kept and still excludes freemail
domains -- it screens scraped addresses and is unrelated to Hunter.
```

---

### Task 3: Prospect day and daily headroom

**Files:**
- Create: `prospect_day.py`
- Modify: `config.py`, `airtable_client.py`, `main.py:191`
- Create: `tests/test_prospect_day.py`, `tests/test_count_added_today.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `prospect_day.today_iso() -> str` — `"YYYY-MM-DD"` in `PROSPECT_DAY_TZ`
  - `airtable_client.count_added_today(table_name: str, qualification: str | None = None) -> int` — **raises `AirtableReadError`** on failure
  - `airtable_client.AirtableReadError` (subclass of `RuntimeError`)
  - `config.DAILY_QUALIFIED_CAP`, `config.DAILY_FLAGGED_CAP`, `config.CANDIDATE_OVERSHOOT`, `config.PROSPECT_DAY_TZ`

- [ ] **Step 1: Write the failing tests**

`tests/test_prospect_day.py`:

```python
"""The prospect day must be timezone-pinned, not host-local."""
import re


def test_today_iso_format():
    from prospect_day import today_iso

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", today_iso())


def test_uses_configured_zone_not_host_local(monkeypatch):
    """A UTC CI runner, a UTC+8 laptop, and Toronto must agree on the day."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    import prospect_day

    # 2026-08-08 02:00 UTC is still 2026-08-07 (22:00) in Toronto, and is
    # already 2026-08-08 (10:00) on the UTC+8 dev machine. Only the
    # configured zone may decide.
    fixed = datetime(2026, 8, 8, 2, 0, tzinfo=ZoneInfo("UTC"))

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz)

    monkeypatch.setattr(prospect_day, "datetime", FrozenDatetime)
    monkeypatch.setattr(prospect_day, "PROSPECT_DAY_TZ", "America/Toronto")
    assert prospect_day.today_iso() == "2026-08-07"
```

`tests/test_count_added_today.py`:

```python
"""count_added_today must raise on read failure, never return 0."""
import pytest


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = "error body"

    def json(self):
        return self._payload


def test_counts_records_across_pages(monkeypatch):
    import airtable_client

    pages = [
        _Resp(200, {"records": [{"id": "r1"}, {"id": "r2"}], "offset": "next"}),
        _Resp(200, {"records": [{"id": "r3"}]}),
    ]
    monkeypatch.setattr(airtable_client.requests, "get", lambda *a, **k: pages.pop(0))
    monkeypatch.setattr(airtable_client.time, "sleep", lambda s: None)

    assert airtable_client.count_added_today("tblFake") == 3


def test_raises_on_non_200(monkeypatch):
    import airtable_client

    monkeypatch.setattr(airtable_client.requests, "get", lambda *a, **k: _Resp(500))

    with pytest.raises(airtable_client.AirtableReadError):
        airtable_client.count_added_today("tblFake")


def test_raises_on_request_exception(monkeypatch):
    import airtable_client

    def boom(*a, **k):
        raise airtable_client.requests.RequestException("network down")

    monkeypatch.setattr(airtable_client.requests, "get", boom)

    with pytest.raises(airtable_client.AirtableReadError):
        airtable_client.count_added_today("tblFake")


def test_qualification_filter_is_included(monkeypatch):
    import airtable_client

    captured = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured["params"] = params
        return _Resp(200, {"records": []})

    monkeypatch.setattr(airtable_client.requests, "get", fake_get)
    airtable_client.count_added_today("tblFake", qualification="Qualified")

    assert "Qualified" in captured["params"]["filterByFormula"]
```

- [ ] **Step 2: Run them to confirm they fail**

Run: `python -m pytest tests/test_prospect_day.py tests/test_count_added_today.py`
Expected: FAIL — `ModuleNotFoundError: prospect_day`, and `count_added_today` does not exist.

- [ ] **Step 3: Add config constants**

Append to `config.py`:

```python
# --- Daily prospect caps ---
# "Prospect" = a record successfully pushed to Airtable. Counted per niche
# per day from Airtable's own "Date Added" field, so a second run on the
# same day tops up to the cap rather than doubling it.
DAILY_QUALIFIED_CAP = int(os.getenv("DAILY_QUALIFIED_CAP", 40))
DAILY_FLAGGED_CAP = int(os.getenv("DAILY_FLAGGED_CAP", 20))

# Discovery banks this multiple of the remaining headroom in fresh
# candidates, covering the ones lost to enrichment failure and dedupe.
CANDIDATE_OVERSHOOT = float(os.getenv("CANDIDATE_OVERSHOOT", 1.5))

# The zone that defines a "prospect day". Deliberately NOT the Pacific
# zone quota_tracker uses: quota tracks Google's reset schedule, this
# tracks review capacity on the reviewing team's working day.
#
# Pinned rather than host-local because three clocks are in play — the
# GitHub Actions runner (UTC), the dev machine (UTC+8), and head office
# (Toronto). Unpinned, a CI run and a local run would disagree about the
# date and each claim a separate daily cap.
PROSPECT_DAY_TZ = os.getenv("PROSPECT_DAY_TZ", "America/Toronto")
```

- [ ] **Step 4: Create `prospect_day.py`**

```python
"""
Single source of truth for "what day is it" where prospect counting is
concerned.

Both the "Date Added" value written onto a record and the daily-cap query
that counts those records must use this, or they drift apart and the cap
silently misreads its own budget.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from config import PROSPECT_DAY_TZ


def today_iso() -> str:
    """Today's date as YYYY-MM-DD in the configured prospect-day zone."""
    return datetime.now(ZoneInfo(PROSPECT_DAY_TZ)).strftime("%Y-%m-%d")
```

- [ ] **Step 5: Add `AirtableReadError` and `count_added_today` to `airtable_client.py`**

Add the import `from prospect_day import today_iso` and, near the top after the logger:

```python
class AirtableReadError(RuntimeError):
    """
    Raised when a read that a safety decision depends on cannot be
    completed.

    Deliberately breaks this module's usual log-and-return-falsy
    convention. That convention exists so one bad record can't kill a
    run; it is wrong for count_added_today(), where a silent empty result
    reads as "nothing added today" and hands out a full daily budget —
    failing open in the one direction that overspends.
    """
```

Then append:

```python
def count_added_today(table_name: str, qualification: str | None = None) -> int:
    """
    Count records in `table_name` whose "Date Added" is today, optionally
    narrowed to a single "Qualification" value.

    Filters server-side, so this returns at most the day's own records
    (~60) rather than paginating the whole table. Costs no YouTube quota.

    Raises AirtableReadError if the read cannot be completed — callers
    must skip the niche rather than assume a full budget.
    """
    conditions = [f"DATESTR({{Date Added}}) = '{today_iso()}'"]
    if qualification:
        conditions.append(f"{{Qualification}} = '{qualification}'")
    formula = f"AND({', '.join(conditions)})" if len(conditions) > 1 else conditions[0]

    count = 0
    offset = None
    while True:
        params = {"fields[]": "Channel ID", "filterByFormula": formula, "pageSize": 100}
        if offset:
            params["offset"] = offset

        try:
            resp = requests.get(_base_url(table_name), headers=_headers(), params=params, timeout=30)
        except requests.RequestException as e:
            raise AirtableReadError(f"count_added_today({table_name}) request failed: {e}") from e

        if resp.status_code != 200:
            raise AirtableReadError(
                f"count_added_today({table_name}) failed: {resp.status_code} {resp.text}"
            )

        data = resp.json()
        count += len(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            break
        time.sleep(API_SLEEP_SECONDS)

    return count
```

- [ ] **Step 6: Point `Date Added` at the same helper**

In `main.py`, add `from prospect_day import today_iso` and change line 191 from
`"Date Added": date.today().isoformat(),` to `"Date Added": today_iso(),`.
Remove the now-unused `from datetime import date` import if nothing else uses it.

- [ ] **Step 7: Run the tests**

Run: `python -m pytest`
Expected: all pass.

- [ ] **Step 8: Verify the formula against the live schema**

`DATESTR()` assumes `Date Added` is a Date field. Confirm before trusting it:

```bash
python -c "
from airtable_client import count_added_today
from config import AIRTABLE_TABLE_HOME_THEATER as t
print('added today:', count_added_today(t))
"
```
Expected: a number, no exception. If Airtable returns `INVALID_FILTER_BY_FORMULA`, the field is text — change the condition to `{Date Added} = 'YYYY-MM-DD'` and re-run.

- [ ] **Step 9: Commit**

```bash
git add -A
```
Message:
```
feat: add timezone-pinned prospect day and daily headroom count

count_added_today() raises rather than returning 0 on read failure --
a silent empty result would read as "nothing added today" and grant a
full daily budget.

PROSPECT_DAY_TZ is pinned because CI runs UTC while the team is UTC+8;
host-local time would let each claim a separate daily cap.
```

---

### Task 4: Channel age from `publishedAt`

**Files:**
- Modify: `enrichment.py:191-240`
- Create: `tests/test_channel_age.py`

**Interfaces:**
- Consumes: nothing
- Produces: `get_channel_stats()` gains `"published_at": str` (ISO 8601, `""` when absent), and `enrichment.channel_age_months(published_at: str) -> float | None`

- [ ] **Step 1: Write the failing test**

`tests/test_channel_age.py`:

```python
"""Channel age derives from publishedAt; absent data must not disqualify."""
import pytest


def test_age_of_known_date():
    from enrichment import channel_age_months

    age = channel_age_months("2024-08-07T00:00:00Z")
    assert age is not None and age > 12


def test_recent_channel_is_young():
    from datetime import datetime, timedelta, timezone

    from enrichment import channel_age_months

    recent = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    age = channel_age_months(recent)
    assert age is not None and age < 2


@pytest.mark.parametrize("bad", ["", None, "not-a-date"])
def test_unparseable_returns_none(bad):
    """None means 'unknown', and unknown must never be treated as new."""
    from enrichment import channel_age_months

    assert channel_age_months(bad) is None
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `python -m pytest tests/test_channel_age.py`
Expected: FAIL — `cannot import name 'channel_age_months'`.

- [ ] **Step 3: Implement**

Add to `enrichment.py` (the `datetime` import already exists in this module):

```python
# Average days per month, for turning a channel's age into the "months"
# unit the briefs are written in. Approximate on purpose — nothing here
# depends on calendar-exact month boundaries.
DAYS_PER_MONTH = 30.44


def channel_age_months(published_at: str | None) -> float | None:
    """
    Age of a channel in months, from the ISO 8601 timestamp channels.list
    returns in snippet.publishedAt.

    Returns None when the value is missing or unparseable — callers must
    treat None as "unknown" and NOT as "new", since absent data is not
    evidence against a channel.
    """
    if not published_at:
        return None
    try:
        created = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        logger.info("Unparseable publishedAt %r — treating channel age as unknown.", published_at)
        return None
    delta_days = (datetime.now(timezone.utc) - created).days
    return delta_days / DAYS_PER_MONTH
```

- [ ] **Step 4: Return it from `get_channel_stats`**

Add to the returned dict in `get_channel_stats()` (after `"country"`), costing no extra quota since `part=snippet` is already requested:

```python
        # From the snippet already being fetched — no extra quota.
        "published_at": snippet.get("publishedAt", ""),
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add -A
```
Message:
```
feat: expose channel publishedAt and age in months

Rides along in the part=snippet response channels.list already
requests, so it costs no additional quota. Unparseable dates return
None ("unknown"), never a young age.
```

---

### Task 5: Qualification gate

**Files:**
- Modify: `scoring.py`, `main.py:55-86`
- Create: `tests/test_qualify.py`

**Interfaces:**
- Consumes: `enrichment.channel_age_months` (Task 4)
- Produces:
  - `scoring.QUALIFIED = "Qualified"`, `scoring.BELOW_VIEW_MINIMUM = "Below View Minimum"`, `scoring.NEW_CHANNEL = "New Channel"`
  - `scoring.qualify(avg_views: float, channel_age_months: float | None, min_avg_views: float, min_channel_age_months: float | None) -> str`
  - `NICHES[niche]["min_avg_views"]` and `NICHES[niche]["min_channel_age_months"]`

- [ ] **Step 1: Write the failing test**

`tests/test_qualify.py`:

```python
"""Per-niche qualification thresholds, from the April 2024 briefs."""
import pytest

HOME_THEATER_MIN_VIEWS = 10_000
LIFESTYLE_MIN_VIEWS = 2_000


def test_meets_both_criteria():
    from scoring import QUALIFIED, qualify

    assert qualify(15_000, 24, HOME_THEATER_MIN_VIEWS, 12) == QUALIFIED


def test_exactly_at_view_minimum_qualifies():
    from scoring import QUALIFIED, qualify

    assert qualify(10_000, 24, HOME_THEATER_MIN_VIEWS, 12) == QUALIFIED


def test_just_below_view_minimum_is_flagged():
    from scoring import BELOW_VIEW_MINIMUM, qualify

    assert qualify(9_999, 24, HOME_THEATER_MIN_VIEWS, 12) == BELOW_VIEW_MINIMUM


def test_lifestyle_uses_its_own_lower_threshold():
    """2,500 views fails Home Theater but passes Lifestyle."""
    from scoring import BELOW_VIEW_MINIMUM, QUALIFIED, qualify

    assert qualify(2_500, 24, HOME_THEATER_MIN_VIEWS, 12) == BELOW_VIEW_MINIMUM
    assert qualify(2_500, 24, LIFESTYLE_MIN_VIEWS, None) == QUALIFIED


def test_young_channel_is_flagged():
    from scoring import NEW_CHANNEL, qualify

    assert qualify(15_000, 6, HOME_THEATER_MIN_VIEWS, 12) == NEW_CHANNEL


def test_view_failure_wins_when_both_fail():
    from scoring import BELOW_VIEW_MINIMUM, qualify

    assert qualify(500, 3, HOME_THEATER_MIN_VIEWS, 12) == BELOW_VIEW_MINIMUM


def test_unknown_age_does_not_disqualify():
    from scoring import QUALIFIED, qualify

    assert qualify(15_000, None, HOME_THEATER_MIN_VIEWS, 12) == QUALIFIED


def test_no_age_requirement_ignores_young_channel():
    from scoring import QUALIFIED, qualify

    assert qualify(5_000, 1, LIFESTYLE_MIN_VIEWS, None) == QUALIFIED


@pytest.mark.parametrize(
    "niche,expected_views,expected_age",
    [("Home Theater", 10_000, 12), ("Lifestyle Sofa", 2_000, None)],
)
def test_niche_criteria_match_the_briefs(niche, expected_views, expected_age):
    from main import NICHES

    assert NICHES[niche]["min_avg_views"] == expected_views
    assert NICHES[niche]["min_channel_age_months"] == expected_age
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `python -m pytest tests/test_qualify.py`
Expected: FAIL — `cannot import name 'qualify'`.

- [ ] **Step 3: Implement `qualify()`**

Add to `scoring.py`, with the other named constants at the top:

```python
# --- Qualification outcomes (values of the Airtable "Qualification" field) ---
QUALIFIED = "Qualified"
BELOW_VIEW_MINIMUM = "Below View Minimum"
NEW_CHANNEL = "New Channel"
```

and the function:

```python
def qualify(
    avg_views: float,
    channel_age_months: float | None,
    min_avg_views: float,
    min_channel_age_months: float | None,
) -> str:
    """
    Check a channel against its niche's hard requirements from the
    influencer briefs, returning the value for the Airtable
    "Qualification" field.

    Thresholds are passed in rather than read from a constant because
    they differ per niche (Home Theater wants 10k+ average views,
    Lifestyle Sofa 2k+); they live on the NICHES entries in main.py.

    Failing channels are flagged, never discarded — a human decides.

    Precedence: when both criteria fail, BELOW_VIEW_MINIMUM is reported.
    A single-select holds one value, and views are the criterion that
    prompted this gate.

    channel_age_months of None means "unknown" and never disqualifies —
    absent data is not evidence against a channel.
    """
    if avg_views < min_avg_views:
        return BELOW_VIEW_MINIMUM
    if (
        min_channel_age_months is not None
        and channel_age_months is not None
        and channel_age_months < min_channel_age_months
    ):
        return NEW_CHANNEL
    return QUALIFIED
```

- [ ] **Step 4: Add the criteria to `NICHES`**

In `main.py`, add to the `"Home Theater"` entry:

```python
        # From the Home Theater brief (Cynthia Lim, 15 April 2024):
        # "Has a Min 10k+ views on YouTube" and "Not a new channel".
        "min_avg_views": 10_000,
        "min_channel_age_months": 12,
```

and to `"Lifestyle Sofa"`:

```python
        # From the Lifestyle Sofa brief: "Has min of 2k+ view on YouTube
        # videos". The brief sets no channel-age requirement. Its
        # Instagram thresholds (100k+ followers, 20k+ reel views) are out
        # of scope — this pipeline only observes YouTube.
        "min_avg_views": 2_000,
        "min_channel_age_months": None,
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add -A
```
Message:
```
feat: add per-niche qualification gate

Home Theater requires 10k+ avg views and a channel older than 12
months; Lifestyle Sofa requires 2k+ avg views with no age requirement.
Failing channels are flagged for review, not discarded.
```

---

### Task 6: DO NOT CONTACT blocklist

**Files:**
- Create: `do_not_contact.py`, `tests/test_do_not_contact.py`

**Interfaces:**
- Consumes: `enrichment.normalize_handle`, `airtable_client._base_url`, `airtable_client._headers`
- Produces:
  - `do_not_contact.Blocklist` with `.handles: set[str]`, `.emails: set[str]`, `.names: set[str]`, and `.match(handle="", email="", name="") -> str` returning the matched key description or `""`
  - `do_not_contact.fetch_blocklist() -> Blocklist` — raises `BlocklistUnavailable` on any failure
  - `do_not_contact.BlocklistUnavailable` (subclass of `RuntimeError`)

- [ ] **Step 1: Write the failing test**

`tests/test_do_not_contact.py`:

```python
"""
The blocklist is a suppression list: it fails closed and matches
generously. Wrongly skipping a prospect costs one lead; wrongly
contacting a blocklisted person is the harm being prevented.
"""
import pytest


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = "error body"

    def json(self):
        return self._payload


FIELD_NAME = "fldCExrqXONKfUxd5"
FIELD_URL = "fldBFsOvwaBkTN7yX"
FIELD_EMAIL = "fldA5r2RO4xZJ1Nbl"


def _page(records, offset=None):
    payload = {"records": records}
    if offset:
        payload["offset"] = offset
    return _Resp(200, payload)


def test_parses_all_observed_url_formats(monkeypatch):
    import do_not_contact

    records = [
        {"fields": {FIELD_URL: "https://www.youtube.com/@EmmaMariesWorld"}},
        {"fields": {FIELD_URL: "youtube.com/@Tarasimonstudios"}},
        {"fields": {FIELD_URL: "https://www.youtube.com/@Chroniques_Atlas/videos"}},
    ]
    monkeypatch.setattr(do_not_contact.requests, "get", lambda *a, **k: _page(records))
    monkeypatch.setattr(do_not_contact.time, "sleep", lambda s: None)

    bl = do_not_contact.fetch_blocklist()
    assert bl.handles == {"emmamariesworld", "tarasimonstudios", "chroniques_atlas"}


def test_matches_handle_case_insensitively(monkeypatch):
    import do_not_contact

    records = [{"fields": {FIELD_URL: "https://www.youtube.com/@LinusTechTips"}}]
    monkeypatch.setattr(do_not_contact.requests, "get", lambda *a, **k: _page(records))
    monkeypatch.setattr(do_not_contact.time, "sleep", lambda s: None)

    bl = do_not_contact.fetch_blocklist()
    assert bl.match(handle="linustechtips")
    assert bl.match(handle="LINUSTECHTIPS")
    assert not bl.match(handle="someoneelse")


def test_matches_email_and_name(monkeypatch):
    import do_not_contact

    records = [
        {"fields": {FIELD_EMAIL: "  Info@LinusMediaGroup.com \n", FIELD_NAME: "Linus Tech Tips"}},
        {"fields": {FIELD_NAME: "superwog"}},  # no URL at all
    ]
    monkeypatch.setattr(do_not_contact.requests, "get", lambda *a, **k: _page(records))
    monkeypatch.setattr(do_not_contact.time, "sleep", lambda s: None)

    bl = do_not_contact.fetch_blocklist()
    assert bl.match(email="info@linusmediagroup.com")
    assert bl.match(name="Superwog")
    assert not bl.match(name="Completely Different Channel")


def test_blank_values_never_match(monkeypatch):
    """The most dangerous bug: empty string matching an empty set entry."""
    import do_not_contact

    records = [{"fields": {FIELD_URL: "", FIELD_EMAIL: "", FIELD_NAME: ""}}]
    monkeypatch.setattr(do_not_contact.requests, "get", lambda *a, **k: _page(records))
    monkeypatch.setattr(do_not_contact.time, "sleep", lambda s: None)

    bl = do_not_contact.fetch_blocklist()
    assert not bl.match(handle="", email="", name="")
    assert not bl.match(handle="anyone")


def test_raises_on_non_200(monkeypatch):
    import do_not_contact

    monkeypatch.setattr(do_not_contact.requests, "get", lambda *a, **k: _Resp(500))

    with pytest.raises(do_not_contact.BlocklistUnavailable):
        do_not_contact.fetch_blocklist()


def test_raises_on_request_exception(monkeypatch):
    import do_not_contact

    def boom(*a, **k):
        raise do_not_contact.requests.RequestException("network down")

    monkeypatch.setattr(do_not_contact.requests, "get", boom)

    with pytest.raises(do_not_contact.BlocklistUnavailable):
        do_not_contact.fetch_blocklist()


def test_raises_when_blocklist_is_suspiciously_empty(monkeypatch):
    """A 200 with no rows means the table moved or was emptied, not that
    nobody is blocklisted."""
    import do_not_contact

    monkeypatch.setattr(do_not_contact.requests, "get", lambda *a, **k: _page([]))
    monkeypatch.setattr(do_not_contact.time, "sleep", lambda s: None)

    with pytest.raises(do_not_contact.BlocklistUnavailable):
        do_not_contact.fetch_blocklist()
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `python -m pytest tests/test_do_not_contact.py`
Expected: FAIL — `ModuleNotFoundError: do_not_contact`.

- [ ] **Step 3: Implement `do_not_contact.py`**

```python
"""
Enforces the "DO NOT CONTACT" suppression list (Airtable table
tblHO0kJw0cBqV8Mw, ~498 rows).

Modeled on external_dedupe.py, which solves the same shape of problem —
an Airtable table keyed by @handle URL rather than Channel ID — but with
three deliberate differences, all because this is a suppression list
rather than a dedupe list:

1. FAILS CLOSED. external_dedupe logs errors and returns partial results,
   which is fine for dedupe (worst case: a known channel is re-added).
   Here the same behaviour would yield an empty set on an Airtable hiccup
   and read as "nobody is blocklisted". Every failure raises instead, and
   the caller must abort. An empty result is treated as a failure too:
   this table is never legitimately empty.

2. NO CACHING. external_dedupe caches 24h against ~18k rows. This table
   is ~5 pages and takes seconds, so it is fetched fresh every run —
   somebody added to the blocklist this morning is honoured this
   afternoon. A stale suppression cache is exactly the failure this
   module exists to prevent.

3. THREE MATCH KEYS, MATCHED GENEROUSLY. Handle is the reliable key, but
   some rows carry only a name, and some carry an agency email shared
   across several channels. Error costs are asymmetric — a false positive
   costs one lead, a false negative is the harm being prevented — so all
   three are indexed and any hit blocks.

Fields are requested BY ID (returnFieldsByFieldId=true) rather than by
name: the table is manually maintained, and a column rename would
silently empty the blocklist.
"""
import logging
import time
from dataclasses import dataclass, field

import requests

from airtable_client import _base_url, _headers
from config import API_SLEEP_SECONDS
from enrichment import normalize_handle

logger = logging.getLogger(__name__)

DO_NOT_CONTACT_TABLE_ID = "tblHO0kJw0cBqV8Mw"

# Verified field IDs. Every text field here is multilineText, so values
# can carry stray newlines and padding — always strip before indexing.
FIELD_NAME = "fldCExrqXONKfUxd5"
FIELD_URL = "fldBFsOvwaBkTN7yX"
FIELD_EMAIL = "fldA5r2RO4xZJ1Nbl"

# Instagram rows are indexed too, not filtered out: it's the same person,
# and over-matching is the safe direction for a suppression list.


class BlocklistUnavailable(RuntimeError):
    """
    Raised when the blocklist cannot be established with confidence.

    Callers MUST abort rather than continue. Proceeding with a partial or
    empty blocklist means contacting people who asked not to be
    contacted.
    """


@dataclass
class Blocklist:
    handles: set[str] = field(default_factory=set)
    emails: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)

    def match(self, handle: str = "", email: str = "", name: str = "") -> str:
        """
        Return a short description of which key matched, or "" for no
        match. Blank inputs never match, even though the index may
        contain blanks from empty cells.
        """
        h = (handle or "").strip().lstrip("@").lower()
        if h and h in self.handles:
            return f"handle @{h}"

        e = (email or "").strip().lower()
        if e and e in self.emails:
            return f"email {e}"

        n = (name or "").strip().casefold()
        if n and n in self.names:
            return f"name '{name.strip()}'"

        return ""

    def __len__(self) -> int:
        return len(self.handles) + len(self.emails) + len(self.names)


def fetch_blocklist() -> Blocklist:
    """
    Build the blocklist index fresh from Airtable. Costs no YouTube quota.

    Raises BlocklistUnavailable on any request failure, non-200 response,
    or an empty result.
    """
    blocklist = Blocklist()
    offset = None
    row_count = 0

    while True:
        params = {
            "fields[]": [FIELD_NAME, FIELD_URL, FIELD_EMAIL],
            "returnFieldsByFieldId": "true",
            "pageSize": 100,
        }
        if offset:
            params["offset"] = offset

        try:
            resp = requests.get(
                _base_url(DO_NOT_CONTACT_TABLE_ID), headers=_headers(), params=params, timeout=30
            )
        except requests.RequestException as e:
            raise BlocklistUnavailable(f"DO NOT CONTACT fetch failed: {e}") from e

        if resp.status_code != 200:
            raise BlocklistUnavailable(
                f"DO NOT CONTACT fetch failed: {resp.status_code} {resp.text}"
            )

        data = resp.json()
        records = data.get("records", [])
        row_count += len(records)

        for record in records:
            fields = record.get("fields", {})

            handle = normalize_handle(fields.get(FIELD_URL, "") or "")
            if handle:
                blocklist.handles.add(handle)

            email = (fields.get(FIELD_EMAIL, "") or "").strip().lower()
            if email:
                blocklist.emails.add(email)

            name = (fields.get(FIELD_NAME, "") or "").strip().casefold()
            if name:
                blocklist.names.add(name)

        offset = data.get("offset")
        if not offset:
            break
        time.sleep(API_SLEEP_SECONDS)

    if row_count == 0:
        raise BlocklistUnavailable(
            "DO NOT CONTACT table returned zero rows — treating as a failure, "
            "not as an empty blocklist. Check the table ID and token scope."
        )

    logger.info(
        "DO NOT CONTACT index: %d rows -> %d handles, %d emails, %d names.",
        row_count, len(blocklist.handles), len(blocklist.emails), len(blocklist.names),
    )
    return blocklist
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_do_not_contact.py`
Expected: all pass.

- [ ] **Step 5: Verify against the live table**

```bash
python -c "
from do_not_contact import fetch_blocklist
bl = fetch_blocklist()
print(f'handles={len(bl.handles)} emails={len(bl.emails)} names={len(bl.names)}')
print('LinusTechTips blocked:', bl.match(handle='linustechtips'))
"
```
Expected: a few hundred handles, and the Linus match reports `handle @linustechtips`. Costs no YouTube quota, so this is safe to run while quota is exhausted.

- [ ] **Step 6: Commit**

```bash
git add -A
```
Message:
```
feat: add DO NOT CONTACT blocklist enforcement

Fails closed on any fetch error and on an empty result -- a partial
blocklist would silently allow contacting people who opted out.
Fetched fresh each run (no cache) so same-day additions are honoured.
Matches on handle, email, and name; reads fields by ID so a column
rename can't empty the list.
```

---

### Task 7: CloakBrowser About-page email step

**Files:**
- Create: `browser_email.py`, `tests/test_browser_email.py`
- Modify: `main.py` (`resolve_email` signature), `backfill_missing_emails.py:51-80`

**Interfaces:**
- Consumes: `enrichment.extract_business_email`
- Produces:
  - `browser_email.BrowserEmailScraper` — context manager with `.find_email(channel_id: str) -> str`
  - `browser_email.null_scraper() -> BrowserEmailScraper` — a disabled instance whose `find_email` always returns `""`
  - `main.resolve_email(stats: dict, performance: dict, scraper=None) -> str`

- [ ] **Step 1: Write the failing test**

`tests/test_browser_email.py`:

```python
"""The browser step must fail soft and reuse one session."""


class _FakePage:
    def __init__(self, text):
        self._text = text

    def goto(self, url, **kwargs):
        self.url = url

    def locator(self, selector):
        return self

    def inner_text(self, timeout=None):
        return self._text

    def close(self):
        pass


class _FakeBrowser:
    def __init__(self, text="", fail=False):
        self._text = text
        self._fail = fail
        self.pages_created = 0
        self.closed = False

    def new_page(self):
        self.pages_created += 1
        if self._fail:
            raise RuntimeError("browser exploded")
        return _FakePage(self._text)

    def close(self):
        self.closed = True


def test_extracts_email_from_about_text():
    from browser_email import BrowserEmailScraper

    browser = _FakeBrowser("Business enquiries: hello@creator.com")
    scraper = BrowserEmailScraper(browser=browser)
    assert scraper.find_email("UC123") == "hello@creator.com"


def test_returns_empty_when_no_email_present():
    from browser_email import BrowserEmailScraper

    scraper = BrowserEmailScraper(browser=_FakeBrowser("no contact details here"))
    assert scraper.find_email("UC123") == ""


def test_browser_failure_is_soft():
    """A browser error must never break the pipeline."""
    from browser_email import BrowserEmailScraper

    scraper = BrowserEmailScraper(browser=_FakeBrowser(fail=True))
    assert scraper.find_email("UC123") == ""


def test_one_session_serves_many_channels():
    """Regression: the backfill launched a browser per channel."""
    from browser_email import BrowserEmailScraper

    browser = _FakeBrowser("a@b.com")
    scraper = BrowserEmailScraper(browser=browser)
    for i in range(5):
        scraper.find_email(f"UC{i}")

    assert browser.pages_created == 5  # five pages...
    assert not browser.closed          # ...but the browser stayed open


def test_null_scraper_is_inert():
    from browser_email import null_scraper

    assert null_scraper().find_email("UC123") == ""


def test_resolve_email_prefers_free_steps_over_browser():
    """The browser must only run when both free steps found nothing."""
    import main
    from browser_email import BrowserEmailScraper

    browser = _FakeBrowser("browser@found.com")
    scraper = BrowserEmailScraper(browser=browser)

    stats = {"business_email": "about@page.com", "channel_id": "UC1"}
    performance = {"repeated_email": ""}
    assert main.resolve_email(stats, performance, scraper) == "about@page.com"
    assert browser.pages_created == 0


def test_resolve_email_falls_through_to_browser():
    import main
    from browser_email import BrowserEmailScraper

    browser = _FakeBrowser("browser@found.com")
    scraper = BrowserEmailScraper(browser=browser)

    stats = {"business_email": "", "channel_id": "UC1"}
    performance = {"repeated_email": ""}
    assert main.resolve_email(stats, performance, scraper) == "browser@found.com"
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `python -m pytest tests/test_browser_email.py`
Expected: FAIL — `ModuleNotFoundError: browser_email`.

- [ ] **Step 3: Implement `browser_email.py`**

```python
"""
Reads a YouTube channel's public About page in CloakBrowser and scans the
rendered text for a contact email.

This only reads text already visible on the public page. It does not
attempt to reveal YouTube's gated "View email address" button.

One browser instance serves the whole run. The earlier implementation in
backfill_missing_emails.py launched and closed a browser per channel,
which at 40+ channels per niche per day is both slow and a stronger
automation signal than a single session.

Every failure is soft: the chain continues without a browser-sourced
email rather than breaking the run.
"""
import logging
from urllib.parse import quote

from enrichment import extract_business_email

logger = logging.getLogger(__name__)

NAV_TIMEOUT_MS = 30000
TEXT_TIMEOUT_MS = 5000


class BrowserEmailScraper:
    """
    Wraps a CloakBrowser instance. Construct with `enabled=False` (or via
    null_scraper()) to get an inert scraper that always returns "".

    Usable as a context manager so the browser is closed even if the run
    raises.
    """

    def __init__(self, browser=None, enabled: bool = True):
        self._browser = browser
        self._enabled = enabled and browser is not None

    @classmethod
    def launch(cls, headless: bool = True) -> "BrowserEmailScraper":
        """Start a CloakBrowser session, or return an inert scraper if it
        can't be started."""
        try:
            from cloakbrowser import launch
        except ImportError:
            logger.warning("CloakBrowser is not installed — browser email step disabled.")
            return cls(enabled=False)

        try:
            return cls(browser=launch(headless=headless))
        except Exception as exc:
            logger.warning("CloakBrowser failed to launch (%s) — browser email step disabled.", exc)
            return cls(enabled=False)

    def find_email(self, channel_id: str) -> str:
        """Return an email found in the channel's About page text, or ""."""
        if not self._enabled:
            return ""

        about_url = f"https://www.youtube.com/channel/{quote(channel_id)}/about"
        page = None
        try:
            page = self._browser.new_page()
            page.goto(about_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            visible_text = page.locator("body").inner_text(timeout=TEXT_TIMEOUT_MS)
            return extract_business_email(visible_text)
        except Exception as exc:
            logger.info("Browser email lookup failed for %s: %s", channel_id, exc)
            return ""
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass

    def close(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception as exc:
                logger.info("Browser close failed: %s", exc)
            self._browser = None
            self._enabled = False

    def __enter__(self) -> "BrowserEmailScraper":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def null_scraper() -> BrowserEmailScraper:
    """An inert scraper, for runs with the browser step turned off."""
    return BrowserEmailScraper(enabled=False)
```

- [ ] **Step 4: Wire it into `resolve_email`**

Replace `resolve_email()` in `main.py` (as rewritten in Task 2) with:

```python
def resolve_email(stats: dict, performance: dict, scraper=None) -> str:
    """
    Email fallback chain, cheapest and strongest signal first:

      1. An address repeated across several recent video descriptions.
      2. A single mention in the channel's own About description.
      3. The rendered About page, read in CloakBrowser.

    Steps 1-2 use data already fetched during enrichment and cost
    nothing. Step 3 only runs when both found nothing, and only when a
    scraper is supplied.
    """
    email = performance.get("repeated_email") or stats.get("business_email", "")
    if not email and scraper is not None:
        email = scraper.find_email(stats["channel_id"])
    return email
```

- [ ] **Step 5: Replace the backfill's per-channel launcher**

In `backfill_missing_emails.py`, delete `_extract_email_with_cloakbrowser()` (lines 51-80) and `CLOAKBROWSER_TIMEOUT_MS`. Add `from browser_email import BrowserEmailScraper, null_scraper`, change `backfill_table()` to take a `scraper` argument instead of `use_cloakbrowser`, and replace the two-line email resolution with:

```python
        email = resolve_email(stats, performance, scraper)
```

In `main()`, wrap the niche loop:

```python
    scraper = BrowserEmailScraper.launch() if args.use_cloakbrowser else null_scraper()
    try:
        for niche_name, table_name in TABLES.items():
            result = backfill_table(niche_name, table_name, args.limit, scraper)
            for key in totals:
                totals[key] += result[key]
            by_source.update(result["by_source"])
    finally:
        scraper.close()
```

Update the source-attribution branch to use `"CloakBrowser visible text"` only when `scraper` is enabled.

- [ ] **Step 6: Run the tests**

Run: `python -m pytest`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add -A
```
Message:
```
feat: reuse one CloakBrowser session for About-page email lookups

Extracts the browser email step into browser_email.py and holds a
single session for the whole run instead of launching one browser per
channel. All failures stay soft.
```

---

### Task 8: Wire the caps into the pipeline

The integration task. Everything it consumes now exists.

**Files:**
- Modify: `discovery.py:136-160`, `main.py:125-305`, `.github/workflows/channel-vetting.yml`
- Create: `tests/test_run_niche_caps.py`, `tests/test_discovery_early_stop.py`

**Interfaces:**
- Consumes: everything from Tasks 3-7
- Produces: `run_discovery(keywords, max_results_per_keyword=50, days_back=90, exclude_ids=None, target_fresh=None) -> list[dict]`

- [ ] **Step 1: Write the failing tests**

`tests/test_discovery_early_stop.py`:

```python
"""Discovery must stop spending 100-unit searches once it has enough."""


def _fake_keyword_results(keyword):
    """Three fresh channels per keyword, named after it."""
    return [
        {"channel_id": f"{keyword}-{i}", "channel_title": f"{keyword} {i}", "matched_keywords": [keyword]}
        for i in range(3)
    ]


def test_stops_once_target_fresh_is_met(monkeypatch):
    import discovery

    searched = []

    def fake_search(keyword, max_results=50, days_back=90):
        searched.append(keyword)
        return _fake_keyword_results(keyword)

    monkeypatch.setattr(discovery, "discover_channels_by_keyword", fake_search)
    monkeypatch.setattr(discovery.time, "sleep", lambda s: None)

    discovery.run_discovery(["a", "b", "c", "d"], target_fresh=5)
    assert searched == ["a", "b"]  # 6 fresh after two keywords; c and d never searched


def test_searches_all_keywords_when_no_target(monkeypatch):
    import discovery

    searched = []

    def fake_search(keyword, max_results=50, days_back=90):
        searched.append(keyword)
        return _fake_keyword_results(keyword)

    monkeypatch.setattr(discovery, "discover_channels_by_keyword", fake_search)
    monkeypatch.setattr(discovery.time, "sleep", lambda s: None)

    discovery.run_discovery(["a", "b", "c"])
    assert searched == ["a", "b", "c"]


def test_excluded_ids_do_not_count_toward_target(monkeypatch):
    """Already-tracked channels aren't fresh, so discovery must keep going."""
    import discovery

    searched = []

    def fake_search(keyword, max_results=50, days_back=90):
        searched.append(keyword)
        return _fake_keyword_results(keyword)

    monkeypatch.setattr(discovery, "discover_channels_by_keyword", fake_search)
    monkeypatch.setattr(discovery.time, "sleep", lambda s: None)

    exclude = {f"a-{i}" for i in range(3)}
    discovery.run_discovery(["a", "b", "c"], exclude_ids=exclude, target_fresh=3)
    assert searched == ["a", "b"]


def test_matched_keywords_still_merge(monkeypatch):
    import discovery

    def fake_search(keyword, max_results=50, days_back=90):
        return [{"channel_id": "shared", "channel_title": "Shared", "matched_keywords": [keyword]}]

    monkeypatch.setattr(discovery, "discover_channels_by_keyword", fake_search)
    monkeypatch.setattr(discovery.time, "sleep", lambda s: None)

    result = discovery.run_discovery(["a", "b"])
    assert result[0]["matched_keywords"] == ["a", "b"]
```

`tests/test_run_niche_caps.py`:

```python
"""Caps count successful pushes only, and the two budgets are separate."""
import pytest


def test_qualified_cap_stops_the_loop(monkeypatch):
    import main

    pushed = []
    monkeypatch.setattr(main, "push_record", lambda t, r: pushed.append(r) or True)

    remaining = main.push_until_full(
        candidates=[{"channel_id": f"UC{i}"} for i in range(10)],
        build_record=lambda c: ({"Channel ID": c["channel_id"], "Qualification": "Qualified"}, "Qualified"),
        table_name="tbl",
        qualified_headroom=3,
        flagged_headroom=0,
    )
    assert len(pushed) == 3
    assert remaining["qualified"] == 3


def test_flagged_have_their_own_budget(monkeypatch):
    import main

    pushed = []
    monkeypatch.setattr(main, "push_record", lambda t, r: pushed.append(r) or True)

    result = main.push_until_full(
        candidates=[{"channel_id": f"UC{i}"} for i in range(10)],
        build_record=lambda c: (
            {"Channel ID": c["channel_id"], "Qualification": "Below View Minimum"},
            "Below View Minimum",
        ),
        table_name="tbl",
        qualified_headroom=5,
        flagged_headroom=2,
    )
    assert len(pushed) == 2
    assert result["flagged"] == 2
    assert result["qualified"] == 0


def test_failed_push_does_not_consume_budget(monkeypatch):
    """Regression: the old loop counted attempts, not successes."""
    import main

    attempts = {"n": 0}

    def flaky_push(table, record):
        attempts["n"] += 1
        return attempts["n"] > 2  # first two fail

    monkeypatch.setattr(main, "push_record", flaky_push)

    result = main.push_until_full(
        candidates=[{"channel_id": f"UC{i}"} for i in range(10)],
        build_record=lambda c: ({"Channel ID": c["channel_id"], "Qualification": "Qualified"}, "Qualified"),
        table_name="tbl",
        qualified_headroom=3,
    )
    assert result["qualified"] == 3
    assert attempts["n"] == 5  # two failures + three successes


def test_zero_headroom_pushes_nothing(monkeypatch):
    import main

    monkeypatch.setattr(main, "push_record", lambda t, r: pytest.fail("should not push"))

    result = main.push_until_full(
        candidates=[{"channel_id": "UC1"}],
        build_record=lambda c: ({"Channel ID": "UC1", "Qualification": "Qualified"}, "Qualified"),
        table_name="tbl",
        qualified_headroom=0,
        flagged_headroom=0,
    )
    assert result == {"qualified": 0, "flagged": 0, "skipped": 0, "pushed_ids": set()}
```

- [ ] **Step 2: Run them to confirm they fail**

Run: `python -m pytest tests/test_discovery_early_stop.py tests/test_run_niche_caps.py`
Expected: FAIL — `run_discovery()` rejects `target_fresh`; `main.push_until_full` does not exist.

- [ ] **Step 3: Add early-stop to `run_discovery`**

Replace `run_discovery()` in `discovery.py`:

```python
def run_discovery(
    keywords: list[str],
    max_results_per_keyword: int = 50,
    days_back: int = 90,
    exclude_ids: set[str] | None = None,
    target_fresh: int | None = None,
) -> list[dict]:
    """
    Run discover_channels_by_keyword() across `keywords`, dedupe channels
    across searches, and merge matched_keywords for channels hit by more
    than one.

    When `target_fresh` is set, stops searching further keywords once
    that many *fresh* candidates (those not in `exclude_ids`) have been
    banked. Each search.list call costs 100 units, so this is the main
    lever on daily quota spend — the caller only needs enough candidates
    to fill the day's remaining cap.

    Stopping early means later keywords in the list get searched less
    often, skewing the candidate mix toward whatever is listed first.
    Accepted deliberately; rotate the keyword order if that becomes a
    problem.

    `exclude_ids` is supplied by the caller so this module stays ignorant
    of Airtable.
    """
    exclude_ids = exclude_ids or set()
    merged: dict[str, dict] = {}

    # enumerate, not keywords.index(): a duplicated keyword would make
    # index() report the position of the first copy.
    for position, keyword in enumerate(keywords, start=1):
        logger.info("Discovering channels for keyword: '%s'", keyword)
        found = discover_channels_by_keyword(keyword, max_results=max_results_per_keyword, days_back=days_back)

        for channel in found:
            cid = channel["channel_id"]
            if cid in merged:
                existing_keywords = set(merged[cid]["matched_keywords"])
                existing_keywords.update(channel["matched_keywords"])
                merged[cid]["matched_keywords"] = sorted(existing_keywords)
            else:
                merged[cid] = channel

        if target_fresh is not None:
            fresh = len(set(merged) - exclude_ids)
            if fresh >= target_fresh:
                logger.info(
                    "Banked %d fresh candidate(s) (target %d) after %d keyword(s) — "
                    "skipping the remaining %d to save quota.",
                    fresh, target_fresh, position, len(keywords) - position,
                )
                break

        time.sleep(API_SLEEP_SECONDS)

    logger.info("Discovery complete: %d unique channels.", len(merged))
    return list(merged.values())
```

- [ ] **Step 4: Add the new imports to `main.py`**

Everything Tasks 3-7 produced has to be imported before it can be used. Add:

```python
from browser_email import BrowserEmailScraper, null_scraper
from do_not_contact import BlocklistUnavailable, fetch_blocklist
from enrichment import channel_age_months
from prospect_day import today_iso
from scoring import QUALIFIED, qualify
from airtable_client import AirtableReadError, count_added_today
from config import (
    CANDIDATE_OVERSHOOT,
    DAILY_FLAGGED_CAP,
    DAILY_QUALIFIED_CAP,
    USE_CLOAKBROWSER,
)
```

Merge these into the existing `from enrichment import (...)`, `from scoring import ...`, `from airtable_client import ...`, and `from config import (...)` blocks rather than adding duplicate import statements.

- [ ] **Step 5: Extract the capped push loop in `main.py`**

Add above `run_niche()`:

```python
def push_until_full(
    candidates: list[dict],
    build_record,
    table_name: str,
    qualified_headroom: int,
    flagged_headroom: int = 0,
) -> dict:
    """
    Push candidates until both daily budgets are exhausted or the
    candidates run out.

    `build_record(candidate)` returns `(record, qualification)`, or
    `(None, reason)` to skip the candidate without spending budget.

    Only SUCCESSFUL pushes consume budget. The previous loop counted
    attempts, so a run of Airtable failures would have burned the day's
    allowance without writing anything.

    Returns counts plus "pushed_ids", the Channel IDs actually written —
    matching the original loop, which added to newly_tracked_ids only
    when push_record returned True.
    """
    counts = {"qualified": 0, "flagged": 0, "skipped": 0, "pushed_ids": set()}

    for candidate in candidates:
        if counts["qualified"] >= qualified_headroom and counts["flagged"] >= flagged_headroom:
            logger.info("Both daily budgets are full — stopping this niche.")
            break

        record, qualification = build_record(candidate)
        if record is None:
            counts["skipped"] += 1
            continue

        bucket = "qualified" if qualification == QUALIFIED else "flagged"
        headroom = qualified_headroom if bucket == "qualified" else flagged_headroom
        if counts[bucket] >= headroom:
            counts["skipped"] += 1
            continue

        if push_record(table_name, record):
            counts[bucket] += 1
            counts["pushed_ids"].add(record["Channel ID"])
        # A failed push is logged inside push_record and costs no budget.

    return counts
```

Add `QUALIFIED` to the `scoring` import in `main.py`.

- [ ] **Step 6: Rewire `process_candidate` and `run_niche`**

`process_candidate()` gains `blocklist` and `scraper` parameters and returns `(record, qualification)`:

```python
def process_candidate(
    candidate: dict,
    external_handles: dict[str, str],
    blocklist,
    niche_config: dict,
    scraper,
) -> tuple[dict | None, str]:
    """Enrich, screen, qualify, and build an Airtable record for one candidate."""
    channel_id = candidate["channel_id"]

    # Checkpoint 1 — free, before spending ~3 quota units on enrichment.
    hit = blocklist.match(name=candidate.get("channel_title", ""))
    if hit:
        logger.info("BLOCKED (pre-enrichment) %s — DO NOT CONTACT (%s).", candidate.get("channel_title"), hit)
        return None, "blocked"

    stats = get_channel_stats(channel_id)
    time.sleep(API_SLEEP_SECONDS)
    if stats is None:
        return None, "unreachable"

    # Checkpoint 2 — the reliable key, known only after channels.list.
    hit = blocklist.match(handle=stats.get("handle", ""), name=stats.get("channel_title", ""))
    if hit:
        logger.info("BLOCKED %s — DO NOT CONTACT (%s).", stats.get("channel_title"), hit)
        return None, "blocked"

    handle = stats.get("handle", "")
    if handle and handle in external_handles:
        logger.info(
            "Skipping %s — already tracked in '%s' (@%s).",
            stats.get("channel_title"), external_handles[handle], handle,
        )
        return None, "duplicate"

    performance = get_recent_video_performance(channel_id, stats.get("uploads_playlist_id"))
    time.sleep(API_SLEEP_SECONDS)
    if performance is None:
        logger.info("Skipping %s — no accessible recent video performance data.", stats.get("channel_title"))
        return None, "unreachable"

    upload_freq = calc_upload_frequency(performance["upload_dates"])
    fake_risk = calc_fake_follower_risk(
        stats["subscriber_count"], performance["avg_views"], performance["avg_engagement_rate"]
    )
    overall_score = calc_overall_score(
        stats["subscriber_count"],
        performance["avg_views"],
        performance["avg_engagement_rate"],
        upload_freq,
        fake_risk,
        DEFAULT_NICHE_MATCH,
    )

    qualification = qualify(
        performance["avg_views"],
        channel_age_months(stats.get("published_at", "")),
        niche_config["min_avg_views"],
        niche_config["min_channel_age_months"],
    )

    email = resolve_email(stats, performance, scraper)

    # Checkpoint 3 — catches agency addresses shared across channels.
    if email:
        hit = blocklist.match(email=email)
        if hit:
            logger.info("BLOCKED %s — DO NOT CONTACT (%s).", stats.get("channel_title"), hit)
            return None, "blocked"

    record = {
        "Channel Name": stats["channel_title"],
        "Channel URL": f"https://www.youtube.com/channel/{channel_id}",
        "Channel ID": channel_id,
        "Subscriber Count": stats["subscriber_count"],
        "Avg Views (last 10 videos)": round(performance["avg_views"], 1),
        "Engagement Rate": round(performance["avg_engagement_rate"], 2),
        "Upload Frequency": f"{round(upload_freq)} videos/month",
        "Content Language": performance.get("content_language") or "Unknown",
        "Email": email,
        "Fake Follower Risk Score": fake_risk,
        "Overall Score": overall_score,
        "Qualification": qualification,
        "Status": DEFAULT_STATUS,
        "Source": f"{SOURCE_LABEL} ({', '.join(candidate.get('matched_keywords', []))})",
        "Notes": "",
        "Date Added": today_iso(),
    }
    return record, qualification
```

In `run_niche()`, before discovery, read the headroom and abort the niche if it can't be read:

```python
    try:
        qualified_today = count_added_today(table_name, QUALIFIED)
        flagged_today = count_added_today(table_name) - qualified_today
    except AirtableReadError as e:
        logger.error("Cannot read today's counts for '%s' (%s) — skipping niche.", niche_name, e)
        return 0, 0, set()

    qualified_headroom = max(0, DAILY_QUALIFIED_CAP - qualified_today)
    flagged_headroom = max(0, DAILY_FLAGGED_CAP - flagged_today)
    logger.info(
        "'%s': %d/%d qualified and %d/%d flagged already added today.",
        niche_name, qualified_today, DAILY_QUALIFIED_CAP, flagged_today, DAILY_FLAGGED_CAP,
    )
    if qualified_headroom == 0 and flagged_headroom == 0:
        logger.info("'%s' is already at its daily cap — skipping (no quota spent).", niche_name)
        return 0, 0, set()

    target_fresh = int((qualified_headroom + flagged_headroom) * CANDIDATE_OVERSHOOT)
    discovered = run_discovery(
        keywords,
        max_results_per_keyword=max_results_per_keyword,
        days_back=days_back,
        exclude_ids=globally_tracked_ids,
        target_fresh=target_fresh,
    )
```

Then replace the manual push loop (lines 245-262 of the original `run_niche`) with a call to `push_until_full`. `push_until_full` calls `build_record(candidate)` with one argument, so the extra dependencies are bound in a closure:

```python
    counts = push_until_full(
        new_candidates,
        lambda c: process_candidate(c, external_handles, blocklist, niche_config, scraper),
        table_name,
        qualified_headroom,
        flagged_headroom,
    )

    logger.info(
        "'%s': pushed %d qualified, %d flagged, skipped %d.",
        niche_name, counts["qualified"], counts["flagged"], counts["skipped"],
    )
    return len(discovered), counts["qualified"] + counts["flagged"], counts["pushed_ids"]
```

`run_niche()` gains `blocklist`, `niche_config`, and `scraper` parameters, passed down from `run()`. The lambda exists because `push_until_full` calls `build_record` with a single argument; the other dependencies are bound in the closure.

In `run()`, fetch the blocklist once before any niche runs and abort the whole run if it is unavailable:

```python
    try:
        blocklist = fetch_blocklist()
    except BlocklistUnavailable as e:
        logger.error("ABORTING: %s", e)
        raise SystemExit(1)
```

and open one browser for the run:

```python
    scraper = BrowserEmailScraper.launch() if USE_CLOAKBROWSER else null_scraper()
    try:
        ...  # niche loop
    finally:
        scraper.close()
```

- [ ] **Step 7: Add the CLI and env toggles**

Add `USE_CLOAKBROWSER = os.getenv("USE_CLOAKBROWSER", "false").lower() == "true"` to `config.py`, and a `--daily-cap N` argument to `main()` that overrides `DAILY_QUALIFIED_CAP` so the capping path is testable cheaply.

In `.github/workflows/channel-vetting.yml`, add `USE_CLOAKBROWSER: "false"` to both env blocks with a comment noting CloakBrowser is unverified on `ubuntu-latest`.

- [ ] **Step 8: Run the tests**

Run: `python -m pytest`
Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add -A
```
Message:
```
feat: cap daily prospects and stop discovery early

Each niche is capped at 40 qualified prospects/day with a separate
20/day budget for below-criteria finds, counted from Airtable so a
second same-day run tops up rather than doubling. Discovery stops
searching keywords once enough fresh candidates are banked, which is
the main lever on the ~2,000 units a full run previously spent before
enriching anything.

Blocklist screening runs at three checkpoints; an unavailable
blocklist aborts the run.
```

---

### Task 9: Airtable schema, views, and the existing-rows audit

The spec calls the audit out of scope but time-sensitive: this work only protects *future* runs, and both tables already hold rows that have never been checked against the blocklist.

**Files:**
- Create: `audit_blocklist.py`

- [ ] **Step 1: Create the `Qualification` field on both tables**

`push_record` sends `typecast: True`, which auto-creates missing select *options* but not the field itself. Run this once — it is idempotent in the sense that a second run returns a duplicate-name error and changes nothing:

```python
import requests

from airtable_client import _headers
from config import AIRTABLE_BASE_ID

TABLES = {
    "Home Theater": "tblzmzZw0xiKDrNZw",
    "Lifestyle Sofa": "tblUtCymzl7Qjmlh4",
}

payload = {
    "name": "Qualification",
    "type": "singleSelect",
    "options": {
        "choices": [
            {"name": "Qualified", "color": "greenLight2"},
            {"name": "Below View Minimum", "color": "yellowLight2"},
            {"name": "New Channel", "color": "orangeLight2"},
        ]
    },
}

for niche, table_id in TABLES.items():
    resp = requests.post(
        f"https://api.airtable.com/v0/meta/bases/{AIRTABLE_BASE_ID}/tables/{table_id}/fields",
        headers=_headers(),
        json=payload,
        timeout=30,
    )
    print(niche, resp.status_code, resp.text[:200])
```

Expected: `200` for each table. A `422` with `DUPLICATE_OR_EMPTY_FIELD_NAME` means the field already exists — safe to ignore.

> The token needs the `schema.bases:write` scope, which is separate from the `data.records:write` scope the pipeline uses. If this returns `403 NOT_AUTHORIZED`, create the field through the Airtable UI instead — nothing downstream depends on how it was created.

- [ ] **Step 2: Create the review views**

On each table:
- `Needs Review — Below Criteria`, filtered to `Qualification` is not `Qualified`.
- Tighten the existing default grid view to `Qualification` is `Qualified`.

- [ ] **Step 3: Write the audit script**

`audit_blocklist.py`:

```python
"""
One-off audit: cross-check rows ALREADY in the niche tables against the
DO NOT CONTACT list.

The pipeline's blocklist screening only protects future runs. Rows added
before that screening existed have never been checked, and those are the
ones most likely to be contacted first.

Read-only by default. Costs no YouTube quota.

    python audit_blocklist.py            # report only
    python audit_blocklist.py --mark     # also set Status to "Do Not Contact"
"""
import argparse
import logging
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests

from airtable_client import _base_url, _headers, push_record
from config import AIRTABLE_TABLE_HOME_THEATER, AIRTABLE_TABLE_LIFESTYLE_SOFA
from do_not_contact import BlocklistUnavailable, fetch_blocklist
from enrichment import normalize_handle

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

TABLES = {
    "Home Theater": AIRTABLE_TABLE_HOME_THEATER,
    "Lifestyle Sofa": AIRTABLE_TABLE_LIFESTYLE_SOFA,
}


def _all_records(table_name: str) -> list[dict]:
    records, offset = [], None
    while True:
        params = {"pageSize": 100}
        if offset:
            params["offset"] = offset
        resp = requests.get(_base_url(table_name), headers=_headers(), params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        records.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            return records


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit existing rows against DO NOT CONTACT")
    parser.add_argument("--mark", action="store_true", help='Set Status to "Do Not Contact" on hits')
    args = parser.parse_args()

    try:
        blocklist = fetch_blocklist()
    except BlocklistUnavailable as e:
        logger.error("ABORTING: %s", e)
        raise SystemExit(1)

    total_hits = 0
    for niche_name, table_name in TABLES.items():
        if not table_name:
            continue
        for record in _all_records(table_name):
            fields = record.get("fields", {})
            hit = blocklist.match(
                handle=normalize_handle(fields.get("Channel URL", "")),
                email=fields.get("Email", ""),
                name=fields.get("Channel Name", ""),
            )
            if not hit:
                continue
            total_hits += 1
            print(f"[{niche_name}] BLOCKLISTED: {fields.get('Channel Name')} ({hit})")
            if args.mark:
                push_record(table_name, {"Channel ID": fields.get("Channel ID"), "Status": "Do Not Contact"})

    print(f"\n{total_hits} blocklisted row(s) found across {len(TABLES)} table(s).")
    if total_hits and not args.mark:
        print("Re-run with --mark to flag them, after reviewing the list above.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the audit read-only**

Run: `python audit_blocklist.py`
Expected: a list of any blocklisted rows already present. Review before running with `--mark`.

> **Note on `--mark`:** `push_record` PATCHes the full dict, but this script sends only `Channel ID` and `Status`, so reviewer Notes are preserved. Adding more fields to that payload would overwrite them.

- [ ] **Step 5: Commit**

```bash
git add audit_blocklist.py
```
Message:
```
feat: add one-off DO NOT CONTACT audit for existing rows

Pipeline screening only protects future runs; rows added earlier were
never checked. Read-only by default.
```

---

### Task 10: Documentation

**Files:**
- Modify: `CLAUDE.md`, `README.md`

- [ ] **Step 1: Update `CLAUDE.md`**

Add to the project-structure tree: `prospect_day.py`, `do_not_contact.py`, `browser_email.py`, `audit_blocklist.py`, `tests/`.

Add a "Daily caps" section covering: caps are per niche per day, counted from Airtable rather than a local file; `PROSPECT_DAY_TZ` is pinned and deliberately differs from `quota_tracker`'s Pacific zone; `count_added_today()` raises rather than failing open, and why that breaks the module convention.

Add a "DO NOT CONTACT" section: fails closed, never cached, matches on handle/email/name, three checkpoints, reads fields by ID.

Add a "Qualification" section: per-niche thresholds from the briefs, the both-fail precedence rule, unknown age never disqualifying.

- [ ] **Step 2: Update `README.md`**

Update the pipeline-flow description for the caps and blocklist, document the new env vars (`DAILY_QUALIFIED_CAP`, `DAILY_FLAGGED_CAP`, `CANDIDATE_OVERSHOOT`, `PROSPECT_DAY_TZ`, `USE_CLOAKBROWSER`), remove the Hunter/Modash sections, and add a "Running the tests" section (`python -m pytest`).

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md README.md
```
Message:
```
docs: document daily caps, qualification gate, and blocklist
```

---

### Task 11: `cloakbrowser-mcp` for agents

Makes CloakBrowser drivable by Claude Code and any other MCP client working in this repo — useful for opening real channel pages to prototype extraction and debug selectors *before* the logic lands in `enrichment.py`. Entirely separate from the pipeline's Python path; nothing in `main.py` imports or depends on this.

**Files:**
- Create: `.mcp.json`
- Modify: `.gitignore`, `README.md`

**Interfaces:**
- Consumes: nothing
- Produces: an MCP server named `cloakbrowser`, available to MCP clients opened in this directory

- [ ] **Step 1: Verify the package runs before committing to it**

Run: `npx -y cloakbrowser-mcp@1.10.0 --help`
Expected: usage output. Node 24.12.0 and npm 11.6.2 are already present on this machine. If this fails, stop and report — do not write a config pointing at something that doesn't run.

- [ ] **Step 2: Create `.mcp.json`**

```json
{
  "mcpServers": {
    "cloakbrowser": {
      "command": "npx",
      "args": ["-y", "cloakbrowser-mcp@1.10.0"],
      "env": {
        "CLOAKBROWSER_LICENSE_KEY": "${CLOAKBROWSER_LICENSE_KEY:-}"
      }
    }
  }
}
```

The version is pinned rather than floating: an MCP server that silently changes behaviour between sessions is hard to debug. The license key passes through from the environment when set and is harmless when unset — no secret is stored in this file, which is committed.

- [ ] **Step 3: Keep the key out of git**

`.gitignore` already excludes `.env`. Confirm `.mcp.json` contains no literal key — it must reference the env var only.

- [ ] **Step 4: Verify the server is picked up**

Restart Claude Code in this directory and confirm `cloakbrowser` appears in `/mcp`. Note that MCP servers requiring interactive auth are unavailable in headless runs; this one needs none.

- [ ] **Step 5: Document it in `README.md`**

Add a short section explaining the split: the pipeline uses the Python `cloakbrowser` package directly, while `.mcp.json` exposes `cloakbrowser-mcp` for interactive agent use. State plainly that the MCP server is **not** used by the GitHub Actions run and is not required for the pipeline to work.

- [ ] **Step 6: Commit**

```bash
git add .mcp.json README.md .gitignore
```
Message:
```
feat: expose CloakBrowser to MCP clients via cloakbrowser-mcp

Lets Claude Code drive real channel pages when prototyping extraction
logic. Separate from the pipeline, which uses the Python cloakbrowser
package directly -- an MCP server needs an LLM client, which the cron
job has no reason to grow.
```

---

### Task 12: CloakBrowser in GitHub Actions

Full CI support, shipped **off by default**. GitHub-hosted runners use Azure datacenter ranges that YouTube challenges aggressively, and CloakBrowser patches fingerprints rather than IP reputation, so the scheduled run must not silently depend on it until a manual run proves it works.

**Files:**
- Modify: `.github/workflows/channel-vetting.yml`, `.env.example`

- [ ] **Step 1: Add a `workflow_dispatch` input to enable the browser**

Under `workflow_dispatch.inputs`, alongside the existing `test_mode`:

```yaml
      use_cloakbrowser:
        description: "Enable CloakBrowser About-page email lookups (unproven on runner IPs)"
        type: boolean
        default: false
```

- [ ] **Step 2: Install headless Chromium system libraries**

CloakBrowser downloads a patched Chromium but not the shared libraries it links against. Add before the pipeline step:

```yaml
      - name: Install Chromium system dependencies
        if: inputs.use_cloakbrowser == true
        run: |
          sudo apt-get update
          sudo apt-get install -y --no-install-recommends \
            libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
            libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
            libxrandr2 libgbm1 libasound2t64 libpango-1.0-0 libcairo2
```

- [ ] **Step 3: Cache the patched Chromium download**

It lands in `~/.cloakbrowser/` (see `cloakbrowser/config.py::get_cache_dir`, overridable via `CLOAKBROWSER_CACHE_DIR`). It is a large download; without caching every run re-fetches it.

```yaml
      - name: Cache CloakBrowser Chromium
        if: inputs.use_cloakbrowser == true
        uses: actions/cache@v4
        with:
          path: ~/.cloakbrowser
          key: cloakbrowser-${{ runner.os }}-v1
```

- [ ] **Step 4: Wire the env vars into both pipeline steps**

Add to the `env:` block of both the full and test-mode steps:

```yaml
          # Off for scheduled runs. CloakBrowser defeats fingerprinting,
          # not datacenter IP reputation, and GitHub runners sit in Azure
          # ranges YouTube challenges hard. Enable via workflow_dispatch
          # to test; only make it the default once a manual run proves it.
          USE_CLOAKBROWSER: ${{ inputs.use_cloakbrowser == true }}
          # Optional. CloakBrowser runs unlicensed without it.
          CLOAKBROWSER_LICENSE_KEY: ${{ secrets.CLOAKBROWSER_LICENSE_KEY }}
          # Pinned so the runner (UTC) agrees with head office on the day.
          PROSPECT_DAY_TZ: America/Toronto
```

Confirm the `HUNTER_API_KEY` lines are already gone (Task 2, Step 8).

- [ ] **Step 5: Document the new variables**

Add to `.env.example`, all commented as optional:

```
# Optional: enable CloakBrowser About-page email lookups (default false)
USE_CLOAKBROWSER=false
# Optional: CloakBrowser Pro license key. Runs unlicensed if unset.
CLOAKBROWSER_LICENSE_KEY=
# Timezone defining a "prospect day" for the daily caps.
PROSPECT_DAY_TZ=America/Toronto
```

- [ ] **Step 6: Validate the workflow YAML**

Run: `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/channel-vetting.yml')); print('workflow YAML OK')"`
Expected: `workflow YAML OK`. Install `pyyaml` first if absent — it is a dev-only check and does not belong in `requirements.txt`.

- [ ] **Step 7: Prove it on a real runner**

After merging, trigger `workflow_dispatch` with `test_mode: true` and `use_cloakbrowser: true`. Read the logs for `Browser email lookup failed` entries — a run where *every* channel logs a failure means the runner IP is being challenged, which is the expected outcome and the reason the default is off. Report the result rather than flipping the default.

> Do not enable `use_cloakbrowser` on the `schedule` trigger until this manual run shows browser lookups actually succeeding.

- [ ] **Step 8: Commit**

```bash
git add .github/workflows/channel-vetting.yml .env.example
```
Message:
```
ci: support CloakBrowser in GitHub Actions, disabled by default

Adds Chromium system deps, a cache for the patched binary, and a
workflow_dispatch toggle. Left off for scheduled runs: CloakBrowser
defeats fingerprinting, not datacenter IP reputation, and GitHub
runners sit in Azure ranges YouTube challenges. Prove via manual
dispatch before making it the default.
```

---

### Task 13: Correct the caps and stop the candidate pool running dry

Two changes from a human decision on 2026-08-09, after Tasks 3 and 8 were already built.

**Why the caps change:** the requirement was always "each table produces around 30-40 per day", but the budgets were built additive — 40 qualified **plus** 20 flagged — allowing up to 60 rows per table per day. That was a plan error.

**Why the window changes:** uniqueness across days is already guaranteed three ways (`globally_tracked_ids` spanning both niche tables, `external_handles` over four other base tables, and `push_record` PATCHing on existing Channel ID). The real risk is the opposite one — running dry. Discovery uses `order=relevance` over a fixed keyword list with `max_results_per_keyword=50`, and relevance ranking is stable day to day, so **the same ~50 channels come back for each keyword every day**. Dedupe filters the ones already taken and the run consumes the next slice, until the slice is empty:

| Niche | Keywords | Ceiling (50/kw) | Realistic after overlap | Days at 40/day |
|---|---|---|---|---|
| Home Theater | 9 | 450 | ~300-400 | ~8-10 |
| Lifestyle Sofa | 11 | 550 | ~400-500 | ~10-13 |

After that the pipeline returns nothing new while still spending ~100 units per keyword per day re-reading a pool it has fully consumed. YouTube permits paginating to ~500 results per query, so only the first ~10% is ever touched today.

A rolling recent window fixes this at the source: creators upload continuously, so a window of "the last N days" is self-renewing. It is deterministic and needs no cursor state (YouTube's `pageToken` values are not durable across days).

**Files:**
- Modify: `config.py`, `main.py`
- Create: `tests/test_discovery_window.py`

**Interfaces:**
- Consumes: `discovery.run_discovery(..., days_back=...)` (already parameterised)
- Produces: `config.DISCOVERY_DAYS_BACK`; `main.main()` gains `--days-back N`

- [ ] **Step 1: Write the failing tests**

`tests/test_discovery_window.py`:

```python
"""The search window is configurable and defaults to a recent rolling window."""


def test_default_window_is_recent_not_ninety_days():
    import config

    assert config.DISCOVERY_DAYS_BACK == 7


def test_caps_sum_to_forty():
    """The requirement is ~30-40 new rows per table per day, total."""
    import config

    assert config.DAILY_QUALIFIED_CAP == 30
    assert config.DAILY_FLAGGED_CAP == 10
    assert config.DAILY_QUALIFIED_CAP + config.DAILY_FLAGGED_CAP == 40


def test_run_passes_configured_window_to_discovery(monkeypatch):
    """run() must not hardcode 90 any more."""
    import main

    seen = {}

    def fake_run_niche(niche_name, table_name, keywords, max_results, days_back, *a, **k):
        seen["days_back"] = days_back
        return 0, 0, set()

    monkeypatch.setattr(main, "run_niche", fake_run_niche)
    monkeypatch.setattr(main, "fetch_blocklist", lambda: object())
    monkeypatch.setattr(main, "get_existing_channel_ids", lambda t: set())
    monkeypatch.setattr(main, "fetch_external_handles", lambda: {})
    monkeypatch.setattr(main, "get_today_spend", lambda: 0)

    main.run(niches=main.NICHES, max_results_per_keyword=50, days_back=7)
    assert seen["days_back"] == 7


def test_days_back_cli_override(monkeypatch):
    """--days-back lets a one-off wide sweep reach the backlog."""
    import sys

    import main

    captured = {}
    monkeypatch.setattr(main, "run", lambda **kw: captured.update(kw))
    monkeypatch.setattr(sys, "argv", ["main.py", "--days-back", "90"])

    main.main()
    assert captured["days_back"] == 90
```

- [ ] **Step 2: Run them to confirm they fail**

Run: `python -m pytest tests/test_discovery_window.py`
Expected: FAIL — `DISCOVERY_DAYS_BACK` does not exist, caps are still 40/20, `run()` hardcodes 90.

- [ ] **Step 3: Correct the caps**

In `config.py`, change the two existing values and extend the comment:

```python
# Each niche table produces at most 40 new rows per day, total. The two
# budgets are separate so a weak discovery day cannot fill the table with
# below-criteria channels and crowd out real prospects.
DAILY_QUALIFIED_CAP = int(os.getenv("DAILY_QUALIFIED_CAP", 30))
DAILY_FLAGGED_CAP = int(os.getenv("DAILY_FLAGGED_CAP", 10))
```

- [ ] **Step 4: Add the discovery window constant**

Append to `config.py`:

```python
# How far back search.list looks for videos, in days.
#
# Deliberately a SHORT rolling window rather than a fixed 90 days. Search
# results are ranked by relevance and that ranking is stable, so a wide
# fixed window returns the same channels every day; once they are all
# tracked, the pipeline produces nothing while still spending ~100 units
# per keyword re-reading a consumed pool. A recent window is self-renewing
# because creators keep uploading.
#
# Use --days-back 90 for a one-off sweep of the backlog (e.g. the first
# run against an empty table).
DISCOVERY_DAYS_BACK = int(os.getenv("DISCOVERY_DAYS_BACK", 7))
```

- [ ] **Step 5: Stop hardcoding 90 in `main.py`**

`main()` currently calls `run(niches=..., max_results_per_keyword=50, days_back=90)` and, in test mode, `days_back=90`. Both must use the resolved window. Add the CLI argument:

```python
    parser.add_argument(
        "--days-back",
        type=int,
        default=DISCOVERY_DAYS_BACK,
        help="How many days back to search for videos. Defaults to DISCOVERY_DAYS_BACK "
             "(7). Pass a larger value for a one-off backlog sweep, e.g. --days-back 90.",
    )
```

and pass `days_back=args.days_back` in both the test-mode and full-run branches. Import `DISCOVERY_DAYS_BACK` from `config` in the existing config import block.

- [ ] **Step 6: Log when a niche finishes under its cap**

In `run_niche`, after `push_until_full` returns, when `counts["qualified"] < qualified_headroom`, log a warning that names the niche and both numbers, e.g.:

```python
    if counts["qualified"] < qualified_headroom:
        logger.warning(
            "'%s' finished under its qualified budget (%d of %d). Discovery is running "
            "dry for these keywords — widen --days-back for a one-off sweep, or add "
            "keywords from the brief's secondary content types.",
            niche_name, counts["qualified"], qualified_headroom,
        )
```

This is the signal that the niche is saturating, which the rolling window makes a normal and expected end state rather than a bug.

- [ ] **Step 7: Run the tests**

Run: `python -m pytest`
Expected: all pass. Existing tests referencing the old cap values must be updated to the new ones — do not weaken an assertion to make it pass; change it to the correct expected value.

- [ ] **Step 8: Commit**

```bash
git add -A
```
Message:
```
fix: cap each table at 40 rows/day and use a rolling search window

Caps were additive (40 qualified + 20 flagged = 60/table/day) against a
requirement of ~30-40. Now 30 + 10.

Discovery searched a fixed 90-day window with stable relevance ranking,
so it re-read the same ~50 channels per keyword every day and would
have run dry in ~8-13 days while still spending quota. A short rolling
window is self-renewing. --days-back overrides it for a backlog sweep.
```

---

## Verification before the first real run

The YouTube quota resets at **15:00 local (00:00 Pacific)**. Everything below Task 8 is unit-testable without it.

1. `python -m pytest` — all green.
2. `python -c "from do_not_contact import fetch_blocklist; print(len(fetch_blocklist().handles))"` — no quota cost.
3. `python audit_blocklist.py` — read-only.
4. After quota reset: `python main.py --test --daily-cap 2` — confirms the blocklist fetch, qualification gate, and cap arithmetic end-to-end for a handful of units.
5. Only then: `python main.py`.

## Known pre-existing hazards, deliberately not fixed

Carried from the spec; each deserves its own change:

- **`push_record()` PATCHes the full record dict**, including `"Notes": ""` and `"Status": "New"`, so re-pushing an existing channel wipes reviewer notes. `globally_tracked_ids` mostly prevents this.
- **The quota tracker undercounts.** On 2026-08-07 it read 6,121 against a ceiling of 8,000 while Google already returned `quotaExceeded`.
- **403 quota errors are indistinguishable from dead channels** — `get_channel_stats()` returns `None` for both, so `backfill_missing_emails.py` reports quota exhaustion as "private/deleted/no videos".
