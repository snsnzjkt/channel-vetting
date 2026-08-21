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

## Deferred from the channel age / upload frequency review (2026-08-18)

- **Per-niche cadence floor.** `MIN_UPLOADS_PER_YEAR` stays a module constant.
  Deferred on measurement, not effort: across 60 Home Theater rows with reviewer
  verdicts, cadence does **not** separate Approved (median 7/month, range 1-30)
  from Rejected (median 5/month, range 1-150). "DaBuild" is Approved at 1/month;
  "Zero Fidelity" is Rejected at 1/month. Revisit once `Uploads/Yr (last 10)`
  has accumulated a real distribution to read reviewer verdicts against — that
  column is the instrument. `CHANNEL_FILTERS_PLAN.md` §3 lists the exact traps
  (63 test config literals, `REQUIRED_NICHE_KEYS`, `audit_prospects.py:131`'s
  delete authority, the fail-closed default) so they don't get re-derived.

- **Probe influencers.club for vendor-side cadence / account-age filters.**
  Potentially higher leverage than anything in that plan. Discovery bills **0.01
  per creator returned, before any gate sees them**, and is the larger credit
  stream (5.5 of ~7.3 credits on a measured run). If the vendor supports either
  filter, the creator is never returned, never billed, never enriched at a
  YouTube unit, and never reaches a reviewer. Same shape as the already-recorded
  `location` leak. Cost to find out: 2 probes at `limit=1`, ~0.02 credits, using
  the method documented at `niches.py:83-98`. Not done in the review because it
  spends real money against a live paid API.

- **The "zero rows, exit code 0" observability hole.** If every `push_record`
  fails (schema mismatch, renamed column, computed field), `push_until_full`
  (`main.py:819`) increments no counter — not even `skipped`; the refill loop
  keeps buying candidates to `DISCOVERY_MAX_ROUNDS` or the 6-credit cap; the
  "finished under its qualified budget" warning is gated `if not use_discovery`
  (`main.py:1676`) so it never fires on the **primary** influencers.club path;
  and `any_cap_check_completed` is True, so `main.py:1849` exits 0. A weekday
  cron burns a full day's credits and quota, writes nothing, and reports green.
  Fix: count push failures in `push_until_full`, and warn or exit non-zero when
  candidates were examined but zero rows were written. This is the same class of
  hole `any_cap_check_completed` was added to close, one level down.

- **Redefine cadence over long-form, de-duplicated uploads.** `upload_dates` is
  taken from raw `items[:10]` at `enrichment.py:799`, before `videos.list` is
  called at `:823` — so cadence counts Shorts and double-counts re-uploads,
  unlike the avg-views column next to it. Fixing it is free (the classification
  already exists) but makes the number incomparable with every existing
  `Upload Frequency` value, so it needs its own `KNOWN CONSEQUENCE` pass.

- **`table_has_field` caches a failed probe for the whole run.**
  `airtable_client.py:189-202` treats a `RequestException` as "field absent" and
  caches it process-wide at WARNING severity. One transient error means a full
  day of rows written with those cells blank, and blank cells drop out of a
  reviewer's `>= N` filter entirely. Distinguish "probed and absent" (cache)
  from "probe failed" (don't cache), or log the failure at ERROR.

- **Extract `atomic_json.py` as a leaf module.** The tmp-sibling + fsync +
  `_replace_with_retry` + `except BaseException` cleanup dance now exists in
  THREE places: `quota_tracker._save_log`, `discovery._save_cache`, and
  `credit_tracker._save_log`. Two of the three already import the *private*
  `quota_tracker._replace_with_retry`, which is the definition of a helper that
  should have been promoted. The copies have begun to drift (only credit_tracker
  prunes inside the writer), and the Windows file-lock fix that ended three real
  runs has one implementation only by accident of that private import. A leaf
  module — `write_json_atomic(path, obj)` + `replace_with_retry()`, importing
  nothing from the project, following the `iso_time.py` / `text_safety.py`
  precedent — would leave the three call sites differing only in their READ-side
  failure policy, which is the one place they legitimately differ. Deferred here
  because two of the three copies predate the credit ledger and the fix touches
  modules outside that change.

- **Move the credit ledger off a GitHub Actions cache.** `credit_log.json` is now
  in both halves of the split cache, but a GH cache is explicitly "NOT a store of
  record" (7-day idle eviction, 10GB LRU, no signal on a miss) — and it is the
  only cached path with no correctness backstop behind it, since quota self-heals
  via Google's reset and the row caps are recomputed from Airtable's "Date
  Added". A miss means an empty ledger and a full fresh allowance, i.e. fail-OPEN
  in the one place the ledger exists to fail closed. It also cannot see a local
  `python main.py`. The repo already has the right pattern: the global Airtable
  Outreach Log behind `outreach_ledger.LedgerStore` (protocol) +
  `outreach_airtable.py` (real store), deliberately storage-agnostic so the logic
  stays testable without Airtable. Credits want the same two-layer shape. Partly
  mitigated already by persisting the vendor's own `credits_left`, which is
  authoritative and survives any cache loss.

## Follow-ups spun out separately

- Stale test counts: `CLAUDE.md` says 488, `README.md` says 521, actual is
  **568**.
- Stale comment at `main.py:172-176` — says "neutral midpoint (50/100)" while
  the constant is `70.0`.

## Deferred from the Gemini relevance-verification review (2026-08-21)

- **Wire the Tier 1 relevance score into `Overall Score`.** This is the
  "Relevance classifier" item above, and `GEMINI_VERIFY_PLAN.md` is the step that
  makes it possible — but it is gated on **measurement, not effort**. Changing
  what `DEFAULT_NICHE_MATCH = 70.0` contributes changes the meaning of a column
  reviewers already use, and this repo has an explicit precedent that scores
  written before and after such a change are **not comparable** (see the
  `avg_views` long-form comment in `enrichment.py`). Revisit only after the
  backtest in `GEMINI_VERIFY_PLAN.md` §2.14 step 7 reports whether Tier 1's
  `on_niche` separates Approved from Rejected across the 146 labelled rows in
  `PROSPECT_AUDIT_2026-08-20.md`. If it does not separate them, this item dies
  the same way the per-niche cadence floor did.

- **Multi-window video sampling (3 × 8s instead of 1 × 25s).** Directly attacks
  the weakest remaining premise in the plan — that one 25-second window
  represents a channel — and the free-tier token budget allows it. Deferred for
  one reason only: passing three `videoMetadata` parts that reference the same
  YouTube URL in a single request is **unverified**, and the plan's own rule is
  that unverified request shapes do not ship. `verify_video.py` is where it gets
  proven; if it works, this is a ~20-minute change.

- **"Zero rows, exit code 0" — the observability hole this plan did NOT fix.**
  Raised by the CEO review voice as in-scope-non-negotiable; kept out of scope
  because it is a real, separate bug that predates this plan, and because the
  plan's rescue-only architecture removes this plan's own contribution to it
  (nothing in it can reduce row count). It remains the **next-best observability
  fix in the repo**: count push failures in `push_until_full`, and warn or exit
  non-zero when candidates were examined and zero rows were written. A run that
  burns a day's discovery credits and writes nothing should not report green.

- **Install the Codex CLI.** Every phase of the 2026-08-21 `/autoplan` review ran
  `[subagent-only]` because `codex` is not on this machine, so no finding in that
  review is CONFIRMED in the two-voice sense — each one is single-voice and was
  verified against the code by hand instead. `npm i -g @openai/codex` then
  `codex login` restores the second independent model for the next review.

- **Migrate Gemini calls to the Interactions API when `video_metadata` lands
  there.** `GEMINI_VERIFY_PLAN.md` deliberately targets `POST
  /v1beta/models/{model}:generateContent`, which Google's own docs now label
  **Legacy**, because the recommended successor (`POST /v1beta/interactions`,
  header `Api-Revision: 2026-05-20`) does **not yet support `video_metadata`** —
  the clipping field the entire 25-second design depends on. Google states this
  limitation explicitly. Until it lifts, Interactions would mean sending whole
  videos, which violates the brief and burns the 8h/day free YouTube ceiling
  ~48x faster on a 20-minute source.
  **Trigger:** re-check the Interactions video-understanding docs for
  `start_offset` / `end_offset`. When present, migrate both tiers together:
  flat `input` array of `{"type": ...}` objects instead of `contents[].parts[]`,
  top-level `response_format` array, `steps[]` instead of `candidates[]`, and set
  `store=false` (Interactions retains server-side by default — 1 day free, 55
  days paid; we use neither `previous_interaction_id` nor `background=true`, so
  opting out is free). No sunset date has been announced for `generateContent`,
  so this is debt to watch, not an emergency.
