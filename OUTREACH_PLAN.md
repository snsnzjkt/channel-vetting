<!-- /autoplan restore point: ~/.gstack/projects/snsnzjkt-channel-vetting/feat-exclude-off-brand-topics-autoplan-restore-20260814-015752.md -->

# Review & Outreach System — Plan (rev 2, post-review)

Turn vetted prospects sitting in Airtable into sent, logged, auditable
outreach — with a human approving every channel first, and a removed channel
never reachable again.

The vetting pipeline (discovery → enrichment → scoring → Airtable push) is
built and documented in `CLAUDE.md`. This plan covers only what happens
**after** a row lands in a niche table.

> **rev 2** integrates the `/autoplan` review below. Five changes are
> structural, not cosmetic: the duplicate-send guard was **wrong** (Airtable
> has no unique constraints), the Airtable button had the **wrong blast
> radius** (it renders per-row and would send to everyone), `push_record`
> **cannot** be reused for the ledger, the Gmail client **escapes** the test
> network guard, and the "permanent suppression" claim was **false** for the
> common case. Rev 1's versions of those five are recorded in the review
> report as rejected, so nobody re-proposes them.

---

**STATUS: APPROVED** at the `/autoplan` final gate (2026-08-14), with the four
prerequisites below held as genuinely blocking. First implementation task is
**T1** (`outreach_ledger.py`), which is buildable against stubs and needs none
of them.

## Gate decisions recorded (D1-D6)

Recorded at the review gate. **Nothing is built or sent on the strength of
these alone** — implementation needs an explicit go-ahead, and `--send` has
its own gates below.

| # | Decision | Effect on this plan |
|---|---|---|
| D1 | Capacity ~10-40 collaborations/month → build, scale caps to match | `OUTREACH_DAILY_CAP` defaults **10**, not 50. `DAILY_QUALIFIED_CAP` should be re-tuned to review capacity (see R10). |
| D2 | Add opt-out + postal address to both templates | `OUTREACH_FOOTER_TEXT` + `OUTREACH_UNSUBSCRIBE_URL` are **required** config with no default; `--send` refuses to start without them. |
| D3 | Dedicated sending domain, warmed 2-4 weeks | Transport targets a separate domain; replies forwarded. Corporate domain is never the cold sender. Build step for warmup added. |
| D4 | Split removal: `Rejected` (reversible) + `Do Not Contact` (permanent) | Two reviewer actions. Only the second writes a suppression row. |
| D5 | **Button stays cut** — per-row `☑ Queue for outreach` + scheduled run | Requirement 3 is delivered as a queue-then-schedule flow, not a run-everything button. Webhook endpoint stays in `TODOS.md`. |
| D6 | Plan approved; clear the 4 prerequisites, then start T1 | The six taste decisions in the audit trail stand as decided. |

---

## What already exists (do not rebuild)

Verified by reading the code.

| Requirement | Already provides it | Where |
|---|---|---|
| Reviewer workflow state | `Status` single-select, default `New` | `config.py:51` |
| Reviewer state survives pipeline re-runs | `PROTECTED_UPDATE_FIELDS = ("Status", "Notes")` stripped from every PATCH | `airtable_client.py:243` |
| Search / filter / sort / auth / mobile | Airtable's own grid + Interface Designer | Airtable |
| Permanent, fail-closed suppression | `do_not_contact.fetch_blocklist()` — handle+email+name, 3 checkpoints, aborts run if unreachable | `do_not_contact.py` |
| Formula-injection-safe filters | `_quote_formula_value()` | `airtable_client.py:66` |
| CSV-injection-safe writes | `csv_safe()` | `main.py:568` |
| Retrying HTTP, POST excluded from retries | `http_client.py` sessions | `http_client.py:57-76` |
| Real-HTTP test guard at the `requests` chokepoint | autouse `HTTPAdapter.send` patch | `tests/conftest.py:23-43` |
| Prospect-day clock | `prospect_day.today_iso()` | `prospect_day.py` |
| Schema-option preflight pattern | `_status_option_exists()` | `audit_blocklist.py:73-95` |

**Correction to rev 1:** rev 1 cited `config.py:51` as evidence the live
`Status` field has five options (`New/Reviewing/Approved/Rejected/Contacted`).
It does not — that line defines `DEFAULT_STATUS = "New"` only. The five values
exist **solely in a comment** (`airtable_client.py:237-238`), and
`audit_blocklist.py` exists *because that comment is not trustworthy*: it
preflights the live schema before writing a new `Status` value, and records
that this repo's `AIRTABLE_TOKEN` **lacks schema-read scope (403 on the meta
API)**, so the authoritative check is unavailable. **No code in this repo has
ever written `Contacted`.** See R1.

---

## NOT in scope

Deferred deliberately. Each goes to `TODOS.md`.

- **Custom web review app.** Rejected on DRY — Airtable already ships the grid,
  search, sort, auth, permissions, and mobile.
- **The Airtable button → webhook trigger.** Cut from v1: it needs a publicly
  reachable authenticated HTTPS endpoint that does not exist in this
  architecture, and a button field renders **per row** (see R4).
- **Drip / follow-up sequences.** v1 defines touch 1 and *reserves* touch 2 via
  the campaign label. No cadence engine.
- **A/B testing framework.** Reinstate once reply data exists.
- **Relevance classifier** (the real fix for `DEFAULT_NICHE_MATCH`).
- **Inbound creator-program page** — the strongest reframing found; separate work.
- **Reply *parsing*.** v1 tracks reply state by hand; no inbox NLP.
- **CRM sync.**

---

## Architecture

Airtable is the system of record and the review surface. **One** entry point
sends. Two new Airtable tables hold the ledger and the history.

```
                      ┌────────────────────────────────────────┐
                      │  Airtable (system of record + review)  │
  main.py ──push────► │                                        │
  (existing, +5       │  Home Theater  ┐  Status=New           │
   free fields)       │  Lifestyle Sofa┘                       │
                      │                                        │
                      │  Interface page "Review queue"         │
                      │   name│Source│About│recent titles      │
                      │   ▸ ✓ Approve  ▸ ✗ Reject  ▸ ⛔ DNC     │
                      │   ▸ ☑ Queue for outreach               │
                      │                                        │
                      │  DO NOT CONTACT ◄──── only ⛔ writes    │
                      │  Outreach Log  ◄─┐ (ledger + guard)    │
                      │  Audit Trail   ◄─┤ (history)           │
                      └──────┬───────────┼────────────────────-┘
                             │           │
                  read Queued│           │claim / verify / settle
                             ▼           │
                      ┌──────────────────┴─────────────────────┐
                      │  outreach.py  — the ONLY sender        │
                      │   scheduled run, single concurrency    │
                      │   group, startup lease row             │
                      │   ├─ niches.py          (extracted)    │
                      │   ├─ text_safety.py     (extracted)    │
                      │   ├─ outreach_templates.py             │
                      │   ├─ outreach_ledger.py  claim+day cap │
                      │   └─ mailer.py  → http_client.GMAIL    │
                      └──────────────────┬─────────────────────┘
                                         ▼
                            Gmail REST (dedicated domain)
```

### New / changed modules

| File | Job |
|---|---|
| `text_safety.py` | **NEW, extracted.** `csv_safe()` moved here + its inverse `csv_unsafe()`. `main.py` imports from it. Prevents `outreach.py` importing the whole pipeline (R7). |
| `niches.py` | **NEW, extracted.** The `NICHES` registry + template mapping. Same reason. |
| `outreach_ledger.py` | Claim-verify-send protocol, the ever-sent guard, the prospect-day cap, `--reconcile`/`--settle`. **The riskiest piece — built first, against stubs.** |
| `outreach_templates.py` | Keyword-cluster templates, render with escaping + header-injection stripping + URL validation. |
| `mailer.py` | Gmail REST via a new `http_client.GMAIL` session. |
| `outreach_airtable.py` | **New functions** — `get_queued_prospects()` (paginated), `find_rows_by_field()`, `create_row()`, `patch_row()`. **Does NOT reuse `push_record()`** (R3). |
| `audit_trail.py` | Append-only history writes (one function; folded here rather than its own module). |
| `outreach.py` | Entry point + run summary + exit codes + SIGINT handling. |

---

## Requirement 1 — Review interface

Airtable native, but using the whole product, not just filtered grids.

### 1a. Four fields added to both niche tables

The reviewer's actual question is *"is this creator a good brand fit for
luxury home-theater seating?"* — and **not one existing field answers it.**
All twelve are audience/quality metrics. So the decision is really made in a
YouTube tab, 80 times a day.

Three of the four are **already in payloads the pipeline fetches and
discards** — zero extra quota, zero extra API calls, ~12-line diff in
`main.py`'s record dict:

| Field | Source | Cost |
|---|---|---|
| `Handle` | `stats["handle"]`, already fetched at blocklist checkpoint 2 | free — **and required for suppression to work at all** (R2) |
| `About` | `stats["description"]`, full untruncated text already returned | free |
| `Recent Video Titles` | `snippet.title` from the `videos.list` items already iterated (`enrichment.py:542`) | free |
| `Attribution Code` | generated per row, not fetched | free |

`About` and `Recent Video Titles` go through `csv_safe()`.

**`Channel Avatar` was proposed and is REJECTED — do not re-add it.**
`snippet.thumbnails` is genuinely in the `channels.list` response and genuinely
discarded (`part=snippet` at `enrichment.py:391`; nothing reads `thumbnail`),
which is how it got into the first draft as a fourth "free" field. But:

- **It does not answer the fit question.** A logo or a face does not
  distinguish a home-cinema reviewer from a homesteading vlogger. That was the
  entire justification for this group of fields.
- **"Free" describes the URL, not the field.** Rendering it as an image needs
  an **Attachment** field, which makes Airtable download and STORE the file
  (attachment quota), and URL-sourced attachment writes are asynchronous and
  can fail without surfacing an error. As plain text it renders as a link and
  gives no visual benefit at all.
- **The URLs rot.** `yt3.ggpht.com` URLs carry size/crop suffixes and change
  when a creator updates their picture, so a stored URL goes stale and a stored
  attachment silently becomes a frozen snapshot.
- It would sit permanently blank on every existing row unless backfilled, and
  each extra field widens the `push_record` payload — where one wrong field
  name fails the WHOLE record.

### 1b. An Interface Designer record-review page per niche

Grid views cannot show "23 of 47," which is the missing progress signal for
someone working a daily queue. The Interface record-review layout can, natively.

- Left: `Channel Name`, `Source` (the matched keywords — today's *only*
  relevance signal), `About`, `Recent Video Titles`, `Subscriber Count`, `Avg Views`.
- Right: `Status`, `Rejection Reason`, `Contact Method`, `Notes`.
- Actions: `✓ Approve`, `✗ Reject`, `⛔ Do Not Contact`, `☑ Queue for outreach`.
- Grid views stay for ops and export.

### 1c. Views — with sort, grouping, field order, and row height specified

Rev 1 specified filter and sort only. In a grid, the information hierarchy
*is* the column order, the hidden set, and the row height, so leaving those out
hands the implementer `main.py`'s dict insertion order — which buries `Email`
tenth and puts `Channel ID` third.

**Sort correction.** Rev 1 sorted the queue by `Overall Score` desc. That is
wrong: `DEFAULT_NICHE_MATCH = 70.0` (`main.py:176`) is a **constant for every
channel**, weighted `0.10` (`scoring.py:35`), so `Overall Score` is ~90%
channel *size* and **0% brand fit**. It ranks a 400k-sub gaming channel above a
12k-sub home-cinema reviewer, and invites "score is high, click Approve."
Post-2026-08 every surviving row already clears every floor, so the score's
range is compressed too.

→ **Group by `Qualification`, then sort `Date Added` desc, then `Subscriber
Count` desc.** Date-first because this is a daily drip, not a backlog: the
reviewer should see immediately when yesterday's batch didn't drain.

Per niche table:

| View | Filter | Notes |
|---|---|---|
| `Pending Review` | `Status` is `New` or `Reviewing` | frozen: `Channel Name`. Visible in order: name, URL, `Source`, `About`, `Recent Video Titles`, `Subscriber Count`, `Avg Views`, `Qualification`, `Status`, `Notes`. Row height Medium. Description states what an empty view means. |
| `Needs a second look` | `Status` = `Reviewing` | `Reviewing` becomes a real parking lot, not a synonym for `New` |
| `Reviewing > 3 days` | + staleness | catches abandoned parks |
| `Approved — awaiting outreach` | `Status`=`Approved` AND `Email`≠'' AND `Outreach Ineligible Reason`='' AND (`Last Send State`='' OR =`NotSent`) | **drains on the ledger, not on `Status`** (R5) |
| `Queued for outreach` | `Send Requested At` ≠ '' AND `Last Send State`='' | the cooling-off window |
| `Approved — no email` | `Status`=`Approved` AND `Email`='' | worked via `Contact Method` |
| `Manual outreach queue` | `Contact Method` starts `Manual` | exit condition is `Not contactable` |
| `Rejected — not yet suppressed` | `Status`=`Rejected` AND DNC link empty | makes a half-completed removal visible |
| `Removed` | `Status` = `Rejected` | grouped by `Rejection Reason` |
| `Contacted` | `Status` = `Contacted` | sorted by `Last Contacted At` — **a lookup that now exists** (rev 1 sorted on a nonexistent field and was unbuildable at that line) |

On `Outreach Log` (rev 1 specified **no views on either new table**):
`Needs attention — NotSent`, `Stranded — MaybeSent`, `Sent today`.

### 1d. New reviewer fields

| Field | Type | Why |
|---|---|---|
| `Rejection Reason` | Single select: `Off-niche`, `Audience mismatch`, `Content quality`, `Brand-unsafe`, `Already partnered`, `Other` | Without it, review is a terminal sink and nobody can answer "what share of rejections were off-niche?" — the exact feedback loop that would improve the keyword list |
| `Contact Method` | Single select: `Email — pipeline`, `Manual — YouTube`, `Manual — website form`, `Not contactable` | gives the no-email dead end an exit |
| `Reply State` | Single select: `No Reply`, `Replied`, `Interested`, `Declined` | the minimum learning loop — set by hand from the shared inbox |
| `Send Requested At` | Date w/ time | set by `☑ Queue for outreach` |
| `Outreach Ineligible Reason` | Single line text | written on skip so the queue view stops lying |
| `Last Send State` / `Last Contacted At` / `Provider Message ID` | Rollup / Lookup | via the linked record (R5) |
| `Status Changed At` / `Last Modified By` | Date / Airtable native, scoped to `Status` | the only way an automation can populate `Actor` (R9) |

### 1e. Status vocabulary — reuse, but **verify before writing**

Map the requirement's terms onto the existing options: Pending Review → `New`
(+`Reviewing`), Approved → `Approved`, Removed → `Rejected`, Contacted →
`Contacted`. Do not rename or add: `typecast=True` silently *creates* a missing
single-select option, mutating the live schema and dropping rows out of saved
views.

**But `Contacted` has never been written by this repo and cannot be confirmed
(no schema scope).** So: all single-select writes from outreach code send
**`typecast=False`**, so an unknown option 422s loudly instead of being
created. First `Contacted` write is gated behind `--check-setup` (below) and
an explicit `--i-verified-the-option` opt-in when the preflight returns
`None` — the same contract `audit_blocklist.py` already uses.

---

## Requirement 2 — Only approved are eligible; removed are permanent

### 2a. Eligibility (positive gate)

`get_queued_prospects()` selects, server-side, on
`Status = 'Approved'` **AND** `Send Requested At` is set **AND** `Email` ≠ ''
**AND** `Outreach Ineligible Reason` = '' — values through
`_quote_formula_value()`, with `offset` pagination (rev 1 specified none; at
thousands of rows a single GET truncates silently).

`New`, `Reviewing`, `Rejected`, `Contacted` are ineligible by construction.
There is no default-include path.

### 2b. Permanence — and why rev 1's version was **false**

Rev 1 claimed writing a DO NOT CONTACT row makes removal permanent "in both
the vetting pipeline and outreach, forever, without new code." That was wrong
in two ways:

1. **The handle index would have been empty.** The niche rows store
   `Channel URL = https://www.youtube.com/channel/UC…` (`main.py:804`).
   `fetch_blocklist()` indexes handles via `normalize_handle(FIELD_URL)`
   (`do_not_contact.py:211`), and `normalize_handle()` **requires a literal
   `@`** (`enrichment.py:46-57`). A `/channel/UC…` URL yields `""`. `main.py:927`
   says it outright: *"The niche tables store a Channel ID, not a handle."*
   So suppression would have rested on **email** (blank for ~56% of rows per
   the README's own 28%→44% coverage measurement) and **name** (casefold-exact,
   and the creator can rename at will).
2. **A reviewer in a grid cannot call a Python function.** Rev 1 delivered
   permanence as `remove_prospect()`. Reviewers would set `Status = Rejected`
   — the obvious one-click action — and no suppression row would be written.
   The guarantee would fail silently for every removal made the normal way.

**Fixed:**

- `Handle` is written to both niche tables at push time (free, R2), so the
  DNC row carries a handle that survives `normalize_handle()`. A test asserts
  the written URL survives a `normalize_handle()` round trip.
- **`⛔ Do Not Contact` is an Airtable automation**, not a Python helper: it
  sets `Status = Rejected`, links and creates the DNC row (handle + email +
  name), and writes the Audit Trail row. `remove_prospect()` remains for
  scripted use only.
- Per **D4**, `✗ Reject` is the *reversible* action and writes **no** DNC row.
  Only `⛔` is permanent. `Rejected — not yet suppressed` is therefore an
  expected state, not an error.
- DNC writes use the **maintenance token**, not the CI-scoped one (R12).

### 2c. Send-time re-check uses all three keys

Rev 1 re-checked by email only. `Blocklist.match()` takes handle, email, and
name because *"a false positive costs one lead, a false negative is the harm
being prevented"* (`do_not_contact.py:40-44`) — checking one of three at the
single irreversible moment inverts that. Now passes **handle + email + name**
(all three on the row), with name run through `csv_unsafe()` first (R6).
Blocklist unavailable → **abort before any send**.

---

## Requirement 3 — One action, and a duplicate guard that actually works

### 3a. The button is cut. Queue-then-schedule instead.

Rev 1's "Run outreach" button was wrong on three counts:

- **Blast radius.** A button *field* renders **per row**. A reviewer clicks it
  on row 14 and it sends up to 50 emails to everyone else. That is an
  accidental mass send in week one.
- **No receiver.** An Airtable `Send request` action needs a publicly
  reachable authenticated HTTPS endpoint with TLS, HMAC verification, replay
  protection, and a way to launch a 20-minute job from a request that must
  return in ~30s. None of that exists here, and rev 1 budgeted it as a no-code
  schema bullet.
- **Risk inverted.** `--dry-run` is the CLI default, but the automation must
  pass `--send` — so the safe surface belonged to engineers and the surface a
  non-technical reviewer touches had no dry run, no confirm, no undo, and no
  result (fire-and-forget), giving them every reason to click again.

**Replaced with:** a per-row `☑ Queue for outreach` that stamps
`Send Requested At`, and a **scheduled** `outreach.py --send` that picks queued
rows up. This gives correct blast radius (one row, one click), a real
cooling-off window (de-queue before the next run), per-row visible state via
the R5 lookups, and **a single entry point** — which is also half the fix for
R4 below.

### 3b. The duplicate guard — rev 1's version did not work

Rev 1's claim/settle was described as "a PATCH-or-create keyed on
`{Channel ID}:{campaign}`", with failure mode F11 mitigated by *"claim key
makes the second run's claims collide → skip."*

**Airtable has no unique constraints.** "PATCH-or-create" is necessarily
read-then-write (exactly `airtable_client.py:266-271`'s shape). So:

```
run A: GET key → 0 rows
run B: GET key → 0 rows
run A: POST claim              run B: POST claim
run A: SEND  ─────────────►    run B: SEND  ─────────────►   TWO EMAILS
```

Nothing collides. Two claim rows exist and both emails are already gone; a
*third*, later run detects it — after the damage. Rev 1's F6 described
*detection* and called it *handling*. And the stated mitigation, a CI
`concurrency` group, serialises **one workflow in one repo** — `CLAUDE.md`
already documents that blind spot — while rev 1's own design had *three*
uncoordinated triggers (button, local CLI, cron).

**Fixed — four layers, in order:**

1. **Single entry point.** Button cut (3a). One scheduled workflow, its own
   `concurrency` group, `cancel-in-progress: false`.
2. **Startup lease.** A single-row lock in Airtable, claimed by PATCH
   (addressed at a known record id, therefore retry-safe), holder + timestamp
   checked at startup, released in a `finally`, with a stale-lease age
   threshold.
3. **Claim-verify-send.** POST the claim, then **re-read every row for that
   key**. If more than one exists, the row with the lexicographically higher
   Airtable record id **aborts before sending**. Deterministic tiebreak; no
   send on ambiguity. This is the only layer that survives two genuinely
   independent processes, and it is implementable today.
4. **Ever-sent guard, campaign-independent.** The question is *"has this
   Channel ID ever been `Sent`?"* — not "under this campaign." Rev 1's
   `OUTREACH_CAMPAIGN` defaulted to `<niche>-<YYYY-MM>`, so **every key
   changed on the 1st of the month** and every previously-emailed row became
   claimable again. Campaign is now a **label**; deliberate re-contact needs
   `--allow-recontact`. The month component, where used, comes from
   `prospect_day`, not `datetime.now()` — a UTC runner and a UTC+8 dev machine
   otherwise mint two different keys for ~8 hours on the 1st.

### 3c. Four send states, because "Failed" was a duplicate-send path

Rev 1 had three states and annotated the failure branch *"(nothing was
delivered)"*, making Gmail 5xx retryable. A 5xx or a read timeout means
**unknown**, not "not delivered" — the message may be queued and the response
lost. `http_client.py:176-191` already spells out this exact asymmetry for
influencers.club, and `CLAUDE.md` applies it to money; rev 1 quoted that
reasoning to justify the claim and then violated it one branch later, for
irreversible email.

| State | Meaning | Retryable |
|---|---|---|
| `Claimed` | claim written, send not yet attempted | no — reconcile |
| `Sent` | provider returned success + message id | never |
| `NotSent` | **provably** not delivered: connect error, 4xx message rejection, pre-flight render/attachment/validation failure | yes, via `--retry-failed` |
| `MaybeSent` | 5xx, read timeout, connection dropped mid-response, unknown exception | **never auto-retried** — human reconcile |

Over-counting `MaybeSent` is the safe direction. `--retry-failed` **PATCHes the
existing row** back to `Claimed`; it never POSTs a second row under the same key.

### 3d. Send-path safety

- **`--dry-run` is the default and writes NOTHING to Airtable** — no claim, no
  `Contacted` PATCH, no audit row. Stated explicitly because the wrong answer
  is catastrophic: a dry run that POSTs claims burns every idempotency key and
  the real `--send` then skips 100% of the batch, having sent nothing while
  looking successful. A test asserts zero write calls.
- `--dry-run` and `--send` are in an `argparse` mutually exclusive group.
- **`--dry-run` writes each rendered message to
  `outreach_preview/<campaign>/<channel_id>.eml`** (RFC-822, both MIME parts,
  attachments) and prints one fully-rendered message inline. The operator's
  real question before authorising 50 brand emails is *"what does it say with
  this creator's name in it?"* — a recipient count doesn't answer that.
  `.eml` opens in any mail client, so HTML rendering and attachment sizes are
  checkable too. Mirrors `cleanup_external_duplicates.py`'s print-everything-
  before-`--confirm` precedent.
- **`OUTREACH_DAILY_CAP` (default 10 per D1), counted from the Outreach Log's
  `Claimed At` against `prospect_day.today_iso()`** — and that read **raises**
  on failure rather than returning 0, for the documented `count_added_today()`
  reason: a silent 0 reads as "nothing sent today" and hands out a full budget.
  Rev 1 had only a per-*run* cap, which is not a cap: five runs before lunch
  was 250 emails.
- **Per-address dedupe.** A channel can legitimately be tracked in *both*
  niche tables (`CLAUDE.md`), and rev 1 deliberately keyed on Channel ID
  "because two channels can legitimately share an agency email address" — so
  an agency fronting five approved channels got five near-identical emails in
  one run. Now: a normalized-address set per prospect day; second and later
  hits skip and log which channel they belonged to. Protects deliverability
  more than the 2s pacing does.
- **Footer required (D2).** `OUTREACH_FOOTER_TEXT` and
  `OUTREACH_UNSUBSCRIBE_URL` have **no defaults**; `--send` refuses to start
  without them, with an error naming CAN-SPAM/PECR. This converts a legal
  exposure into a startup failure the operator cannot skip. The *wording* is
  the operator's call; the *presence* is mandatory.
- **Credentials are required, not soft-disabled.** Rev 1's test plan said
  "missing Gmail creds → clean disable." That's the `null_scraper()` contract
  for *optional enrichment*; applied to a mailer it claims N rows, fails N
  sends, and leaves a batch to reconcile having sent nothing. Creds are
  validated at startup, before the blocklist fetch, exiting non-zero. Soft-
  disable is correct **only** under `--dry-run`.
- `--niche NAME` (repeatable), so one niche can send while the other's copy is
  still in legal review.
- **SIGINT.** A 50-recipient run is ~100s of paced sending; the operator will
  Ctrl-C it. A handler sets a flag and stops at the next *candidate boundary* —
  never mid claim→send→settle — settles what is in flight based on whether
  `send()` returned, and prints the full summary before exiting. Documented:
  "a second Ctrl-C may strand one claim; run `--reconcile`."
- `--reconcile` prints, per stranded row, the exact `--settle <key> --state
  sent|notsent` command. Threshold is `OUTREACH_STRANDED_AFTER_MINUTES`
  (default 60). Backed by a server-side formula, not a table scan.

---

## Requirement 4 — Personalized emails

### 4a. Segment by keyword cluster, not by niche

Rev 1 used one template per niche. Verified against `main.py:74-84,124-136`,
that is the wrong granularity: Home Theater's keywords include `car and truck
review`, `power tools review`, `sports podcast commentary`, `homesteading
vlog`; Lifestyle Sofa's include `day in the life stay at home mom` and `home
cleaning and organizing`. These are deliberate *audience-proxy* keywords — fine
for discovery, fatal for one template. Identical copy to an AV reviewer and to
a homesteading vlogger makes the second an obvious untargeted blast, and that
is the one that generates spam complaints.

→ **4-5 templates** keyed on the matched keyword in `Source`: AV/home-theater
native · man-cave/room-makeover · adjacent-male-interest (tools, trucks,
sports) · home decor/interiors · family lifestyle. A missing mapping **skips
the niche with a logged error**, mirroring `run_niche()`'s contract — never a
mid-run `KeyError`.

### 4b. Render safety

- **Header injection.** Rev 1 mandated `html.escape()` on the HTML body and
  stopped. The Gmail API takes an RFC-822 blob; a channel name containing
  `\r\nBcc: …` reaching the `Subject:` line injects headers into mail
  DKIM-signed by the brand's own domain. `main.py:583-588` already flags
  `Channel Name` as attacker-controlled. → Assemble via
  `email.message.EmailMessage` (which encodes and folds headers), and strip
  `\r`, `\n`, `\x00` from **every** substituted value at the render boundary
  regardless of destination part.
- **URL validation.** Rev 1 trusted `{channel_url}` into an `href`. The
  pipeline's reason for not `csv_safe()`-ing it holds for the *writer* only;
  the *reader* takes whatever is in the cell today, and any collaborator can
  paste `javascript:…`. → Validate
  `^https://www\.youtube\.com/(channel/UC[A-Za-z0-9_-]{22}|@[A-Za-z0-9._-]+)$`,
  else rebuild from `Channel ID`, else skip the row. Escaping is not validation.
- **`csv_unsafe()` at every read boundary.** `csv_safe()` prepends `'` to any
  value starting `= + - @` — so a creator named `-Bob's AV` renders as
  **"Hey '-Bob's AV,"** in brand-approved copy, and a stored `'+promo@studio.com`
  **fails** `EMAIL_PATTERN.fullmatch`, silently dropping a legitimate prospect
  and parking it in the queue view forever. One shared inverse, applied at
  every Airtable read, with a round-trip test.
- Empty/whitespace name → neutral greeting. Empty `Channel ID` → **skip before
  keying** (else the key is `":campaign"` and collides with every other such row).
- `html.escape(quote=True)` on the HTML part; raw (but CR/LF-stripped) on plaintext.
- Attachments resolved from a **fixed allowlist**, never a config-supplied path
  join. Validated **once at startup**, not per channel — the same two JPEGs go
  to every Lifestyle Sofa recipient, so a missing asset is a *whole-niche*
  condition, not the per-channel one rev 1's F9 claimed. Two JPEGs re-encoded
  per message is megabytes per send; **hosted images are the default** and
  attachments are opt-in.
- `TEMPLATE_VERSION` is a module constant per template, pinned by a test that
  hashes the body — so any edit fails until the version is deliberately bumped.
  Otherwise the ledger is confidently wrong about what a creator received.
- **Personalization stays name + URL for v1**, but as a *hypothesis, not a
  design constraint*: rev 1 asserted "metrics read as scraping" with no
  response-rate number behind it, and an email whose only variable content is
  the recipient's name and their own link *is* a mail-merge blast. The
  distinction that matters is demonstrating you watched the channel — the
  newest video title does that, and it is now on the row (1a) for free. Test it
  against name-only once `Reply State` has data.

### 4c. Transport — Gmail REST through `http_client`, on a dedicated domain (D3)

Rev 1 chose `google-api-python-client`. **It uses `httplib2`, so it bypasses
`tests/conftest.py`'s guard entirely** — the guard patches `HTTPAdapter.send`,
the `requests` chokepoint (`conftest.py:23-43`). A missed call site would email
a real creator from a test run. Rev 1's proposed mitigation (patch a module
attribute) is exactly the fragile pattern that docstring says the guard was
moved to the transport layer to replace.

→ **`google-auth` for the token only**, plus a new `http_client.GMAIL` session
POSTing `https://gmail.googleapis.com/gmail/v1/users/me/messages/send`, with
POST **excluded from `allowed_methods`** and **no** `post_with_rate_limit_retry`
wrapper — a retried send is a duplicate *email*, the most expensive version of
the duplicate-row failure, and a Gmail 429 must not auto-repeat it. The
existing guard then covers it for free, `safe_body()` applies, and the
credential travels as a header set once at import, per the YouTube-key rule.

Credentials accept **base64 JSON in an env var** (path as a local
convenience): a file path cannot be supplied as a GitHub Actions secret, and CI
should never materialize a private key onto the runner filesystem.

---

## Requirement 5 — Audit trail

### Outreach Log (ledger + guard)

Fields as rev 1, plus: **`Prospect Row` (linked record)** to the niche table,
`Verify Result`, and `Send State` widened to the four values in 3c. The link is
load-bearing — rev 1 joined by a text `Channel ID`, which Airtable **cannot
traverse**, so send state was invisible to reviewers (R5).

### Audit Trail (history)

Fields as rev 1, **minus the unobtainable ones**: Airtable's record-updated
trigger exposes only *current* values and does not expose the editing
collaborator in a no-code action, so rev 1's `Detail: before → after` and
`Actor` were both unimplementable by the mechanism assigned to them. → The
automation copies `Status Changed At` and the native `Last Modified By` field
(1d) into the row, and `Detail` records the **after** value only. Actions
gain `Run Started` / `Run Completed` with counts, so an empty queue is
distinguishable from an aborted pipeline.

"Append-only" is dropped as a word — Airtable enforces nothing; the table is
permission-locked instead.

Audit failure after a successful send is logged loudly and **does not raise**:
the Outreach Log is the ledger of record.

---

## Configuration

```
# --- Required for --send (no defaults; --send refuses to start without) ---
GMAIL_SENDER_EMAIL=              # on the DEDICATED domain, not the corporate one
GMAIL_CREDENTIALS_B64=           # base64 service-account/OAuth JSON
OUTREACH_FOOTER_TEXT=            # postal address — CAN-SPAM (D2)
OUTREACH_UNSUBSCRIBE_URL=        # opt-out — CAN-SPAM/PECR (D2)
AIRTABLE_TABLE_OUTREACH_LOG=
AIRTABLE_TABLE_AUDIT_TRAIL=
AIRTABLE_TABLE_OUTREACH_LOCK=
# --- Optional (defaults shown) ---
# OUTREACH_DAILY_CAP=10                 # per prospect day, from the ledger (D1)
# OUTREACH_MAX_PER_RUN=10
# OUTREACH_SLEEP_SECONDS=2
# OUTREACH_STRANDED_AFTER_MINUTES=60
# OUTREACH_CAMPAIGN=                    # a LABEL, not the guard
# GMAIL_CREDENTIALS_JSON=               # local path convenience only
```

All read in `config.py`. `AIRTABLE_TOKEN` (CI) gets write scope on the niche
tables and the two new tables — **not** DO NOT CONTACT. DNC writes use the
separate maintenance token (R12).

---

## Failure modes registry

Each row carries the literal operator-facing message for abort paths.

| # | Failure | Blast radius | Handling |
|---|---|---|---|
| F1 | Crash between claim and settle | 1 channel | `Claimed`/`MaybeSent`, never auto-retried; `--reconcile` prints the `--settle` command |
| F2 | Gmail 5xx / read timeout | 1 email | **`MaybeSent`**, not `NotSent`; excluded from `--retry-failed` |
| F3 | Gmail connect error / 4xx rejection | 1 email | `NotSent`, retryable |
| F4 | Gmail quota exhausted | rest of run | stop cleanly, settle in-flight, print summary, exit 2 |
| F5 | Blocklist unreachable | whole run | **abort before any send.** `ABORTING: DO NOT CONTACT unavailable after retries. Cause: <e>. Fix: check AIRTABLE_TOKEN scope on the DO NOT CONTACT table, then re-run.` |
| F6 | Two concurrent runs | up to whole run | lease + claim-verify-send; higher record id aborts pre-send |
| F7 | Campaign key rotates | every prior recipient | fixed: ever-sent guard is campaign-independent |
| F8 | `Contacted` PATCH fails post-send | reviewer view | ledger says `Sent`; queue view drains on the **rollup**, so the row does not stick |
| F9 | Header injection via channel name | 1 email, brand DKIM | `EmailMessage` + CR/LF/NUL strip at render |
| F10 | `javascript:` in `Channel URL` | 1 email | regex validate → rebuild → skip |
| F11 | Missing attachment asset | **whole niche** | validated once at startup; abort |
| F12 | Reviewer approves a blocklisted channel | 1 channel | send-time handle+email+name re-check |
| F13 | Malformed/`csv_safe`-mangled email | 1 channel | `csv_unsafe()` then `fullmatch`; write `Outreach Ineligible Reason` |
| F14 | **Sending-domain reputation damaged** | **all company email** | dedicated warmed domain (D3); corporate domain never the cold sender |
| F15 | Same human emailed 2-5× per run | deliverability | per-prospect-day address set |
| F16 | Unknown `Send State` value (hand-typo) | ledger integrity | `typecast=False`; unknown value treated as `MaybeSent`, never absent |
| F17 | `Contacted` option doesn't exist | schema mutation | `typecast=False` + `--check-setup` + explicit opt-in |
| F18 | Airtable automation auto-disables after failures | audit gap | end-of-run assertion `audit rows == sends attempted`; non-zero exit |
| F19 | Actions timeout kills run mid-loop | N stranded | end-of-run stranded count; non-zero exit |
| F20 | Recipient PII in CI logs (90-day retention) | GDPR | redact to `j***@domain.tld` outside `--dry-run` |
| F21 | Missing Gmail creds under `--send` | whole run | validated at startup, exit 1 |

---

## Run summary and exit codes

Rev 1 referenced a "run summary" three times and specified none, and inherited
no exit-code contract — so a button-triggered run that sent zero emails would
look like success. `main.py:1386-1393` sets the precedent (`SystemExit(1)` when
nothing meaningful happened, "so a scheduled run that did nothing is never
reported as green").

Summary prints: campaign label, mode, and per niche — sent, `NotSent`,
`MaybeSent`, and skipped **by reason** (already sent, blocklisted, bad email,
duplicate address, over cap); stranded count; `Contacted`-PATCH-failure count;
audit-rows-written vs sends-attempted; remaining daily cap; and the exact
resume command.

| Exit | Meaning |
|---|---|
| 0 | ran and did its work |
| 1 | aborted (blocklist, creds, footer, assets, schema, lease held) |
| 2 | ran, but every niche was skipped / capped / halted |

Pinned by tests.

---

## Test plan

Alongside the existing suite (**568 tests**, verified — `CLAUDE.md` says 488
and `README.md` says 521; both stale, logged separately). The `HTTPAdapter.send`
guard covers the Gmail session for free once 4c lands.

**Cases rev 1's plan would NOT have caught, and which are the point:**

- **Interleaved claim** — stub the session so run B's key GET lands between run
  A's GET and POST. Assert **at most one send**. Highest-risk behaviour; rev 1
  had no case for it.
- **Ambiguous outcome** — mailer raises `ReadTimeout` / returns 503 → assert
  state is **not** `NotSent` and `--retry-failed` does not pick it up.
- **Claim POST raises** (`RequestException` escapes `post_with_rate_limit_retry`
  because POST is excluded from read-retries) → no send; key re-read; treated
  as `MaybeSent`.
- **Month rollover** — same channel, prior `Sent`, clock advanced → skip.
- **Cross-niche / shared address** — one channel in both tables; five channels
  sharing one address → one email each.
- **Header injection** — `"Ch\r\nBcc: x@y.z"` → assert the serialized message
  has one header block and no injected `Bcc`.
- **URL validation** — `javascript:alert(1)` and a lookalike host → skip.
- **`csv_safe` round trip** — `'=Name` and `'+a@b.com` → correct greeting,
  address passes `fullmatch`.
- **Handle round trip** — the URL `remove_prospect()` writes survives
  `normalize_handle()`.
- **Dry run writes nothing** — assert zero Airtable write calls.
- **`--dry-run --send`** → argparse rejects.
- **Pagination** — `offset` honoured on the queued read.
- **Day ledger** — two consecutive runs cannot exceed the cap; ledger read
  failure **aborts** rather than assuming a full budget.
- **Send POST is never retried** — the `GMAIL` session excludes POST.
- **Expired/invalid creds**, not just missing — the 2am-Friday case.
- **`typecast=False`** on every single-select write.
- **Empty `Channel ID`** → skipped before keying.
- Plus rev 1's original eligibility / permanence / template / logging cases.

A **mailer guard** still gets its own autouse fixture as belt-and-braces.

---

## Build order

Reordered: rev 1 put "the riskiest piece" third, behind untestable manual
Airtable work that included an undesigned webhook. The riskiest piece is the
concurrency guard, and it can be built and tested against stubs on day one with
**no Airtable schema at all**.

**Prerequisites (blocking — not "open questions"):**

- **P-a.** Confirm Workspace control for the dedicated sending domain, and the
  Gmail auth model. Service-account DWD needs a Workspace **admin** to
  authorize the client id for `gmail.send`; OAuth user-consent needs an
  interactive browser and a refresh token that expires in 7 days while the app
  is in Testing. Note the samples' DKIM domain is `valenciatheaterseating.com`
  while the operator is on `hendersonassociates.ca` — nobody has confirmed
  which Workspace this runs in.
- **P-b.** Start domain warmup (2-4 weeks, D3). Everything else can proceed
  in parallel.
- **P-c.** Get the footer text + unsubscribe URL (D2) and legal sign-off on
  jurisdictions.
- **P-d.** One-paragraph build-vs-buy comparison, including whether
  influencers.club ships an outreach tier.

**Then:**

1. `text_safety.py` + `niches.py` extraction (mechanical; unblocks everything).
2. `outreach_ledger.py` — claim-verify-send, ever-sent guard, day cap, lease —
   **against stubs, no schema needed.** Interleaved-claim test first.
3. Airtable schema: 4 new fields + reviewer fields on niche tables; 3 new
   tables; linked record + rollups; views; Interface page; the 3 automations.
4. `outreach.py --check-setup` — asserts every field name exists, `Send State`
   options are exactly the four expected, `Contacted` preflight, Gmail creds
   authenticate, assets present, blocklist fetches, footer set. Makes step 3
   verifiable instead of hopeful.
5. `main.py` writes the 5 new fields.
6. `outreach_templates.py` — clusters, escaping, injection strip, URL validation.
7. `mailer.py` + `http_client.GMAIL` + `requirements.in` → regenerate
   `requirements.txt` with the documented `uv pip compile --generate-hashes
   --universal` (CI installs `--require-hashes` and will hard-fail otherwise).
8. `outreach_airtable.py`, `audit_trail.py`.
9. `outreach.py` wiring, summary, exit codes, SIGINT, `--reconcile`/`--settle`.
10. `⛔` / `✗` automations + `remove_prospect()` for scripted use.
11. **`--dry-run` end-to-end against the real base; a named human reads every
    `.eml` and signs off before `--send` exists in anyone's muscle memory.**
12. `README.md` reviewer section + Airtable field/view **descriptions** (free
    documentation at the point of use — reviewers who live in Airtable will
    never open the README), `TODOS.md`, and **`CLAUDE.md`**: the claim
    protocol's non-atomicity, the `MaybeSent` classification rule, the
    campaign-label contract, and the mailer's fail-fast **exception** to the
    soft-disable house pattern. Undocumented, the next maintainer "fixes" the
    mailer to soft-disable because that is the convention.

---
---

# GSTACK REVIEW REPORT

`/autoplan`, mode **SELECTIVE EXPANSION**. Phases: CEO → Design → Eng → DX.

**Voices: `[subagent-only]` throughout.** Codex hit its account usage limit
(resets 2026-09-12) and returned no review on the first call, so it was not
attempted for the remaining phases. Every consensus table below is
single-voice: read "FLAGGED" as "one independent voice plus verified code
reading", never as cross-model agreement. Where two *phases* independently
found the same defect, that is noted under Cross-phase themes and is the
strongest signal available in this run.

## Phase 1 — CEO (strategy & scope)

### 0A. Premise challenge — 9 found, 4 wrong, 3 unverified

| # | Premise | Verdict |
|---|---|---|
| P1 | Reviewers live in Airtable, so it is the review UI | **Holds** |
| P2 | The bottleneck is outreach throughput | **Resolved at D1** — ~10-40/mo; caps scaled down |
| P3 | Cold email is the right first touch | Holds weakly; inbound unexamined |
| P4 | Corporate domain is fine to send from | **WRONG** → D3 |
| P5 | One email per channel per campaign is enough | **WRONG** — most cold replies land on touches 2-4 |
| P6 | Name+URL is enough personalization | **UNVERIFIED** — asserted with no number |
| P7 | Legal footer is a copy preference | **WRONG** → D2 |
| P8 | A reviewer can work 80 rows/day | **UNVERIFIED** — ~2.7 h/day, unnamed person |
| P9 | Build beats buy | **UNEXAMINED** → prerequisite P-d |

### 0C. Dream state

CURRENT: vetted rows land and nothing happens to them.
THIS PLAN: approve in Airtable → scheduled run sends cluster-templated email →
claim-verify-send ledger → audit history.
12-MONTH IDEAL: relevance-scored queue → 20 high-fit rows/day → 2-touch
sequence from a warmed domain → replies triaged and attributed → reply rate
drives the vetting criteria.

**Delta:** rev 2 builds the send mechanism, the guard, and the audit spine, and
adds the *minimum* learning loop (`Reply State`, `Attribution Code`). Relevance
scoring and cadence stay unbuilt.

### 0C-bis. Alternatives

| Approach | Effort (human / CC) | Verdict |
|---|---|---|
| A. Airtable-native review + in-repo Python sender | ~2 wk / ~4 h | **CHOSEN** |
| B. Custom Next.js review app | ~5 wk / ~2 d | REJECTED — DRY |
| C. Buy a cold-email SaaS + Airtable sync | ~3 d / ~3 h | **Not evaluated → prerequisite P-d** |
| D. Hand-sent, 20/wk, no code | ~0 | Viable at the low end of D1 |

### 0E. Temporal interrogation

HOUR 1 works. WEEK 2 the queue is ~600 and the reviewer bulk-approves.
MONTH 2 nobody can state the reply rate. MONTH 3 a customer's quote lands in
spam. MONTH 6 the pool is burned. **The plan broke at HOUR 6+ on measurement
and suppression semantics, not on the send path** — which is why rev 2's
expansions target those.

### CEO findings

| # | Severity | Finding | Disposition |
|---|---|---|---|
| R-C1 | CRITICAL | Corporate sending domain risks **all company email** + Workspace AUP suspension; absent from rev 1's registry | **D3** — dedicated warmed domain; F14 |
| R-C2 | CRITICAL | Legal footer filed as "open question" *below* a build order that produces a working `--send` | **D2** — required config, `--send` refuses to start |
| R-C3 | CRITICAL | No reply loop → no learning; `Template Version` written and never read | `Reply State` + `Attribution Code` added |
| R-C4 | CRITICAL | `remove_prospect()` made **every routine rejection permanent** | **D4** — split reject / DNC |
| R-C5 | CRITICAL | Build-vs-buy never asked, while rejecting a web app on DRY | Prerequisite P-d |
| R-C6 | HIGH | Queue sorted by `Overall Score`, which is 10% × constant `70.0` → **0% brand fit** | Sort changed to group+date+subs |
| R-C7 | HIGH | One template per niche spans `power tools review` … `homesteading vlog` | Keyword-cluster templates |
| R-C8 | HIGH | "Metrics read as scraping" asserted with no number | Restated as a hypothesis to test |
| R-C9 | HIGH | No attribution → ROI uncomputable | `Attribution Code` |
| R-C10 | HIGH | Reviewer capacity assumed; no bulk path, no queue-depth signal | Interface page + re-tune `DAILY_QUALIFIED_CAP` |

## Phase 2 — Design (reviewer workflow)

Verdict: **the no-custom-UI decision is right; rev 1's execution of it was
thin** — it used Airtable's weakest primitive (filtered grids) and ignored
Interface Designer, value-writing buttons, and linked-record rollups.

| # | Severity | Finding | Disposition |
|---|---|---|---|
| R1 | CRITICAL | **The row contains nothing that answers the reviewer's question.** All 12 fields are audience metrics; About and recent video titles are already in fetched payloads and discarded | 3 free fields + Attribution Code added (1a); avatar REJECTED, see 1a |
| R2 | CRITICAL | `remove_prospect()` is Python; removal happens in the **Airtable UI** → permanence fails silently for every normal removal. And the row has **no handle** to write | Airtable automation + `Handle` field |
| R3 | CRITICAL | Button field renders **per row** → click on row 14, send 50 to everyone. No loading/success/error state; reviewer surface has no dry run | **Button cut**; queue-then-schedule |
| R4 | CRITICAL | No link between niche row and ledger → send state invisible; `Approved — awaiting outreach` lies **permanently** after a `Contacted`-PATCH failure. Zero views on either new table | Linked record + rollups; queue drains on the ledger; 3 log views |
| R5 | HIGH | Automations cannot supply the **previous value** or the **editing collaborator** → `Detail: before → after` and `Actor` unimplementable | After-value only + `Last Modified By` |
| R6 | HIGH | No structured `Rejection Reason` → review is a terminal sink | Field added |
| R7 | HIGH | Empty queue indistinguishable from an aborted pipeline | `Run Started`/`Run Completed` audit actions |
| R8 | HIGH | No parking state; `Reviewing` dissolved into a synonym for `New`; Approve is the lazy path | `Reviewing` as a real view + equal-weight buttons |
| R9 | MEDIUM | `Contacted` view sorts on a **field that does not exist** — unbuildable as written | `Last Contacted At` lookup |
| R10 | MEDIUM | `csv_safe()`'s apostrophe breaks `EMAIL_PATTERN.fullmatch` → row stuck in the queue forever | `csv_unsafe()` at read boundaries |
| R11 | MEDIUM | Field order, visibility, freezing, row height, grouping all unspecified | Specified per view (1c) |

## Phase 3 — Eng (architecture & correctness)

Verdict: **"well-written prose around a duplicate-prevention protocol that does
not work, on an infrastructure path that does not exist, using a transport that
escapes the test guard."**

| # | Severity | Finding | Disposition |
|---|---|---|---|
| E1 | CRITICAL | **Claim/settle cannot prevent concurrent duplicates.** Airtable has no unique constraints → read-then-write race; rev 1's F6 described *detection* and called it *handling*; the CI `concurrency` group serialises one workflow in one repo while rev 1 had three triggers | Single entry point + lease + **claim-verify-send** + campaign-independent ever-sent guard |
| E2 | CRITICAL | Settling a 5xx/timeout as `Failed` is a **duplicate-send path**; rev 1 quoted `influencers.py`'s asymmetry to justify the claim then violated it one branch later | 4 states; `MaybeSent` never auto-retried |
| E3 | CRITICAL | **Header injection** via channel name into `Subject:` — F8 defended only the body | `EmailMessage` + CR/LF/NUL strip |
| E4 | CRITICAL | `{channel_url}` trusted into an `href` but read from a **human-editable cell** | Regex validation → rebuild → skip |
| E5 | HIGH | `Contacted` sourced from a **comment**, not schema; token lacks meta-API scope; `typecast=True` would mint it | `typecast=False` + `--check-setup` + opt-in |
| E6 | HIGH | Campaign default `<YYYY-MM>` → **guard resets on the 1st**; naive `datetime.now()` splits UTC/UTC+8 | Campaign is a label; guard is ever-sent |
| E7 | HIGH | Same human emailed 2-5× per run (cross-niche + shared agency address) | Per-prospect-day address set |
| E8 | HIGH | Per-**run** cap is not a cap | Day cap from the ledger, raises on read failure |
| E9 | HIGH | `google-api-python-client` uses `httplib2` → **escapes the `HTTPAdapter.send` guard** | `google-auth` + `http_client.GMAIL` |
| E10 | HIGH | Button→webhook has **no server**; budgeted as a schema bullet | Cut from v1 |
| E11 | MEDIUM | **`push_record` would PATCH the previous campaign's row and destroy the ledger** | New `outreach_airtable.py` functions |
| E12 | MEDIUM | `import main` drags in Playwright + the whole pipeline | `text_safety.py` / `niches.py` extraction |
| E13 | MEDIUM | Empty `Channel ID` → key `":campaign"` collides with every such row | Skip before keying |
| E14 | MEDIUM | No pagination on the approved read; ~5 Airtable writes/channel against a 5 req/s **per-base** limit | `offset` loop; cap keeps volume low |
| E15 | MEDIUM | Recipient PII in 90-day CI logs, for EU/UK data subjects | Redaction outside `--dry-run` |
| E16 | MEDIUM | Outreach token given write access to **DO NOT CONTACT** | Maintenance token split |

## Phase 3.5 — DX (operator & maintainer)

| # | Severity | Finding | Disposition |
|---|---|---|---|
| X1 | CRITICAL | Same as E1, found independently | see E1 |
| X2 | CRITICAL | Same as E2, found independently | see E2 |
| X3 | CRITICAL | `/channel/UC…` URL yields **zero** handle-index entries → "permanent forever" degrades to name-only + often-blank email | `Handle` field + round-trip test |
| X4 | CRITICAL | Send-time re-check is email-only; `match()` takes three keys | handle+email+name |
| X5 | CRITICAL | "Missing creds → clean disable" claims N rows then fails N sends | creds required for `--send` |
| X6 | HIGH | `--dry-run` + `--send` precedence undefined; **whether dry-run writes claims** unspecified — wrong answer burns every key | mutually exclusive; writes nothing; tested |
| X7 | HIGH | Run summary referenced 3×, specified 0×; **no exit-code contract** | Both specified + pinned |
| X8 | HIGH | No SIGINT story on a ~100s paced run | boundary-safe handler |
| X9 | HIGH | `--reconcile` lists but cannot settle; hand-editing the ledger + `typecast` mints `Snet` | `--settle` + `typecast=False` |
| X10 | HIGH | `--dry-run` previews the recipient **list**, not the **email** | `.eml` per recipient |
| X11 | MEDIUM | `--limit` already means per-niche in `backfill_missing_emails.py:143`; no `--niche` flag | renamed + `--niche` added |
| X12 | MEDIUM | ~25 manual Airtable steps before any code is testable | `--check-setup` |
| X13 | MEDIUM | Missing asset is a **whole-niche** condition modelled as per-channel | startup validation |
| X14 | MEDIUM | `TEMPLATE_VERSION` has no source of truth or bump discipline | constant + hash-pinning test |
| X15 | MEDIUM | Build order never mentions `requirements.in`; CI `--require-hashes` hard-fails | added as step 7 |
| X16 | MEDIUM | `CLAUDE.md` not updated — next maintainer "fixes" the fail-fast mailer back to soft-disable | added as step 12 |

## Consensus tables

```
CEO      [subagent-only]        Claude  Codex  Consensus
  Premises valid?                 NO     N/A    FLAGGED
  Right problem?                  UNSURE N/A    FLAGGED
  Scope calibration?              NO     N/A    FLAGGED
  Alternatives explored?          NO     N/A    FLAGGED
  Market/competitive risk?        NO     N/A    FLAGGED
  6-month trajectory sound?       NO     N/A    FLAGGED      0/6 confirmed

DESIGN   [subagent-only]        Claude  Codex  Consensus
  Information hierarchy right?    NO     N/A    FLAGGED
  States specified?               NO     N/A    FLAGGED
  Journey sustainable?            NO     N/A    FLAGGED
  Specificity sufficient?         NO     N/A    FLAGGED
  Right surface chosen?           YES    N/A    (single-voice)
  Affordances correctly scoped?   NO     N/A    FLAGGED      1/6 positive

ENG      [subagent-only]        Claude  Codex  Consensus
  Architecture sound?             NO     N/A    FLAGGED
  Test coverage sufficient?       NO     N/A    FLAGGED
  Concurrency/correctness?        NO     N/A    FLAGGED
  Security threats covered?       NO     N/A    FLAGGED
  Error paths handled?            NO     N/A    FLAGGED
  Deployment risk manageable?     NO     N/A    FLAGGED      0/6 confirmed

DX       [subagent-only]        Claude  Codex  Consensus
  Safe first send < 5 steps?      NO     N/A    FLAGGED
  CLI naming consistent?          NO     N/A    FLAGGED
  Errors actionable?              NO     N/A    FLAGGED
  Docs/summary specified?         NO     N/A    FLAGGED
  Resume/abort safe?              NO     N/A    FLAGGED
  Observability adequate?         NO     N/A    FLAGGED      0/6 confirmed
```

## Cross-phase themes

Concerns raised independently by two or more phases — the highest-confidence
signal available in a single-model run.

1. **The duplicate guard does not work** — Eng E1 + DX X1, arrived at
   independently from the same root (Airtable has no unique constraints). This
   invalidated rev 1's load-bearing design decision.
2. **`Failed` on an ambiguous transport outcome is a duplicate-send path** —
   Eng E2 + DX X2, both citing `influencers.py`'s ConnectTimeout asymmetry.
3. **Suppression was not actually permanent** — Design R2 (no handle on the
   row, Python helper unreachable from the UI) + DX X3 (`normalize_handle()`
   requires a literal `@`) + CEO R-C4 (semantics conflated). Three phases,
   three different failure paths, same false guarantee.
4. **`csv_safe()` round-trip corruption** — Design R10 + Eng: mangles the
   greeting *and* silently drops legitimate prospects.
5. **The button is the wrong affordance** — Design R3 (per-row blast radius) +
   Eng E10 (no server) + DX (third uncoordinated trigger feeding E1).
6. **Legal footer is a gate, not a question** — CEO R-C2 + DX, both
   independently proposing the same fix: make it required config so `--send`
   cannot start without it.

## Decision audit trail

| # | Phase | Decision | Class | Principle | Rationale | Rejected |
|---|---|---|---|---|---|---|
| 1 | CEO | Airtable-native review, no web app | Mechanical | P4 DRY | Re-implementing Airtable's grid for reviewers who hold seats | Custom Next.js app |
| 2 | CEO | Defer drip/A-B/CRM/classifier | Mechanical | P3 | Out of blast radius | Building them now |
| 3 | CEO | Add `Reply State` + `Attribution Code` | Taste | P1, P2 | In blast radius, 1 field each; without them nothing is measurable | Deferring measurement |
| 4 | CEO | Sort by group+date+subs, not `Overall Score` | Mechanical | P5 | Verified: 10% × constant 70.0 = 0% brand fit | Score-desc |
| 5 | CEO | Keyword-cluster templates | Taste | P1 | Verified keyword breadth | One per niche |
| 6 | Design | 3 free fields + Attribution Code on niche rows | Mechanical | P2, P5 | Already in fetched payloads; zero quota. Avatar dropped on review: decoration, not fit signal, and attachment storage + URL rot are real costs | Discarding them; `Channel Avatar` |
| 7 | Design | Interface record-review page | Taste | P1 | Only surface that shows progress | Grid-only |
| 8 | Design | Linked record + rollups | Mechanical | P5 | A text join is not traversable | Text `Channel ID` join |
| 9 | Design | Removal via automation | Mechanical | P5 | Reviewers cannot call Python | `remove_prospect()` only |
| 10 | Eng | Claim-verify-send + lease + single entry | Mechanical | P1 | Only fix surviving two processes | Rev 1's claim/settle |
| 11 | Eng | 4 send states | Mechanical | P1 | 5xx is not proof of non-delivery | 3 states |
| 12 | Eng | `EmailMessage` + CR/LF strip + URL regex | Mechanical | P1 | Header injection from a brand-signed domain | HTML escape only |
| 13 | Eng | `typecast=False` on selects | Mechanical | P5 | `Contacted` unverifiable, no meta scope | Trusting the comment |
| 14 | Eng | `http_client.GMAIL`, not googleapiclient | Mechanical | P4 | Reuses the existing guard for free | httplib2 transport |
| 15 | Eng | New ledger functions, not `push_record` | Mechanical | P1 | It would PATCH and destroy the ledger | Reusing `push_record` |
| 16 | Eng | Extract `text_safety` / `niches` | Mechanical | P5 | Avoids the Playwright import chain | `import main` |
| 17 | DX | `--check-setup` | Taste | P1 | Makes 25 manual steps verifiable | Hope |
| 18 | DX | `.eml` dry-run previews | Taste | P1 | Answers the operator's real question | Count + addresses |
| 19 | DX | Run summary + exit codes | Mechanical | P1 | A zero-send run must not look green | Unspecified |
| 20 | DX | Day cap from the ledger, raises | Mechanical | P1 | Mirrors `count_added_today()` | Per-run cap only |
| 21 | DX | `--dry-run`/`--send` exclusive; dry writes nothing | Mechanical | P5 | The wrong answer burns every key | Undefined |
| 22 | All | Button cut from v1 | Taste | P3, P5 | Per-row blast radius, no receiver, feeds the race | Button→webhook |

## Implementation tasks

`jq` is not installed on this machine, so the per-phase JSONL aggregator could
not run; this list is assembled directly from the findings above.

- [ ] **T1 (P1, human ~3 d / CC ~45 m) — `outreach_ledger.py`** — claim-verify-send, lease, campaign-independent ever-sent guard, prospect-day cap. Build against stubs first; interleaved-claim test first of all. *Eng E1/E6/E8, DX X1.*
- [ ] **T2 (P1, human ~1 d / CC ~20 m) — four send states + failure classification** — only provably-not-sent settles `NotSent`. *Eng E2, DX X2.*
- [ ] **T3 (P1, human ~1 d / CC ~20 m) — `Handle`, `About`, `Recent Video Titles`, `Attribution Code` written in `main.py`** — unblocks suppression and the review surface. *Design R1/R2, DX X3.*
- [ ] **T4 (P1, human ~1 d / CC ~20 m) — render safety** — `EmailMessage`, CR/LF/NUL strip, URL regex, `csv_unsafe()`. *Eng E3/E4, Design R10.*
- [ ] **T5 (P1, human ~0.5 d / CC ~10 m) — required footer config**; `--send` refuses without it. *CEO R-C2 / D2.*
- [ ] **T6 (P1, human ~2 d / CC ~30 m) — Airtable schema** — 3 tables, linked record, rollups, 10 views, Interface page, 3 automations. *Design R3/R4/R9/R11.*
- [ ] **T7 (P2, human ~1 d / CC ~20 m) — `outreach_airtable.py`** — paginated reads, ledger writes, `typecast=False`. *Eng E5/E11/E14.*
- [ ] **T8 (P2, human ~1 d / CC ~20 m) — `mailer.py` + `http_client.GMAIL` + `requirements.in` regen.** *Eng E9, DX X15.*
- [ ] **T9 (P2, human ~1 d / CC ~20 m) — `--check-setup` preflight.** *DX X12.*
- [ ] **T10 (P2, human ~1 d / CC ~20 m) — run summary, exit codes, SIGINT, `--reconcile`/`--settle`.** *DX X7/X8/X9.*
- [ ] **T11 (P2, human ~0.5 d / CC ~15 m) — `.eml` dry-run previews.** *DX X10.*
- [ ] **T12 (P2, human ~0.5 d / CC ~10 m) — extract `text_safety.py` / `niches.py`.** *Eng E12.*
- [ ] **T13 (P2, human ~2 d / CC ~40 m) — keyword-cluster templates + hash-pinned versions.** *CEO R-C7, DX X14.*
- [ ] **T14 (P3, human ~0.5 d / CC ~10 m) — PII redaction + maintenance-token split.** *Eng E15/E16.*
- [ ] **T15 (P3, human ~0.5 d / CC ~15 m) — `CLAUDE.md` + `README.md` + `TODOS.md` updates.** *DX X16.*
