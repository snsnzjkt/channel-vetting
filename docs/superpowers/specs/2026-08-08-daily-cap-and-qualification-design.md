# Daily prospect cap, qualification gate, and DO NOT CONTACT enforcement

Date: 2026-08-08
Status: Approved design, not yet implemented

## Problem

Four separate issues, all touching the same per-candidate path in `main.py`:

1. **Unbounded volume.** `run()` enriches and pushes every candidate discovery
   returns. Discovery searches all 20 keywords upfront at 100 units each
   (~2,000 units) before a single channel is enriched. On 2026-08-07 this
   exhausted the 10,000-unit daily quota.
2. **Low-view prospects reach reviewers.** Channels whose recent videos fall
   below the brief's view minimum are pushed anyway. Scoring blends
   `avg_views` at 20% weight on a log scale, so a 500-view channel and a
   15,000-view channel land at similar Overall Scores. Nothing in the table
   expresses "fails a hard requirement."
3. **No blocklist enforcement.** The base has a DO NOT CONTACT table
   (`tblHO0kJw0cBqV8Mw`, 498 rows) that the pipeline has never consulted.
4. **Paid email lookups no longer wanted.** Hunter.io and Modash are being
   dropped in favour of the free scraper chain plus CloakBrowser.

## Decisions

| Question | Decision |
|---|---|
| What counts as a "prospect" | A record successfully pushed to Airtable |
| Cap scope | Per niche: 40/day each (30 is the expected floor, not enforced) |
| Daily state | Counted from Airtable `Date Added` = today, not a local file |
| Stopping rule | Hard cap; stop as soon as it is reached |
| Discovery | Stop searching further keywords once enough fresh candidates are banked |
| Below-threshold prospects | Do **not** count against the 40; separate ceiling of 20/niche/day |
| Marking | New `Qualification` single-select field |
| Channel age | Home Theater only, flag under 12 months |
| Review surface | Filtered grid views in each niche table |

## Criteria, from the influencer briefs (Cynthia Lim, updated 15 April 2024)

| Niche | Min avg views (last 10) | Min channel age |
|---|---|---|
| Home Theater | 10,000 | 12 months |
| Lifestyle Sofa | 2,000 | none specified |

The Lifestyle brief's Instagram requirements (100k+ followers, 20k+ reel
views, 60-100 comments per 1k likes) are out of scope — this pipeline only
observes YouTube.

---

## Component 1 — Daily headroom

New in `airtable_client.py`:

```python
def count_added_today(table_name: str, qualification: str | None = None) -> int
```

Server-side `filterByFormula` on `Date Added` = today, optionally narrowed by
`Qualification`. Because the filter runs server-side it returns at most ~60
records — a single page, unlike the full-table pagination
`get_existing_channel_ids()` performs. Costs no YouTube quota.

**Date source.** The comparison date must be the same value
`process_candidate()` stamps into `Date Added` (currently `date.today()`,
local system time). Extract that into one shared helper so the writer and
the counter cannot drift.

This deliberately does **not** use `quota_tracker`'s Pacific date. They
measure different things: quota tracks Google's reset schedule, the cap
tracks human review capacity on the user's local day. The gap is large in
practice — the machine runs ~15 hours ahead of Pacific — so a fresh local
day can begin while the YouTube quota is still exhausted. Both limits apply
independently; neither is derived from the other.

**Failure mode.** On an Airtable read error the existing helpers log and
return empty, which here would read as "0 added today" and grant a full
budget — failing open in the one direction that overspends. `count_added_today()`
must therefore **raise** on any request exception or non-200 response rather
than returning a count. This is a deliberate exception to the
`airtable_client.py` convention of logging and returning a falsy value: that
convention exists so one bad record cannot kill a run, but here a silent
failure inflates the budget. `run_niche()` catches it, logs, and skips the
niche.

**Named constants** (in `config.py`, overridable via `.env`):

```python
DAILY_QUALIFIED_CAP = 40   # per niche, per day
DAILY_FLAGGED_CAP   = 20   # per niche, per day
CANDIDATE_OVERSHOOT = 1.5  # discovery target multiplier
```

## Component 2 — Qualification gate

`NICHES` entries gain per-niche criteria (per-niche facts belong with the
niche definition):

```python
"Home Theater":   {..., "min_avg_views": 10_000, "min_channel_age_months": 12}
"Lifestyle Sofa": {..., "min_avg_views":  2_000, "min_channel_age_months": None}
```

The logic goes in `scoring.py` as `qualify(...)`, matching the existing
convention that judgment thresholds live there. It returns one of three
values for the new `Qualification` field:

- `Qualified`
- `Below View Minimum`
- `New Channel`

**Rules:**

- When both criteria fail, `Below View Minimum` wins. A single-select holds
  one value, and views are the criterion that prompted this work.
- Missing `publishedAt` → do not flag. Absent data must not disqualify.
- `avg_views` is the existing last-10-videos average, so the gate measures
  what the brief asks about ("latest videos"), not lifetime average views.

**Enrichment change.** `get_channel_stats()` gains `published_at` from the
`part=snippet` response it already fetches — zero additional quota.

**Airtable schema.** `push_record` sends `typecast: True`, which auto-creates
missing *options* within a select field but not the field itself. The
`Qualification` field must be created on both niche tables before first run.

**No automated content matching exists.** `DEFAULT_NICHE_MATCH` is hardcoded
to 50. The flag therefore means "surfaced by this niche's keywords but below
the view minimum" — content match remains a human judgment at review time.

## Component 3 — DO NOT CONTACT enforcement

New `do_not_contact.py`, modeled on `external_dedupe.py` but with three
deliberate divergences driven by it being a suppression list rather than a
dedupe list.

**Source table `tblHO0kJw0cBqV8Mw` (498 rows) has no Channel ID field.**
Observed schema:

| Field ID | Contents |
|---|---|
| `fld46pLTI2YQM43Av` | Platform (single-select: YouTube, Instagram) |
| `fldA5r2RO4xZJ1Nbl` | Email (often blank) |
| `fldBFsOvwaBkTN7yX` | Channel URL (blank on some rows) |
| `fldCExrqXONKfUxd5` | Name |
| `fldvmD7QmwX8YvMgi` | Subscribers (text, e.g. "1.43M subscribers") |
| `fldjAHTo96ZVZlzm8` | Country (single-select) |

URL formats vary: `https://www.youtube.com/@X`, bare `youtube.com/@X`, and
trailing `/videos`. `enrichment.normalize_handle()` already handles all three.

**Divergence 1 — fail closed.** `fetch_external_handles()` logs errors and
returns partial results, which is acceptable for dedupe. For a blocklist the
same behaviour yields an empty set on an Airtable hiccup, reading as "nobody
is blocklisted." Any fetch error, non-200 response, or truncated pagination
must **abort the run** rather than continue.

**Divergence 2 — no caching.** `external_dedupe` caches 24h against ~18k
rows. This table is 498 rows (~5 pages, a few seconds). Fetch fresh every
run so a blocklist entry added this morning is honoured this afternoon. A
stale suppression cache is precisely the failure being guarded against.

**Divergence 3 — three match keys, matched generously.** Error costs are
asymmetric: wrongly skipping a good prospect costs one lead; wrongly
contacting a blocklisted person is the harm being prevented.

- **Handle** (primary) — `normalize_handle()` over the URL field.
- **Email** (secondary) — lowercased, exact match.
- **Name** (tertiary) — casefolded, catching rows with no URL
  (e.g. `superwog`, `thisgeorgiaclay`).

Instagram-platform rows are included, not filtered — same person, and
over-matching is the safe direction.

**Three checkpoints:**

| Where | Key | Rationale |
|---|---|---|
| Before enrichment | `channel_title` from `search.list` | Free; avoids ~3 quota units on a blocked channel |
| After `channels.list` | `@handle` | The reliable key; only known once stats are fetched |
| Before `push_record` | resolved email | Catches agency addresses shared across channels |

## Component 4 — CloakBrowser in the email chain

The resolution chain becomes, with Hunter removed:

1. `find_repeated_email()` — address seen in ≥3 sampled video descriptions.
2. `extract_business_email()` — single mention in the About description.
3. CloakBrowser — visible text of the public About page.

**One browser per run, not per channel.** The existing implementation in
`backfill_missing_emails.py` launches and closes a browser for every channel.
At 40+ channels per niche per day that is both slow and a stronger automation
signal than a single session. Hold one instance open for the run, open a page
per channel, close at the end.

Failures stay soft: any browser error returns `""` and the chain continues.

**Noted risk, accepted by the user.** CloakBrowser is a bot-detection-evasion
browser ("stealth Chromium... source-level fingerprint patches"). Moving it
from a maintenance script into the main pipeline materially increases exposure
to blocking and is a YouTube ToS matter. Raised twice, directed twice.

## Component 5 — Remove Hunter and Modash

**Delete:** `hunter_client.py`, `modash_client.py`, `modash_backfill.py`.

**`config.py`:** remove `HUNTER_API_KEY`, `MODASH_API_KEY`, `MODASH_API_BASE_URL`.
Remove the same keys from `.env` and `.env.example`.

**`main.py`:** drop the `find_domain_email` and `extract_candidate_domain`
imports; remove the `use_hunter` parameter from `resolve_email()` entirely.

**`enrichment.py`:** remove `extract_candidate_domain()` and
`DOMAIN_SEARCH_BLOCKLIST` — both existed solely to feed Hunter.

> **Keep `EMAIL_DOMAIN_BLOCKLIST` (= `THIRD_PARTY_DOMAINS`) and keep freemail
> domains out of it.** It screens scraped email addresses, which the pipeline
> still does. Merging `FREEMAIL_DOMAINS` into it was a real past bug that
> silently discarded every `@gmail.com` match — gmail being 53% of collected
> addresses. Removing Hunter removes the *other* list, not this one.

`FREEMAIL_DOMAINS` stays; `backfill_missing_emails.py` uses it for reporting.

**`backfill_missing_emails.py`:** remove the `--with-hunter` flag (already a
no-op — `use_hunter=False` is hardcoded at the call site) and the "Hunter
domain search" attribution branch.

**Docs:** update `CLAUDE.md` and `README.md` — the email fallback chain, the
two-pass backfill ordering section, and the Modash preflight notes.

## Component 6 — Assembled per-candidate flow

```
candidate → name on blocklist? ─ skip
  → enrich (~3 units) → handle on blocklist? ─ skip
  → handle in external tables? ─ skip
  → qualify(avg_views, channel_age)
  → resolve_email (repeated → About → CloakBrowser)
  → email on blocklist? ─ skip
  → Qualified?  yes → push if qualified_today < DAILY_QUALIFIED_CAP
                no  → push if flagged_today  < DAILY_FLAGGED_CAP
  → stop niche when both budgets are full
```

Only *successful* pushes increment either counter. The existing loop
increments `processed` even when `push_record()` returns `False`; the caps
must key off the return value instead, or a run of Airtable failures would
silently consume the day's budget without writing anything.

**Discovery early-stop.** `run_discovery()` gains `exclude_ids: set[str]` and
`target_fresh: int | None`. After each keyword it counts fresh (non-excluded)
candidates and breaks once `target_fresh` is met. Discovery stays ignorant of
Airtable — the caller supplies the exclude set. Cross-keyword
`matched_keywords` merging is preserved for the keywords actually searched.

`target_fresh = (qualified_headroom + flagged_headroom) * CANDIDATE_OVERSHOOT`, the overshoot
covering candidates lost to enrichment failure and dedupe. When candidates run
out before the cap is reached, log it — that is the signal keywords need
re-tuning. Because search results are cached per keyword per day, a second run
the same day re-reads searched keywords for free and extends into new ones.

**Keyword ordering.** Early-stop means later keywords are searched less often,
skewing the candidate mix toward whatever is listed first. Accepted for now;
daily rotation was considered and deferred.

## Component 7 — Review surface

Per niche table:

- **"Needs Review — Below Criteria"**: filtered to `Qualification ≠ Qualified`.
- Tighten the existing main view to `Qualification = Qualified`.

Creatable via the Airtable API.

## Testing

`python main.py --test` remains the cheap end-to-end check. It must exercise
the blocklist fetch, the qualification gate, and the cap arithmetic. Because
`--test` uses one keyword and `max_results=5`, caps will not bind; add a
`--daily-cap` override so the capping path itself can be tested cheaply.

Unit-testable without network access, and worth covering:

- `qualify()` across both niches, at and either side of each threshold,
  both-fail precedence, and missing `publishedAt`.
- Blocklist matching over the real URL format variants, blank URLs, and
  case differences.
- Headroom arithmetic when today's count already meets or exceeds the cap.

## Out of scope, but recommended before the next outreach batch

**Audit existing rows against the blocklist.** This design only protects
future runs. Both niche tables contain rows from previous runs that have never
been checked against DO NOT CONTACT, so blocklisted people may already be
sitting there ready to be contacted. A one-off cross-check script should be
run before the next outreach send.

## Known pre-existing hazards, not fixed here

- **`push_record()` PATCHes the full record dict**, and every push includes
  `"Notes": ""` and `"Status": "New"`. Re-pushing an existing channel wipes
  reviewer notes and resets workflow status. `globally_tracked_ids` mostly
  prevents this today, but it constrains where the flag reason can be stored.
- **Quota tracker undercounts.** On 2026-08-07 `quota_log.json` read 6,121
  against a ceiling of 8,000 while Google was already returning
  `quotaExceeded`. Something spends quota without calling `record_spend()`.
- **403 quota errors are indistinguishable from dead channels.**
  `get_channel_stats()` returns `None` for both, so
  `backfill_missing_emails.py` files quota failures under "private/deleted/no
  videos" and reports a clean-looking zero-result run.
