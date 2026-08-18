<!-- /autoplan restore point: /c/Users/Kate/.gstack/projects/snsnzjkt-channel-vetting/main-autoplan-restore-20260818-222709.md -->

# Channel age + upload frequency: reviewer-facing filters

Reviewed by `/autoplan` 2026-08-18. Mode: SELECTIVE EXPANSION. Voices: Claude
subagent x2 (CEO + eng). Codex unavailable — authenticated but **out of usage
quota until 2026-09-12**, so every phase below is `[subagent-only]`.

## What the user asked for

> "I need an age filter for the channel and i need to filter the channels on
> their upload frequency"

## What already exists (do not rebuild)

Both signals are already computed and already act on candidates. The gap is not
measurement — it is that **a reviewer cannot filter on either one in Airtable**.

| Signal | Computed where | Acts how | In Airtable? |
|---|---|---|---|
| Channel age | `enrichment.channel_age_months()` (`enrichment.py:535`) | `scoring.qualify()` → soft flag `"New Channel"` | **No column at all** |
| Upload cadence | `enrichment.calc_upload_frequency()` (`enrichment.py:1309`) | `MIN_UPLOADS_PER_YEAR = 6` hard discard (`main.py:352`, `main.py:554`); `MAX_DAYS_SINCE_LAST_UPLOAD = 365` | `Upload Frequency`, verified live as **`singleLineText`** — Airtable cannot filter or sort it numerically |

Also already existing, and the plan's first draft missed it: **`airtable_client.table_has_field()`**
(`airtable_client.py:157`, per-run cached at `:151`) with a working precedent at
`main.py:1194` for the `Handle` column. This is the repo's own solution to the
"unknown field name fails the whole record" hazard.

## Live measurement (2026-08-18, base `appgBTwBS36JG9ATV`)

Run before implementing, because CLAUDE.md's best calls in this repo were all
measured first. **Home Theatre – Prospects (`tblzmzZw0xiKDrNZw`), 60 rows with
reviewer verdicts:**

| | n | Uploads/month, sorted | Median |
|---|---|---|---|
| Approved | 11 | 1, 3, 4, 6, 7, 7, 13, 14, 19, 19, 30 | 7 |
| Rejected | 21 | 1, 1, 2, 3, 3, 3, 5x7, 6, 6, 7, 11, 19, 33, 38, 150 | 5 |
| New | 28 | — | — |

**Cadence does not separate Approved from Rejected.** The ranges overlap almost
entirely. "DaBuild" is Approved at 1/month (~12/yr, just over the floor);
"Zero Fidelity" and "Garrett Odom" are Rejected at 1/month. There is no
niche-level or threshold-level signal here to tune on.

**All 60 rows read `Qualification = "Qualified"`. The age flag has fired zero
times.** `min_channel_age_months: 12` has never produced a "New Channel" row.
Two consequences: the age flag is inert as a reviewer signal (which is
independent support for the user's instinct that a *column* is the useful
thing), and Home Theater's `DAILY_FLAGGED_CAP` is going unused — CLAUDE.md
currently claims that only happens on Lifestyle Sofa.

**Views: exactly one, `Grid 1` (default grid).** So the first draft's premise
that "the reviewer's saved views depend on `Upload Frequency`" is **false** —
nothing depends on it. Reviewers are not filtering at all yet, which makes the
blank-cell semantics below more important, not less.

## Confirmed premises (user, this run)

1. **Age stays a soft flag.** Do NOT move age into `pre_push_drop_reason()`.
   `qualify()`'s two-value contract is untouched. What's wanted is a numeric,
   filterable column.
2. **Cadence gets a numeric column AND a per-niche floor.** (The per-niche half
   is challenged below on the measurement — see Decisions.)

## Decisions taken at the gate (2026-08-18)

| # | Decision | Chosen |
|---|---|---|
| D1 | Per-niche cadence floor | **DEFERRED** — no reviewer signal to tune on; ship the column first and let it produce the distribution |
| D2 | Age column shape | **`Channel Created` Date + Airtable formula** — a months snapshot decays and would be wrong on every older row |
| D3 | Who creates the Airtable fields | **The user, by hand** — production schema change on tables holding 60+ live rows |
| D4 | Same-day cadence artifact | **Omit** — `calc_upload_frequency` returns `None` for a zero-width window |

## Scope

### 1. `Channel Created` — new Airtable **Date** field, both niche tables
Write `stats["published_at"]` (already fetched by `channels.list`, no arithmetic).
An Airtable **formula field** derives months on read:

```
DATETIME_DIFF(TODAY(), {Channel Created}, 'months')
```

That field is filterable and sortable, recomputed on every read, and never goes
stale. `Date Added` (`main.py:1169`) is the working precedent for writing a date
through `push_record`.

`qualify()` still needs the months value, and the first draft's snippet for it
had two runtime errors, both of which would unwind the whole run
(`process_candidate` → `push_until_full` → `run_niche` → `run` catch nothing —
the same path CLAUDE.md documents for the `ReadTimeout` and `os.replace` bugs):
`stats["published_at"]` raises `KeyError` (existing code uses
`stats.get("published_at", "")` at `main.py:1076`), and `round(None, 1)` raises
`TypeError`. Correct shape:

```python
published_at = stats.get("published_at", "")
age_months = channel_age_months(published_at)          # still feeds qualify()
qualification = qualify(age_months, niche_config["min_channel_age_months"])
...
if published_at and niche_table and table_has_field(niche_table, "Channel Created"):
    record["Channel Created"] = published_at[:10]       # YYYY-MM-DD
```

Note the write is guarded on `published_at`, not on `age_months` — a timestamp
that `channel_age_months()` cannot parse into a float is still a date Airtable
may accept, and the two guards answer different questions. Slice to `YYYY-MM-DD`
to match `Date Added`'s format (`prospect_day.today_iso()`).

### 2. `Uploads Per Year` — new Airtable Number field, both niche tables
Write the `uploads_per_year` already computed at `main.py:991`. `None` (fewer
than 2 dated uploads) omits the field.

**What this number actually measures, stated because publishing it as a filter
raises the stakes:** `upload_dates` is taken from the raw newest-10 playlist
items at `enrichment.py:799`, *before* `videos.list` is even called (line 823)
— so before Shorts classification and before `drop_duplicate_uploads()`. It
therefore counts Shorts and double-counts re-uploads, contradicting CLAUDE.md's
"What counts as a video" rules that govern the avg-views column next to it. It
is also a 10-video extrapolation, so a span of days can read as hundreds/year.
Decision: keep the raw-window definition (changing it would make the number
incomparable with the existing `Upload Frequency` text column) and **name the
column `Uploads/Yr (last 10)`** so it does not over-promise.

### 3. Per-niche cadence floor — **DEFERRED (D1)**
`MIN_UPLOADS_PER_YEAR` stays a module constant at `main.py:352`.
`pre_push_drop_reason` keeps its current signature, `NICHES` keeps four keys,
`REQUIRED_NICHE_KEYS` is untouched, and `audit_prospects.py` needs no change.
Nothing in scope now touches the cadence gate's behaviour.

Deferred because the 60-row measurement found no reviewer signal to tune on, and
because the column shipped here is the instrument that would produce one. What
this avoided, recorded so it isn't re-derived if the decision is revisited:
- Strict `niche_config["min_uploads_per_year"]` would `KeyError` on 63 hand-built
  niche config literals across 8 test files (34 reaching `process_candidate`).
  Use `.get(..., MIN_UPLOADS_PER_YEAR)`.
- Adding the key to `REQUIRED_NICHE_KEYS` (`main.py:1694`) or the duplicate check
  at `main.py:1492` breaks named tests (`test_pipeline_regressions.py:680`,
  `:176`; `test_discovery_refill.py:80`, `:174`; `test_prefilter.py:36`;
  `test_discovery_wiring.py:245`, `:307`, `:587`, `:652`) and silently rots two
  more that would then pass for the wrong reason
  (`test_pipeline_regressions.py:151`, `:203`).
- The new parameter must default to `MIN_UPLOADS_PER_YEAR`, not `0`. A `0`
  default makes `uploads_per_year < 0` always False and silently deletes the gate.
- **`audit_prospects.py:131` must change in the same commit.** Its docstring
  (`:87-95`) promises it "can never disagree" with the pipeline because it reuses
  `pre_push_drop_reason`, and it has delete authority (`delete_failures()`,
  `:209`) — a permissive default there is a script that deletes on stale rules.

### 3b. Same-day cadence artifact — **omit (D4)**
`calc_upload_frequency` currently returns `float(len(parsed))` when all sampled
uploads share one calendar day (`enrichment.py:1324`), so 10 same-day uploads
read as 10/month = 120/yr. Published as a sortable column, those artifacts rank
above genuinely prolific channels. Return `None` instead — a zero-width window is
unmeasurable, matching the project's None-is-unknown rule.

**This return-type change cascades, and every call site must be handled:**

| Site | Today | With `None` |
|---|---|---|
| `main.py:987` | `upload_freq = calc_upload_frequency(...)` | type becomes `float \| None` |
| `main.py:991` | `upload_freq * 12` | guard: `None` → `uploads_per_year = None` |
| `main.py:1139` | `f"{round(upload_freq)} videos/month"` | guard: `None` → omit or `"unknown"` |
| `main.py:1067` | `calc_overall_score(..., upload_freq, ...)` | pass `0.0`; `_normalize_upload_frequency(None)` would raise |
| gate `main.py:554` | `uploads_per_year < min` | unchanged — `None` never disqualifies |

**KNOWN CONSEQUENCE:** Overall Scores for same-day-window channels written after
this change are not comparable with ones written before (the upload-consistency
component, weight `0.15`, drops from ~100 to 0 for them). This is the same class
of note CLAUDE.md carries for the long-form averages change. Don't "fix" an
apparent score drop by reverting.

### 4. Deploy safety — use the existing guard, not a manual ordering
Guard both writes with `table_has_field(niche_table, FIELD)`, mirroring
`main.py:1192-1195`. The first draft accepted a manual "columns first, code
second" ordering whose failure mode is total row loss on both niches. The probe
cannot be forgotten and self-heals the moment the column appears. Note the
guard must be `if niche_table and table_has_field(...)` — the unconditional form
trips `tests/test_discovery_wiring.py:769` (`test_no_handle_field_probe_without_a_table_name`),
which makes `table_has_field` a `pytest.fail`.

### 5. Blank-cell semantics — write the filter shape down
Both blanks are legitimate and both channels are deliberately **kept** by the
pipeline (absent data never disqualifies). In Airtable a blank Number does not
match `>= N`, so a naive reviewer filter **hides exactly the rows the pipeline
chose to surface**, and hides all pre-deploy rows too. Document the blank-safe
form in the README schema section:

```
OR({Uploads/Yr (last 10)} = BLANK(), {Uploads/Yr (last 10)} >= 6)
```

CLAUDE.md already records that `{Field} != BLANK()` does not behave as expected
on empty date fields in this base (measured 2026-08-14), so blank handling here
is a known landmine and must not be left for a reviewer to rediscover.

### 6. Fix in the blast radius (both pre-existing, both in the function being elevated)
- **`calc_upload_frequency` uses strict `datetime.strptime` (`enrichment.py:1321`)**
  and raises `ValueError` on any timestamp with fractional seconds or a `+00:00`
  offset. `days_since_last_upload` next door (`:564`) routes through the tolerant
  `_parse_iso_timestamp`, which `enrichment.py:516` calls "the single home of the
  tolerant-parse rule… so the two can't drift". They drifted. One malformed
  `videoPublishedAt` ends the run mid-niche with quota already spent. Two-line
  fix; in scope because this plan makes the function's output reviewer-facing.
- **Stale docs:** `README.md:396` and `DISCOVERY_CREDIT_PLAN.md:97` both say
  `MIN_UPLOADS_PER_YEAR (10)`; it is 6. CLAUDE.md says 911 tests; 925 collect.

### 7. Backfill
`Channel Created` (or age) for the ~80 existing rows costs **2 quota units** —
`channels.list` takes 50 IDs per 1-unit call, and `backfill_handles.py` is a
ready-made template. Without it every new filter hides all historical rows.
Cadence backfill is dearer (~2 units/channel, ~160 total) and is optional.

## Architecture

```
  process_candidate()                                    NEW / CHANGED
  ┌──────────────────────────────────────────┐
  │ get_channel_stats()                      │
  │   └─ stats.get("published_at","")        │
  │        ├─ channel_age_months() ──────────┼──▶ age_months ──▶ qualify() (UNCHANGED)
  │        └─ [:10] ─────────────────────────┼──▶ record["Channel Created"]  ★NEW
  │                                          │         └─▶ Airtable FORMULA derives months
  │ get_recent_video_performance()           │
  │   └─ performance["upload_dates"]         │
  │        (raw newest-10, pre-Shorts,       │
  │         pre-dedupe — enrichment.py:799)  │
  │        └─ calc_upload_frequency() ───────┼──▶ upload_freq: float | None  ★CHANGED
  │             ★tolerant-parse fix          │       ├──▶ "Upload Frequency" text (guard None)
  │             ★None on zero-width window   │       ├──▶ calc_overall_score(0.0 if None)
  │                                          │       └─▶ uploads_per_year (None if None)
  │ pre_push_drop_reason(                    │             ├──▶ record["Uploads/Yr…"]  ★NEW
  │   uploads_per_year=...,                  │             └──▶ cadence gate (UNCHANGED)
  │   MIN_UPLOADS_PER_YEAR )   ◀── D1 deferred, still the module constant
  └──────────────────────────────────────────┘
                     │
                     ▼  both ★NEW writes gated by:
        table_has_field(niche_table, FIELD)   ◀── existing, airtable_client.py:157
                     │                             precedent: main.py:1194 (Handle)
                     ▼
              push_record()  ── PATCH if exists, else POST

  NOT TOUCHED (D1 deferred): audit_prospects.py:131, REQUIRED_NICHE_KEYS,
  main.py:1492, pre_push_drop_reason's signature, NICHES key count.
```

Shadow paths for both new writes:

| Path | Input | Behaviour |
|---|---|---|
| Happy | `published_at="2019-04-02T…"`, `uploads_per_year=84.0` | date + number written |
| Nil | `published_at` absent → `""` | field omitted; row still written; channel kept |
| Empty | `<2` dated uploads, or zero-width window → `None` | field omitted; cadence gate skipped (`main.py:554`) |
| Error | column missing / probe fails | `table_has_field` False → field omitted, row still written (no 422, no row loss) |

## Error & Rescue Registry

| Codepath | What can go wrong | Exception | Rescued? | Action | Reviewer sees |
|---|---|---|---|---|---|
| `round(age_months, 1)` | `age_months is None` | `TypeError` | **Y (fixed)** | `is not None` guard, omit field | blank cell |
| `stats[...]` | key absent | `KeyError` | **Y (fixed)** | `.get(..., "")` | blank cell |
| `calc_upload_frequency` | odd timestamp format | `ValueError` | **Y (fixed §6)** | tolerant parse, drop bad entries | correct number |
| `table_has_field` | transient network error | `RequestException` | Y (existing) | logs warning, assumes absent, **caches for the run** | blank cells for the whole day, WARNING only |
| `push_record` | column missing/renamed | none (422) | Y | logged, record fails, **no counter increments** | row silently never appears |
| `push_record` | column created as Formula/Rollup | none (422) | N ← **GAP** | — | whole record rejected |
| `push_record` | column created as Text | none (200) | N ← **GAP** | `typecast=True` coerces to string | column sorts `"10" < "6"` — the exact bug being fixed |

## Failure Modes Registry

| Codepath | Failure mode | Rescued? | Test? | Reviewer sees | Logged? |
|---|---|---|---|---|---|
| age write | `None` age | Y | planned T7 | blank cell | N (correct) |
| cadence write | `None` cadence | Y | planned T7 | blank cell | N (correct) |
| both writes | column absent | Y (§4) | planned T6 | blank cell | probe warning |
| both writes | **field created as wrong TYPE** | **N** | **N** | **unfilterable column, feature silently useless** | **N** | ← **CRITICAL GAP**, mitigated by the D3 verification step |
| `table_has_field` | probe error cached run-wide | partial | N | a day of blank cells | WARNING only |
| reviewer filter | blank excluded by `>= N` | N (doc only, §5) | N/A | rows vanish from the queue | N |
| whole run | schema mismatch on every push | N | N | **0 rows, full credit burn, exit code 0** | errors only | ← pre-existing, see below |

**Pre-existing critical gap, one level up (out of scope, TODO):** if every push
fails, `push_until_full` (`main.py:819`) increments no counter, the refill loop
keeps buying candidates, the "finished under budget" warning is gated
`if not use_discovery` (`main.py:1676`) so it never fires on the primary
influencers.club path, and `any_cap_check_completed` is True so the run
**exits 0**. The §4 guard removes this plan's exposure to it; the generic hole
stays open.

## Airtable fields to create (D3 — by hand, both niche tables)

`tblzmzZw0xiKDrNZw` (Home Theatre – Prospects) and `tblUtCymzl7Qjmlh4`
(Lifestyle – Sofa Prospects). Create all three on **both** tables — README:84-88
documents manual per-table setup, so per-table schema drift is the normal state
and one table getting only two of three is the likely mistake.

| Field name (exact) | Type | Notes |
|---|---|---|
| `Channel Created` | **Date** | ISO format. Written by the pipeline. |
| `Channel Age (Months)` | **Formula** | `DATETIME_DIFF(TODAY(), {Channel Created}, 'months')`. Never written by the pipeline — it must stay computed, or it decays. |
| `Uploads/Yr (last 10)` | **Number**, precision **1** | Written by the pipeline. |

Two type mistakes silently defeat the whole feature and neither raises an error:
- Creating `Uploads/Yr (last 10)` as **Single line text** — `typecast=True`
  coerces the number to a string, `table_has_field` still reports present, and
  the column sorts lexicographically (`"10" < "6"`), which is exactly the bug
  being fixed.
- Creating it as **Formula or Rollup** — the probe passes, then every
  `push_record` 422s on writing a computed field, failing the **whole record**.

After creating them, verification is one MCP `get_table_schema` call per table;
confirm the types read back as `date`, `formula`, `number`.

## Test plan

New tests (patterned on existing ones so a mis-wire cannot pass unseen):

| # | Test | Mirrors |
|---|---|---|
| T1 | both new fields written when columns present | `test_discovery_wiring.py:745` (Handle) |
| T2 | neither field written when columns absent | `test_discovery_wiring.py:757` |
| T3 | no probe when `table_name` missing from config | `test_discovery_wiring.py:769` |
| T4 | empty `published_at` omits `Channel Created`, does not raise | new (F12 regression) |
| T5 | `None` cadence omits the field; gate still skipped | `test_prepush_gate.py:605` |
| T6 | `Uploads/Yr` value is numeric, `Channel Created` is `YYYY-MM-DD` | `main.py:1123` contract |
| T7 | neither field is `csv_safe()`-wrapped | `test_csv_injection.py:375` |
| T8 | `calc_upload_frequency` tolerates fractional-second / offset timestamps | `enrichment.py:510` rule |
| T9 | `calc_upload_frequency` returns `None` for a zero-width window (D4) | new |
| T10 | the `None` cadence cascade: no `TypeError` in the display string, `uploads_per_year is None`, `calc_overall_score` still returns a float | new (D4 regression) |
| T11 | the pipeline never writes `Channel Age (Months)` (it is a formula field) | new |

Regression command, before and after: `python -m pytest` (925 baseline).
CLAUDE.md mandates this for any change to the pre-push gate or qualification.

## Implementation Tasks

**T0 is a prerequisite for T1/T2 landing usefully, but not for them landing safely** —
the `table_has_field` guard means code merged before the columns exist is a no-op
that self-heals when they appear.

- [ ] **T0 (P1, human: ~15min / CC: n/a)** — Airtable — Create the 3 fields by hand on both niche tables, then verify types
  - Surfaced by: D3 + eng F14 / CEO #9 — a Text or Formula field silently defeats the feature or fails every record
  - Files: (Airtable schema — see "Airtable fields to create" above)
  - Verify: `get_table_schema` reads back `date`, `formula`, `number`
- [ ] **T1 (P1, human: ~30min / CC: ~5min)** — main.py — Write `Channel Created`, guarded; hoist `published_at` to a local
  - Surfaced by: D2 + eng F12 / CEO #6 — `round(None,1)` and `stats[...]` both unwind the whole run
  - Files: `main.py`
  - Verify: `python -m pytest tests/test_pipeline_regressions.py`
- [ ] **T2 (P1, human: ~20min / CC: ~5min)** — main.py — Write `Uploads/Yr (last 10)`, guarded by `table_has_field`
  - Surfaced by: eng F1 / CEO #5 — repo already has the guard; manual deploy ordering risks total row loss
  - Files: `main.py`
- [ ] **T3 (P1, human: ~30min / CC: ~10min)** — enrichment.py/main.py — `calc_upload_frequency`: tolerant parse **and** `None` on a zero-width window, plus all 4 cascade call sites
  - Surfaced by: eng F11 (run-killer) + D4 (sortable artifact). Carries a KNOWN CONSEQUENCE note for Overall Score
  - Files: `enrichment.py`, `main.py`
- [ ] **T4 (P2, human: ~1h / CC: ~10min)** — tests — Add T1-T11 from the test plan
  - Surfaced by: eng F13 — plan had no test section, in a repo that mandates one
  - Files: `tests/test_pipeline_regressions.py`, `tests/test_discovery_wiring.py`, `tests/test_csv_injection.py`, `tests/test_enrichment_resilience.py`
- [ ] **T5 (P2, human: ~20min / CC: ~5min)** — backfill — Backfill `Channel Created` over the ~80 existing rows (2 quota units)
  - Surfaced by: CEO #2 — otherwise every new filter hides all historical rows
  - Files: new one-off script, `backfill_handles.py` as template
- [ ] **T6 (P2, human: ~20min / CC: ~5min)** — docs — README schema list + blank-safe filter form; fix `MIN_UPLOADS_PER_YEAR (10)`→6 and 911→925
  - Surfaced by: eng F15 / F9 — three stale doc sites, and the blank trap is undocumented
  - Files: `README.md`, `CLAUDE.md`, `DISCOVERY_CREDIT_PLAN.md`

## NOT in scope

- **Making age a discard gate** — premise 1 explicitly rejects it.
- **Per-niche cadence floor (D1)** — deferred; no reviewer signal to tune on yet. See §3 for the trap list if revisited.
- **Changing the 6/yr value** — the 60-row measurement shows no reviewer signal to tune on.
- **Writing `Channel Age (Months)` from Python** — it is a formula field by design (D2). Writing it would reintroduce the decay.
- **Redefining cadence over long-form/de-duplicated uploads** — would make the number incomparable with the existing `Upload Frequency` column; needs its own `KNOWN CONSEQUENCE` pass.
- **Retyping `Upload Frequency` from text to Number** — no view depends on it (verified), but historical rows hold the text values.
- **Probing influencers.club for vendor-side cadence/age filters** — costs real credits against a live paid API; deferred to TODOS (it is plausibly the higher-leverage fix: 0.01/creator billed *before* any gate sees them).
- **The "zero rows, exit 0" observability hole** (`push_until_full` counters, the `if not use_discovery` warning gate) — pre-existing, one level up, deferred to TODOS.
- **Same-day cadence artifact** (`enrichment.py:1324` returns `float(len(parsed))`, so 10 same-day uploads read as 120/yr) — see D4.

## Dream state delta

12-month ideal: reviewers filter and sort the queue on every criterion the
pipeline measures, and the pipeline never pays for a creator who cannot become a
row. This plan delivers the first half for two criteria and, by writing the
numbers down, produces the distribution needed to decide thresholds on evidence
rather than assertion. It does nothing for the second half — vendor-side
filtering stays unprobed, and discovery keeps billing 0.01 per returned creator
before any gate looks.

<!-- AUTONOMOUS DECISION LOG -->
## Decision Audit Trail

| # | Phase | Decision | Class | Principle | Rationale | Rejected |
|---|---|---|---|---|---|---|
| 1 | Phase 0 | Mode = SELECTIVE EXPANSION | Mechanical | /autoplan | Enhancement to an existing system | EXPANSION, HOLD, REDUCTION |
| 2 | Phase 0 | Skip design review | Mechanical | P3 | No UI; grep hits are "long-form"/Playwright navigation | Running it |
| 3 | Phase 0 | Skip DX review | Mechanical | P3 | Internal ops pipeline; no SDK/API/installable surface; users are reviewers in Airtable | Running it |
| 4 | Phase 0 | Verify live Airtable schema before planning writes | Mechanical | P1 | Unknown field name fails the whole record | Assuming schema |
| 5 | CEO | Measure Status x cadence on 60 live rows before tuning | Mechanical | P1 | Repo convention: measure before shipping a threshold | Asserting the need |
| 6 | CEO | Enumerate views to test the "saved views" premise | Mechanical | P1 | Premise was load-bearing and unverified; proved false | Leaving it assumed |
| 7 | Eng | Guard both writes with `table_has_field` | Mechanical | P4 DRY | Repo already solved this for `Handle`; removes total-row-loss risk | Manual deploy ordering |
| 8 | Eng | `.get(..., MIN_UPLOADS_PER_YEAR)` not strict indexing | Mechanical | P3 | Strict form KeyErrors on 63 test config literals | Strict indexing |
| 9 | Eng | Do NOT add the key to `REQUIRED_NICHE_KEYS` | Mechanical | P3 | Breaks 9 named tests, silently rots 2 more | Adding it |
| 10 | Eng | Default the new param to `MIN_UPLOADS_PER_YEAR`, fail closed | Mechanical | P5 | `= 0` silently deletes the gate for any forgotten caller | `= 0` |
| 11 | Eng | Fix the plan's snippet (`.get`, None guard, hoisted local) | Mechanical | P5 | Two runtime errors that unwind the whole run | Shipping as written |
| 12 | Eng | Add `audit_prospects.py:131` to scope | Mechanical | P2 | Same-function caller with delete authority | Leaving it out |
| 13 | Eng | Fix strict `strptime` in `calc_upload_frequency` | Taste→auto | P2 boil lakes | In blast radius, 2 lines, is a run-killer | Deferring |
| 14 | Eng | Keep the raw-window cadence definition, rename the column | Taste→auto | P3 | Redefining makes it incomparable with the existing text column | Redefining now |
| 15 | Eng | Add the 8-test set | Mechanical | P1 | Repo mandates tests for gate/cap changes | Shipping untested |
| 16 | Eng | Backfill age (2 quota units), cadence optional | Mechanical | P1 | Otherwise every filter hides all 80 historical rows | No backfill |
| 17 | Eng | Document the blank-safe filter form | Mechanical | P1 | Blank Number does not match `>= N`; hides deliberately-kept rows | Leaving it to the reviewer |
| 18 | Eng | Defer the vendor-side filter probe to TODOS | Mechanical | P6 | Costs real credits on a live paid API; needs consent | Probing now |
| 19 | Eng | Defer the "zero rows, exit 0" observability hole to TODOS | Mechanical | P3 | Pre-existing, one level up, outside this blast radius | Fixing here |
| 20 | Phase 0 | Fix stale docs in the same pass | Mechanical | P2 | README says cadence floor is 10 (it is 6); CLAUDE.md says 911 tests (925) | Leaving stale |
| 21 | Gate | D1: defer the per-niche cadence floor | **User** | measurement | 60 rows show cadence does not separate Approved from Rejected | Shipping it now |
| 22 | Gate | D2: `Channel Created` Date + Airtable formula | **User** | P1 | A months snapshot is wrong on every row older than the delta | Months-as-Number |
| 23 | Gate | D3: user creates the Airtable fields by hand | **User** | safety | Production schema change on tables holding 60+ live rows | Agent via MCP |
| 24 | Gate | D4: `None` for a zero-width cadence window | **User** | P5 | Same-day dumps would rank above prolific channels in a sort | Leaving the artifact |

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 1 | clean | 13 findings (1 critical, 4 high), all resolved or deferred with rationale |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | unavailable | quota exhausted until 2026-09-12 |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | clean | 15 findings (2 critical, 5 high); 1 critical gap closed by the D3 verification step |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | skipped | no UI scope |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | skipped | no developer-facing scope |

- **CROSS-MODEL:** Codex unavailable (authenticated, out of usage quota until 2026-09-12). Both voices are Claude subagents with independent context and different lenses; 7 findings were raised independently by both. Genuine cross-model coverage is absent this run — treat single-voice findings accordingly, and consider re-running `/codex review` against the diff after 2026-09-12.
- **VERDICT:** CEO + ENG CLEARED — ready to implement. 6 tasks, T0 (Airtable fields) is the user's step and gates the feature working, not the code landing safely.

NO UNRESOLVED DECISIONS
