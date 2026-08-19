# The Airtable send bodies — what they contain, and the rules for editing them

Rewritten 2026-08-19. **This file no longer carries body text to paste.**

Its previous version existed because PR #19 concluded the `gmailSendEmail` node
"cannot be authored through the API at all", so the copy had to be hand-transcribed
into the Airtable UI and this file was the transcription source. PR #20 corrected
half of that (the node *is* creatable, for an account the API connection's own owner
controls), and 2026-08-19 settled the rest: **both send bodies, including their
attachments, links, signature table and inline HTML, were authored entirely through
`update_automation`.** Nothing here is pasted by hand any more.

Keeping a second copy of the body in this file was therefore pure drift risk — the
exact failure it was written to prevent. The automations are the source of truth:

| | Automation |
|---|---|
| Home Theater | `HT · SEND (demo)` — `wfly5pft8ELxoWI3L` |
| Lifestyle Sofa | `LS · SEND (demo)` — `wflAjlxnKuNzL2oJa` |

Both carry the full reasoning in their own `description` field. Read that before
editing either. What follows is only what a *maintainer* needs that does not fit in
an automation description.

## What the Airtable email renderer supports

Measured 2026-08-19 across four rounds of live sends. **Do not re-derive this.**

| Feature | Works? |
|---|---|
| Markdown links `[text](url)` | yes |
| Markdown `**bold**` | yes |
| Raw `<a>` | yes |
| Raw `<img>` | yes — subject to the underscore trap below |
| Inline CSS, e.g. `<p style="…">` | yes — this is what makes a styled signature possible |
| `<table>`, incl. per-cell borders | yes — confirmed by live send; the two-column signature renders with its vertical divider |
| Markdown image `![alt](url)` | **no** — renders as a literal `!` followed by an ordinary link |
| Markdown `---` horizontal rule | **no** — renders as three literal hyphens |

Because `---` does not render, every divider in these bodies is a styled border
(`<div style="border-top:…">`, or `border-left` on a table cell).

### The underscore trap — it caused two false negatives

Markdown consumes `_underscores_` as emphasis **before** the HTML is parsed. Any URL
containing them is corrupted, and the link or image then fails **silently** — no
error, no log, the element simply is not there.

An early probe therefore appeared to prove that raw `<img>` *and* `<table>` were both
stripped by the renderer. **Neither was.** The Shopify image URLs inside those tests
carried five underscores each and had been mangled before either element was judged.
The first of those wrong conclusions is what moved the product photos from inline to
attachments.

**Every image URL in a body percent-encodes its underscores as `%5F`**, and so do two
of the Home Theater influencer links (`HKPdY4_D-is`, `b_nYBE4TY2o`) whose YouTube
video ids contain them. Both the Shopify CDN and YouTube were verified to still serve
the encoded form.

**This applies to the message body only.** Attachment URLs are fetched by the Airtable
API, not parsed as Markdown, and need no encoding.

## Where the Airtable copy deliberately differs from `outreach_templates.py`

The Python templates remain the brand-approved source for the *words*. The Airtable
bodies add presentation Python does not have, and drop a footer Python cannot drop.
`Template Version` therefore carries an `-airtable` suffix so a row's provenance is
never ambiguous.

| | `outreach_templates.py` | Airtable body |
|---|---|---|
| Channel link anchor text | creator name, `html.escape`d | creator name, **unescaped** — see below |
| Signature | plain `<p>` block | two-column table, divider, logo |
| Logo | none | `Valencia_Logo_2025.png`, linked to the site |
| Product photos | none | attached (Lifestyle only) |
| Compliance footer | **required**, `--send` refuses without it | **removed** |

**If you edit the words in either place, edit both.** Python's copy is pinned by a
body-hash test; the Airtable copy is not, which is the whole reason the version
string is suffixed.

## Two risks accepted on request (2026-08-19) — do not silently "fix" either

**1. There is no compliance footer.** No unsubscribe link, no postal address, no
phone. The operator was told beforehand that CASL (the sender is Canadian), CAN-SPAM
(which requires *both* an opt-out and a physical address), and UK PECR / EU GDPR all
apply — `search_zones.py` targets US/CA/UK/EU/AU — and accepted the risk explicitly.
The compliant alternative, a `List-Unsubscribe` header, is unavailable: Airtable's
`gmailSendEmail` step exposes no header control.

- **DO NOT CONTACT is now the only route off the list, and it is manual.** Someone has
  to add a creator to that table by hand from a reply.
- **`outreach.py` cannot reproduce this.** `OUTREACH_FOOTER_TEXT` and
  `OUTREACH_UNSUBSCRIBE_URL` are required config with no defaults, `--send` refuses to
  start without them, and `safe_unsubscribe_url()` raises. Migrating the send back to
  Python reinstates the footer, or requires deliberately weakening that gate.
- Supersedes plan decision **D2**, for the Airtable path only.

**2. Creator-controlled text sits inside a link.** Both bodies use `Channel Name` as
the anchor text of the channel link. Airtable cannot escape it; `render()` can, and
does. A creator who names their channel with markup could place their own URL inside
mail DKIM-signed by the brand's domain.

Mitigated by using a raw `<a>` rather than Markdown: `](https://evil.com)` escapes a
Markdown link in about ten characters, while breaking out of an `<a>` element needs a
longer and far more conspicuous payload. **Reduced, not removed.** Reverting is a
one-word change — swap the anchor text for a literal phrase. The `href` was never the
risk; it is machine-built from `Channel ID` with a literal prefix.

## Attachments, and why the expression looks wrong

Product photos live in the **`Email Assets`** table (`tblQsZJfPdlrtH1eP`), one row per
niche keyed on the `NICHES` name — `Home Theater` (US spelling) and `Lifestyle Sofa`.
Each SEND automation looks its row up by that exact string.

- **Renaming a row silently stops the attachments.** `findRecords` returns an empty
  result rather than failing, so the mail sends with no photos and no error — while
  the Lifestyle copy still says "I have attached a couple of pieces".
- **Never point an `<img>` at an Airtable attachment URL.** They expire within hours:
  the mail looks right in a test and shows broken images in a creator's inbox the next
  day, with nothing to observe. Attaching the file is safe because the bytes travel
  with the message.
- The expression is `map` → `map` → `first` → `spread`, and every step is load
  bearing. `attachments` wants a flat array of FILE; `findRecords` returns an array of
  RECORDS even at `limit: 1`. Three simpler shapes were rejected: a numeric path index
  (paths must be strings), a string `"0"` index (`missingPropertyName`), and a single
  `map` to `cellValuesByFieldId` (yields objects, not files).
- Airtable **regenerates the lookup node's key** when it is added and rewrites the
  reference to match. Do not hand-edit those keys.

## Before either automation is enabled

1. **Ticking `Send email now` sends immediately.** There is no cooling-off window
   on this path — that is a property of the Python sender. Both Queue automations
   previously claimed otherwise in their descriptions; corrected 2026-08-19.
   The field was **renamed** from `Queue for outreach` on 2026-08-19 because the old
   name promised the queue this path does not have. Safe to rename: the automations
   match on field ID, and the string appears in no `.py` file. Airtable automations
   have **no delay action**, so the window cannot be added here — see
   `AIRTABLE_INTERFACE_STEPS.md`.
2. Confirm no real row has `Send Requested At` set, or enabling will fire for it.
   Two `ZZ TEST` rows once had it set and would each have fired on enable before anyone
   clicked anything; both were cleared, then deleted entirely on 2026-08-20 along with
   every ZZ TEST row and Outreach Log entry. **Both prospect tables now read 0 stamped
   and 0 ticked, and the Outreach Log is EMPTY** — so its next row will be a real send.
   Re-check after any bulk edit; this is the precondition that silently stops being true.
   - **The re-fire guard is now VERIFIED on both niches** (2026-08-19), which this file
     previously recorded as never observed. Both test sends settled to `Sent`, both
     linked-prospect cells resolved to the channel NAME rather than a `rec…` string, and
     both `Last Send State` rollups populated. **The negative half is now RUN too
     (2026-08-19): the rollup guard held for 87 seconds against a fresh stamp with
     `Status` manually reset to `Approved`, and released only when the ledger row was
     deleted.** See "The re-fire guard" below — including why that procedure disables one
     of the two guards, and why deleting a ledger row mid-stamp re-fires the send.
3. **The reviewer-facing half is DONE (2026-08-20).** The send control now lives on
   `📧 Send Emails — Home Theater` / `— Lifestyle Sofa` and it works. An earlier version
   of this file said it could not be added through the API; that was wrong —
   `recordReview` pages come out read-only, `grid` pages do not. See
   `AIRTABLE_INTERFACE_STEPS.md`.

## The re-fire guard: TWO independent blocks, and how testing fools you

Established 2026-08-19 across three test sends, two of which I misread. Read this before
concluding the guard is broken.

**The trigger carries two independent guards, not one:**

| Condition | Field type | Set by |
|---|---|---|
| `Status = Approved` | plain singleSelect | the send's LAST step writes `Contacted` |
| `Last Send State isEmpty` | **rollup** through a record link | the claim row's `Send State` |

A normally-completed send trips **both**. `Contacted` is not `Approved`, so the first
blocks a re-fire without the rollup being involved at all. That redundancy is the real
protection, and it is easy to miss because only the rollup is described as "the guard".

**A proposed third condition, `Status is not Contacted`, is REDUNDANT — do not add it.**
`Status = Approved` already excludes `Contacted`. It was recommended once on the false
premise that it would have blocked the observed spurious sends; it would have blocked
none of them, because `Status` was `Approved` in every case.

### Why the negative test appears to fail

The documented procedure — *set `Status` back to `Approved`, clear and re-set
`Send Requested At`* — **deliberately disables the first guard** in order to test the
second in isolation. A send firing during that test therefore does NOT demonstrate a
production defect. It demonstrates that one of two guards was switched off by the tester.

Measured on the realistic path: with `Status` reset to `Approved` and a fresh stamp, the
rollup guard **held for 87 seconds** and nothing sent.

### THE FOOTGUN: never remove a ledger row while the stamp is still set

The 87-second block ended the instant the ledger row was deleted:

```
18:19:36.786   re-stamp; Last Send State = "Sent"   -> correctly BLOCKED
18:21:02.xxx   ledger row DELETED -> rollup empties -> guard RELEASED
18:21:02.510   claim written -> sent 1.5s later
```

Deleting or **unlinking** a claim row empties the rollup. If `Send Requested At` is still
stamped, all four conditions become true again and the send fires within a second. The
automation is behaving correctly; the cleanup was wrong.

**Correct cleanup order: clear `Send Requested At` FIRST, then remove ledger rows.**
This also explains earlier confusion in the same session when ledger rows were unlinked
to re-arm test rows.

### Latency varies enormously — never conclude from an early check

Observed claim latency after a stamp: **1.5 s** in one run, **87 s** in another. Two
"the guard passed" conclusions were drawn during that gap and both were wrong. Wait a
bounded interval and re-list the Outreach Log before concluding anything; report "no
result yet" rather than "passed".

### One bypass remains unexplained

A synthetic test — ledger row hand-created and linked by hand, `Status` left at
`Approved`, nothing deleted — fired at +1.5 s with the rollup reading `"Sent"`. No
mechanism was established and none is guessed here. Note it required `Status` to be
manually held at `Approved`, which production never does after a send, so the
double-guard design covers it. Treat hand-written or hand-linked Outreach Log rows as
outside the guard's tested envelope.

## GO LIVE — the order IS the instruction

Two fields separate demo from production, and **the order matters more than either
change**. Both must be done in BOTH automations; they are independent of each other.

### Step 1 — every other edit, first

Copy, links, attachments, trigger conditions, anything at all. Do it now, because step 3
permanently ends the ability to make these programmatically.

### Step 2 — `To`

Automations → the Gmail step → **To**. It currently holds typed text,
`edrine.e@hendersonassociates.ca`. Replace it with a **reference to the prospect's
`Email` field** — insert the field token, do not type an address.

**How to tell the states apart:** plain text = demo, safe, cannot reach a creator. A
field token = **LIVE**. Check it before any session where you intend to tick something,
on both automations.

The moment it is a token, every creator on the send pages (49 as of 2026-08-20) sits one
tick from a real cold email, with no undo.

### Step 3 — `From`, a ONE-WAY DOOR

The **From** address is the connected Google account on that same Gmail step. It must
become `james@valenciatheaterseating.com`, and **James must attach it himself while
signed into that account** — the API can only attach an account owned by whoever
authorised the connection, so nobody can do it on his behalf.

**This permanently locks the automation to hand-editing.** `update_automation` is a full
replacement, so once the step points at his account the API can no longer rewrite that
automation at all, and every later change — one word of copy, one link — becomes manual
UI work forever. That is why it is step 3 and not step 1.

Until it is done, the visible sender is a Henderson address while the signature reads
`james@valenciatheaterseating.com`. The two disagree, so it cannot ship as is.

### The stop switch

Toggling the two SEND automations **off** halts everything immediately. First thing to
reach for if anything looks wrong.

### Before the first real batch

Run `python audit_blocklist.py` — read-only, no quota. The Airtable send checks DO NOT
CONTACT by **email only** (314 of 1329 entries); that script checks handle, email and
name, and is the only check covering the whole list. Verified 0 hits across both tables
on 2026-08-20.
