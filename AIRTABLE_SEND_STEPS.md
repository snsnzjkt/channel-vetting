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

1. **Ticking `Queue for outreach` sends immediately.** There is no cooling-off window
   on this path — that is a property of the Python sender. Both Queue automations
   previously claimed otherwise in their descriptions; corrected 2026-08-19.
2. Confirm no real row has `Send Requested At` set, or enabling will fire for it.
3. `To` must be repointed from the demo address to the `Email` field.
4. **James attaches his Google account LAST.** The API can only attach an account
   owned by the same person who authorised the connection, and `update_automation` is
   a full replacement — so once the step points at his account, the whole automation
   becomes un-writable by the API and every later edit is by hand.
