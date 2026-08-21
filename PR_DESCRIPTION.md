Attaches a **rescue ladder** to the existing title-based relevance gate, using
only the Gemini free tier. Off by default; the pipeline behaves exactly as it does
today until `GEMINI_ENABLED=true`.

## The problem

`off_target_reason` discards a candidate whose recent video **titles** are
dominated by an off-target vertical. It is free, deterministic, runs on every
candidate, and rejects ~46% of Home Theater ones. It also has a documented
false-negative mode: a genuine prospect whose titles use none of the anticipated
vocabulary. That is how "Jasper Tran - House Design Ideas" was lost, and it is why
`TODOS.md` has carried "Relevance classifier" as *"the differentiated half of the
product"* through two deferrals.

## What this does

| Tier | Input | Runs on |
|---|---|---|
| **1 — text** | channel bio + up to 50 video titles + 50 descriptions | every candidate reaching the gate |
| **2 — video** | ~25s of one representative long-form upload, clipped **server-side** by Google | only flagged candidates tier 1 voted to rescue |

Tier 1's inputs were already being fetched, for free, on every candidate —
`video_descriptions` was pulled for exactly one thing (`find_repeated_email`).
Tier 2 answers what text cannot: is a real person on camera in a real space, or is
this reposted manufacturer footage?

A candidate the gate **flagged** is re-admitted only if **both** tiers confirm it.
A candidate the gate let through is scored and continues **whatever the score
says** — the score is recorded, never used as a gate. A positive
must-match relevance gate was already built, measured and rejected in this repo;
this does not rebuild it.

## Rescue-only: this cannot reduce output

There is no new drop reason. Every failure edge — disabled, no key, 429, timeout,
4xx, malformed, cap reached, no suitable video, unreadable ledger — is
**indistinguishable from this feature not existing**. Nothing here ever writes to
`rejected_handles.json`.

That is the whole safety argument, and it is structural rather than careful: the
only branch that changes control flow is the one that sets `rescued=True`.

## Cost safety

**The guarantee is not in this code.** Per Google's API terms the Gemini API is a
"Paid Service" *only* through a Cloud project with an **active billing account**.
The key belongs to a project with none, so an over-quota request returns
`429 RESOURCE_EXHAUSTED` and cannot be billed. No API reports billing status, so
nothing here can verify it — the model allowlist catches an operator **typo**,
which is a different job, and `config.py` says so at the mechanism rather than
implying a check that does not exist.

Second layer:
- 429 **excluded** from the session's retry forcelist; a `PerDay` 429 latches the
  run and pins the day counter so a same-day re-run issues nothing.
- POST added to `allowed_methods` — without it the 5xx retry would be dead
  config, which this file documents in bold for the sibling session.
- `read_retries=0` (a read retry re-spends a request whose response we lost) and
  `respect_retry_after=False` (urllib3 sleeps the header verbatim, so a
  `Retry-After: 86400` would park the run past the job timeout).
- The response's own `modelVersion` is checked, so we know what **served** us and
  not merely what we asked for.
- Five distinct cap reasons, never one collapsed string, plus a wall-clock brake.

## Why `:generateContent` and not the Interactions API

Google labels this endpoint legacy and recommends `/v1beta/interactions` — which
does **not yet support `video_metadata`**, the clipping field this design rests
on. Google states that limitation explicitly. Sending whole uploads would breach
the requirement and burn the free tier's 8h/day YouTube allowance ~48× faster on a
20-minute source. `TODOS.md` carries the trigger to migrate once clipping lands.

Related: installing Google's own `gemini-interactions-api` skill struck **four of
seven** models an earlier read of the pricing page had accepted. A pricing page
proves a model is billed at zero, not that it is supported. The lockfile is
committed so the allowlist's provenance is recorded.

## Why raw REST and not `google-genai`

Same reason `http_client` refuses `google-api-python-client`: an SDK ships its own
transport, which the autouse guard in `tests/conftest.py` cannot see. A missed
mock would spend real free-tier quota from a test run. Safety, not style — and it
means `requirements.txt` and both `--require-hashes` installs are untouched.
**No new runtime dependency.**

## Verified against the real API, not from documentation

- A 25s clip of a 30-minute source reports **2,476 prompt tokens** (~99/sec,
  matching ~100/sec at `MEDIA_RESOLUTION_LOW`) — so **only the window is
  processed**, which the docs never actually state.
- **True positives:** OCM Reviews, Audio Arkitekts and Pursuit Perfect System —
  all three named in this repo's own comments as genuine prospects — rescue, with
  evidence citing timestamps and specific hardware.
- **True negative:** Linus Tech Tips scores **15** and is not rescued, declined at
  the text tier *without* spending a video request.
- **Median selection** picked an 11,210-view video over the channel's
  122,546-view outlier, as designed.
- **Cache:** a second run served all six verdicts for **zero** requests.

## A bug the tests could not have caught

`_parse_verdict` hardcoded `matches`, the **video** tier's boolean; the text tier
returns `on_niche`. Every well-formed text verdict failed validation, and because
tier 1 gates tier 2, **the entire rescue path was dead** — invisibly, because
"unavailable" is a legitimate outcome.

The unit fixture put *both* keys in every payload, so it was more permissive than
the API it stood in for. Fixed in 2095fe2, with two tests asserting each tier
rejects the other's shape. A fixture must be exactly as strict as the thing it
replaces.

## Tests

**1186 passing, 2.4s. Baseline was 1106 — 80 added, 0 regressions.**

The one existing signature that moved is `_make_session`, which gains a `total=`
parameter so this session can have its own retry budget. `YOUTUBE`, `AIRTABLE`,
`GMAIL` and `INFLUENCERS` are unchanged and asserted so.

## Operator setup

`README.md` §5b: the four-step billing check (including the literal Cloud Console
sentence that *is* the guarantee), the `verify_video.py` probe and the two numbers
to read from it, the four optional Airtable columns and which one may be a Single
select, criteria-authoring rules, and what the run summary means.

Run summary prints on **every** run, zeros included — a line that hid itself when
the count was zero would be missing in exactly the case worth noticing:

```
gemini relevance:  model=gemini-3.5-flash-lite (served: gemini-3.5-flash-lite,
                   allowlisted) — 6 request(s) this run (3 video), 6/300 run cap,
                   6/600 requests today (3/120 video), 0 cache hit(s), ~26k tokens, 27s
gemini verdicts:   3 scored, 3 RESCUED, 0 unavailable
```

`RESCUED` is the number to watch — it is whether the feature earns anything.

## Deliberately not in this PR

Recorded in `TODOS.md` with reasons: the offline backtest against the 146 labelled
rows in `PROSPECT_AUDIT_2026-08-20.md`, multi-window sampling (3×8s — the
multi-part request shape is unverified), and wiring the text score into
`Overall Score` (gated on that backtest, because it changes the meaning of a
column reviewers already use).

`GEMINI_VERIFY_PLAN.md` carries the design and the review that shaped it — 43
findings across three independent voices, including two that corrected claims in
the plan's own first revision, plus the decision audit trail.

## What measurement changed after the first draft

The plan was written, reviewed, then **tested against reality**, and reality moved
it four times. All of it is recorded in `GEMINI_VERIFY_PLAN.md` §2.16-§2.19.

**1. The text relevance criteria were inverted.** Backtested against 96 rows
carrying the reviewer's own Approved/Rejected: `P(Approved | on-niche)` came out
at **27% against a 38% base rate**, and **0 of 5** in Home Theater. The five
channels the criteria rated most on-niche were all rejected. The model was
answering accurately; the question was wrong. The advisory text tier is now
**off by default** on that evidence, and it no longer gates anything.

**2. The reviewer's real screen is brand-versus-creator, not content.** Apartment
Therapy and ADAM Audio were rejected for being a publisher and a manufacturer
while posting genuinely on-topic video. So a creator-vs-brand criterion was added
as a **veto** rather than a score — it caught ADAM Audio exactly right
("a branded watermark throughout and promotional/marketing content from a
manufacturer") and the content ratio then re-admitted it at 2/3, which a
manufacturer should not be. Required criteria are now checked before the model's
own aggregate. First setting that discriminates: **Approved 6/6, Rejected 1/2**.

**3. The free tier's real ceiling is ~100 requests/day, per MODEL.** Measured, not
read: Google refused at 106 on `gemini-3.5-flash-lite` while the other allowlisted
models were untouched. The original defaults (600/day, 300/run) could never bind.
Corrected to 80/day and 70/run, and the ledger became per-model — which is what
makes the free-model fallback possible at all.

**4. Two bugs that only a live run could find.**
- `_parse_verdict` hardcoded `matches`, the video tier's boolean, so every
  well-formed **text** verdict was discarded as malformed — and because the text
  tier gated the video tier, **the entire rescue path was dead**, invisibly. The
  unit fixture carried both keys, so it was more permissive than the API.
- `_may_request` asked the day-cap question **without a model**, reading the
  global counter, so with the preferred model spent and two free models behind it
  the fallback never ran. The isolation fixture lifts the day ceilings, so no mock
  could reach it.

Both now have regression tests that pin the narrower invariant rather than the
symptom.

## Yield: what this does and does not fix

The operator's actual complaint was too few rows, sometimes zero for Home Theater.
Measured with `audit_prospects.py` over 107 tracked rows (78 pass / 29 fail):

```
  outside_search_zone      11     no_declared_country       5
  broadcast_tv              3     below_view_minimum        3
  too_few_longform_videos   2     shorts_only               2
  excluded_topic            1     upload_cadence_too_low    1
  video_below_view_minimum  1   <- the gate that was lowered
```

**Strictness is not the binding constraint.** 6 discovery credits/run buys 600
creators examined at a measured 1 row per 100-150, so 4-6 rows per run across
*both* niches — zero for one niche in one run is arithmetic. That, plus the two
larger gate levers, is recorded in `TODOS.md` rather than acted on unilaterally.

`MIN_VIEWS_PER_VIDEO_RATIO` did go 0.50 -> 0.30 at the operator's direction, and
what it gives up is named in the test rather than deleted from it: the live "Kat
and Sourabh" case (3 of 10, 57k average) was caught before and now passes.

## Note on the diff

This branch forked from `fix/discovery-yield-credit-leak-email-source`, which is
not yet in `main`. Against `main` the diff therefore includes that branch's two
commits (`a1c2728`, `4cafcc4`). They disappear once it merges. My four are
`23cf497`, `0cccc65`, `3b95e13`, `2095fe2`.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
