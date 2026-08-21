# Channel Vetting Pipeline

Discovers, enriches, scores, and pushes potential brand-partnership YouTube
channels into Airtable for human review.

## How it works

The pipeline runs once per niche (see `NICHES` in `main.py`), each niche
writing to its own Airtable table, and is bounded by two daily budgets so a
weak day can't flood a table with below-criteria channels:

0. **Daily cap check** — each niche's table is capped at `DAILY_QUALIFIED_CAP`
   (default 30) qualified rows and `DAILY_FLAGGED_CAP` (default 10) flagged
   rows per day (40 rows max), counted from Airtable's own "Date Added"
   field rather than a local file — so a second run the same day tops up
   the day's count instead of doubling it. A niche already at both caps is
   skipped before any quota is spent.
1. **Discovery** — finds candidate channels for the niche. With
   `INFLUENCERS_API_KEY` set, it uses **influencers.club creator search**
   (`influencer_discovery.py`), filtering server-side on the niche's own
   criteria — content language, a subscriber floor, creator gender, a
   creator **location** restricted to the niche's own search zone, a negation
   list covering off-brand topics *and* gaming / generic-tech bios, and an
   `ai_search` description of the niche — so far more of what it returns
   survives the hard requirements below than raw keyword search does. Your
   DO NOT CONTACT handles are excluded server-side (never dropped under the
   vendor's per-request cap), so no credit is spent surfacing a creator you
   are already suppressing. With no key set, discovery falls back to
   **YouTube `search.list`** (`discovery.py`): keyword search (type=video),
   cached per keyword per day, over a deliberately short and self-renewing
   `DISCOVERY_DAYS_BACK` (7) day window rather than a wide one that returns
   the same already-tracked channels every day. Either way, discovery keeps
   going until the day's qualified budget is filled or candidates run out.
2. **Pre-filter** — candidates already present in your Airtable base are
   dropped before any enrichment quota is spent on them.
3. **DO NOT CONTACT screening** (`do_not_contact.py`) — every candidate is
   checked against a suppression list (by handle, email, and name) at three
   points in the pipeline. The list is fetched fresh at the start of every
   run (never cached) and the whole run **aborts** if it can't be fetched
   with confidence — proceeding with a partial or empty blocklist risks
   contacting someone who asked not to be.
4. **Enrichment** (`enrichment.py`) — pulls subscriber/view counts and the
   last 10 videos' performance for each remaining candidate. It also reads
   the last 50 videos' descriptions looking for a contact email — a wider
   window that costs no extra quota, since the underlying calls are billed
   per-call rather than per-video.
4b. **Relevance** (`main.off_target_reason`) — a candidate is discarded when
   its recent video titles are DOMINATED by an off-target vertical: gaming,
   phones/PCs, generic gadgets, or AI/crypto. It reads the last ~50 video
   TITLES, which enrichment already fetched, because a title is evidence of what
   a channel *publishes* while a bio is a claim it *made once* (and four tracked
   channels have no usable bio at all).

   This is deliberately **negative evidence only**. Nothing is required of a
   candidate — a positive "must match an on-niche term" gate was built, measured
   and rejected in 2026-08, because it discarded a real prospect scoring 0/50
   while missing an off-niche channel whose woodworking titles carried
   "furniture" and "interior". Each niche's `on_target_terms` exist purely to
   **rescue** a flagged channel, never to admit one: that asymmetry is what
   keeps "OCM Reviews" (Fosi DACs, IEMs, an Atmos soundbar — 6% off-target, 60%
   on-target) while dropping "DragsterTV" (Forza money glitches — 4%
   off-target, 0% on-target) at almost the same off-target score.

   Placed immediately after the performance fetch, so an off-target channel
   costs no long-form paging, no scoring, and — the point — **no paid email
   credit**. Measured against the 147 rows live on 2026-08-21 it rejects 29 of
   the 63 Home Theater rows (46%) and 0 of the 84 Lifestyle rows.

5. **Hard requirements** (`main.pre_push_drop_reason`, `search_zones.py`) —
   a candidate is **discarded**, with no row written, unless it clears all
   of: 10,000+ average views (both niches), **at least half of the judgeable
   long-form videos in that window over 10,000 views** (see
   `MIN_VIEWS_PER_VIDEO_RATIO` — the README previously said "each of the last
   10", which has not been the rule since the ratio was introduced), 30+ public videos, **at least 10 uploads a year**,
   **a most-recent upload inside a rolling 12 months**, and a location
   inside the allowed search zones — **US, Canada, UK, Europe, Australia;
   Ireland excluded**. Dead channels and Shorts-only channels are dropped
   here too. The per-video, cadence, and recency floors read the same
   already-fetched last-10 window as the average, so they cost no extra
   quota; an unmeasurable one (too thin a window, no parseable upload date)
   is *kept*, not dropped. Location comes from the channel's own `country`
   setting (85% of channels in the live tables set it), falling back to the
   region subtag of its content language (`en-GB` → GB) for the rest. A
   channel that declares neither is *kept*, not dropped — absent data
   isn't evidence against it.
6. **Qualification** (`scoring.py`) — the one soft criterion left: whether
   the channel meets that niche's minimum age. A channel that doesn't is
   **flagged for review, not discarded** — a human makes the final call.
7. **Scoring** (`scoring.py`) — computes a fake-follower risk score and a
   weighted overall score.
8. **Airtable push** (`airtable_client.py`) — creates or updates a row per
   channel in that niche's table (never duplicates), until both the
   qualified and flagged daily budgets are full or candidates run out.

Creators a niche's query has already returned and the gates already rejected
are remembered in `rejected_handles.json` and excluded server-side on the next
run, so the vendor is not paid 0.01 twice for the same reject — the discovery
endpoint sorts by relevancy deterministically, so without this the same rejects
come back every run. It is an optimisation only: a missing or unreadable file
costs credits, never correctness, and DO NOT CONTACT screening is untouched by
it. Entries expire after `REJECTED_HANDLES_RETENTION_DAYS` (90) so a channel
that has since grown gets looked at again.

Quota spend is tracked in `quota_log.json` and capped by `QUOTA_CEILING`
(default 8000/10000 daily units) so a run never blows your daily YouTube API
budget.

## Setup

### 1. Create an Airtable Personal Access Token (PAT)

1. Go to https://airtable.com/create/tokens.
2. Click **Create new token**.
3. Name it (e.g. "channel-vetting-pipeline").
4. Add scopes: `data.records:read` and `data.records:write`.
5. Add access to the specific base you'll use for this project.
6. Click **Create token** and copy the value — it's shown only once.
7. In your base, create **one table per niche** (currently: Home Theater
   and Lifestyle Sofa — see `NICHES` in `main.py`). Easiest way: build one
   table with the schema below, then right-click its tab → **Duplicate
   table → Duplicate table structure only** for each additional niche, so
   every table has an identical field set. Each table needs: Channel
   Name, Channel URL, Channel ID, Subscriber Count, Avg Views (last 10
   videos), Engagement Rate, Upload Frequency (Single line text — see
   note below), Content Language, Email (**Email** field type — not
   Single line text), Fake Follower Risk Score, Overall Score,
   Qualification (single select: Qualified / New Channel — see note
   below), Status (single
   select: New/Reviewing/Approved/Rejected/Contacted), Source, Notes,
   Date Added.

   Two further **optional** columns are written when they exist and skipped
   silently when they don't (the pipeline probes once per table per run, so
   adding them later switches them on with no code change):

   - **Email Source** (Single line text) — which step of the five-step email
     chain produced the address, or, when none did, why: `none found
     (not_found)`, `none found (invalid_or_expired)`, `none found (all 5 steps
     ran)`. Without it a blank Email cell cannot be told apart from a row
     written before the column existed.
   - **Email Type** (Single line text) — influencers.club's own label for the
     address (`personal_email`, `other`, ...), written only when step 4 is what
     found it, since the other four sources have no such concept. This is the
     value that shows as "Other" in the vendor's dashboard.

   Grab each table's ID (open the table → **Help → API documentation**,
   or read it from the URL — the `tbl...` segment) for step 4.

   > `Qualification` now records one thing: whether the channel is old
   > enough for its niche. `New Channel` means it isn't, and the row is
   > written anyway (`Status = New`) so a human reviewer can decide.
   >
   > A third option, `Below View Minimum`, existed before the 2026-08
   > criteria change. The view floor is now a hard requirement — channels
   > under it are dropped rather than written — so the pipeline never
   > produces that value again. **Keep the option on an existing table**:
   > it holds the rows written under the old rules, and deleting it would
   > blank their Qualification cell. A brand-new table doesn't need it.

   > This pipeline also requires a **DO NOT CONTACT** suppression table to
   > already exist in the same base — it's referenced by a hardcoded
   > table ID and field IDs (`DO_NOT_CONTACT_TABLE_ID`, `FIELD_NAME`,
   > `FIELD_URL`, `FIELD_EMAIL`) at the top of `do_not_contact.py`, rather
   > than an env var, since it's shared infrastructure rather than a
   > per-niche table. Pointing this at a different base means updating
   > those constants to match. Every candidate is checked against it by
   > handle, email, and name; if it can't be read, the whole run aborts
   > rather than risk contacting someone who opted out.

   > `Upload Frequency` is written as a formatted string (e.g. `"2.5
   > videos/month"`), not a raw number — if you make it a Number field
   > instead, update the `f"{upload_freq} videos/month"` line in
   > `main.py` to send `upload_freq` directly.

   > `Email`: YouTube's API does not expose a channel's gated
   > "business inquiries" email (it's behind a CAPTCHA-protected reveal
   > button specifically to block scraping, and this pipeline does not
   > attempt to bypass that). Instead, it does a best-effort regex scan
   > for a plain-text email, preferring one that recurs across at least
   > `EMAIL_MIN_VIDEO_REPEATS` (default 3) of the channel's last
   > `EMAIL_SCAN_SAMPLE_SIZE` (default 50) video descriptions — a much
   > more reliable "this is their real contact"
   > signal than a single mention — and falling back to the channel's
   > About description if no repeated one is found. If both come up
   > empty, it pages back through `EMAIL_DEEP_SCAN_PAGES` (default 4)
   > pages of *older* uploads and applies the same repeat test across
   > everything scanned so far, at 2 quota units per page. If that also
   > comes up empty and `INFLUENCERS_API_KEY` is set, it asks
   > influencers.club to resolve the channel ID to a validated address —
   > one HTTP call, and nothing is billed when no address is found. If
   > that misses too and `USE_PLAYWRIGHT_STEALTH=true`, a last-resort
   > lookup follows the channel's public external link list in Playwright
   > with stealth enabled — each link that isn't a social/platform domain,
   > then that site's `/contact` page — and applies the same pattern.
   > Hunter.io and Modash have been removed and are not coming back.
   > Often still blank; treat as a bonus signal, not a guarantee.

   > **Optional readable counts**: `Subscriber Count` and `Avg Views` stay
   > as Number fields (so you can still sort/filter numerically) but you
   > can add two Formula fields per table for a human-readable version —
   > `Subscribers (Display)` and `Avg Views (Display)`, formatted like
   > `"121K Subscribers"` / `"3.5M Subscribers"`. Formulas:
   > ```
   > IF(
   >     {Subscriber Count} >= 1000000,
   >     ROUND({Subscriber Count} / 1000000, 1) & "M Subscribers",
   >     IF(
   >         {Subscriber Count} >= 1000,
   >         ROUND({Subscriber Count} / 1000, 0) & "K Subscribers",
   >         {Subscriber Count} & " Subscribers"
   >     )
   > )
   > ```
   > Same pattern for `Avg Views (last 10 videos)`, suffixed `" Avg Views"`.
   > Pipeline code needs no changes for this — it's purely an Airtable-side
   > computed field.
8. Grab your Base ID from the base's API docs page
   (https://airtable.com/api, select your base — the ID starts with `app`).

### 2. Create a Google Cloud project + YouTube Data API key

1. Go to https://console.cloud.google.com/ and create a new project (or
   reuse one).
2. Go to **APIs & Services > Library**, search for **YouTube Data API v3**,
   and enable it.
3. Go to **APIs & Services > Credentials**, click **Create credentials >
   API key**.
4. (Recommended) Restrict the key to the YouTube Data API v3 to limit
   blast radius if it ever leaks.
5. Copy the key.

Note: the free tier gives you 10,000 quota units/day. A `search.list` call
costs 100 units; `channels.list`, `playlistItems.list`, and `videos.list`
each cost ~1 unit. This is why discovery (search) quota is capped
separately and more conservatively than enrichment quota.

### 3. Install dependencies

```bash
cd channel-vetting
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS/Linux
pip install -r requirements.txt
```

### 3b. Optional: Playwright + stealth for the browser email step

The last step of the email fallback chain (`browser_email.py`) uses
Playwright with stealth to follow a channel's public external link list to
the creator's own site. It is off by default; enable it with
`USE_PLAYWRIGHT_STEALTH=true`. If Playwright has not yet downloaded
Chromium, run `python -m playwright install chromium` once first.

To try that browser-backed path against already-tracked, email-less rows
without re-running discovery:

```bash
python backfill_missing_emails.py --use-playwright-stealth
```

That adds a public-page browser check on top of the free text-based email
steps — there is no paid fallback to disable.

### 4. Configure environment variables

```bash
copy .env.example .env        # Windows
# cp .env.example .env        # macOS/Linux
```

Fill in `.env` with your `AIRTABLE_TOKEN`, `AIRTABLE_BASE_ID`,
`AIRTABLE_TABLE_HOME_THEATER`, `AIRTABLE_TABLE_LIFESTYLE_SOFA` (the two
table IDs from step 1.7), and `YOUTUBE_API_KEY`. Everything else in
`.env.example` is optional and defaults sensibly:

| Variable | Default | Purpose |
|---|---|---|
| `QUOTA_CEILING` | 8000 | YouTube quota ceiling per day (of the 10,000 free-tier budget) |
| `API_SLEEP_SECONDS` | 0.5 | Delay between individual API calls |
| `DAILY_QUALIFIED_CAP` | 30 | Max qualified rows pushed per niche table per day |
| `DAILY_FLAGGED_CAP` | 10 | Max flagged (below-criteria) rows pushed per niche table per day |
| `CANDIDATE_OVERSHOOT` | 1.5 | Multiple of the remaining row shortfall that one discovery round banks in fresh candidates. Sizes a round only — `run_niche()` keeps discovering until the qualified cap is met or the keywords run out, so this does not limit the day's yield |
| `EXPECTED_CANDIDATES_PER_KEYWORD` | 40 | Unique channels one keyword is expected to yield (measured ~42 at `max_results=50` over a 7-day window). Converts a row shortfall into a keyword count for the next discovery round |
| `DISCOVERY_DAYS_BACK` | 7 | How many days back `search.list` looks for videos (short and self-renewing by design — see below; `--days-back` overrides per run) |
| `PROSPECT_DAY_TZ` | `America/Toronto` | Timezone defining a "prospect day" for the daily caps above — deliberately separate from `quota_tracker.py`'s Pacific-Time YouTube quota clock |
| `EMAIL_DEEP_SCAN_PAGES` | 4 | Extra pages of older uploads scanned for a contact email when the free steps find nothing (2 quota units per page, per channel; 0 disables) |
| `OFF_TARGET_MIN_SHARE` | 0.10 (code) | Share of recent video titles that must read as gaming / phones-PCs / gadgets / AI-crypto before the relevance gate fires (it also has to exceed the on-target share) |
| `REJECTED_HANDLES_FILE` | `rejected_handles.json` | Where the already-rejected-creator cache lives (gitignored; cached in CI) |
| `REJECTED_HANDLES_RETENTION_DAYS` | 90 | How long a rejection is honoured before the creator is discovered again |
| `LONGFORM_SCAN_MAX_PAGES` | 3 | Extra pages of older uploads paged through to confirm 30+ non-Shorts videos, and only for channels the newest 50 left short of that bar (2 quota units per page; 0 judges on the newest 50 alone) |
| `USE_PLAYWRIGHT_STEALTH` | `false` | Enables the Playwright link-list email fallback (see §3b). The search-zone filter does not depend on it. `USE_CLOAKBROWSER` is still accepted as an alias |
| `INFLUENCERS_API_KEY` | _(unset)_ | Enables influencers.club **discovery** (replacing `search.list`) and email chain **step 4** (enrich-by-handle). Unset means both are skipped and discovery falls back to `search.list` — the pipeline runs fine without it |
| `INFLUENCERS_BASE_URL` | `https://api-dashboard.influencers.club` | API host override |
| `INFLUENCERS_MAX_LOOKUPS_PER_RUN` | 100 | Hard cap on step-4 email lookups per run, bounding credit spend. Only channels the free steps missed consume one, and a lookup that finds no address is not billed |
| `INFLUENCERS_MAX_DISCOVERY_CREDITS_PER_RUN` | 6 | Per-run credit ceiling for discovery (0.01 credits per creator returned). A runaway guard, not a normal-use limit |
| `GEMINI_ENABLED` | `false` | Master switch for relevance verification. Only the literal `true` enables it |
| `GEMINI_FREE_ONLY` | `true` | Enforces the hardcoded free-tier model allowlist. Only the literal `false` disables it |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | Must be in `GEMINI_FREE_TIER_MODELS`; anything else switches verification off for the run with a loud error |
| `GEMINI_BASE_URL` | Google's endpoint | Override only to point at a local stub |
| `GEMINI_VIDEO_ALWAYS` | `true` | Video-check every candidate, not just flagged ones. One request each — the main budget consumer |
| `GEMINI_TEXT_TIER` | `false` | The advisory 0-100 text score. Off on evidence: measured as non-predictive (see plan §2.16). Never gates a rescue |
| `GEMINI_TIMEOUT` | 60 | Longer than other timeouts: Google fetches and decodes the clip server-side |
| `GEMINI_MAX_RETRIES` | 1 | 5xx/network only. Never a 429 — that is the free-tier wall |
| `GEMINI_MIN_CONFIDENCE` | 0.6 | **Provisional.** Below this a candidate is not rescued. Raising it never causes a drop, only fewer rescues |
| `GEMINI_MAX_REQUESTS_PER_RUN` | 70 | Both tiers, all niches, per process. `MAX_GEMINI_REQUESTS_PER_RUN` is accepted as an alias |
| `GEMINI_MAX_VIDEO_REQUESTS_PER_RUN` | 30 | The only cap that touches the free tier's 8h/day YouTube allowance |
| `GEMINI_MAX_REQUESTS_PER_DAY` | 80 | Persisted in `gemini_log.json`, keyed on the **Pacific** day (Google's quota day) |
| `GEMINI_MAX_VIDEO_REQUESTS_PER_DAY` | 40 | As above, video only |
| `GEMINI_MAX_SECONDS_PER_RUN` | 900 | Wall-clock brake. Keep below the workflow's `timeout-minutes` |
| `GEMINI_CLIP_SECONDS` / `_MIN_START_SECONDS` / `_START_FRACTION` | 25 / 90 / 0.25 | Which 25 seconds. Not the opening — that is intro, branding and the sponsor read |
| `GEMINI_VERDICT_VERSION` | 1 | Bump to invalidate every cached verdict by hand after a prompt change |
| `GEMINI_CACHE_RETENTION_DAYS` | 30 | Shorter than the 90 used elsewhere: `GEMINI_MODEL` is a floating alias |

### 5. Edit your keywords / niches

`main.py`'s `NICHES` dict holds one entry per niche: its search keywords
(real terms pulled from the Types of Content Posting > Primary sections of
the "Lifestyle Sofa" and "Home Theater" Influencer Profiling briefs,
Cynthia Lim, 15 April 2024), which Airtable table it pushes to, and its
qualification thresholds — `min_avg_views` and `min_channel_age_months`
(`None` if the niche has no age requirement, as with Lifestyle Sofa).

Each entry also carries a `discovery_filters` dict — the server-side
filters influencers.club discovery uses: content language, creator
`gender`, a subscriber floor, a `location` list built from the niche's own
`allowed_country_codes`, and an `ai_search` description of the niche
(or `topics` codes where the taxonomy fits). When `INFLUENCERS_API_KEY` is
set these drive discovery; the `keywords` are only the `search.list`
fallback used when no key is configured. Reword `ai_search` to steer which
creators surface.

Note that `min_avg_views` is **10,000 for both niches** as of the 2026-08
criteria change. Lifestyle Sofa's brief says 2,000; that was deliberately
overridden to put the two niches on the same bar. The other two shared
requirements — 30+ public videos and the allowed search zones — aren't
per-niche knobs: they live in `MIN_VIDEO_COUNT` (`main.py`) and
`search_zones.py`.

Add/replace keywords as new niche briefs come in — pull from a brief's
actual content-type list, not its demographic/psychographic sections
(those describe the audience, not searchable video topics). To add a
whole new niche, add a new `NICHES` entry with all four keys, plus a
matching env var and Airtable table — a niche entry missing either
threshold key is skipped (with a logged error) rather than crashing the
run.

### 5b. Optional: Gemini relevance verification (free tier only)

**What it does.** Step 4b (the title-based relevance gate) discards a candidate
whose recent video titles are dominated by an off-target vertical. It is free and
deterministic, and it rejects ~46% of Home Theater candidates — but it has a
known false-negative mode: a genuine prospect whose titles happen to use none of
the anticipated vocabulary. This feature attaches a **rescue ladder** to that
drop, and does nothing else:

1. **Text tier** reads the channel bio plus up to 50 video titles and 50 video
   descriptions — all of which enrichment already fetched, for free, on every
   candidate. It scores every candidate that reaches the gate.
2. **Video tier** sends ~25 seconds of one representative long-form upload and
   asks whether the criteria are actually satisfied on screen.

A candidate the title gate **flagged** is re-admitted only if **both** tiers
confirm it. A candidate the gate let through is scored and continues **whatever
the score says** — the score is recorded for you, never used as a gate.

> **This cannot reduce your row count.** There is no new drop reason. Every
> failure path — feature off, no key, quota reached, timeout, malformed reply,
> no suitable video — leaves the candidate exactly where today's pipeline leaves
> it. Nothing here is ever written to `rejected_handles.json`.

**Step 1 — mint a key that cannot be billed.** This is the whole cost guarantee,
and it is not in the code. Per Google's API terms the Gemini API is a "Paid
Service" *only* through a Cloud project with an active billing account.

1. https://aistudio.google.com/apikey → **Create API key** → create it in a
   **new** Google Cloud project used for nothing else.
2. Open
   `https://console.cloud.google.com/billing/linkedaccount?project=YOUR_PROJECT`.
   It must read **"This project has no billing account."** *That sentence is the
   guarantee.* An unbilled project returns `429 RESOURCE_EXHAUSTED` past the free
   ceiling and cannot be charged.
3. Restrict the key to the **Generative Language API** — same reasoning as the
   YouTube key in step 2, and it matters more here because this key sits in the
   Actions job env alongside `AIRTABLE_TOKEN` for the whole run.
4. **Re-check step 2 after every rotation.** No API reports billing status, so
   nothing in this repo can check it for you.

Also worth knowing before you enable it: the free tier is "Unpaid Services" under
those terms, so Google may train on and human-review what is submitted. What this
pipeline submits is a public YouTube URL, public titles and descriptions, and
your own niche criteria — never a creator's email address, never Airtable data.

**Step 2 — prove it works before wiring it in.** One request, ~10 seconds, no
discovery credits, no YouTube quota, no Airtable writes:

```bash
python verify_video.py --niche "Home Theater" "https://www.youtube.com/watch?v=VIDEO_ID" --duration 1800
```

It prints the exact request body, the model requested, the model Google says
actually **served** it (`modelVersion`), the token count, and the parsed verdict.
Two things to look at:

- **`served allowlisted: True`** — your proof you are on a free-tier model, from
  Google's own response rather than from our code.
- **`tokens/second` near 100** — confirms only the 25-second clip was processed,
  not the whole upload. If this jumps by an order of magnitude, stop: the request
  shape has drifted and the whole video is being ingested.

**Step 3 — add four columns to *both* niche tables.** All four are optional: the
pipeline probes once per table per run and silently skips any that are missing, so
you can add them later with no code change. Exact names, exact case:

```
Relevance State        Single select   (options: scored, rescued, unavailable)
Relevance Detail       Single line text
Relevance Notes        Long text
Verified Video URL     URL
```

> `Relevance State` is the **only** one that may be a Single select, and only
> because its options are a closed set. `push_record` sends `typecast=True`,
> which silently *creates* a missing option — harmless for three fixed values,
> but if you make `Relevance Detail` a select it will mint a new option for every
> unique string (`score 78 (on-niche, 0.90)`, `score 79 (...)`, …) and your saved
> views will fill with one-off options. Keep the other three as text/URL.
>
> Easiest way to keep the two tables in sync: add them to one, then right-click
> its tab → **Duplicate table → Duplicate table structure only**.

Example cell values after a first run: `rescued` / `rescued 0.88 (video
confirmed)`, `scored` / `score 78 (on-niche, 0.90)`, `unavailable` /
`unavailable (quota_exhausted)`. `Verified Video URL` is the video that was
judged, with a `&t=` offset — click it to check the AI's work in one step.

**Step 4 — write your criteria.** `text_criteria` and `video_criteria` live per
niche in `niches.py`, beside `on_target_terms`. Two rules:

- A **video** criterion must be answerable from ~25 seconds of footage alone.
  *"Is a person presenting to camera"* works; *"does the creator own their home"*
  does not.
- Keep each list to **2-4 entries**. Every entry is a separate judgement the
  model must produce evidence for, and a long list dilutes all of them.

Editing either list invalidates every cached verdict for that niche
automatically — the criteria are hashed into the cache key — so retuning costs
requests, never correctness.

**Step 5 — turn it on.** Locally, `GEMINI_ENABLED=true` in `.env`. For the
scheduled run you need **both** a `GEMINI_API_KEY` repository secret **and** a
`GEMINI_ENABLED` secret set to the literal `true`; the workflow reads them
explicitly and CI does not use `.env` at all.

**What you will see in the run summary:**

```
gemini relevance:  model=gemini-3.5-flash-lite (served: gemini-3.5-flash-lite,
                   allowlisted) — 41 request(s) this run (7 video), 41/300 run cap,
                   121/600 requests today (7/120 video), 6 cache hit(s), ~98k tokens, 84s
gemini verdicts:   34 scored, 3 RESCUED, 4 unavailable
```

These print on **every** run, zeros included — a line that hid itself when the
count was zero would be missing in exactly the case worth noticing. **`RESCUED`
is the number to watch:** it is whether the feature is earning anything. If it
sits at 0 across a week, either the criteria are too strict or the title gate has
no false negatives worth recovering, and you should retune or switch it off. If
`cache hit(s)` is pinned at 0 on CI while non-zero locally, `gemini_cache.json`
is not persisting between runs — check the `actions/cache` paths in the workflow.

**When the free quota runs out:** verification stops for the rest of the run, one
warning is logged, every remaining candidate keeps the verdict the existing gates
gave it, and the rest of the pipeline finishes normally. There is no retry, no
switch to another model, and no paid fallback — by construction, not by policy.

**To turn it off:** `GEMINI_ENABLED=false`. No deploy, no revert, no schema
change. The pipeline returns to exactly its previous behaviour.

### 6. Run the test flow first

```bash
python main.py --test
```

This runs on the first niche only and, unless you pass `--daily-cap`,
bounds the run to **2 qualified / 1 flagged** rows so it stays cheap. That
bound matters: when influencers.club discovery is active it fills the daily
cap rather than honouring `max_results`, so without it a "test" would
discover toward a full day of real credits and quota. It's enough to
confirm YouTube, Airtable (and influencers.club, if the key is set) are all
wired up correctly. Pass `--daily-cap N` to set a different bound for that
run — useful for testing the capping behavior against production Airtable.

### 7. Run the full pipeline

```bash
python main.py
```

> **First run against an empty (or recently emptied) table:** the default
> discovery window (`DISCOVERY_DAYS_BACK`, 7 days) is deliberately short —
> see "Discovery window" in `CLAUDE.md` — so a plain `python main.py` on a
> table with no existing rows will skip anything published more than a
> week ago and likely come back mostly empty. For that first sweep, run
> `python main.py --days-back 90` instead to pull in the backlog; switch
> back to the plain 7-day default for every run after that.

## Files

| File | Purpose |
|---|---|
| `config.py` | Loads `.env`, defines constants (quota ceiling, daily caps, weights inputs, etc.) |
| `http_client.py` | Shared retrying HTTP sessions (Airtable / YouTube / influencers.club); API keys travel as headers, never query params |
| `influencer_discovery.py` | influencers.club creator-search discovery source (replaces `search.list` when a key is set) |
| `discovery.py` | `search.list`-based channel discovery + per-day search cache (the discovery fallback) |
| `enrichment.py` | `channels.list` + `playlistItems.list` + `videos.list` stats |
| `scoring.py` | Fake-follower risk heuristic + weighted overall score + `qualify()` (channel age) |
| `search_zones.py` | Allowed-country tables (US/CA/UK/EU/AU, minus Ireland) + `zone_verdict()` |
| `do_not_contact.py` | DO NOT CONTACT suppression list — fetched fresh every run, fails closed |
| `external_dedupe.py` | 24h-cached @handle index over the base's other YouTube tables, to skip channels already tracked elsewhere |
| `influencers.py` | influencers.club enrich-by-handle lookup (step 4 of the email chain) |
| `browser_email.py` | Playwright link-list email fallback (last step of the email chain) |
| `prospect_day.py` | Single source of truth for "what day is it" for the daily caps (`PROSPECT_DAY_TZ`) |
| `airtable_client.py` | Dedupe check, create/update records, `count_added_today()` (per-table, one table per niche) |
| `quota_tracker.py` | Daily quota spend log (resets at midnight Pacific Time) |
| `audit_blocklist.py` | One-off: check rows already in the niche tables against DO NOT CONTACT |
| `backfill_missing_emails.py` | One-off: re-run the email chain over rows that have no email yet |
| `cleanup_external_duplicates.py` | One-off: delete niche-table rows already tracked elsewhere in the base (guarded by `--confirm`) |
| `main.py` | Orchestrates the full pipeline; `--test` and `--daily-cap` flags |
| `tests/` | pytest suite (see "Running the tests" below) |

## Running on a schedule (GitHub Actions)

`.github/workflows/channel-vetting.yml` runs the full pipeline on weekdays at
18:00 UTC — 02:00 Asia/Manila the next morning, which is where the operator is —
and can also be triggered manually from the Actions tab, with an option to run
in `--test` mode. The cron's Mon-Fri is UTC, and 18:00 UTC is 14:00/13:00 the
same day in `PROSPECT_DAY_TZ`, so rows land stamped Mon-Fri in Toronto. The
Manila week is therefore Tue-Sat; the comment on the cron itself spells out why
that is the right way round.

Setup:
1. Push this repo to GitHub.
2. In the repo, go to **Settings > Secrets and variables > Actions > New
   repository secret** and add the five required secrets: `AIRTABLE_TOKEN`,
   `AIRTABLE_BASE_ID`, `AIRTABLE_TABLE_HOME_THEATER`,
   `AIRTABLE_TABLE_LIFESTYLE_SOFA`, `YOUTUBE_API_KEY` — same values as your
   local `.env`. Optionally add `INFLUENCERS_API_KEY` to turn on
   influencers.club discovery and email step 4; without it the scheduled run
   falls back to `search.list`. No browser license secret is needed.
3. The workflow pins `PROSPECT_DAY_TZ` to `America/Toronto` in its env and
   turns `USE_PLAYWRIGHT_STEALTH` **on for scheduled runs** — which also runs
   the `playwright install --with-deps chromium` step, since pip installs the
   Playwright driver but not the browser binary. Caveat: turning it on makes
   the browser email step *run*, but GitHub-hosted runners sit on Azure
   datacenter IPs that YouTube challenges hard, and stealth patches browser
   fingerprints, not IP reputation — so "running" is not "working". `main.py`
   logs a warning when the step was requested but the browser could not
   start; watch the run log for it.
4. To run immediately, use **Actions > Channel Vetting Pipeline > Run
   workflow**. That manual dispatch exposes a `use_playwright_stealth` toggle
   (on by default) — uncheck it to run without the browser step.
5. To change the schedule, edit the `cron` line in the workflow file
   (cron is UTC; see https://crontab.guru to build a new expression).

## Running the tests

```bash
python -m pytest
```

Runs the full suite in `tests/` (652 tests; for the exact count run
`python -m pytest --collect-only -q | tail -1`) covering
discovery windowing/early-stop, the influencers.club discovery source
(pagination, credit budget, handle→channel-ID bridging, and its DO NOT
CONTACT / already-tracked exclusion), the pre-push gate (view floor,
video-count floor, dead and Shorts-only channels), the search-zone tables
and their three-state verdict, qualification, the DO NOT CONTACT
fail-closed paths, `count_added_today()`/daily-cap behavior,
`prospect_day.py`, the candidate pre-filter, every step of the email chain
(including the older-uploads scan's quota arithmetic and which step gets
credited for a hit), and a regression check that the removed paid
email-finder integrations (Hunter.io, Modash) stay removed. No network
calls or real credentials are needed — everything is mocked.

## Tuning

- Adjust scoring weights and thresholds at the top of `scoring.py`.
- Adjust `QUOTA_CEILING` in `.env` if you want more/less headroom for
  enrichment calls after discovery.
- Adjust `DAILY_QUALIFIED_CAP` / `DAILY_FLAGGED_CAP` in `.env` if 30/10 per
  niche per day doesn't match your review team's actual capacity. Note
  that with only the channel-age criterion left able to flag a row,
  Lifestyle Sofa (no age requirement) never produces a flagged row, so its
  flagged budget goes unused.
- Per-niche thresholds (`min_avg_views`, `min_channel_age_months`) live on
  each `NICHES` entry in `main.py`, not in `.env`.
- The shared hard requirements are elsewhere: at the top of `main.py`,
  `MIN_VIDEO_COUNT` (30), `MIN_VIEWS_PER_VIDEO` (10,000 per video across the
  last 10), `MIN_UPLOADS_PER_YEAR` (6), and `MAX_DAYS_SINCE_LAST_UPLOAD`
  (365); and the allowed countries in `search_zones.py`
  (`ALLOWED_COUNTRY_CODES`, plus the name tables the About-panel lookup
  uses). Widening "Europe" to include Russia, Belarus or Turkey is a
  one-line edit there — they're excluded by default and flagged in a
  comment.
- `DEFAULT_NICHE_MATCH` in `main.py` is a fixed placeholder fed into every
  Overall Score, since automated topical/niche matching isn't implemented —
  reviewers can factor niche fit in manually via the Airtable
  "Notes"/"Status" fields.
