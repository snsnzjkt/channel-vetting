"""
Order the reviewer's queue by how the keyword that found a channel has performed.

## What this is, and what it deliberately is not

It is an ORDERING. It never drops a row, never writes to `rejected_handles.json`,
and never changes which candidates reach Airtable — so its worst case is the
ordering we have today, which is arrival order. That bound is the reason it can
ship on a signal that is not yet statistically proven, when a GATE on the same
signal could not.

Measured 2026-08-25 over 65 labelled rows, leave-one-out so no row scores itself:

    per-keyword approval rate   AUC 0.602   95% CI [0.442, 0.749]
    AI metadata screen          AUC 0.432   95% CI [0.288, 0.588]

Neither is significant at that sample size. The keyword signal is shipped over
the AI one on three grounds that do not depend on significance: a better point
estimate, zero cost per candidate against one Gemini request, and a human can
read the table and disagree with it. The AI screen also measured 63-68% recall as
a gate — see YIELD_OPTIMIZATION_PLAN.md 14.16 — and an opaque signal whose point
estimate sits below random has nothing to recommend it here.

## Why the keyword is a real signal

Section 14.7 measured a 9x spread in reviewer approval across the keywords that
find candidates: `home theater products review` ran 5 of 5 approved while
`country living home` ran 1 of 9. `main.push_record` already writes the finding
keyword into the `Source` field of every row, so the history needed to score a
new candidate is sitting in the tables.

## The honest limits

Cell counts are small — 1 to 9 labelled rows per keyword — so a rate is a hint,
not a probability. `MIN_LABELLED_FOR_RATE` refuses to score a keyword with too
little history rather than reporting a confident 0% or 100% off one row, and a
candidate with no usable keyword gets the neutral prior instead of a zero, which
would sort it to the bottom on missing data. That is the same rule the gates
follow: absent data never disqualifies.

Only the free `search_list` discovery path carries a real keyword. The paid
vendor path writes the constant string "influencers.club discovery", so every
candidate from it shares one bucket and this signal is blunt there by
construction.
"""
import re
from collections import defaultdict

# A keyword needs at least this many labelled rows before its rate is used. Below
# it, one verdict would set a 0% or 100% rate and sort every future candidate on
# a single reviewer decision.
MIN_LABELLED_FOR_RATE = 3

# What a candidate scores when no keyword of its own has enough history. The
# BASE RATE, deliberately — a neutral prior sorts it among the middle, while a
# zero would bury every candidate whose keyword is simply new.
NEUTRAL_PRIOR = 0.5

_SOURCE_KEYWORDS = re.compile(r"\(([^)]*)\)\s*$")


def source_keywords(source: str) -> list:
    """
    The finding keywords recorded in a row's `Source` field.

    `main.push_record` writes `"{SOURCE_LABEL} (kw1, kw2)"`, so the keywords are
    the parenthesised tail. Parsed rather than stored separately because that
    field is what exists on the ~280 rows already in the tables, and a signal
    that needs a migration before it can be measured does not get measured.
    """
    match = _SOURCE_KEYWORDS.search(source or "")
    if not match:
        return []
    return [k.strip() for k in match.group(1).split(",") if k.strip()]


def approval_rates(labelled_rows) -> dict:
    """
    {keyword: {"approved": n, "rejected": n, "rate": float}} from labelled rows.

    `labelled_rows` is an iterable of dicts with "source" and "label", where
    label is "Approved" or "Rejected". Anything else is ignored: a row still
    awaiting review is absence of evidence, not evidence.

    Keywords below MIN_LABELLED_FOR_RATE are present in the output with their
    counts but carry `rate: None`, so a caller can show a reviewer "we have only
    one verdict for this keyword" rather than silently treating thin history as
    a confident rate.
    """
    tally = defaultdict(lambda: {"approved": 0, "rejected": 0})
    for row in labelled_rows or []:
        label = (row or {}).get("label")
        if label not in ("Approved", "Rejected"):
            continue
        bucket = "approved" if label == "Approved" else "rejected"
        for keyword in source_keywords(row.get("source", "")):
            tally[keyword][bucket] += 1

    out = {}
    for keyword, counts in tally.items():
        total = counts["approved"] + counts["rejected"]
        out[keyword] = {
            **counts,
            "rate": (counts["approved"] / total
                     if total >= MIN_LABELLED_FOR_RATE else None),
        }
    return out


def priority_score(source: str, rates: dict) -> tuple:
    """
    (score, explanation) for one candidate. Higher sorts earlier.

    The score is the BEST rate among the candidate's keywords, not the mean.
    A candidate found by both a strong and a weak keyword is a candidate the
    strong keyword found, and averaging would punish it for the coincidence of
    having been matched twice — the same reasoning `off_target_reason` uses when
    it lets on-target evidence rescue rather than average.

    The explanation always names the keyword and its record, because a reviewer
    who disagrees with an ordering needs to see what produced it.
    """
    keywords = source_keywords(source)
    scored = []
    for keyword in keywords:
        entry = (rates or {}).get(keyword) or {}
        if entry.get("rate") is not None:
            scored.append((entry["rate"], keyword, entry))
    if not scored:
        thin = [k for k in keywords if k in (rates or {})]
        why = (f"no keyword with {MIN_LABELLED_FOR_RATE}+ verdicts"
               + (f" (thin: {', '.join(thin[:3])})" if thin else ""))
        return NEUTRAL_PRIOR, why
    scored.sort(reverse=True)
    rate, keyword, entry = scored[0]
    return rate, (f"{keyword} {rate:.0%} approved "
                  f"({entry['approved']}/{entry['approved'] + entry['rejected']})")


def rank(candidates, rates: dict) -> list:
    """
    `candidates` ordered best-first, each annotated with its score and reason.

    Ties break on the original order, which keeps the output stable run to run
    and means a batch of candidates sharing one keyword stays in arrival order
    rather than being shuffled by an implementation detail.
    """
    scored = []
    for i, candidate in enumerate(candidates or []):
        score, why = priority_score((candidate or {}).get("source", ""), rates)
        scored.append({**candidate, "priority": score, "priority_reason": why,
                       "_i": i})
    scored.sort(key=lambda c: (-c["priority"], c["_i"]))
    for c in scored:
        c.pop("_i", None)
    return scored
