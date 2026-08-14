# Plan: stop wasting discovery credits; keep statistics on YouTube v3

> **STATUS: implemented 2026-08-14.** C0, C1, C1b, C2 (subscriber floor only),
> C3, C4 and C5 are all in the working tree, plus the per-video floor
> recalibration decided at the approval gate. 665 tests pass. Still open:
> C2's `location` filter (needs a live vendor probe) and Defect 6 (writing a
> `Handle` field to Airtable so already-tracked rows stop being re-bought).
> Decisions taken at the gate:
> 1. Per-video floor → **70% of the settled window must clear 10,000**, applied
>    to both niches; Lifestyle Sofa's 2,000 figure stays overridden.
> 2. The enrichment quota ceiling (C5) is a **prerequisite**, landed before C1.
> 3. C0 landed first as an immediate brake.

Branch: `main` · Commit at analysis time: `15e353d` · Written 2026-08-14

Reported symptom:

```
2026-08-13 20:28:12,027 [INFO] 'Home Theater' so far: 1/2 qualified, 0/1 flagged (16.00 discovery credits spent).
```

16.00 credits at 0.01/creator returned = **1,600 creators billed** to produce
**1 qualified row**. That is a ~0.06% credit-to-row rate. The YouTube
`search.list` path it replaced ran at 15-20% candidate survival. Discovery is
currently far more expensive per row than the source it was meant to improve on.

---

## Part 1 — Root cause of the credit waste

Four compounding defects. #1 and #2 are the dominant pair and together explain
the observed 16.00 credits.

### Defect 1 — `[:target]` discards creators we already paid for

`influencer_discovery.py::discover()` paginates at `PAGE_LIMIT = 50`. The vendor
bills 0.01 per creator **returned**, so one page costs 0.5 credits for 50
creators — and the code knows this; the comment at line 182 says the cost is
"charged whether or not we end up using every account".

But the loop condition is `while len(candidates) < target`, and the return is:

```python
result = list(candidates.values())[:target]
```

With a small `target`, one page satisfies the condition immediately, and then
46 of the 50 paid-for creators are thrown away unexamined.

### Defect 2 — the discarded creators are not remembered, so we re-buy them

In `main.py::_run_discovery_rounds()`:

```python
seen_handles.update(c["handle"] for c in new_candidates)
```

`new_candidates` is the **truncated** list. The 46 creators that were billed but
truncated never enter `seen_handles`, so they are never added to
`exclude_handles`, so the next round's request returns them again and bills again.
The run pays repeatedly for the same creators it keeps throwing away.

### Defect 3 — `target` is derived from rows, but billing is per 50-result page

`target = max(1, int(rows_wanted * CANDIDATE_OVERSHOOT))` with
`CANDIDATE_OVERSHOOT = 1.5`. As the budget fills, `rows_wanted` shrinks, so
`target` shrinks — but the minimum billable unit stays a 50-creator page. The
waste ratio therefore gets **worse** the closer the niche is to its cap.

Reproducing the reported line exactly (`qualified_headroom=2`, `flagged_headroom=1`):

| Round | rows_wanted | target | billed | examined | wasted |
|---|---|---|---|---|---|
| 1 | 3 | 4 | 50 (0.5 cr) | 4 | 46 |
| 2 | 2 | 3 | 50 (0.5 cr) | 3 | 47 |
| … | … | … | … | … | … |
| 32 | 2 | 3 | 50 (0.5 cr) | 3 | 47 |

32 rounds x 0.5 = **16.00 credits**, ~100 creators examined, 1 qualified row.
`DISCOVERY_MAX_ROUNDS = 50` is the only thing that eventually stops it, and the
loop can only exit early when discovery reports dry or the qualified cap fills.

### Defect 4 — the gates that do the dropping are not filtered server-side

`influencer_discovery.py:9` claims the endpoint filters
"`profile_language`, **`location`**, `number_of_subscribers`, `topics`"
server-side. The string `location` appears **nowhere** in either niche's
`discovery_filters`. What is actually sent:

| Sent | Not sent, but a hard discard gate |
|---|---|
| `profile_language: ["en"]` | **`location`** — search zone (US/CA/UK/EU/AU, not IE) |
| `gender` | `min_avg_views` (10,000) |
| `ai_search` (semantic) | `MIN_VIEWS_PER_VIDEO` (10,000) |
| `number_of_subscribers: {min: 2000}` | `MIN_VIDEO_COUNT` (30) |
| `keywords_not_in_description` (off-brand) | `MIN_LONGFORM_VIDEO_COUNT` (30) |
| | `MIN_UPLOADS_PER_YEAR` (10) |
| | `MAX_DAYS_SINCE_LAST_UPLOAD` (365) |
| | Shorts-only |

The search zone was measured as one of the largest single drop buckets (50 of 230
candidates on the 2026-08-11 sample). The vendor supports filtering it and we pay
for it anyway.

Note the subscriber floor sent to the vendor (2,000) is far below the effective
bar the gates enforce, so it screens almost nothing out.

---

## Part 2 — Statistics provenance

**Already correct, and worth locking down before it drifts.** Every value used
for gating, scoring, or writing comes from the YouTube Data API v3:

- `pre_push_drop_reason(...)` reads `stats.get("subscriber_count")` and
  `performance.get(...)` — `get_channel_stats()` / `get_recent_video_performance()`.
- `calc_fake_follower_risk` / `calc_overall_score` read `stats[...]` / `performance[...]`.
- The Airtable record writes `stats["subscriber_count"]`, `performance["avg_views"]`,
  `performance["avg_engagement_rate"]`.

The vendor's own numbers are **dead fields**. `_to_candidate()` populates
`subscriber_count` (from `profile.followers`) and `engagement_percent`, and
nothing anywhere reads either one.

That is the same latent trap as the `BLOCKING_STATES` constant removed in #11:
a field that looks authoritative, sits on the candidate dict all the way through
`process_candidate`, and would silently become load-bearing the moment someone
reads it. A future contributor writing `candidate["subscriber_count"]` would get
vendor data into a column labelled with a YouTube-verified number, with no test
failing.

One legitimate exception to keep: `number_of_subscribers: {min: 2000}` is a
server-side **discovery pre-filter** on vendor data. That is the right use — it
decides what we pay to see, not what we believe. Its only failure mode is a false
negative (vendor undercounts, we never see a valid channel), which costs no
credits.

---

## Corrections after review (2026-08-14)

An independent review refuted three claims in the first draft. All three were
verified against the code before accepting:

1. **The 16.00 figure is a mid-run snapshot, not a total.** The log line at
   `main.py:1198` prints at the end of *every* round and says "so far". The loop
   exits only at `rounds > DISCOVERY_MAX_ROUNDS` (50), so that run was heading to
   **~25.00 credits**.
2. **The survival rate used to size the benefit was the wrong funnel's.** The
   15-20% figure is `search.list`'s. This run examined ~97 creators for 1
   qualified row: **~1%**. So round 1 does *not* fill a 2-row budget, and the
   headline saving is **~12x** (page amortisation across rounds), not 32x.
3. **Two existing tests must be rewritten, not kept green** — see the test plan.

And it surfaced two defects the first draft missed entirely, both verified:

### Defect 5 — nothing on this path enforces the YouTube quota ceiling

`can_afford_search()` has exactly one non-test call site: `discovery.py:160`,
gating `search.list`. `main.py` imports only `get_today_spend` (for the summary
print); `enrichment.py` imports only `record_spend`. The influencers.club path
never calls `search.list` (`run_niche` empties `remaining_keywords` at
`main.py:1347`), so **`QUOTA_CEILING` is consulted nowhere in a discovery run.**
Enrichment spend is recorded and never gated.

Per candidate: 1 unit `channels.list` + 2 units performance, plus up to 6 for
long-form paging and 4 for the email deep scan — floor 3, ceiling 13. This is why
C1 must not be read as "examine everything billed": at 50-100 candidates/round it
converts a bounded money leak into an unbounded quota leak.

### Defect 6 — every already-tracked row is re-bought on every run, forever

The niche tables store a Channel ID, not a handle, so their rows can never enter
`exclude_handles` (the comment at `main.py:1136-1140` admits this). Each tracked
creator is therefore re-returned (0.01 credits) and re-resolved (1 YouTube unit)
on every run, caught only by the `known_channel_ids` check at `main.py:831`. The
cost grows linearly with table size and never stops. This needs no vendor
feature: add a `Handle` field to the Airtable schema, write `stats["handle"]`, and
feed those handles into `_discovery_exclude_handles`.

---

## Proposed changes

Ordered by dependency. C0 is a one-line mitigation that should land first.

### C0 — Spend brake, landed first (independent of everything else)

`INFLUENCERS_MAX_DISCOVERY_CREDITS_PER_RUN` defaults to **50** credits = 5,000
creators, and the run is structurally capable of hitting it (2 niches x 50 rounds
x 0.5). Lower the default, and give `--test` an explicit discovery-credit cap.

Also correct `main.py:1650-1652`, which claims `--test` "bounds discovery spend
too". The reported run refutes that: capping rows *shrinks* `target`, which makes
the waste-per-round **worse**.

### C1 — Never discard a creator we were billed for (fixes defects 1-3)

**Slice at headroom, carry the rest. Explicitly NOT "examine everything billed."**

- `discover()` returns **all** accumulated candidates; `target` becomes a floor
  deciding whether to fetch another *page*, never a slice on results already paid
  for.
- `_run_discovery_rounds` adds **every returned handle** to `seen_handles`, so a
  billed creator is never re-requested. This is the fix for the re-buying loop.
- It hands `push_until_full` only `backlog[:target]` and keeps the remainder in an
  in-run backlog, drained before any new vendor request.

The two halves matter independently: returning everything stops the re-buying,
slicing at headroom stops the quota blow-up. Shipping the first without the
second is Defect 5's failure mode.

Effect: pages are amortised across rounds instead of re-bought each round.
**~25 credits → ~2 credits** for the same rows, with per-round enrichment
unchanged from today.

Termination note: a round that drains the backlog issues no vendor call but still
increments `rounds`, so `DISCOVERY_MAX_ROUNDS` can expire having bought little.
Count **vendor requests**, not loop iterations, against that cap.

### C1b — Bound `push_until_full`'s enrich-then-discard tail

`main.py:698` calls `build_record()` (full 3-13 units) **before** the per-bucket
headroom check at `:705`. The outer break at `:694` fires only when *both*
budgets are full — and the flagged budget structurally cannot fill for Lifestyle
Sofa (`min_channel_age_months` is `None`, so `qualify()` never returns anything
else). So once qualified is full, every remaining candidate is fully enriched and
then thrown away as "skipped".

Not fixable by reordering, since qualification is only known post-enrichment.
Bound it instead: stop when the qualified budget is full and the flagged budget
has produced nothing in the last N candidates.

### C5 — An enrichment-side quota ceiling (prerequisite for C1, per defect 5)

Add a `can_afford_enrichment()` analogue, called at the top of
`process_candidate`. Without it the discovery path has no quota ceiling at all,
and any change to how many candidates get examined is unbounded by construction.

### C2 — Send the filters the gates actually enforce (fixes defect 4)

Reordered after review: **the subscriber floor is the primary lever, not
`location`.** It is one line of config with no vendor-probe risk, and it is
currently two orders of magnitude below the effective bar (see the per-video
floor discussion below).

- **Raise `number_of_subscribers.min`** from 2,000 toward the real floor. No probe
  needed; the field is already in use.
- `location` — the search-zone country set, minus Ireland. Documented as
  supported at `influencer_discovery.py:9` but never sent. Needs a live probe.
- Any available view / upload-recency filter, if the vendor exposes one.

**Unverified filter names must not be shipped.** A wrong field name either 400s
the request (which silently disables discovery, since `_post` fails soft and
returns `None`) or is ignored while appearing to work. Probe first, exactly as the
existing comments describe for `gender` and `keywords_not_in_description`.

Docstring correction while here: `influencer_discovery.py:9` claims both
`location` and `topics` are filtered server-side. Neither is sent — but `topics`
is **deliberately** unused (`main.py:106-111`, `:163-166`: pinning topics would
exclude the furniture/homebody and decor/house-tour creators). The correction must
distinguish "missing, should be added" (`location`) from "deliberately not used"
(`topics`), or the next reader will "fix" `topics` and narrow the funnel.

**Unverified filter names must not be shipped.** A wrong field name either 400s
the request (discovery silently disabled, since `_post` fails soft and returns
`None`) or is ignored while looking like it works. Probe first, exactly as the
existing comments describe for `gender` and `keywords_not_in_description`.

### C3 — Make vendor statistics structurally unable to reach a verdict

Drop `subscriber_count` and `engagement_percent` from the candidate dict in
`_to_candidate()`. Keep `handle`, `channel_title`, `influencers_user_id`,
`matched_keywords`.

Then add a test asserting a discovery candidate carries no statistic field, so
the guarantee holds mechanically rather than by convention.

Consequence to check before doing it: `channel_title` from `profile.full_name` is
used by DO NOT CONTACT checkpoint 1 (the free pre-enrichment title match). That
one stays — it is an identifier, not a statistic.

### C4 — A credit-efficiency line in the run summary

Report credits spent, creators billed, creators examined, and rows produced. The
reported symptom was only visible because the credit figure happened to be in the
log next to the row count; make that ratio explicit so a regression is obvious in
one line.

---

## The likely real cause of the ~1% yield — needs a decision, not a patch

`MIN_VIEWS_PER_VIDEO = 10_000` (`main.py:298`, gated at `:512`) is checked against
`min_views`, which `enrichment.py:695` computes as
`min(performance_views)` — the **weakest settled video** in the newest-10 window.

So a channel must clear 10,000 on *every* recent video **and** average 10,000. For
a channel averaging exactly 10k, roughly half its videos sit below the average, so
its weakest one almost certainly fails. The effective bar is therefore not "10,000
average" but something closer to a **25,000-50,000 average**.

The brief says "min of 2k+ views" (Lifestyle Sofa), later unified to 10,000
average. Nothing authorised a jump to an effective 25-50k. This gate landed in PR
#7 and is the most plausible single explanation for a 1% survival rate — no amount
of credit-efficiency work recovers rows that this gate discards.

This is a calibration decision for the user, not something to change quietly. See
the open questions.

---

## Test plan

New tests, all offline against the existing HTTP-blocking fixture:

1. `discover()` returns every creator from a billed page even when `target` is
   smaller than the page — the direct regression test for defect 1.
2. A creator returned in round 1 appears in round 2's `exclude_handles` — the
   regression test for defect 2, and the one that pins the re-buying loop shut.
3. The backlog is drained before any second vendor request is issued, and
   `push_until_full` receives at most `target` candidates (the C1 quota guard).
4. A discovery candidate dict contains no `subscriber_count` / no
   `engagement_percent` key (C3).
5. `_run_discovery_rounds` with headroom 2 and a 50-creator page issues exactly
   one vendor request — the end-to-end assertion that the reported waste is gone.
6. Credit accounting still totals the vendor's reported `credits_cost` after the
   truncation change.
7. `can_afford_enrichment()` stops `process_candidate` at the ceiling (C5).

**Two existing tests must be REWRITTEN, not kept green** (the first draft wrongly
claimed the suite stays green):

- `tests/test_influencer_discovery.py:125-127`
  `test_paginates_until_target_then_stops` asserts `len(got) == 3  # trimmed to
  the target`. C1 deliberately removes that trim; the test becomes an assertion
  about how many *pages* were fetched.
- `tests/test_influencer_discovery.py:73-80` `test_accounts_become_candidates`
  asserts exact dict equality including `subscriber_count` and
  `engagement_percent`. C3 removes both keys.

Also stale after C3: `influencer_discovery.py:22`, `:137-138`, and
`tests/test_influencer_discovery.py:5`.

Existing suites that must stay green otherwise: `test_discovery_refill.py`,
`test_prepush_gate.py`, `test_run_niche_caps.py`; full suite 652 before, 652 minus
the 2 rewrites plus 7 new after.

---

## Not in scope

- The persistent server-side exclusion list for bases over the 10,000-handle cap.
  Already noted in `_prepare_exclude`. Related note: `_prepare_exclude` truncates
  the union at 10,000 on an **alphabetical** sort, so the docstring's claim that
  blocklist handles are never dropped is false past that boundary. C1 grows
  `seen_handles` 12-25x faster toward it. Worth a follow-up; not this change.
- Reworking `CANDIDATE_OVERSHOOT` for the `search.list` keyword path. C1 makes it
  page-aware for discovery only; the keyword path bills no money.
- The relevance classifier / `DEFAULT_NICHE_MATCH` (already in `TODOS.md`).
- Whether 0.01/creator is the right vendor plan.

## Open questions for the user

1. **The per-video 10k floor.** See the section above: as written it demands a
   ~25-50k average, well beyond the brief's 10,000. Is that intended? This is
   probably worth more rows than every credit fix combined.
2. **C2's `location` filter** depends on a live vendor probe. If the field name or
   country format can't be verified, C2 ships only the subscriber-floor half. C0,
   C1, C1b, C3 and C5 are all vendor-independent and land either way.

## Applied from the /simplify review (four parallel reviewers)

- **Flagged hunt short-circuits when flagged is IMPOSSIBLE.** `qualify()` returns
  QUALIFIED unconditionally when `min_channel_age_months` is None, so for
  Lifestyle Sofa no candidate can ever land in the flagged bucket.
  `push_until_full` now takes `flagged_possible` and stops immediately instead of
  spending `FLAGGED_ONLY_PATIENCE` (10) enrichments to rediscover a fact the
  niche config already states. Exact rather than probabilistic.
- **`_can_afford(cost, what)` extracted**; `can_afford_search` and
  `can_afford_enrichment` are now thin wrappers over one implementation.
- **The discovery subscriber floor is derived per-niche** from that niche's own
  `min_avg_views` via `DISCOVERY_SUBSCRIBER_FLOOR_RATIO` (0.5), not a hardcoded
  global. Every other qualification lever is per-niche, and these two have
  diverged before. A test pins the relationship.
- **The patience counter's lifecycle is co-located** (increment and reset in one
  place, guard computed once).

### Reviewed and deliberately NOT changed

- **Dropping `min_views` as derivable from `settled_views`.** Both are computed
  from the same list on adjacent lines, so they cannot drift — unlike the
  BLOCKING_STATES case this session removed, there is no trap here. It stays as
  the named value the drop log reports.
- **Caching `get_today_spend()` to avoid a per-candidate disk read.** Measured as
  ~230-460 extra file opens per run of a tiny JSON file, against 100ms+ network
  calls per candidate — not a bottleneck. An in-memory total would re-open the
  fail-open direction the atomic-write design exists to prevent.
- **Removing the `dry` flag.** It prevents a wasted billed vendor request while
  the backlog drains; the suggested alternative changes behaviour.

### Follow-up worth doing separately

The `search.list` keyword loop in `run_niche` and the discovery loop in
`_run_discovery_rounds` now maintain the same refill invariant twice with
different vocabulary (`seen_ids`/`rounds` vs `seen_handles`/`vendor_requests`).
They will drift the next time someone tunes termination in one. Extracting the
shared "spend the budget, stop when full or dry" skeleton is a real cleanup, but
it is a refactor of both paths and belongs in its own change.

## Revised savings estimate

| Change | Credits | YouTube quota | Vendor probe needed |
|---|---|---|---|
| C0 brake | caps the blast radius | — | no |
| C1 + C1b | ~25 → ~2 per niche | unchanged per round | no |
| C2 subscriber floor | fewer junk creators billed | fewer wasted enrichments | no |
| C2 `location` | removes a large drop bucket | same | **yes** |
| C3 | — | — | no |
| C5 | — | adds the missing ceiling | no |
| Defect 6 (handles) | stops re-buying tracked rows | 1 unit/row/run saved | no |
