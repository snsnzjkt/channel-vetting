<!-- /autoplan restore point: /Users/kate/.gstack/projects/snsnzjkt-channel-vetting/feat-gemini-relevance-verification-autoplan-restore-20260824-225129.md -->
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

---

## 12. Criteria optimization, 2026-08-22 — mined from the labels, not guessed

Every candidate criterion was scored against the reviewer's own verdicts (21/31
Home Theater, 37/53 Lifestyle) before shipping. A term ships only if it catches
more REJECTED than it kills APPROVED.

| candidate | kills approved | catches rejected | shipped? |
|---|---|---|---|
| `av_specialist` (HT) | 0/21 | 5/31 | **yes** |
| `story_recap` (both) | 0 | 0 | yes — instruction, zero harm |
| `property_showcase` (LS) | 0/37 | 2/53 | **yes** |
| `travel_vlog` (LS) | 0/37 | 2/53 | **yes** |
| `realestate_listing` (LS) | **1**/37 | 2/53 | **NO** — see below |
| `reaction_farm`, `news_politics`, `sports_league`, `music_perf`, `food_only` | 0 | 0 | no — no effect |

`realestate_listing` was rejected deliberately. Net +1 is not worth a lost
prospect when the brief is "still want many output, not super strict".

### The inversion that mattered

`av_specialist` vocabulary was in Home Theater's **on_target_terms**, i.e. as
RESCUE vocabulary. Measured there it rescued **0 approved and 6 rejected**
channels — working exclusively for the channels the reviewer turns down.
"speakers" appears in 0 of 21 approved titles and 8 of 31 rejected.

It is an exclusion now, and removed from on_target_terms. Both halves are
required: a term on both lists scores off == on, and the gate needs off > on.

Caught: Zero Fidelity (66%), Lenny Florentine (62%), New Record Day (42%),
Forever Analog (16%) — every dedicated AV reviewer the reviewer rejected.

### Manhwa

The `movie review and reaction` KEYWORD is kept. Removing it would cost real
volume, and movie/reaction creators are a plausible home-entertainment
audience. The content type is excluded instead of the query that found it.

### Net effect on the gate

```
                        approved dropped     rejected caught
  before this session   14/21  (67%)         9/31  (29%)     discrimination -38%
  after                  0/21  ( 0%)         4/31  (13%)     discrimination +13%
```

Zero approved channels lost, and the gate now points the right way. Lifestyle
went from completely inert (0/37, 0/53) to 0 approved / 1 rejected.

### Still not strict
Nothing here narrows discovery. Every change is a targeted exclusion measured
to cost zero approved channels. Volume levers (geography, gender, budget)
remain untouched by instruction.

---

## 13. RESULTS — both niches, 2026-08-24

Sequential runs, 90-day window, with the measured criteria live.

```
                    discovered    rows    credits            notes
  Lifestyle Sofa       134         19     2.0 disc + email   was 0 — crash fix unblocked it
  Home Theater         323          6     0 disc + email     + 29 from the earlier sweep
```

Tables now hold **65 rows awaiting review** (34 Home Theater, 31 Lifestyle).
Session credit cost 5.04 of the 200/month ceiling; YouTube quota 3,580/10,000.

### What the new filters kept out (18 channels in one run)

```
  toys / kids     Baby Doll Series, Dollhouse Mini World, Toys and Colours,
                  Sammys collectable toys hall
  AV specialist   AVForums, ListenUp, Electro Empire, Galaxy Geeks
  automotive      Edmunds Cars, Diecast Cars-Trucks, Motor Future, My Car world
  other           Watch Stuff With Us, Meow-some! Live
```

`Baby Doll Series` is the same channel type the reviewer originally flagged by
hand. It is now caught automatically.

### Drop distribution differs completely by niche

```
  HOME THEATER (323)              LIFESTYLE SOFA (134)
    outside_search_zone   85        below_view_minimum   34
    no_declared_country   59        shorts_only          12
    below_view_minimum    45        too_few_longform      3
    off_target_niche      18        no_declared_country   1
    too_few_longform      13
```

Geography is 144 of 234 Home Theater drops and **1** of 50 for Lifestyle —
because paid discovery filters location server-side while raw YouTube search
does not. Unchanged by instruction, and it is now the binding constraint on
Home Theater by a wide margin.

### DIMINISHING RETURNS, stated plainly

Home Theater produced 29 rows on the first 90-day sweep and 6 on the second,
identical run. **The window does not refill.** The first sweep took the best
candidates and they are now correctly skipped (277 of 323 skipped on the
second). Re-running the same window is not a lever.

What is left, in order of size:
1. Geography — 144 of 234 drops. Declined by the operator.
2. New keywords for both niches — free, untried.
3. A wider window (180+ days) — free, diminishing.
4. Credit budget — no longer binding: 5.04 spent of 200/month.

### Cost shape has changed
Discovery is no longer the cost driver; EMAIL ENRICHMENT is. 0.20 per lookup,
only on candidates that clear every gate, so it scales with output rather than
with waste. At the observed rate the 200/month ceiling supports well over 500
rows. Budget is no longer the constraint it was when this plan opened.

---

# 14. PROPOSAL (2026-08-24) — two-layer AI pipeline for BOTH niches

Operator brief, verbatim in intent:

> **Layer 1 — Metadata-Based Discovery.** A first AI layer analyses metadata
> only to generate a broad list of potentially relevant content. Prioritise
> **recall over precision**. Use title, author, date, category, tags, source and
> other structured fields to identify a wide candidate set without processing
> full content.
>
> **Layer 2 — Content-Based Validation.** A second AI layer takes the Layer 1
> candidates and checks the actual content. It **summarises** the content and
> uses that summary to decide whether the item is genuinely relevant to the
> target criteria.
>
> **Flow:** Metadata -> broad candidate list -> content summarisation ->
> relevance check -> final results.
>
> Goal: efficient AND accurate. Metadata for fast broad discovery; deeper AI
> analysis only on the smaller candidate set that needs validation.

Applies to **Home Theater** and **Lifestyle Sofa**. The criteria for both are
already established in `niches.py` (`on_target_terms`, `text_criteria`,
`video_criteria`, `OFF_TARGET_TERMS`) and mined from reviewer labels in
section 12 — this proposal does not restate them, it re-plumbs how they are
applied.

## 14.1 Proposed implementation (as written)

| Layer | Input | Job | Output |
|---|---|---|---|
| L1 | candidate metadata only | broad relevance sweep, recall-biased | candidate list |
| L2 | actual content of L1 survivors | summarise, then judge relevance | final results |

## 14.2 What this plan must establish before it can ship

1. Where L1 and L2 sit relative to the gates that already exist.
2. Whether L2 may DROP a candidate, or only rescue one.
3. What it costs against the Gemini request ceilings.
4. Whether it beats the current arrangement on the reviewer's own labels.

---

## 14.3 CEO REVIEW — Phase 1 (`/autoplan`, 2026-08-24)

Dual voices degraded to **`[subagent-only]`**: `codex exec` returns
`401 Unauthorized: Missing bearer or basic authentication`
(`codex-cli 0.149.0`). Re-enable with `codex login` or `$CODEX_API_KEY`.

### 0A. Premise challenge

Five premises are load-bearing. Four are wrong or unsupported as written.

| # | Premise (as the brief states it) | Verdict | Evidence |
|---|---|---|---|
| P-a | "The pipeline should use a two-layer AI approach" | **Already true** | A metadata layer (`main.off_target_reason`, keyword scoring on ~50 video titles + bio) and a content layer (`gemini_verify.judge`: video tier deciding, text tier advisory) already exist and are wired in sequence at `main.py:1380-1400` |
| P-b | Layer 1 can use "title, author, date, category, tags, source" | **FALSE** | A candidate at discovery carries exactly `handle`, `channel_title`, `matched_keywords` — nothing else. Paid path: `influencer_discovery.py:513-518` ("IDENTIFIERS ONLY — deliberately no statistics"). Free path: `discovery.py:227-231` reads only `channelId` and `channelTitle` and **discards the snippet description**. There is no date, category, tag or author field anywhere at that boundary |
| P-c | The cost to avoid is "processing the full content upfront" | **Mostly FALSE** | Video titles and descriptions arrive **free** on a `channels.list`/playlist fetch the pipeline already makes (`main.py:1358`), which is why `off_target_reason` runs where it does. The actual cost driver is **email enrichment at 0.20 credits/lookup** (§13: "Discovery is no longer the cost driver; EMAIL ENRICHMENT is"), and the actual scarce resource is **Gemini free-tier requests** — which this proposal multiplies |
| P-d | Better relevance validation produces more useful output | **FALSE for yield, TRUE for reviewer attention** | §13 drop distribution: `off_target_niche` is **18 of 234** Home Theater drops and **0 of 50** Lifestyle. `outside_search_zone` + `no_declared_country` is **144 of 234** (64%) and is declined by operator instruction. Relevance is ~7% of the loss surface |
| P-e | Layer 2 decides "whether the content is genuinely relevant" | **Open — operator's call** | This grants Layer 2 **drop authority**. The current tier is rescue-only by explicit design: "Nothing below can make the output smaller" (`gemini_verify.py:944`). Reversing that is a one-way door on volume against the standing instruction "still want many output, not super strict" (§12). Goes to the premise gate |

**The premise that survives.** §11 is blunt about what actually landed on the
free keyword path: Drew Binsky (travel, 7.3M), Josh Pate's College Football
Show, `1221 Manhwa Recap` — "Not one of them is a home theater channel."
So *precision on the free path* is a real, live, correctly-identified problem.
The proposal diagnoses the right pain. It prescribes a layer the repo already
has, for a constraint that is 7% of the loss, using metadata that does not
exist at the point it wants to read it.

### 0B. What already exists (existing-code leverage map)

| Sub-problem the proposal names | Already implemented as | Where |
|---|---|---|
| Metadata-only broad relevance sweep | `off_target_reason` — negative-evidence keyword gate over ~50 titles + bio, recall-biased by construction (nothing is *required* to match) | `main.py:716-800` |
| "Recall over precision" at layer 1 | Explicitly the existing design. A positive must-match gate **was built, measured and rejected** on 2026-08-15 for dropping a real prospect at 0/50 | `main.py:731-737`, `niches.py` above `EXCLUDED_TOPIC_TERMS` |
| Content-based relevance judgement | Gemini **video tier** — 25s of real footage against `video_criteria`, with a per-criterion verdict, confidence, ratio and a `required` veto | `gemini_verify.py:970-1004`, `niches.py:152-183` |
| Summarise content, then judge relevance | Gemini **text tier** — bio + video titles + descriptions -> 0-100 `relevance` score | `gemini_verify.py:488`, `1008-1024` |
| Per-candidate criteria, per niche | `text_criteria` / `video_criteria`, mined from reviewer labels | `niches.py:123-183` (HT), `476-520` (LS) |
| Offline validation against labels | `backtest_relevance.py` — joins cached text verdicts to Airtable `Approved`/`Rejected`, **zero new requests** | `backtest_relevance.py:1-40` |
| Result caching / criteria versioning | `_cache_key` + `criteria_hash`: editing criteria auto-invalidates verdicts | `gemini_verify.py:742`, `545` |

**The text tier is the proposal's Layer 2, and it is switched OFF on purpose.**
`GEMINI_TEXT_TIER` defaults `False` (`config.py:571`) because its `on_niche`
verdict was **measured non-predictive**: 27% approved against a 38% base rate,
0 of 5 in Home Theater (`GEMINI_VERIFY_PLAN.md` 2.16). Turning it on and giving
it authority is precisely the experiment that already failed.

### 0C. Dream state

```
  CURRENT (2026-08-24)
    metadata gate: free keyword scoring, +13% discrimination after §12
    content gate:  Gemini video tier, rescue-only, 1 request/candidate
    binding loss:  geography 144/234 (declined), relevance 18/234
    65 rows sit in Airtable UNJUDGED

  THIS PROPOSAL AS WRITTEN
    + an AI metadata layer reading fields that do not exist at that boundary
    + a summarise hop before a judgement that already reads the source directly
    + drop authority in a tier designed never to drop
    = 3 requests/candidate against a 70/run cap -> 23 candidates judged of 78

  12-MONTH IDEAL
    every relevance term and every AI criterion carries a measured
    catches-rejected vs kills-approved score against reviewer labels;
    keyword-level approval rates steer discovery spend automatically;
    the AI tier spends its scarce free-tier requests only where the
    cheap free gates are genuinely undecided
```

**Delta this plan leaves:** as written, negative — it spends the free tier's
scarcest resource to re-implement two layers that exist, and would judge 29% of
candidates instead of 90%. Re-scoped per 0C-bis, it closes the real gap
(unmined per-keyword signal, unjudged labels) at zero request cost.

### 0C-bis. Implementation alternatives

| # | Approach | Requests/candidate | Effort | Quality risk | Verdict |
|---|---|---|---|---|---|
| A1 | **As written** — new AI L1 on metadata + summarise + judge | 3 | human ~1wk / CC ~2h | High: unvalidated, walls out the budget, reverses rescue-only | **Reject** |
| A2 | **Turn on the existing text tier as Layer 2, backtest first** — `GEMINI_TEXT_TIER=true`, run `backtest_relevance.py` against the labels, ship drop authority only if it beats the keyword gate | 2 | human ~1d / CC ~20m | Low: measured before it gets authority; already-built code path | **Accept as the honest form of Layer 2** |
| A3 | **Layer 1 = per-keyword approval rate, not an AI call** — mine `Source` (which already carries `matched_keywords`, `main.py:1638`) against `Status`, then cut or keep keywords on measured approval rate | 0 | human ~1d / CC ~30m | None: pure measurement, no gate changes | **Accept — this is the highest-value item in the whole review** |
| A4 | Get the 65 pending rows judged, then re-run §12's mining with 65 more labels | 0 | operator time | None | **Accept — precondition for A2** |

A3 is the finding worth the most. §11 observed *once*, by hand, that all 7 Home
Theater rows came from adjacency keywords (`sports podcast commentary`,
`car and truck review`, `homesteading vlog`, `movie review and reaction`) and
none from the home-theater-proper ones. That is a per-candidate metadata signal
that (a) already exists on every row, (b) costs zero requests, (c) is exactly
"metadata-based discovery with recall over precision", and (d) has never been
systematised. `1221 Manhwa Recap` is traceable to a specific keyword; a measured
approval rate per keyword decides its fate without a single AI call.

### 0D. Scope decisions

| Item | Decision | Principle |
|---|---|---|
| Rebuild L1 as an AI call on discovery metadata | **Reject** — the fields do not exist there (`influencer_discovery.py:513`, `discovery.py:227`) | P4 DRY / evidence |
| Insert a summarisation hop before the relevance judgement | **Reject** — lossy, doubles requests, and the criteria already reach the model directly with per-criterion verdicts | P5 explicit over clever |
| Enable + backtest the existing text tier as Layer 2 | **Approve** | P1 completeness |
| Mine per-keyword approval rate (A3) | **Approve** — in blast radius, zero credits, <1d CC | P2 boil lakes |
| Give Layer 2 drop authority | **Premise gate** — one-way door on volume | not auto-decidable |
| Change geography / gender / credit budget | **Out of scope** — declined by standing operator instruction (§10, §11, §13) | P3 |

### 0E. Temporal interrogation

- **Hour 1:** A3 runs. Per-keyword approval rates for both niches, from data already in Airtable. Zero requests, zero credits.
- **Hour 2:** the 65 pending rows are the blocker. §11's standing instruction — "Do not tune anything further until the reviewer has judged these seven" — now applies to 65.
- **Hour 3:** with labels, `backtest_relevance.py` decides whether the text tier earns Layer 2 authority. If it scores like last time (27% vs 38%), the answer is no and the proposal ends there, cheaply.
- **Hour 6+:** if A1 had shipped instead, the run cap silently walls out at candidate 23 of 78 and the remaining 55 carry `STATE_UNAVAILABLE`. Because Home Theater iterates first (`main.py:2381`, §2c), **Lifestyle's candidates are the ones that systematically get no verdict** — the same first-come starvation §2c already documents for credits, reproduced on the request budget.

### 0F. Mode

SELECTIVE EXPANSION. The proposal's diagnosis is kept, its architecture is
replaced with the two zero-cost items that address the same pain, and the one
genuinely irreversible decision (drop authority) is escalated.
---

## 14.4 ENG REVIEW — Phase 3 (`/autoplan`, 2026-08-24)

Phase 2 (design) **skipped**: zero UI terms in the proposal — no component,
screen, form, layout, dialog or dashboard. Phase 3.5 (DX) **skipped**: zero
developer-facing terms; this is an internal batch pipeline with one operator and
no external integrator. Both detections were run, not assumed.

### Section 1 — Architecture: where the layers actually attach

`process_candidate` (`main.py:1189`), annotated with what each step costs:

```
  1239  quota guard
  1241  get_channel_stats                        ~3 YouTube units
  1303  excluded_topic          ] FREE keyword gates on
  1317  broadcast_tv            ] title + About text
  1331  non_english_description ]
  1345  location_drop_reason    ]  <- 144 of 234 HT drops die HERE, pre-fetch
  1358  get_recent_video_performance             ~3 units + long-form paging
        ...video titles + descriptions now in hand, FREE...
  1379  off_target_reason        <== THE PROPOSAL'S "LAYER 1" ALREADY LIVES HERE
  1414  verifier.judge          <== THE PROPOSAL'S "LAYER 2" ALREADY LIVES HERE
                                     1 Gemini VIDEO request, rescue-only
  1426  off_target drop (post-rescue)
  1437  upload_freq / uploads_per_year / days_since   FREE, from data above
  1447  pre_push_drop_reason    <== 107 candidates die HERE, on FREE numbers,
                                    AFTER the paid Gemini request was spent
  1552  resolve_email_with_source                0.20 vendor credits
```

Both proposed layers already have an occupant. The proposal is not additive; it
is a **replacement** of two working, label-calibrated stages, and it must clear
that bar rather than a greenfield one.

### Section 1a — CRITICAL: the AI tier is already budget-starved

Measured, from `gemini_log.json` and §13's drop distribution:

Counted from `run_metrics.jsonl`'s two `completed` 2026-08-24 records (the
authoritative source — top-level `drop_reasons`, not the prose in §13):

```
  Lifestyle Sofa   examined  73   pre-gate drops   3  -> reach the paid block   70
  Home Theater     examined 283   pre-gate drops 184  -> reach the paid block   99
  ---------------------------------------------------------------------------------
  reach the paid Gemini block (main.py:1414)                              169
  Gemini requests actually issued on 2026-08-24                            78
  AI-verdict coverage TODAY                                               46%
```

Over half of the candidates that reach the block already receive no AI verdict.
The request budget — not the model, not the criteria — is the binding constraint
on this layer. Caps:

| cap | value | 2026-08-24 actual |
|---|---|---|
| `GEMINI_MAX_REQUESTS_PER_RUN` | 70 (global, all models, all kinds) | **first wall** |
| `GEMINI_MAX_REQUESTS_PER_DAY` | 80 **per model** | 3.5-flash-lite 40, 3.1-flash-lite 38 |
| `GEMINI_MAX_VIDEO_REQUESTS_PER_DAY` | 40 **per model** | 3.5-flash-lite **40/40 — saturated** |

The primary model hit its video ceiling exactly and traffic spilled to the
fallback. Remaining day headroom across the free chain: **2 video requests, 82
text requests.**

Now cost the proposal:

| shape | requests/candidate | candidates judged per run (cap 70) | coverage of the 169 |
|---|---|---|---|
| current: video only | 1 | 70 | **46%** |
| L2 = summarise + judge | 2 | 35 | 23% |
| **L1 + summarise + judge (as written)** | **3** | **23** | **15%** |

**The proposal cuts AI coverage from 46% to 15%.** It buys "accuracy" by
tripling the price of a verdict inside a budget that is already 4x
oversubscribed, so the net effect is that three times fewer candidates are
examined at all. Failure is silent: `_may_request` returns `run_cap_reached`,
`judge` returns `STATE_UNAVAILABLE`, and the candidate keeps whatever the
keyword gate said (`gemini_verify.py:769-808`). No exception, no drop reason.

Worse, it is **not evenly distributed**. Niches iterate in dict order and Home
Theater is first (`main.py:2381`, §2c). Home Theater consumes the run budget;
**Lifestyle's candidates are the ones that systematically get no verdict** —
the same first-come starvation §2c documents for credits, reproduced on the
request budget, and §13 shows Lifestyle is the niche that actually converts
(19 rows from 134 examined vs 6 from 323).

### Section 1b — HIGH: a free reorder reclaims a third of the budget

`pre_push_drop_reason` (`main.py:1447`) drops on `avg_views`, `shorts_only`,
`video_count`, `uploads_per_year`, `days_since_last_upload` — **every one of
them already present** in `stats`/`performance` as of line 1358, 89 lines
before it runs and 33 lines *after* the paid Gemini request at 1414.

Measured cost of that ordering on 2026-08-24: of the 169 candidates that reach
the paid block, **108 (64%)** die immediately afterwards on free arithmetic —
`below_view_minimum` 79, `shorts_only` 20, `too_few_videos` 7, `not_english` 2.
Home Theater 62 of 99, Lifestyle 46 of 70.

```
  population genuinely needing a request   169 -> 61    (-64%)
  coverage at the same 78 requests          46% -> 100%
```

**The reorder alone fully funds the AI layer.** 78 requests already exceed the
61 candidates that genuinely need one. There is no budget problem to solve here
— there is an ordering problem, and it is 20 lines of movement.

**One gate is deliberately excluded.** `too_few_longform_videos` (16 requests
across those two runs) is NOT part of this. `longform_drop_reason` is split out
of `pre_push_drop_reason` precisely because establishing its count can cost
quota (`enrichment.count_longform_in_older_videos`), so it must run after every
FREE check — which is where it correctly stays. Moving it up as well would trade
YouTube quota (3,580 of 10,000 used) for Gemini requests (78 of ~80). That is
probably the right trade, but it is a different decision with a different cost
and it is NOT reviewed here. Taking it to 45 is a follow-up, not part of R0.

The placement comment at `main.py:1408-1411` justifies the Gemini block's
position against the *email lookup* and *long-form paging*. It never considers
`pre_push_drop_reason`, which is free and sits below it. This is an oversight,
not a design decision.

**Fix:** move the `upload_freq`/`uploads_per_year`/`days_since` computation and
the `pre_push_drop_reason` call to immediately after `off_target_reason`
(line 1379), leaving the Gemini block and its post-rescue `off_target` drop
after them. No signature changes. No behaviour change for any candidate that
survives — only candidates that were going to be dropped anyway stop costing a
request.

**This single reorder delivers the proposal's own stated goal** — "avoid the
cost of processing the entire dataset upfront" — better than the proposal does,
at zero implementation risk.

### Section 2 — Code quality / DRY

- **DRY violation, critical.** A new AI metadata layer duplicates
  `off_target_reason`, which is label-calibrated to +13% discrimination (§12).
  A new content-relevance layer duplicates `gemini_verify` `text_criteria`,
  already built (`gemini_verify.py:488`) and deliberately disabled
  (`config.py:571`) because it measured non-predictive.
- **The summarise hop is strictly lossy.** `build_prompt` (`gemini_verify.py:221`)
  sends the criteria with the source and gets back a per-criterion verdict plus
  confidence, consumed by `verdict_confirms` with a ratio and a `required` veto
  (`gemini_verify.py:253`). Summarise-then-judge replaces that with one
  free-text hop: the second call can no longer see what the first discarded, the
  `required` brand veto loses its evidence, and confidence becomes a judgement
  about a summary rather than about the channel. Two requests to get a weaker
  answer.
- **Cache churn.** `_cache_key(tier, subject, criteria_digest, start_s, end_s)`
  with `criteria_hash` (`gemini_verify.py:742`, `545`) is keyed per tier and
  auto-invalidates on criteria edits. A summary is a *new artifact* needing its
  own key and its own invalidation rule; if the summary is cached and the
  criteria change, verdicts recompute against a summary produced under the old
  criteria. `gemini_cache.json` is already 151KB.

### Section 3 — TEST REVIEW

Every new UX flow, data flow and branch the proposal implies, and its coverage:

| # | New codepath / branch | Test type | Exists? |
|---|---|---|---|
| 1 | L1 runs on a candidate with only `handle` + `channel_title` | unit | **no — and the path is unbuildable, see 0A/P-b** |
| 2 | L1 request when `_may_request` refuses (run/day/video cap) | unit | partial: `tests/test_gemini_*` cover the existing tiers only |
| 3 | Summary produced, then relevance judged from it | unit | **no** |
| 4 | Summary cached, criteria then change -> stale-summary invalidation | unit | **no — the failure mode does not exist today** |
| 5 | L2 returns "not relevant" -> **DROP** (new authority) | unit | **no. There is no `DROP_` reason for it and no test asserts one can exist** |
| 6 | Rescue-only invariant "nothing can make the output smaller" | regression | **exists and WOULD FAIL** — `gemini_verify.py:940-945` |
| 7 | Cap exhausted mid-run -> later candidates unjudged, per niche | integration | **no** — and this is the silent failure that matters most |
| 8 | Niche ordering starves the second niche of requests | integration | **no** |
| 9 | Reorder (1b): pre-push drop no longer spends a request | unit | **no — required if 1b ships** |
| 10 | Backtest: new arrangement vs labels | offline | **`backtest_relevance.py` exists — this is the vehicle** |

**Required before any relevance authority changes:** a backtest against the
existing `Approved`/`Rejected` corpus. This repo has found **three inverted
relevance criteria** (learnings: `channel-vetting-off-target-gate-was-anti-predictive`
— "any relevance heuristic in this repo must be backtested against
Status=Approved/Rejected before it is given authority"). The text tier is the
fourth candidate and it already measured 27% vs a 38% base rate.

**Existing tests that break if L2 gains drop authority:** every test asserting
`judgement.rescued is False` leaves behaviour unchanged, plus
`test_the_niche_filters_reach_the_vendor_payload`-style wiring tests, plus any
test asserting the row count is monotonic in the Gemini tier's failure edges.

### Section 4 — Performance

- `GEMINI_MAX_SECONDS_PER_RUN = 900` (`config.py:502`). §11 measured a 12m17s
  wall clock for one HT run — **737s of a 900s budget**. At 2-3 requests per
  candidate the time budget walls out before the request budget does, and
  `_may_request` returns `time_budget_reached` (`gemini_verify.py:781`).
  The proposal does not mention wall clock at all.
- Latency is serial: `judge` issues the video request, then the text request,
  per candidate, with `API_SLEEP_SECONDS` between YouTube calls. A summarise hop
  adds a full round trip per candidate to a run already at 82% of its brake.

### Failure modes registry

| # | Failure | Severity | Detected today? | Guard |
|---|---|---|---|---|
| F1 | Request budget exhausts; 92% of candidates carry no verdict | **critical** | silent — `STATE_UNAVAILABLE`, no drop reason | reorder (1b) + refuse to add per-candidate requests |
| F2 | Second niche systematically unjudged (dict-order starvation) | **critical** | no | per-niche request reservation, or interleave niches |
| F3 | L2 drop authority reverses rescue-only; output shrinks | **critical** | no test asserts the invariant can't break | premise gate + backtest |
| F4 | Unvalidated relevance layer ships inverted (4th time) | **high** | `backtest_relevance.py`, if run | mandatory backtest vs labels |
| F5 | Stale summary judged against new criteria | **high** | no | fold summary into the criteria hash, or don't cache it |
| F6 | Wall-clock brake trips before the request cap | medium | logged | count seconds/candidate before adding a hop |
| F7 | Paid request spent on a candidate about to fail a free gate | **high** | no | reorder (1b) |

### Not in scope

- Geography (`allowed_country_codes`), gender filter, credit budget — declined
  by standing operator instruction (§10, §11, §13), despite geography being
  144 of 234 HT drops.
- `gemini_log.json` autouse test isolation — known latent gap, no active bug
  (§10 "Not done, deliberately"). Unchanged by this proposal.
- Airtable schema changes.
---

<!-- AUTONOMOUS DECISION LOG -->
## 14.5 Decision Audit Trail (`/autoplan`, 2026-08-24)

| # | Phase | Decision | Class | Principle | Rationale | Rejected alternative |
|---|-------|----------|-------|-----------|-----------|----------------------|
| 1 | 0 | Review mode = SELECTIVE EXPANSION | mechanical | P1 | Proposal's diagnosis is sound, its architecture is not; keep one, replace the other | FULL REWRITE of the proposal |
| 2 | 0 | Skip Phase 2 (design) | mechanical | P3 | UI-term grep returned zero matches on §14 | Run design review anyway |
| 3 | 0 | Skip Phase 3.5 (DX) | mechanical | P3 | DX-term grep returned zero; internal batch pipeline, one operator, no external integrator | Run DX review anyway |
| 4 | 0.5 | Codex voice = unavailable, proceed `[subagent-only]` | mechanical | P6 | Live probe: `401 Unauthorized: Missing bearer or basic authentication`, `codex-cli 0.149.0` | Block the review until auth is fixed |
| 5 | 1 | Reject "L1 = AI call on discovery metadata" | mechanical | P4 DRY + evidence | Candidates carry only `handle`, `channel_title`, `matched_keywords` (`influencer_discovery.py:513-518`, `discovery.py:227-231`); date/category/tags/author do not exist there | Build L1 against fields that would have to be invented |
| 6 | 1 | Reject "the pipeline needs a two-layer AI approach" as a premise | mechanical | P4 DRY | Both layers exist and are wired in sequence at `main.py:1379` and `1414` | Accept the premise and build a third layer |
| 7 | 1 | Keep the proposal's *diagnosis* (free-path precision) | mechanical | P6 | §11: none of HT's 7 rows was a home theater channel; the pain is real | Dismiss the proposal wholesale |
| 8 | 1 | Approve A3 — mine per-keyword approval rate from `Source` vs `Status` | mechanical | P2 boil lakes | Zero credits, zero requests, in blast radius, <1d CC; `matched_keywords` already persisted at `main.py:1638` | Leave §11's one-off hand observation unsystematised |
| 9 | 1 | Geography / gender / budget stay out of scope | mechanical | P3 | Declined by standing operator instruction (§10, §11, §13) even though geography is 144/234 HT drops | Reopen settled operator decisions |
| 10 | 3 | Approve the reorder — `pre_push_drop_reason` above the Gemini block | mechanical | P2 + P5 | 107 candidates spent a paid request then died on free arithmetic; coverage 25% -> 38%, no signature changes | Leave the ordering as-is |
| 11 | 3 | Reject the summarise hop as specified | **taste** | P5 explicit over clever | Two requests to get a weaker answer: the judge loses per-criterion evidence and the `required` brand veto loses its basis (`gemini_verify.py:253`) | Ship summarise-then-judge as written |
| 12 | 3 | Mandate a label backtest before any relevance authority change | mechanical | P1 completeness | Three inverted criteria found in this repo already; the text tier measured 27% vs a 38% base rate | Ship on reasoning alone |
| 13 | 3 | Require per-niche request reservation if per-candidate cost rises | mechanical | P1 | Dict-order iteration (`main.py:2381`) starves Lifestyle, the niche that actually converts (19/134 vs 6/323) | Accept uneven starvation |
| 14 | 3 | Refuse to auto-decide L2 drop authority | mechanical | gate rule | One-way door on output volume against "still want many output, not super strict" | Auto-decide it either way |
| 15 | 3 | Refuse to auto-decide the Layer 1 shape | mechanical | gate rule | Operator specified an AI layer; the evidence says the free signal is better. Their call | Silently substitute A3 for what was asked |
---

## 14.6 DUAL VOICES — `[subagent-only]` (Codex 401, `codex-cli 0.149.0`)

Both voices ran with no prior-phase context. Each independently reached the
central conclusion — the two layers already exist — and each found material the
primary review missed. Every claim below was re-verified against the repo before
being recorded here.

### CLAUDE SUBAGENT (CEO — strategic independence): findings adopted

| # | Finding | Verified how |
|---|---|---|
| CEO-1 | **The prize is precision on pushed rows, not drops.** `backtest_results.json` holds **58 Approved / 84 Rejected = 41% approval rate**. 84 wasted reviewer reviews dwarf the 18 relevance drops. A recall-biased L1 pushes the wrong way on the only number that hurts | parsed: 142 rows, `label` = {Rejected 84, Approved 58} |
| CEO-2 | **`matched_keywords` is a constant on the paid path.** `influencer_discovery.py:203` sets it to the literal `"influencers.club discovery"` — zero discriminating signal for every candidate arriving that way. A3 works on the free `search_list` path only | confirmed in source + the Airtable pull below |
| CEO-3 | **HT `text_criteria` contradicts the §12 exclusion.** `text_criteria[0]` still asks whether the subject is "home audio-visual equipment — **speakers**, projectors, receivers…" while `OFF_TARGET_TERMS["av_specialist"]` now contains `speaker`, `subwoofer`, `audiophile`, `loudspeaker` and **is active for Home Theater** (`off_target_categories` includes `av_specialist`). The keyword layer treats "speaker" as evidence *against*; the AI layer is instructed to treat it as evidence *for* | ran `niches.py` and printed both lists |
| CEO-4 | **The backtest instrument is inert.** `gemini_cache.json` holds **118 entries, 100% `video`, zero `text`**. `backtest_relevance.py:10-12` skips every non-`text` key, so it prints `joined: 0` today | parsed the cache; tier counter = `{'video': 118}` |
| CEO-5 | **The last attempt to backtest the AI layer got zero verdicts.** All **142** rows in `backtest_results.json` have `outcome = "unavailable (day_cap_reached)"` — a 100% failure rate against the day cap | parsed: `{'unavailable (day_cap_reached)': 142}` |
| CEO-6 | §13 wrote a ranked list of remaining levers — **new keywords (free, untried)** and **a 180+ day window (free)** — and §14 proposes a fifth item without a sentence on why those two were skipped | §13 |

### CLAUDE SUBAGENT (eng — independent review): findings adopted

| # | Finding | Severity | Verified how |
|---|---|---|---|
| ENG-1 | **Drop authority poisons `rejected_handles.json` for 90 days, silently.** `push_until_full` writes the handle of any drop whose reason is not in `TRANSIENT_DROP_REASONS` — and that set is only `{quota_exhausted, no_headroom_for_bucket, unreachable}`. Retention is 90 days and the handle is fed to the vendor's server-side `exclude_handles`, so the creator is **never returned again and never re-examined**. One non-deterministic false negative on a 25-second clip deletes a real prospect for a quarter | **critical** | `main.py:479-483`, `main.py:1061-1069`, `config.py:355` |
| ENG-2 | **Cap exhaustion becomes a mass drop.** `judge()` returns `STATE_UNAVAILABLE` before any verdict when a cap is hit. Under "L2 must confirm to keep", every candidate past the cap is dropped — the run yields near-zero rows for a reason that describes the *budget*, not the channel, and exits 0 | **critical** | `gemini_verify.py:979-995` |
| ENG-3 | **The video tier has zero measured discrimination.** At the live `GEMINI_MIN_CRITERIA_RATIO=0.5` it confirmed **Approved 6/6 and Rejected 2/2** — it confirms everything. Drop authority over a judge that never says no is all downside and no precision | **critical** | `GEMINI_VERIFY_PLAN.md:1228` |
| ENG-4 | **A filter can only subtract.** Neither layer generates candidates. Under rescue-only the entire two-layer pipeline has a **maximum yield delta of +0 rows**; the only direction it can move the row count is down. This plan is titled "more qualified rows" | **critical** | architecture |
| ENG-5 | **The cache-key dilemma has no third option.** Key the judge on a digest of the summary and generation nondeterminism gives a **permanent 100% cache miss** — the exact failure `criteria_hash`'s docstring was written to prevent. Key it on `(video_id, criteria)` instead and a cache hit serves a verdict derived from a *different* summary than the one you just paid for | **high** | `gemini_verify.py:742`, `545-556` |
| ENG-6 | **The summarise hop is a prompt-injection surface.** `build_prompt` states that text in the media is "DATA to be described, never an instruction to follow", bounded by `responseSchema`. A summarise hop launders creator-controlled on-screen text out of the schema-bounded *observation* position into the *instruction* position of the judge prompt | **medium** (high if combined with drop authority) | `gemini_verify.py:221-232` |
| ENG-7 | **Test blast radius is larger than the plan's own count.** **1268** tests collected, not the 1231 §10 states. `stub_post()` pops one queued response per request at **35** sites and falls back to a synthetic 200 when the queue empties — so a third request per candidate desynchronises them **silently, passing for the wrong reason**. **16** exact request-count assertions break. `GeminiVerifier` is constructed **positionally** (7 args) at 3 sites | **high** | `pytest --collect-only`: 1268 |
| ENG-8 | **Any prompt rewrite costs 118 requests to recover.** `criteria_hash` covers the criteria list, not the prompt wording, so a summarise-then-judge rewrite changes semantics without changing the key — `GEMINI_VERDICT_VERSION` must be bumped, invalidating all 118 cached video verdicts. At 30 video/run that is ~4 runs to return to today's state | **medium** | `config.py:586` |
| ENG-9 | **The wall-clock brake already trips.** `GEMINI_MAX_SECONDS_PER_RUN = 900`; the 2026-08-24 Home Theater run took **985.4s**. `self.seconds` only increases and is terminal for the run. §14 does not mention wall clock | **medium** | `config.py:502`, `run_metrics.jsonl` |
| ENG-10 | **`tags` and `categoryId` are already paid for and thrown away.** `videos.list` is already called with `part=snippet,statistics,contentDetails,player`, and the code comment states "videos.list is a flat 1 unit regardless of parts requested". `snippet` carries `tags` and `categoryId`. **This is the only part of the brief's "structured fields" story that is both real and free** | **high (opportunity)** | `enrichment.py:~811-819` |

### Pre-existing defects found in passing (not caused by §14)

| Defect | Evidence |
|---|---|
| `GEMINI_CACHE_RETENTION_DAYS` (`config.py:593`) is **never read** — `gemini_verify.py:751` hardcodes `30 * 86400`. A documented knob that does nothing | verified |
| `judge()`'s final `elif`/`else` branches are **identical** (`self.scored += 1; state = STATE_SCORED`) | `gemini_verify.py:1027-1033` |
| A cap hit for an *unflagged* candidate skips the advisory text tier entirely even when only the **video** sub-cap was reached — contradicting the "the text tier continues" promise in the log line | `gemini_verify.py:794-797` vs `:982` |

### CEO consensus table

```
CEO DUAL VOICES — CONSENSUS TABLE            Claude  Codex   Consensus
──────────────────────────────────────────── ─────── ─────── ─────────
 1. Premises valid?                            NO     N/A     NO (single-voice)
 2. Right problem to solve?                    NO     N/A     NO (single-voice)
 3. Scope calibration correct?                 NO     N/A     NO (single-voice)
 4. Alternatives sufficiently explored?        NO     N/A     NO (single-voice)
 5. Competitive/market risks covered?          N/A    N/A     n/a (internal tool)
 6. 6-month trajectory sound?                  NO     N/A     NO (single-voice)
```

### Eng consensus table

```
ENG DUAL VOICES — CONSENSUS TABLE            Claude  Codex   Consensus
──────────────────────────────────────────── ─────── ─────── ─────────
 1. Architecture sound?                        NO     N/A     NO (single-voice)
 2. Test coverage sufficient?                  NO     N/A     NO (single-voice)
 3. Performance risks addressed?               NO     N/A     NO (single-voice)
 4. Security threats covered?                  NO     N/A     NO (single-voice)
 5. Error paths handled?                       NO     N/A     NO (single-voice)
 6. Deployment risk manageable?                NO     N/A     NO (single-voice)
```

**Codex was unavailable, so nothing here is CONFIRMED by cross-model agreement.**
Every finding is single-voice plus my own verification against the repo. Treat
them as evidenced, not as consensus.

### Cross-phase theme

**One theme appears in both phases independently: the request budget, not the
model, is what limits this layer — and the ordering, not the architecture, is
what wastes it.** The CEO voice reached it from the ceiling side (142 backtest
rows, all `day_cap_reached`); the eng voice reached it from the per-candidate
side (297 requests against a 70 cap). Both land on the same fix, which is not in
the proposal: stop spending paid requests on candidates a free gate is about to
reject.

---

## 14.7 MEASURED — per-keyword approval rate (the free "Layer 1")

Run during this review against the live tables. Read-only, zero credits, zero
Gemini requests. This is CEO-2's A3 lever, executed rather than recommended.

**Home Theater — 133 rows (37 Approved, 62 Rejected, 34 unjudged)**

| keyword (from the `Source` field) | labelled | app | rej | approval |
|---|---|---|---|---|
| `home theater products review` | 5 | 5 | 0 | **100%** |
| `home theater tech setup` | 4 | 2 | 2 | 50% |
| `power tools review` | 2 | 1 | 1 | 50% |
| `sports podcast commentary` | 9 | 4 | 5 | 44% |
| `man cave tour` | 6 | 2 | 4 | 33% |
| `homesteading vlog` | 4 | 1 | 3 | 25% |
| `car and truck review` | 2 | 0 | 2 | **0%** |
| `movie review and reaction` | 2 | 0 | 2 | **0%** |
| `entertainment room makeover` | 1 | 0 | 1 | **0%** |
| (no keyword recorded in `Source`) | 64 | 22 | 42 | 34% |

**Lifestyle Sofa — 144 rows (45 Approved, 68 Rejected, 31 unjudged)**

| keyword | labelled | app | rej | approval |
|---|---|---|---|---|
| `house tour apartment tour` | 6 | 2 | 4 | 33% |
| `seasonal home decor` | 3 | 1 | 2 | 33% |
| `country living home` | 9 | 1 | 8 | **11%** |
| `home decor tour` | 4 | 0 | 4 | **0%** |
| `home cleaning and organizing` | 3 | 0 | 3 | **0%** |
| `cozy living room decor` | 2 | 0 | 2 | **0%** |
| `DIY home makeover` | 2 | 0 | 2 | **0%** |
| `minimalist home living` | 2 | 0 | 2 | **0%** |
| (no keyword recorded in `Source`) | 81 | 41 | 40 | 51% |

**What this says.** The signal is real and it is legible at zero cost. Two
readings are already actionable:

- `home theater products review` is 5/5 and `country living home` is 1/9. Those
  are opposite ends of a 9x spread in reviewer approval, available today,
  requiring no AI call.
- §11's hand observation is **partly overturned**. It concluded the
  home-theater-proper keywords "contributed almost nothing" and the adjacency
  keywords were what fired. On labelled outcomes the best-converting HT keyword
  is `home theater products review` (100%), while two adjacency keywords
  (`car and truck review`, `movie review and reaction`) are 0/2 and 0/2. The
  adjacency theory produced *volume*; it did not produce *approval*.

**Caveats, stated rather than buried.** (a) Cell counts are small — 1 to 9
labelled rows per keyword. These are directional, not conclusive. (b) The
"(no keyword recorded)" bucket is the largest in both niches, and per CEO-2 the
paid path writes a constant string, so this lever is only sharp on the free
`search_list` path — which is exactly where Home Theater now runs. (c) 65 rows
are still unjudged; those labels would roughly halve the error bars.
---

## 14.8 REVISED change set (supersedes 14.1)

Ordered by evidence strength. Everything in R0-R4 is **free** — zero credits,
zero Gemini requests — and none of it needs the 65 pending labels first.

### R0 — Reorder: free numeric gates before the paid AI call  `[SHIP FIRST]`
Move the `upload_freq` / `uploads_per_year` / `days_since` computation and the
`pre_push_drop_reason` call (`main.py:1437-1447`) to immediately after
`off_target_reason` (`main.py:1379`), leaving the Gemini block and its
post-rescue `off_target` drop after them.

- **Measured win:** 108 of 169 candidates (64%) currently spend a paid Gemini
  request and then die on free arithmetic. Population needing a request drops
  169 -> 61; coverage at today's 78 requests goes **46% -> 100%**.
  (Excludes `too_few_longform_videos`, 16 more — see 14.4 §1b for why that gate
  correctly stays below the Gemini block.)
- **Cost:** ~20 lines moved. No signature changes. No behaviour change for any
  candidate that survives.
- **This delivers the proposal's own stated goal** — "avoid the cost of
  processing the entire dataset upfront" — better than the proposal does.
- **Test required:** a cap hit at candidate N leaves candidates N+1… with
  identical `(record, reason)` to a no-verifier run.

### R1 — Fix the `text_criteria` / `av_specialist` contradiction  `[PRECONDITION]`
HT `text_criteria[0]` instructs the model that "speakers" indicates on-niche;
`OFF_TARGET_TERMS["av_specialist"]` treats it as off-niche and is active for
Home Theater. Latent today only because `GEMINI_TEXT_TIER=False`. **Turning the
text tier on without fixing this makes the AI layer re-admit exactly the
channels §12 built the exclusion to catch** (Zero Fidelity, New Record Day,
Lenny Florentine, Forever Analog). One edit to `niches.py`; nothing ships that
touches the text tier until it is done.

### R2 — Extract `tags` and `categoryId`  `[FREE STRUCTURED FIELDS]`
`videos.list` is already called with `part=snippet,...` and is "a flat 1 unit
regardless of parts requested" (`enrichment.py`). `snippet.tags` and
`snippet.categoryId` are on the response and discarded. This is the **only**
part of the brief's "title, author, date, category, tags, source" story that
both exists and is free. Extract them, persist them, and they become inputs to
R3 and to any future L1 — deterministic, no AI call.

### R3 — Per-keyword approval rate as the real Layer 1  `[MEASURED IN 14.7]`
Systematise the 14.7 table as a script, re-run it when labels land, and cut or
keep keywords on measured approval rate rather than on volume. This is
"metadata-based discovery, recall over precision" implemented as arithmetic:
it prunes the **query**, before a credit or a quota unit is spent, instead of
judging candidates after they are bought. Sharp on the free `search_list` path;
blunt on the paid path, where `matched_keywords` is a constant (CEO-2).

### R4 — Backtest the tier that actually decides  `[NEVER DONE]`
`backtest_relevance.py` reads only `text` cache keys and the cache holds zero of
them, so it prints `joined: 0`. But the **video** verdicts are on Airtable —
`Relevance State`, `Relevance Detail`, `Relevance Notes`, `Verified Video URL`
(`main.py:1719-1726`). Join those against `Status`. Zero requests, zero credits,
and it measures the layer that decides — which no artifact in this repo has ever
measured. Expect it to confirm ENG-3 (6/6 Approved, 2/2 Rejected = no
discrimination); if so, the criteria need rewriting before any tier gets more
authority.

### R5 — Only then: the honest form of Layer 2  `[GATED ON R1 + labels]`
Rewrite HT `text_criteria` toward the audience-adjacency question the backtest
diagnosed, set `GEMINI_TEXT_TIER=true` for one run, re-run the backtest. Cost
~142 requests, i.e. **two days of the whole free ceiling** — the last attempt at
this returned 142/142 `day_cap_reached`, so it must run after R0 frees the
budget. If it does not beat the 38% base rate, §14 is closed for the price of
one run.

### R6 — Layer 2 drop authority: REFUSED by default
Five invariants break (ENG-1, ENG-2, ENG-3, and the two rev-1 precedents). The
decisive one: an AI-derived drop reason is not in `TRANSIENT_DROP_REASONS`, so it
writes the handle to `rejected_handles.json` for **90 days** and feeds it to the
vendor's `exclude_handles` — a false negative on 25 seconds of footage deletes a
real prospect for a quarter, silently. If the operator wants it anyway, the only
acceptable first step is **shadow mode**: log would-be drops, act on none,
compare against reviewer verdicts for one cycle.

### Rejected from the proposal
- **The summarise hop.** Two requests to get a weaker answer: the judge reads
  prose about frames it never saw, every criterion in both niches is a *visual*
  test, the `required` brand veto loses its evidence, the cache key has no valid
  form (ENG-5), and it opens a prompt-injection path (ENG-6).
- **An AI call on discovery metadata.** The fields do not exist there. Both
  paths yield `{handle/channel_id, channel_title, matched_keywords}` and nothing
  more.

### Deferred, and flagged rather than silently dropped
- **New keywords for both niches** and a **180+ day window** — §13 ranked both
  as free and untried, and §14 skipped both without a sentence. They remain the
  cheapest untried volume levers.
- **Geography** — 144 of 234 HT drops, declined by standing operator instruction.
- Pre-existing defects: dead `GEMINI_CACHE_RETENTION_DAYS`, duplicate `judge()`
  branches, text tier skipped on a video-only cap hit.

### Success metric
Unchanged from §9, and §14 never named it: **reviewer approval rate on pushed
rows.** Today that is **41%** (58 of 142). Rows-per-run and rows-per-credit are
the wrong guard — a diluting change raises them while approval rate falls.

---

## 14.9 GATE — operator answers (2026-08-24)

Asked before implementation. Settled; do not re-litigate.

| # | Question | Answer | Consequence for §14 |
|---|----------|--------|---------------------|
| G1 | May Layer 2 DROP a candidate? | **NO — rescue-only stands** | R6 is CLOSED. No new `DROP_` reason. Nothing in the AI layer may write to `rejected_handles.json`. The invariant "nothing here can make the output smaller" is preserved and must stay tested |
| G2 | What does Layer 1 read? | **The free deterministic signal** — per-keyword approval rate + extracted `tags`/`categoryId` | R2 and R3 are the shipping form of Layer 1. No AI call is added at the discovery boundary. The AI-L1 reading is not deferred-pending-evidence; it is not being built |
| G3 | Keep summarise-then-judge? | **NO — dropped** | The video tier keeps its single-call shape: criteria against frames, per-criterion verdict plus evidence. No summary artifact enters the decision path |
| G4 | gstack upgrade now? | **No, skipped** | Toolchain stays at 1.68.2.0 for this session |

**What these answers do to the proposal, stated plainly.**

Layer 2 keeps its current shape and authority (G1, G3). Layer 1 becomes
arithmetic rather than an AI call (G2). So **§14 does not add an AI layer to this
pipeline at all** — it resolves into four free changes plus one gated
measurement:

- **R0** (reorder) — the one change that delivers §14's stated efficiency goal.
  Coverage 46% -> 100% at zero cost.
- **R1** (fix the `text_criteria` / `av_specialist` contradiction) — precondition
  for anything touching the text tier. Ship regardless; it is a live latent bug.
- **R2** (extract `tags` / `categoryId`) — the only real, free "structured
  fields" from the brief.
- **R3** (per-keyword approval rate) — Layer 1, measured in §14.7.
- **R4** (backtest the video tier off Airtable) — measures the deciding layer for
  the first time. Zero requests.

**The honest failure mode is explicit.** R0-R4 improve *efficiency* and
*measurability*. None of them raises the row count, because under G1 a filter can
only subtract (ENG-4). If the goal is more qualified rows, the levers remain the
ones §13 ranked: geography (144 of 234 HT drops, operator-declined), new keywords
(free, untried), a wider window (free, untried). This review does not pretend
otherwise, and R3's approval-rate table is the tool for choosing which new
keywords to try.

**Still blocking real measurement:** 65 rows unjudged (34 HT, 31 Lifestyle).
§11's standing instruction applies — get them labelled before any further
criteria tuning.

---

## GSTACK REVIEW REPORT

Reviewed by `/autoplan` 2026-08-24. CEO + Eng phases at full depth; Design and DX
skipped on measured zero scope. Codex unavailable (401) — all findings are
single-voice plus repo verification, not cross-model consensus. Verdict:
**§14 rejected as architecture, diagnosis retained.** R0-R4 are free and
independently shippable; R5 is gated on R1 and on the 65 pending labels; R6 is
refused pending an explicit operator decision.

---

## 14.10 SHIPPED — R0, 2026-08-24

| item | detail |
|---|---|
| Change | `pre_push_drop_reason` and the activity-signal computation moved above the Gemini block in `main.process_candidate` |
| Files | `main.py` (block swap + ordering comments), `tests/test_gate_order_request_budget.py` (new, 7 tests) |
| Tests | 1268 baseline -> **1275 passing**, 7 added, **zero regressions** |
| Measured effect | candidates needing a Gemini request 169 -> 61 (-64%); coverage at the observed 78 requests/day 46% -> 100% |
| Behaviour change | none. Identical candidates are dropped for identical reasons; the drop now happens before the spend rather than after |
| Signatures changed | none |

**Correction made while implementing.** The review's first figure was 124
candidates (73%). That wrongly counted `too_few_longform_videos` (16), which is
handled by `longform_drop_reason` — a separate gate that legitimately stays
below the Gemini block because establishing its count can cost quota. The test
`test_the_longform_floor_is_DELIBERATELY_still_below_the_gemini_block` pins that
placement so nobody "fixes" it without deciding the quota-for-requests trade
deliberately. Correct figure: **108 (64%)**, 169 -> 61. The headline conclusion
is unchanged — 78 requests/day already exceeds 61 candidates, so the reorder
fully funds the layer.

**Guard.** `test_the_free_gate_runs_before_the_paid_one_in_source_order` reads
`process_candidate`'s source and asserts `pre_push_drop_reason` appears before
`verifier.judge`. Without it the ordering is one innocuous edit from regressing,
and the regression is invisible: identical row counts, identical drop reasons,
and the only symptom is a request counter nobody watches.

### Follow-up, not shipped
Move `longform_drop_reason` above the Gemini block too (169 -> 45). Requires an
explicit call on spending YouTube quota to save Gemini requests. Quota is at 36%
utilisation and Gemini requests at ~97%, so the trade looks right, but it was
not reviewed.

---

## 14.11 SHIPPED — video topic gate, 2026-08-24

Asked for: a transcript checker, so the pipeline can tell what a video is about
("guns or legos"), using an ASR model such as Whisper.

**The diagnosis was right and the vocabularies already existed.** The gap was
never the terms — `EXCLUDED_TOPIC_TERMS["firearms"]` already holds `firearm`,
`handgun`, `ammo`, and `OFF_TARGET_TERMS["toys_and_kids"]` already holds `lego`,
`minifigure`, `brickheadz`. The gap is the **input**: every relevance signal in
this pipeline reads what a channel is CALLED (`excluded_topic_reason`,
`broadcast_tv_reason` — title and About bio) or what it NAMES its videos
(`off_target_reason` — video titles). **None reads what a video is about.** A
firearms channel titling videos "Range Day 47" passes both.

### The transcript route is closed. Measured, not assumed.

| route | result |
|---|---|
| `captions.download` (YouTube Data API) | requires **OAuth as the channel owner**. No third-party route at any price — a documented Google restriction, not a rate limit |
| caption `baseUrl` from the watch page | **HTTP 200, 0 bytes** |
| `&fmt=json3`, `&fmt=srv3`, with and without `Referer` | **HTTP 200, 0 bytes** each |
| bare `/api/timedtext` | **HTTP 200, 0 bytes** |
| InnerTube `/youtubei/v1/player` | request hung; consistent with the same block |

The track **listing** is still readable — we can confirm English auto-captions
exist — and the **content** is not. `youtube-transcript-api` uses these same
endpoints and hits the same wall.

Local ASR (Whisper / faster-whisper) would work, and does not fit here: it needs
the audio, which means downloading it — against YouTube's terms — plus `ffmpeg`,
model weights, and minutes of CPU per video. The 2026-08-24 Home Theater run
already took **985s against a 900s Gemini brake**; ASR across ~90 candidates is
hours, not minutes. Note also that **Whisper is OpenAI's, not Google's**;
Google's equivalent is Cloud Speech-to-Text, which is paid, and §4 rules paid
usage out.

**What the pipeline already has instead:** Gemini is handed the video URL, not a
transcript, and ingests **audio and frames together** — so "what is said" is
already reachable through `video_criteria`, on a 25-second window. This change
covers the whole sampled catalogue, from data already paid for. They complement.

### What shipped

| item | detail |
|---|---|
| `enrichment.py` | capture `snippet.tags` and `snippet.categoryId` — both already on the `videos.list` response ("a flat 1 unit regardless of parts"), both previously discarded. **This is R2.** |
| `video_topics.py` (new) | share-based topic evidence over creator tags; word-boundary matching; category reporting; reviewer-readable summary |
| `main.py` | `DROP_OFF_TOPIC_TAGS` + the gate, placed with the other **free** gates ahead of the paid Gemini call |
| `config.py` | `VIDEO_TOPIC_GATE` (default **off**), `VIDEO_TOPIC_MIN_SHARE` (0.40), `VIDEO_TOPIC_CATEGORIES` (measured allowlist) |
| `measure_video_topics.py` (new) | scores the signal against reviewer verdicts; caches, so a re-run is free |
| tests | `test_video_topics.py` (14), `test_video_topic_gate.py` (10) |

**Cost: zero.** No credits, no Gemini requests, no new network call, no new
dependency. The tags arrive on a response the pipeline already makes.

### MEASURED before shipping — 211 labelled channels (81 Approved / 130 Rejected)

91% carry tags at all. A channel with no tags can never be dropped by this.

```
  share >= 40%          kills approved   catches rejected    net
  gaming                             0                  2    +2
  sports_commentary                  0                  1    +1
  av_specialist                      0                  1    +1
  toys_and_kids                      0                  1    +1
  ------------------------------------------------------------------
  total                              0                  5    +5
```

Five channels the reviewer rejected, caught at **zero cost to approved ones** —
the same shape and scale as §12's shipped `av_specialist` change.

**Two findings baked into the defaults rather than left to a reader:**

- **`phones_and_pcs` is HARMFUL** at every threshold where it fires (-2 at 10%,
  -1 at 25%), matching the 2026-08-21 title backtest that found the same
  category anti-predictive. It is **excluded from the allowlist** and a test
  asserts it can never drop.
- **Lifestyle Sofa: nothing fires** at 25% over 113 labelled rows. This is a
  Home Theater signal in practice — the same per-niche divergence §13 found in
  the drop distributions. Left enabled for both because an inert gate costs
  nothing, not because it was shown to work there.

### Honest limits

- **Guns and Lego specifically are unvalidated for benefit.** `firearms` fires on
  **zero** of the 211 labelled channels, so the corpus contains no tagged
  firearms channel to catch. `firearms`, `asmr` and `political` ship on the §12
  `story_recap` precedent — instruction-backed exclusions with **zero measured
  harm** — not on measured benefit. `toys_and_kids` did fire and did earn its
  place (+1).
- **Five catches on 211 rows is a real result and a small one.** The gate is
  **default OFF**. Evidence is always computed and logged; only the drop is
  gated. Run it advisory for a cycle, read the `TOPIC ADVISORY` lines, then arm
  it with `VIDEO_TOPIC_GATE=true`.
- Tags are creator-declared, so a creator who tags nothing or tags dishonestly is
  invisible here. That is why this is negative-evidence-only and why absent tags
  are never a verdict.

### To arm it
```
VIDEO_TOPIC_GATE=true          # after a cycle of advisory logs
VIDEO_TOPIC_MIN_SHARE=0.40     # 0.25 costs 1 approved for 4 more catches
```

---

## 14.12 SHIPPED — excluded-subject veto in `video_criteria`, 2026-08-24

The other half of the topic gap. `video_topics.py` (§14.11) closed the half that
creator TAGS cover; this closes the half that only the video itself can answer.
Gemini is handed the video URL and ingests **audio and frames together**, so "a
gun is being fired" and "someone is assembling a Lego set" are directly
observable here and nowhere else in this pipeline.

Added to **both** niches as a fourth criterion, `required: True`:

> **not an excluded subject** — Is the SUBJECT of this clip something other than
> firearms, toys or construction-brick building, ASMR, or party politics? Answer
> no ONLY when one of those is what the video is actually about… Incidental
> presence does NOT count and must still answer yes — a Lego set or action figure
> on a shelf during a room tour, a games console under a television, a rifle on a
> wall rack in the background… If you cannot tell what the subject is, answer yes
> and lower your confidence.

Every clause is about the **subject**, not presence, with the incidental cases
written out. A room tour with a Lego set on the shelf is the niche, not an
exclusion. Getting that backwards would re-create the §12 inversion where
vocabulary meant to describe the niche was in practice describing the rejects.

### A latent bug this exposed, and fixed

Adding a second veto would have **silently loosened** the relevance bar. Proven
before the change, with 2 scored criteria + 1 brand veto at ratio 0.5:

```
  BEFORE the fix, with a 2nd veto added and passing:
    space=0 creator=0 brand=1 topics=1  ->  CONFIRM   (2 of 4 = 0.50)
```

A clip showing **no home, no living space and no creator** would be rescued for
being an independent creator who showed no gun. Passing a veto was counting as
evidence of relevance, because required criteria sat in the ratio denominator.

**Fix:** `verdict_confirms` now counts the ratio over **scored criteria only** —
both halves of the fraction exclude vetoes. A veto is a veto, not evidence.

Proven equivalent for the config shipping at the time of the fix: **all 8**
possible verdicts for 2 scored + 1 required at ratio 0.5 are unchanged, so the
fix is a provable no-op until a second veto exists. Pinned by
`test_the_three_criteria_config_is_unchanged_by_the_ratio_fix`.

```
  AFTER the fix, 2nd veto passing:
    space=0 creator=0 brand=1 topics=1  ->  no   (0 of 2 scored criteria)
    space=1 creator=0 brand=1 topics=1  ->  CONFIRM (1 of 2)   <- as with 3 criteria
  AFTER the fix, exclusion veto FAILING:
    all 8 combinations  ->  refused
```

### What this can and cannot do

**It cannot drop anything.** The tier is rescue-only (G1), so this veto only ever
**blocks a rescue**. A firearms channel that the keyword gates never flagged is
still pushed — this criterion cannot remove it. The risk it carries is therefore
"a legitimate channel loses a rescue it would have won", never "a prospect is
deleted". That bound is why an unmeasured veto is acceptable here at all.

**It is unmeasured, and cannot currently be measured.** It cannot be scored
against the labels the way §12 scored vocabulary: `gemini_cache.json` keys video
verdicts on `video_id` and no `video_id -> channel_id` map is persisted, so the
118 cached verdicts cannot be joined to reviewer labels at all. And `firearms`
fires on zero of the 211 labelled channels, so there is no catch to measure
against. It ships on the §12 `story_recap` precedent — instruction-backed, zero
measured harm — with its blast radius bounded by rescue-only. **Read the
`Relevance Detail` column for a cycle before trusting it.** R4 remains the fix
for the underlying measurability gap.

### Cost

Adding a criterion changes `criteria_hash`, so **all 118 cached video verdicts
are invalidated** — verified: zero cache entries match either niche's new hash.
Each previously cached candidate costs one request again when next examined. Post-R0
that absorbs inside a run or two: 61 candidates per run need a request against 78
available, where before the reorder 169 did.

| item | detail |
|---|---|
| `niches.py` | fourth `required` criterion on both niches |
| `gemini_verify.py` | ratio counted over scored criteria only |
| tests | +3 (`excluded_subject` veto, the loosening guard, the equivalence proof) |
| suite | 1299 -> **1302 passing**, zero regressions |

---

## 14.13 TEST REPORT — 2026-08-24/25

Asked: does the transcript reader work, and does everything work?

**There is no transcript reader.** None was built — §14.11 records why. What
shipped reads creator TAGS (`video_topics.py`) and asks GEMINI to watch the video
(the §14.12 veto). Both were tested; results below.

### 1. Transcript availability — re-proven on three real videos

| video | duration | caption tracks | `baseUrl` | `+Referer` | `json3` | `srv3` |
|---|---|---|---|---|---|---|
| LEGO Ninjago build | 13m08s | **exist** (`en/asr`) | 200 / **0 bytes** | 200 / 0 | 200 / 0 | 200 / 0 |
| Home theater room tour | 4m12s | **exist** (`en/asr`) | 200 / **0 bytes** | 200 / 0 | 200 / 0 | 200 / 0 |
| ASMR whispers | 17m38s | **none at all** | — | — | — | — |

English auto-captions demonstrably exist on two of three and are unreadable on
all three. A transcript reader is not buildable here. Third video had no captions
at all, so even a working reader would cover 2 of 3.

### 2. Live Gemini — the §14.12 excluded-subject veto, through `judge()`

Real videos, real API, the shipping 4-criteria Home Theater config, `flagged=True`.

| case | verdict | detail |
|---|---|---|
| LEGO Ninjago build | **refused** | `failed a required criterion: not an excluded subject` |
| Home theater room tour | **rescued** | `video confirmed 0.90` |
| ASMR whispers | untested | `unavailable (unreachable)` — see §3 |

The veto's own evidence on the LEGO case: *"solely focused on assembling a
Lego-style construction set, which is an excluded subject"*, and
`[home entertainment or living space: no] shows only hands building a
construction-brick model against a white background`. It also correctly answered
`[a real creator, not a repost: yes]` — so it is discriminating per criterion,
not failing everything.

The room tour is the **negative control that matters**: the new veto did not
over-fire on a genuine home-theatre channel. Both halves work.

**ASMR remains untested.** Not a logic failure — every failure edge behaved
correctly (`state=unavailable`, `rescued=False`, run continues, candidate keeps
its existing verdict). It is an unproven category.

### 3. `gemini-3.7-flash` was down — and my first diagnosis was wrong

Four video attempts on `gemini-3.7-flash` hit the 60s read timeout, on both a
4m12s and a 17m38s video. My initial reading was "the third model in the chain is
unusable for video". A one-request text probe corrected it:

```
  gemini-3.7-flash       TEXT -> 503 in 10.8s
      "This model is currently experiencing high demand. Spikes in demand
       are usually temporary. Please try again later."
  gemini-3.5-flash-lite  TEXT -> ok in 1.2s
```

So it was **Google-side capacity, not video-specific and not our code.** A 503
maps to `UNREACHABLE`, which is correctly non-terminal, so the model is retried
rather than blacklisted.

**The real cost is wall clock.** While a chain member is down, each candidate
routed to it burns the full 60s `GEMINI_TIMEOUT`. Against
`GEMINI_MAX_SECONDS_PER_RUN = 900`, fifteen such candidates end the run. It
degrades safely (`time_budget_reached`) but expensively. Worth considering a
shorter timeout or a per-run circuit breaker after N consecutive `UNREACHABLE`s
on the same model. **Not fixed — flagged.**

### 4. BUG FOUND AND FIXED — the summary compared a global sum to a per-model cap

`spend_summary()` printed the day's GLOBAL total against a PER-MODEL ceiling.
Live output during this test:

```
  BEFORE:  83/80 requests today (83/40 video)      <- reads as 104% and 208% over
  REALITY: gemini-3.5-flash-lite 40/80 (40/40 video)
           gemini-3.1-flash-lite 40/80 (40/40 video)
           gemini-3.7-flash       3/80  (3/40 video)   <- every model inside its limit
```

With N allowlisted models the line could show N x the cap while describing a
healthy run — inverting the one question the summary exists to answer. Caps are
per model, documented at `gemini_tracker._model_entry`; the reporting path never
got the memo.

```
  AFTER: today per model — gemini-3.1-flash-lite 40/80 (40/40 video) CAPPED;
         gemini-3.5-flash-lite 40/80 (40/40 video) CAPPED;
         gemini-3.7-flash 3/80 (3/40 video) [day sum 83 requests, 83 video]
```

Google's own PerDay 429 now reads `429-SPENT`, distinctly from our own `CAPPED` —
different operator actions. The sum survives, labelled as a sum. +5 tests.

### 5. Topic gate end-to-end on 211 real channels — 5 fires, all correct

Offline against cached tags, zero cost. Joined to the reviewer's own verdicts:

| topic | share | channel | verdict |
|---|---|---|---|
| gaming | 72% of 1450 tags | Grxnt (`fortnite`, `gamer`, `battle pass`) | **Rejected** |
| gaming | 65% of 1145 tags | Octorious (`playstation`, `ps5`, `dualsense`) | **Rejected** |
| toys_and_kids | 64% of 479 tags | Bricksie (`lego`, `bricklink`) | **Rejected** |
| av_specialist | 53% of 1073 tags | New Record Day (`audiophile`, `hi-fi`, `speaker`) | **Rejected** |
| sports_commentary | 40% of 400 tags | Club 520 Podcast (`podcast`, `nba`) | **Rejected** |

**5 of 5 Rejected. Zero Approved killed.** 20 of 211 channels carry no tags and
can never be dropped. `New Record Day` is one of the channels §12 named.

### 6. The decisive check — the six channels that broke the old gate

`main.off_target_reason`'s docstring called these six "hand-verified off-target".
The 2026-08-21 backtest found **four of them were Approved**, and that gate
measured **-38% discrimination**.

| channel | verdict | tag gate fires? | outcome |
|---|---|---|---|
| Bane Tech | Approved | no | correctly spared |
| DanKamYouKnow | Approved | no | correctly spared |
| NFT TIGERS SPOTON | Approved | no | correctly spared |
| Paul Antill | Approved | no | correctly spared |
| Grxnt | Rejected | gaming 72% | correctly caught |
| Octorious | Rejected | gaming 65% | correctly caught |

**Six for six.** The tag gate splits exactly the way the reviewer did, on the
precise set that inverted the title gate. That is because `phones_and_pcs`,
`generic_gadgets` and `ai_and_crypto` — the categories those four Approved
channels trip — are deliberately **excluded** from `VIDEO_TOPIC_CATEGORIES` on
the measurement in §14.11. The exclusion is doing exactly the work it was added
for.

### Verdict

| component | status |
|---|---|
| Transcript reader | **does not exist and cannot** — captions closed, re-proven on 3 videos |
| Tag topic gate | **works.** 5/5 correct on real data, 6/6 on the historical trap set |
| Excluded-subject veto | **works** on LEGO (refused) and the room-tour control (rescued); ASMR unproven |
| Fail-soft paths | **correct** under a live third-party outage |
| `spend_summary` | **was misreporting; fixed** with 5 tests |
| Suite | 1302 -> **1307 passing**, zero regressions |

Still open: ASMR untested; the 60s-timeout-per-candidate cost when a chain member
is down; and R4, the underlying reason the video tier cannot be backtested.
