# TODOS

Deferred work, with the reason it was deferred. Created 2026-08-14 by the
`/autoplan` review of `OUTREACH_PLAN.md`.

## Deferred from the review & outreach plan

- **Reply parsing / inbox NLP.** v1 tracks reply state by hand via a
  `Reply State` single-select set from the shared inbox. Automated detection is
  a separate system with its own state machine. Do not start it until the
  hand-tracked field has enough data to say whether reply rate is worth
  optimising.

- **Follow-up sequences / drip cadence.** v1 sends **one** email per approved
  channel and *reserves* touch 2 via the campaign label — the ledger already
  supports it, nothing drives it. Most cold-email replies land on touches 2-4,
  so this is the highest-value deferral to revisit first.

- **A/B testing framework.** Dismissed in rev 1 on a wrong premise ("no volume
  at ~60/day" — ~1,200/month is ample). The real blocker is that nothing
  measures outcomes yet. Reinstate once `Reply State` has data.

- **Relevance classifier.** `DEFAULT_NICHE_MATCH = 70.0` (`main.py:176`) is a
  constant for every channel, weighted `0.10` (`scoring.py:35`), so
  `Overall Score` contains **zero** brand-fit signal. A per-keyword relevance
  weight is a dictionary lookup and would make the score mean something. This
  is the differentiated half of the product; the send path is the commodity half.

- **Inbound creator-program page.** The strongest reframing surfaced by the
  review: a public "Valencia Creator Program" page plus an affiliate offer turns
  cold prospecting into inbound applications that self-select for interest and
  arrive pre-consented (no CAN-SPAM/GDPR exposure). Not a variant of this plan —
  a different, probably better, product.

- **Airtable button → webhook trigger.** Cut from v1. Needs a publicly
  reachable authenticated HTTPS endpoint (TLS, HMAC, replay protection, secret
  storage, and a way to launch a ~20-minute job from a request that must return
  in ~30s), which does not exist in this architecture. Also: a button *field*
  renders per row, so it must not be wired to a run-everything action. If
  revisited, it needs its own design section, its own `concurrency` group, and
  its own tests — not a bullet inside "Airtable schema".

- **Custom web review app (Next.js/Vercel + Airtable backend).** Rejected on
  DRY: Airtable already ships the grid, search, sort, auth, permissions, and
  mobile. Revisit only if reviewers need access without Airtable seats.

- **CRM sync (HubSpot / Salesforce).** No CRM in play.

- **Bulk-approve / auto-approve above a threshold.** Deliberately omitted so
  that every send traces to an explicit human approval. If the review queue
  becomes the bottleneck, the correct first move is **lowering
  `DAILY_QUALIFIED_CAP`** to match review capacity, not weakening the gate.

## Follow-ups spun out separately

- Stale test counts: `CLAUDE.md` says 488, `README.md` says 521, actual is
  **568**.
- Stale comment at `main.py:172-176` — says "neutral midpoint (50/100)" while
  the constant is `70.0`.
