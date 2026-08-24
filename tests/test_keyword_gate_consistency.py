"""
A niche must not SEARCH for a category and also list it as a reason to DROP.

Found 2026-08-25 while expanding Home Theater's keywords. `sports_commentary`
was in `VIDEO_TOPIC_CATEGORIES` — the topic gate's drop allowlist — while the
niche carried two discovery keywords aimed at exactly that cluster, the better of
which runs 4 of 9 APPROVED. The reviewer's approved list includes JTL SPORTS,
MAH, Cowboys Report by Chat Sports and The Joel Klatt Show.

That is the same class of defect as the text_criteria/av_specialist contradiction
in test_criteria_consistency.py — a term pulling in two directions at once — but
worse, because it spends 100 quota units per search finding channels the gate
would then remove.
"""
import re

import config
import niches


def _excluded_vocab(category):
    return [t.strip() for t in niches.OFF_TARGET_TERMS.get(category, ())
            if len(t.strip()) >= 4]


def test_no_niche_searches_for_a_category_the_topic_gate_would_drop():
    """
    The guard. If a keyword targets a category in VIDEO_TOPIC_CATEGORIES, the
    pipeline is paying to find rows it would then discard.
    """
    failures = []
    for niche, cfg in niches.NICHES.items():
        for keyword in cfg.get("keywords") or []:
            low = keyword.lower()
            for category in config.VIDEO_TOPIC_CATEGORIES:
                for term in _excluded_vocab(category):
                    if re.search(r"\b" + re.escape(term), low):
                        failures.append(
                            f"{niche}: keyword {keyword!r} targets {category!r}, "
                            f"which the topic gate is allowed to drop "
                            f"(matched {term!r})")
    assert not failures, "\n".join(failures)


def test_sports_commentary_is_NOT_in_the_drop_allowlist():
    """
    Pinned by name, because the reviewer demonstrably buys this cluster: 4 of 9
    rows from "sports podcast commentary" are Approved, the second-best keyword
    record in Home Theater. The +1 catch measured in 14.11 does not justify
    standing between him and it.
    """
    assert "sports_commentary" not in config.VIDEO_TOPIC_CATEGORIES


def test_the_measured_harmful_category_is_still_excluded_from_the_allowlist():
    """phones_and_pcs was net-negative at every threshold — 14.11."""
    assert "phones_and_pcs" not in config.VIDEO_TOPIC_CATEGORIES


def test_the_allowlist_still_has_teeth():
    """
    Trimming it must not empty it. These four earned their place on measurement
    or on standing instruction; a list that shrank to nothing would make the gate
    silently inert while still looking armed.
    """
    for expected in ("gaming", "toys_and_kids", "av_specialist", "firearms"):
        assert expected in config.VIDEO_TOPIC_CATEGORIES, expected
