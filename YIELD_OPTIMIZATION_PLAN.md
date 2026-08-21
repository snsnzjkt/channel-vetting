# Yield Optimization Plan — more qualified rows, same quality bar

> ## ⚠ REVISION 2 (2026-08-22) — the v1 baseline was WRONG. Read this first.
>
> Dual-voice review found factual errors in v1's own numbers. All are VERIFIED
> against the repo, not taken on the reviewer's word. What changed:
>
> | v1 claimed | Truth | Source |
> |---|---|---|
> | ~64 HT creators already tracked/rejected | **320 entries** (262 real + 58 synthetic) | `rejected_handles.json` |
> | Pools of 334 / 2,846 are addressable | Those are **GROSS**. `measure_discovery_pool.py` never sends `exclude_handles`, so net-addressable was never measured | `measure_discovery_pool.py:29-35` |
> | 40 rows/day ceiling, ~13% utilisation | Cap is **per niche table** -> 80/day, ~5% utilisation | `main.py:2065-2072` |
> | ~160 rows/month credit ceiling | Credits also fund email lookups; ~120 rows | `config.py` |
> | "3 runs before/after" is measurable | Ledger holds **one day** of spend | `credit_log.json` |
>
> **Consequence: Home Theater's NET addressable pool is plausibly under 70, not
> ~270.** v1's headline change (C1, query portfolio) was sized against a number
> that was wrong by 4x, and is hereby DEMOTED from load-bearing to last resort.
>
> Two further findings v1 missed entirely:
> - **The FREE path exists and is dead code.** `use_discovery` sets
>   `remaining_keywords = []` (`main.py:2142`), so Home Theater's 9 curated
>   YouTube `search.list` keywords never run while the vendor key is present.
>   They cost **zero credits** and index a different corpus (YouTube, not the
>   vendor DB). For a niche declared exhausted on the paid source, this is the
>   obvious lever and v1 never considered it.
> - **Pool probes spend off-ledger.** `_post` bypasses both `can_afford` and
>   `record_spend` (those live only inside `discover()` at
>   `influencer_discovery.py:261,282`). Every `measure_discovery_pool.py` run
>   spends real credits the ledger never sees.


Authored 2026-08-22 by `/autoplan` after a full-pipeline audit.
Baseline figures are MEASURED (repo instrumentation + prior sessions) unless
marked DERIVED. Nothing in this plan lowers a quality gate.

---

## 1. Baseline funnel (per run, both niches)

```
  ADDRESSABLE POOL (what one ai_search string can reach)
    Home Theater      334      Lifestyle Sofa    2,846      total 3,180
        |
        |  shared 6-credit budget = 600 creators/run  <-- LS bound HERE
        v
  EXAMINED                 600 creators
        |
        |  ~99% die at the gates (1 row per 100-150 examined, measured)
        v
  PUSHED                   4-6 rows
        |
        |  Gemini rescue ladder (rescue-only, can only re-admit)
        v
  FINAL OUTPUT             4-6 rows/run   against DAILY_QUALIFIED_CAP 30 + FLAGGED 10
```

**Utilisation: 4-6 of a 40-row daily ceiling. ~12-15%.**

Ceilings that bind above the daily cap:
- `INFLUENCERS_MAX_CREDITS_PER_MONTH = 200` -> 20,000 creators/month
  -> DERIVED **~160 rows/month absolute maximum** at the measured yield rate.
- `INFLUENCERS_MAX_CREDITS_PER_DAY = 10` -> 1,000 creators/day -> ~8 rows/day.

So the daily cap of 40 is unreachable by construction: the credit ledger
caps the pipeline at roughly a fifth of it before any gate runs.

---

## 2. Where the candidates actually go

Two DIFFERENT bottlenecks, which is why past single-lever fixes underdelivered.

### 2a. Home Theater is POOL-bound (not gate-bound)
`_run_discovery_rounds` reads `filters = niche_config["discovery_filters"]`
**once** and reuses that one `ai_search` string for every round. When the vendor
exhausts it, `dry = True` and the niche stops. Total reachable universe = 334,
of which ~64 are already tracked or in `rejected_handles.json`.

At 1 row per 100-150 creators, **that entire string is worth 1-2 rows, ever.**
Loosening gates cannot fix this; there is nothing left to loosen against.

### 2b. Lifestyle Sofa is BUDGET-bound
Pool 2,846, but only 600 creators/run are affordable. 79% of its pool is
never examined.

### 2c. The starved niche is served FIRST from a shared budget
`InfluencerDiscovery` is constructed **once** (`main.py:2346`) with a single
`max_credits`, then shared. Niches iterate in dict order
(`main.py:2381`), so **Home Theater always goes first** — spending requests
against a near-dry pool before Lifestyle, the niche with 2,846 available
creators and the better yield, gets a look. No per-niche reservation exists.

### 2d. The gates are mostly legitimate
`audit_prospects.py` over 107 tracked rows (78 pass / 29 fail) ranks them:
`outside_search_zone` 11, `no_declared_country` 5, `broadcast_tv` 3,
`below_view_minimum` 3, `too_few_longform` 2, `shorts_only` 2, others 1 each.
Only the top two are worth revisiting, and both are BRIEF decisions rather
than engineering ones (section 5).

### 2e. No per-run metrics are persisted
Drop reasons are logged to stdout and never written to disk. There is no
run-history file, so "before -> after" cannot currently be evidenced. This
blocks the measurement the brief asks for.

---

## 3. Proposed changes, highest impact first

### C1 — Query portfolio per niche (THE headline change)
**What:** `discovery_filters` grows an optional `ai_search_variants: [...]`
list. `_run_discovery_rounds` rotates variants per vendor request, unioning
results by handle (the existing `seen_handles` dedupe already handles overlap),
and only declares `dry` when **every** variant is exhausted.

**Why it raises useful output:** the pool ceiling is a property of one string,
not of the niche. The repo's own probes show wordings return materially
different creator sets (209 / 250 / 125 / 95 for HT, with disjoint top-20
relevancy lists — AV channels vs builders). Union of 4-5 individually vetted
strings plausibly takes HT from 334 to 700-1,000 addressable **without
admitting one off-persona creator**, because each variant is vetted on WHO it
returns, per the standing rule in `niches.py`.

**API usage:** NEUTRAL at a fixed credit budget. Same 600 creators/run — drawn
from a wider pool that no longer runs dry. This is the key property: it buys
volume without buying credits.

**Quality:** neutral if each variant is vetted; the risk is a sloppy variant.
Mitigated by C6 (vetting probe is mandatory before a variant ships).

**Downside:** more moving parts in the discovery loop; a bad variant silently
dilutes the pool. C6 is the guard.

### C2 — Per-niche credit reservation
**What:** split the per-run budget by remaining pool headroom instead of
first-come. Floor each niche at a minimum reservation so neither is starved.

**Why:** stops HT burning the shared budget against a dry pool before LS —
which today is pure waste, since HT requests return mostly-excluded pages.

**API usage:** neutral. **Quality:** none. **Downside:** none material.

### C3 — Persist a per-run funnel record
**What:** append one JSON line per run to `run_metrics.jsonl`: pool estimate,
creators discovered/examined, drop-reason counts, rows pushed, Gemini rescued,
credits spent, wall-clock.

**Why:** the brief requires before -> after evidence. Today it is unmeasurable.

**API usage:** none. **Quality:** none. This is instrumentation only, and it
is what makes every other change in this plan verifiable.

### C4 — Raise the discovery budget (DEFERRED by D2 — revisit with metrics)
6 -> 12 credits/run doubles examined creators to 1,200/run.
DERIVED: ~8-12 rows/run. Requires the day cap (10) to rise with it, or the
second run of a day gets refused.
**The only lever with zero quality cost. It is purely a spend decision.**

### C5 — Retire the day/month ceilings as the true cap (DEFERRED by D2)
`MAX_CREDITS_PER_MONTH = 200` caps output at ~160 rows/month regardless of
every other change here. If the target exceeds that, this constant is the
binding one and no amount of tuning moves it.

### C6 — Variant vetting probe (guard for C1)
Extend `measure_discovery_pool.py` to print the top-20 BY RELEVANCY per
variant, not just totals — the repo's own rule is "judge a reword on who it
returns, not how many". Ship no variant that fails this read.

---

## 4. Explicitly NOT proposed

- Lowering `MIN_AVG_VIEWS`, `MIN_VIEWS_PER_VIDEO_RATIO` (already 0.50->0.30),
  or any performance gate. The measured failure distribution says they are not
  where the yield is.
- Loosening `GEMINI_MIN_CRITERIA_RATIO` below 0.5 or weakening the
  creator-vs-brand veto. Prior backtesting established that veto as load-bearing.
- Any paid Gemini usage. The free-only constraint and the model-rotation
  fallback stay exactly as they are.
- Relaxing the AI prompt to say "yes" more often. The Gemini tier is
  rescue-only and already cannot remove a candidate; making it more permissive
  raises false positives against reviewer attention with no volume upside,
  because it is not the binding constraint.

---

## 5. Operator decisions (NOT auto-decided — these are brief questions)

| # | Lever | Measured effect | Why it is yours |
|---|-------|-----------------|-----------------|
| ~~O1~~ | ~~Drop the `gender` filter~~ | ~~HT 334 -> 1,299~~ | **CLOSED 2026-08-22: operator kept the filter** |
| O2 | Restore Europe zone for HT (`ZONE_CORE \| EUROPE_COUNTRY_CODES`) | 11 of 29 failures | Narrowing came from an instruction naming *Lifestyle*; may be restoring original intent |
| O3 | Keep channels declaring no country | 5 of 29 failures | Breaks the repo's own "absent data never disqualifies" rule |
| O4 | Raise credit budget (C4/C5) | 2x examined per doubling | **DEFERRED 2026-08-22: revisit with `run_metrics.jsonl` evidence** |

---

## 5b. PREMISE GATE — operator answers (2026-08-22)

Asked before implementation. These are settled; do not re-litigate.

| # | Question | Answer | Consequence for this plan |
|---|----------|--------|---------------------------|
| D1 | Target, and is Home Theater worth saving? | **Volume, keep BOTH niches** | C1 must work for Home Theater specifically; retiring the niche is off the table |
| D2 | Raise the credit budget? | **Hold at 6/run. Ship free levers first, revisit with data** | C4 and C5 are DEFERRED, not rejected. The ~160 rows/month ceiling stands for now |
| D3 | Drop the `gender` filter on Home Theater? | **KEEP the filter** | O1 is CLOSED. The 334 -> 1,299 lever is not available |

**What these three answers do to the plan, stated plainly.**

Budget is frozen (D2) and the single biggest measured pool lever is withdrawn
(D3), while Home Theater must still improve (D1). That removes every lever for
Home Theater except one.

**C1 (query portfolio) is now load-bearing.** It is not the best of several
options any more; it is the only remaining path to more Home Theater rows. If
the union of vetted variants does not materially exceed 334, this plan cannot
deliver more Home Theater output and that finding must be reported rather than
worked around by quietly loosening a gate.

Corollaries:
- C2 (per-niche reservation) gains importance: with a frozen budget, wasting
  credits on a dry niche is now the difference between rows and no rows.
- C3 (metrics) gains importance: it is the evidence that reopens D2.
- The honest failure mode is explicit. If C1 lands and Home Theater still
  produces ~0, the answer is D2 or D3, and the plan should say so rather than
  reach for the performance gates, which the measured failure distribution
  says are not the problem.

---

## 6. Test plan

- Unit: variant rotation exhausts all variants before `dry`; dedupe holds
  across variants; per-niche reservation math; metrics writer schema.
- Regression: existing suite green, especially `test_discovery_wiring`
  (learnings warn: threading through `run` -> `run_niche` ->
  `_run_discovery_rounds` -> lambda needs FOUR signatures updated).
- Guard: new consumers of `get_recent_video_performance` must tolerate a bare
  `_stub_performance()` (missing video keys) — read every field with `.get()`.
- Live: one `measure_discovery_pool.py` run (~0.1-0.3 credits) to size the
  union before shipping variants.

## 7. Success metric

Rows per run and rows per credit, from `run_metrics.jsonl`, over 3 runs
before and 3 runs after. Target: **HT stops returning zero**, and combined
rows/run moves from 4-6 toward 10+ at unchanged rows-per-credit or better.
Rows-per-credit falling is the signal that a variant is diluting the pool.

---

## 8. REVISED change set (supersedes section 3)

Ordered by evidence strength, not by ambition. Zero-credit changes first.

### P0 — Fix what is broken regardless of direction
- **P0a. Route pool probes through the ledger.** Add `InfluencerDiscovery.probe(filters, limit)`
  that calls `can_afford` + `record_spend(kind=KIND_DISCOVERY)`. Point
  `measure_discovery_pool.py` and `measure_query_union.py` at it. Closes an
  off-ledger spend hole that the ledger's whole design premise forbids.
- **P0b. Purge test contamination from production state.** 58 synthetic
  handles (`a0`..`a57`, all stamped 2026-08-20) sit in the live Home Theater
  reject cache. A test wrote to a real state file. Remove them and add the
  guard that stops it recurring.
- **P0c. Measure NET addressable pool.** One probe per niche with
  `exclude_handles` populated. ~0.02 credits. **This may invalidate the rest of
  this plan, which is why it runs first.**

### P1 — Ship instrumentation ALONE, then take a real baseline
- **P1. `run_metrics.jsonl`** (was C3), with the review's corrections:
  written in `run()`'s `finally` (`main.py:2412`) so a crashed or
  `SystemExit(1)` run still records; `status` completed/aborted;
  `schema_version`; **per-niche breakdown** (a combined total cannot show "HT
  stopped returning zero"); config-in-effect; `rows_per_credit` derived at read
  time, never stored. Requires plumbing a `drop_reasons` Counter through
  `push_until_full` -> both call sites -> `_run_discovery_rounds` ->
  `run_niche` -> `run`. That is the real multi-signature change; C1's was zero.

### P2 — The free lever for Home Theater (replaces C1 as primary)
- **P2. Per-niche discovery source.** Make `use_discovery` a per-niche choice
  rather than a global exclusivity. Home Theater runs its 9 curated
  `search.list` keywords (zero credits, different corpus, audience-framed
  vocabulary that matches what the reviewer actually approves); Lifestyle keeps
  paid discovery, where it converts. Costs YouTube quota, not vendor credits.

### P3 — Defects the reviewer has already voted on
- **P3. Restore `ZONE_CORE | EUROPE_COUNTRY_CODES` for Home Theater.**
  `PROSPECT_AUDIT_2026-08-20.md` shows the current zone drops three
  already-**Approved** HT channels. This is a regression report, not an open
  question — the narrowing came from an instruction naming *Lifestyle*.

### P4 — Last resort, behind a flag
- **P4. Query portfolio (was C1).** Only if P0c shows real net headroom. Ship
  **one** variant behind a flag, judged on **reviewer approval rate**, never on
  rows-per-credit. Implementation constraints from review, all mandatory:
  variants live as a SIBLING of `discovery_filters` (a key inside it ships to
  the vendor -> 400 -> silent zero rows, `influencer_discovery.py:233` has no
  allowlist); one `discover()` per loop iteration with `seen_handles` refreshed
  between (batch-unioning within a round re-bills the overlap);
  `if not backlog` must `continue` to the next variant, not `break`
  (`main.py:1947`); dry-variant set to stop re-querying; handle
  `discovery_filters` with no `ai_search` and empty variant lists without
  raising.

### Dropped from v1
- **C2 (per-niche credit reservation)** — the review is right that it is
  wrong-signed: a floor guarantees spend to the niche with the worst measured
  conversion. Revisit only after P1 gives real per-niche numbers.

## 9. Metric correction

v1 proposed rows/run and rows/credit. **Both are wrong as a quality guard.** A
diluting variant that returns many mediocre-but-gate-passing channels *raises*
rows-per-credit while lowering approval rate — the alarm reads green in exactly
the failure it exists to catch.

The guard is **reviewer approval rate on pushed rows**, backtested against the
146 existing labelled rows via `backtest_relevance.py`. That corpus already
exists and costs nothing.

---

## 10. IMPLEMENTED 2026-08-22 — what actually shipped

Operator gate: **P0, P1, P2, P4 approved; P3 (Europe zone) declined.**

### Measured before building (P0c)

`measure_discovery_pool.py --net`, the new exclusion arm:

| niche | gross | rejects | **net** | buyable |
|---|---|---|---|---|
| Home Theater | 334 | 262 | **279** | 84% |
| Lifestyle Sofa | 2,846 | 93 | **2,814** | 99% |

This corrected BOTH earlier estimates. v1 said ~270 net by luck off a wrong
reject count; the review predicted "under 70". Neither was right. Excluding 262
rejects removed only 55 creators, because those rejects accumulated under the
OLD broader query (before the 2026-08-21 "gaming setup" removal took it
588 -> 209) and barely intersect today's pool.

Also established: the vendor DOES apply `exclude_handles` when computing
`total`, so the instrument is sound. The script says so explicitly if that ever
stops being true.

### Shipped

| ID | Change | Files |
|----|--------|-------|
| P0a | `InfluencerDiscovery.probe()` — measurement requests now go through `can_afford` + `record_spend` | `influencer_discovery.py`, both measure scripts |
| P0b | Autouse `isolate_rejected_handles` fixture; purged 58 synthetic handles from production state | `tests/conftest.py`, `rejected_handles.json` |
| P0c | `--net` arm measuring net addressable pool | `measure_discovery_pool.py` |
| P1 | `run_metrics.jsonl` — per-run, per-niche yield record written in `run()`'s `finally` | `run_metrics.py` (new), `main.py` |
| P1b | `drop_reasons` Counter plumbed through both `push_until_full` call sites | `main.py` |
| P2 | `discovery_source` per niche; Home Theater -> free `search_list` | `main.py`, `niches.py` |

### P4 (query portfolio) — NOT SHIPPED, and why

P4's own precondition was "only if P0c shows real net headroom." It does not:

- **Home Theater** has 279 net creators, about two rows at the observed rate,
  and is now on the free keyword path where a vendor query portfolio does not
  apply at all.
- **Lifestyle Sofa** has 2,814 net but is BUDGET-bound, not pool-bound. It can
  only examine ~600 creators per run whatever the pool size. A wider pool
  cannot help a niche that cannot afford the pool it already has.

So P4 helps neither niche under the current constraints. `measure_query_union.py`
is committed, ledgered and ready: run it if the budget is ever raised (D2
reopened), which is the condition that would make Lifestyle pool-bound.

### Tests

1207 baseline -> **1231 passing**, 24 added, zero regressions.
New: `tests/test_probe_ledger.py` (8), `tests/test_run_metrics.py` (10),
`tests/test_discovery_source.py` (6).

One existing test needed a real fix, not a rewrite:
`test_the_niche_filters_reach_the_vendor_payload` used Home Theater to check
vendor payload wiring and now falls through to the keyword loop. It forces
`discovery_source="influencers"` explicitly, keeping its original intent.

### Not done, deliberately

- **P3 Europe zone** — declined at the gate.
- **Gender filter** — kept, no flag, per operator decision.
- **Autouse isolation for `gemini_log.json`** — same latent gap as the rejected
  cache, but the Gemini tests already patch their own path, so there is no
  active bug. Flagged rather than built, to stay in scope.

---

## 11. RESULT — full 9-keyword Home Theater run, 2026-08-21

```
  BEFORE (paid vendor path)          AFTER (free search.list path)
  candidates examined   ~5-50        342
  rows written           0-2         7   (5 qualified, 2 flagged)
  discovery credits      up to 6     0
  total credits          ~6          0.24  (0.04 probes + 0.20 email enrichment)
  wall clock             -           12m17s
  YouTube quota          -           4,122 / 10,000
```

Home Theater went from producing zero to producing seven in a single run, at
zero discovery cost. The supply problem is solved.

### Where the 342 went

```
  outside_search_zone        106   <- geography, 64% of all drops with the next
  no_declared_country         67   <- line. NOT CHANGED, by operator instruction.
  below_view_minimum          42
  duplicate                   34
  shorts_only                 18
  non_english_description     11
  excluded_topic               8
  too_few_longform_videos      5
  too_few_videos               4
  off_target_niche             3   <- the relevance gate, after the fix
  not_english                  3
  blocked                      3
  video_below_view_minimum     2
```

The relevance gate accounted for **3 drops out of 269**. Before the fix it was
dropping 67% of the channels the reviewer had approved. It is now out of the way,
which is exactly what it should be.

### THE HONEST PART: what actually landed

Drew Binsky (travel, 7.3M), Josh Pate's College Football Show, JTL SPORTS,
3AW Football, Hi My Car, 1221 Manhwa Recap, stuffeyy, Danny & Diggy.

**Not one of them is a home theater channel.** They came from Home Theater's
ADJACENCY keywords — `sports podcast commentary`, `car and truck review`,
`homesteading vlog`, `movie review and reaction` — which predate this work.
The home-theater-proper keywords contributed almost nothing, because that
corpus is where the niche was already exhausted.

Whether this is success or failure is a question only the reviewer can answer,
and it is the RIGHT question to put to him:

- If the audience-adjacency theory is right — the one the label analysis
  supports, where the reviewer buys an audience for home-entertainment
  FURNITURE rather than AV expertise — then a man-cave-adjacent sports podcast
  is a legitimate prospect and these rows are the point.
- If it is wrong, then these keywords are noise and should be cut, and Home
  Theater has no free corpus worth searching.

`1221 Manhwa Recap` is the row that suggests at least some of the adjacency
vocabulary is too loose regardless of which way the theory falls.

**Do not tune anything further until the reviewer has judged these seven.**
Guessing his taste is what produced the inverted relevance criterion, the
inverted off-target gate, and two rounds of wasted calibration. Seven labelled
verdicts are worth more than any amount of further reasoning here.

### Not changed, by instruction
- Location / search zone — despite being 64% of drops.
- Gender filter.
- Credit budget.
