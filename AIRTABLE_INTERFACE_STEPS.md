# The Outreach interface — the send control, and why the last step is by hand

Written 2026-08-19. Companion to `AIRTABLE_SEND_STEPS.md`, which covers the send
*bodies*. This file covers the *surface a reviewer clicks*.

## The defect this file exists to close

The `Outreach` interface (`pbda83AHjtNi9NIC4`) was built 2026-08-14 and is
**published**. Its Home Theater page shows exactly the right people — verified live
2026-08-19 by reading all 18 records: every one is `Qualified` **and** `Approved`
**and** has an `Email` **and** has an empty `Last Send State`. The page scope filter
is correct and is doing real work.

But **the page had no control on it.** Every field rendered `isEditable: false`, and
the tickbox that actually sends was never placed on the page. So a reviewer opened
the interface, saw the right 18 creators, and had nothing to click.
`OUTREACH_PLAN.md` §3b specifies "Per-row `☑ Queue for outreach`" — that field was
never added to the page. The plan and the build disagreed, and the build was wrong.

## What the API can and cannot do here

**CORRECTION (2026-08-19).** An earlier version of this file stated that
`create_page` cannot express a record scope filter and that the fix therefore had to
be done by hand. **That was wrong**, and the error came from checking only
`describe_page_element`. `recordScopeFilters` is a **page-level** key on
`describe_page_type("visualization")`, not part of the element config. A correctly
scoped page **can** be built through the API, and two were.

What is actually true, all verified 2026-08-19:

1. **There is no `update_page` tool.** `create_page`, `delete_page` and
   `publish_interface` are the whole surface. So you **cannot add a field to an
   existing page** through the API — you can only build a new page that has it.
   This is why the two original pages were left untouched.
2. **`create_page` CAN carry the safety filter**, via page-level
   `recordScopeFilters`. Element config (`describe_page_element`) covers layout only
   — `titleFieldId`, `subtitleFieldId`, `sorts`, `detailFieldIds` and so on. Look at
   the page schema, not the element schema, when hunting for filters.
3. **`recordScopeFilters` on an EXISTING page is still not readable.**
   `list_pages_for_base` does not return it. So a delete-and-recreate of a page whose
   filter you did not author is still destructive and still forbidden — you cannot
   read the filter back to reproduce it. Build alongside; never replace blind.
4. **Draft pages are invisible to the API.** `list_pages_for_base` returns published
   pages only, and `list_records_for_page` on an unpublished page returns
   **422 "Page not found"**. So a new page's filter and its field editability cannot
   be verified through the API until after it is published.
5. **`publish_interface` promotes every pending draft in the interface**, not just
   the page you just made. Check for other in-progress edits before calling it.

### How to verify a new page's filter WITHOUT publishing it

Because of (4), the filter cannot be read back off the draft. Verify it by running
**the same filter expression** against the table with `list_records_for_table` and
comparing the result set to the page it is meant to match. That is what was done
here, and it is the check that makes building a send surface safe:

| Page | Filter result | Matches |
|---|---|---|
| `Send email — Home Theater` | **18** records | exact same 18 record IDs as `pag2sXzhIemnjlh8C` |
| `Send email — Lifestyle Sofa` | **25** records | same filter shape, LS field IDs |

If that count ever comes back **higher** than the page it mirrors, the filter is
wrong and a flagged or rejected creator can reach a live send control. Do not publish
on a mismatch.

## What WAS done through the API (2026-08-19)

| Change | Where | Why |
|---|---|---|
| `Queue for outreach` **renamed** `Send email now` | HT `fldHd5i3jMh5uW7xK`, LS `fldKKoNNc89afRfjF` | The old name promised a queue this path does not have. See below. |
| Warning text added as the field **description** | same two fields | Airtable renders a field description as help text next to the control, so the warning travels with the button instead of living in a doc nobody opens. |
| `Send Requested At` **cleared** on both `ZZ TEST` rows | HT `recq9zwahlDCGvekx`, LS `recsupCMyIdkPxKCw` | Both satisfied all four SEND trigger conditions. Either would have fired **the instant SEND was switched on**, before anyone clicked anything. |

**The rename is safe.** Both automations match the field by **ID**
(`{"tuple": ["fldHd5i3jMh5uW7xK", true]}`), not by name, and `grep` confirms the
string `Queue for outreach` appears in **no** `.py` file in this repo. The only
field name Python reads on this path is `Send Requested At`, which was not renamed.

**Why rename at all.** "Queue for outreach" describes the Python design, where a
human queues rows and a separate scheduled run sends later. On the Airtable path the
queue automation stamps `Send Requested At` and SEND triggers on that stamp within
seconds. The control is a Send button. Naming it after a queue invited exactly the
mistake the name was meant to prevent.

## Verified precondition — do this check again before switching SEND on

As of 2026-08-19, across **both** prospect tables:

- rows with `Send Requested At` set: **0**
- rows with `Send email now` ticked: **0**

So nothing fires on enable. Re-run both checks after any bulk edit or pipeline run;
`AIRTABLE_SEND_STEPS.md` lists this as a blocking precondition and it is the one
that silently stops being true.

**Note on the LS test row.** `recsupCMyIdkPxKCw` had `Send Requested At` set *and*
an **empty** `Last Send State` — meaning the Outreach Log row from the 2026-08-19
verification run no longer exists. The `LS · SEND` description states the
record-link write was verified and that the rollup populates; that evidence is no
longer present in the data. The re-fire guard is therefore **unobserved again**, not
merely unverified. Re-run the positive test before relying on it.

## A delay step is NOT available — do not plan around one

Checked 2026-08-19 against the full action catalog for this base
(`get_create_automation_instructions`): there is **no** delay, wait, sleep or pause
action type. The action enum runs `sendEmail`, `createRecord`, `updateRecord`,
`findRecords`, `sort`, `customScript`, `aiGenerate*`, the third-party senders, and
`noOp`. Nothing that waits.

So a "tick now, send in five minutes" cooling-off window **cannot be built** inside
an Airtable automation. The options that remain, if a real window is wanted:

- **Two deliberate ticks.** `Send email now` sets a `Ready to send` checkbox and
  stamps nothing; a second control on a second page stamps `Send Requested At`. The
  window is the gap between two human actions. Costs 2 new fields per table, a
  rewrite of both Queue automations, and 2 new automations.
- **Go back to `python outreach.py`.** The cooling-off window is native there — that
  is what §3a describes. Note the cost recorded in `AIRTABLE_SEND_STEPS.md`: Python
  reinstates the compliance footer that was deliberately removed, because
  `OUTREACH_FOOTER_TEXT` and `OUTREACH_UNSUBSCRIBE_URL` are required config with no
  defaults and `--send` refuses to start without them.

Neither is done. Ticking `Send email now` still sends immediately.

## THE ONE THAT COST HOURS: `recordReview` pages are READ-ONLY, `grid` pages are not

Measured 2026-08-20, after three rounds of "the tickbox does nothing".

`create_page` exposes **no** field-permission setting, and what you get depends
entirely on the **element type**:

| Element | Fields come out |
|---|---|
| `recordReview` | **`isEditable: false`** — every field, always |
| `grid` | `isEditable: true` |
| `kanban` | `isEditable: true` |

This is why the original `Home Theater` / `Lifestyle Sofa` / `Follow-up` pages
(all `recordReview`) have never had a working control, while `No email` (grid) and
`Send ledger` (kanban) always did. The tickbox **renders** on a recordReview page and
silently swallows the click — the worst possible failure shape, because it looks
functional. Diagnosis is `isEditable` in `list_pages_for_base`, nothing else.

**So a page whose job is to receive a click must be a `grid` or a `kanban`.** A
recordReview page is a viewer. Do not put the send control on one.

**The cost of a grid: EVERY listed column is editable**, and there is no per-field
override through the API. That is why the send pages **deliberately omit
`Send Requested At`** — on a grid it would be a hand-editable field, and hand-editing
it is indistinguishable from pressing send. `Channel Name` and `Email` remain
editable, which is an accepted trade: seeing the recipient before sending is worth
more than protection against a stray keystroke, and a bad edit is visible and
reversible where a send is not.

## NEVER RECYCLE A TEST ROW — make a fresh one per demo

Learned the hard way 2026-08-19, after three failed attempts.

`recordMatchesConditions` fires when a record **enters** the condition set. Airtable
tracks that per record. A row that has already fired, then had its send markers
rewound by hand (`Send Requested At` cleared, `Status` reset, ledger row unlinked or
deleted), **does not reliably fire again** — even when all four trigger conditions are
demonstrably satisfied. Verified: the queue automation stamped correctly, all four
conditions read true, `Outreach Ineligible Reason` was blank, and no Outreach Log row
was ever written, i.e. the send automation never started.

**A brand-new record fires immediately.** `ZZ TEST 2` sent within seconds on its first
tick with no other change made.

So the diagnostic order for "the tickbox did nothing" is:

1. Did `Send Requested At` get stamped? **No** → the page's checkbox is read-only
   (see the grid/recordReview section above), or the queue automation is off.
2. Stamped but no Outreach Log row? → the SEND automation never ran. Check its toggle,
   then **stop trying to reuse the row** and create a fresh one.
3. An Outreach Log row stuck on `Claimed`/`MaybeSent`? → the send was attempted and
   its outcome is unknown. Never auto-retry these.

Do not spend attempts patching a row you have already disturbed. Every reset adds a
variable instead of removing one.

## Live pages as of 2026-08-20 — the send surface is now exactly two grids

| Page | ID | Element | Scope | Live count |
|---|---|---|---|---|
| `📧 Send Emails — Home Theater` | `pagVXrv1qFZcS55RO` | grid, **editable** | Qualified + Approved + emailable + never sent | 17 + test |
| `📧 Send Emails — Lifestyle Sofa` | `pag05Tgmcl4Of9Du8` | grid, **editable** | same, LS field IDs | 24 + test |
| `🧪 DEMO ONLY — Home Theater` | `pagNk2a3QimpsVS9l` | grid, editable | `Channel Name` contains `ZZ TEST` | 1 |
| `🧪 DEMO ONLY — Lifestyle Sofa` | `paghrf4zdgZqAUcex` | grid, editable | same, LS field IDs | 1 |

**`Send Requested At` WAS added as a column on both send pages (2026-08-20), reversing
the earlier decision below.** It was originally omitted because a grid makes every
column editable and a hand-typed stamp is indistinguishable from pressing send. It was
added because it is the only on-page signal that distinguishes a **stuck** row
(stamped, never sent) from an un-actioned one — the exact failure that took three
attempts to diagnose. The editability risk is real and accepted; the diagnostic value
won.

Sorted by subscriber count descending, row height medium, with a record-count summary
under the name column so queue depth is visible without counting. Columns are
`Channel Name`, `Subscribers`, `Channel URL`, `Email`, `Send email now` — the control
LAST, below the address it will mail.

**`Send Requested At` is deliberately NOT a column on these pages.** On a grid every
listed column is editable and the API has no per-field override, so exposing the stamp
would give a reviewer a hand-editable field that is indistinguishable from pressing
send. `Channel Name` and `Email` remain editable, an accepted trade: seeing the
recipient before sending is worth more than protection from a stray keystroke, and a
bad edit is visible and reversible where a send is not.

**The original `Home Theater` (`pag2sXzhIemnjlh8C`) and `Lifestyle Sofa`
(`pagepz31HcUcQSdvw`) pages were DELETED 2026-08-20**, along with two interim
`recordReview` replacements. All four were read-only and therefore traps — the tickbox
drew and swallowed the click. Their `recordScopeFilters` were reproduced in the grids
above and verified equivalent by running the same filter through
`list_records_for_table` before the swap (18 = 18 exact record-ID match at the time).

Note the counts read 17 and 24 rather than 18 and 25 because both `ZZ TEST` rows now
carry `Last Send State = Sent` and have correctly drained out of the queue. That drop
is the system working, not rows going missing.

**Removing a published page takes a `publish_interface` call to take effect** —
`delete_page` only stages it. So an "unpublish" is a publish. Staging the new pages as
drafts and the old ones for deletion, then publishing once, swaps them atomically with
no window where both or neither are visible; that is how this swap was done.

## VERIFIED END TO END, BOTH NICHES (2026-08-19)

The full chain has now run on both, which `AIRTABLE_SEND_STEPS.md` previously listed
as never having happened.

| Niche | Ledger row | Claimed → Settled | Link resolved |
|---|---|---|---|
| Home Theater | `recMQzAmuQcghDhbN` | 16:18:36.189 → 16:18:37.567 | **yes — shows the NAME** |
| Lifestyle Sofa | `rec6zejiu9Y3aDOxZ` | 16:27:59 → 16:28:00.369 | yes |

Both settled `Send State = Sent`, both prospects flipped to `Status = Contacted`, and
**both `Last Send State` rollups populated** — which is the re-fire guard, previously
unobserved on either niche. Both test rows have correctly dropped out of their send
pages, so the queue drains on the ledger as designed.

**Note a timing oddity, benign but do not be confused by it:** the Lifestyle ledger
row was claimed at 16:27:59 while its prospect's `Send Requested At` reads
16:28:03.765 — i.e. the stamp is LATER than the send it supposedly triggered. The
ledger holds exactly one row per niche, so nothing double-sent; treat the stamp as an
unreliable ordering signal and the ledger as the record of truth.

## Superseded — the original build note, kept for provenance

Both were created through the API, in interface `Outreach` (`pbda83AHjtNi9NIC4`).
The two original pages (`pag2sXzhIemnjlh8C`, `pagepz31HcUcQSdvw`) were **not
touched** — see limit (1) above.

| Page | ID | Source | Scope filter |
|---|---|---|---|
| `Send email — Home Theater` | `pag6KtDaxd5odbPGC` | `tblzmzZw0xiKDrNZw` | Qualified + Approved + Email non-empty + `Last Send State` empty |
| `Send email — Lifestyle Sofa` | `pagx9MdU2yP8Zah1N` | `tblUtCymzl7Qjmlh4` | same, LS field IDs |

Layout on both, deliberately in this order:

- List (left): `Channel Name` as title, `Subscribers (Display)` as subtitle, sorted
  `Date Added` desc.
- Detail (right): `Channel URL`, `Email`, `Send Requested At`, then
  **`Send email now` LAST**.

**The control being last is the safety property**, not a style choice. The reviewer
reads the creator and the address, and only then reaches the thing that sends. A
tickbox placed above the evidence gets ticked before the evidence is read.

`Do Not Contact` (HT `fld4aaYN6D64fjO2c`, LS `fldMNGIEQmOR9wePc`) is deliberately
**absent** from both. It belongs on the review surface, where the fit decision is
made. Two irreversible red buttons side by side on a send page is a mis-click waiting
to happen, and the DNC row it writes is permanent.

## The one remaining manual step

`publish_interface` was blocked by the operator's own tooling guardrail, and that is
the right place for a human to stand: publishing makes pages live for the whole team
and promotes every other pending draft with them.

1. Open https://airtable.com/appgBTwBS36JG9ATV/pag6KtDaxd5odbPGC/edit
2. Confirm `Send email now` appears at the **bottom** of the right-hand pane.
3. **Publish.**
4. Repeat for https://airtable.com/appgBTwBS36JG9ATV/pagx9MdU2yP8Zah1N/edit

**Then check editability, which could not be verified before publishing** (limit 4).
If `Send email now` renders read-only, flip that one field to **Editable** in the
page's field settings and republish. Everything else on the page must stay read-only:
they are pipeline output, not reviewer input, and an editable `Email` on a send page
is a way to mis-address a real message. `Send Requested At` in particular must stay
read-only — it is the receipt, and hand-editing it is indistinguishable from clicking
send.

Once published, re-run the count check: the Home Theater page must show **18** and
Lifestyle **25**. A higher number means the filter did not survive, and no creator
should be ticked until it does.

## Still open, deliberately not fixed here

- **The Follow-up page has no control either**, and it is worse than the send pages:
  `Follow-up Requested At` is on the page read-only, but **no automation writes it**
  — there is no `Request follow-up` automation among the ten in this base, and no
  matching checkbox field on either table. §3c specifies the button; it does not
  exist. The page is currently decorative.
- **`No email` exists for Home Theater only** (`pagJ80jgOYJoEIcGO`,
  `tblzmzZw0xiKDrNZw`). Lifestyle has no equivalent, so a Lifestyle prospect with no
  address has no surface at all.
- **Neither page shows the daily send count.** At `OUTREACH_DAILY_CAP` = 10 with a
  human ticking one row at a time, nothing on screen says how many have gone today.
  The Airtable path has no send cap — that is a `python outreach.py` property.
