"""
The two relevance layers must not contradict each other on one vocabulary.

REGRESSION. Home Theater's `text_criteria` asked whether the recurring subject
was "home audio-visual equipment ... speakers, projectors, receivers,
soundbars" — scoring "speakers" as evidence a channel is ON-niche — while
`OFF_TARGET_TERMS["av_specialist"]` holds speaker, subwoofer, audiophile,
turntable, amplifier and is active for that niche, scoring the same word as
evidence it is OFF-niche.

Latent, because GEMINI_TEXT_TIER defaults False. Switching the tier on would have
had the AI layer score highest exactly the channels the exclusion was built to
catch — Zero Fidelity, New Record Day, Lenny Florentine, Forever Analog. The
2026-08-22 mining moved that vocabulary to an exclusion and never revisited the
AI prompt, which is the whole failure mode: a term flipped in one place and left
standing in another.

This test is the guard for the general case, not just that one term.
"""
import config
import niches


def _criteria_text(cfg, key):
    return " ".join((c.get("test") or "").lower() for c in (cfg.get(key) or []))


def test_no_niche_praises_vocabulary_it_also_excludes():
    """
    A term on an active exclusion list must not appear as a positive example in
    the AI criteria for the same niche. A term on both scores off == on, and the
    gate needs off > on to fire — so the two layers cancel.
    """
    failures = []
    for niche, cfg in niches.NICHES.items():
        active = cfg.get("off_target_categories")
        categories = (active if active is not None
                      else list(niches.OFF_TARGET_TERMS))
        excluded = set()
        for category in categories:
            for term in niches.OFF_TARGET_TERMS.get(category, ()):
                term = term.strip()
                # Single short tokens ("dac", "iem", "5.1") match inside ordinary
                # words; only multi-character distinctive terms are checked.
                if len(term) >= 5:
                    excluded.add(term)
        for key in ("text_criteria", "video_criteria"):
            # A criterion flagged `names_exclusions` exists to NAME excluded
            # vocabulary so the model can rule it out. Mentioning "action figure"
            # there is the opposite of endorsing it, so it is skipped — and the
            # intent is declared in niches.py rather than guessed from wording,
            # because guessing is how this class of bug started.
            positives = [c for c in (cfg.get(key) or [])
                         if not c.get("names_exclusions")]
            blob = " ".join((c.get("test") or "").lower() for c in positives)
            for term in sorted(excluded):
                if term in blob:
                    failures.append(f"{niche}.{key} praises excluded term {term!r}")
    assert not failures, "\n".join(failures)


def test_on_target_and_off_target_terms_never_overlap():
    """
    The other half of the same rule, and the one section 12 states outright: a
    term on both on_target_terms and an active exclusion scores off == on, so
    moving a term to an exclusion REQUIRES removing it from the rescue list.
    """
    failures = []
    for niche, cfg in niches.NICHES.items():
        on = {t.strip().lower() for t in (cfg.get("on_target_terms") or [])}
        active = cfg.get("off_target_categories")
        categories = (active if active is not None
                      else list(niches.OFF_TARGET_TERMS))
        for category in categories:
            for term in niches.OFF_TARGET_TERMS.get(category, ()):
                if term.strip().lower() in on:
                    failures.append(f"{niche}: {term!r} is both on-target and "
                                    f"excluded via {category}")
    assert not failures, "\n".join(failures)


def test_the_text_tier_stays_OFF_until_it_is_backtested():
    """
    Rewriting the criteria removed a contradiction; it is not evidence the tier
    predicts anything. It measured 27% approved against a 38% base rate, and this
    repo has found four inverted relevance criteria. Turning it on is a decision
    that needs a fresh number, so the default is pinned here deliberately.
    """
    assert config.GEMINI_TEXT_TIER is False, (
        "GEMINI_TEXT_TIER was enabled by default. It needs a backtest against "
        "Status=Approved/Rejected first — see YIELD_OPTIMIZATION_PLAN.md 14.16 "
        "for the fourth relevance criterion that looked obviously right and "
        "measured anti-predictive."
    )


def test_every_niche_still_has_usable_criteria_after_the_rewrite():
    """A rewrite that empties a list would silently disable the tier."""
    for niche, cfg in niches.NICHES.items():
        for key in ("text_criteria", "video_criteria"):
            entries = cfg.get(key) or []
            assert 2 <= len(entries) <= 4, f"{niche}.{key} has {len(entries)}"
            for c in entries:
                assert c.get("name"), f"{niche}.{key}: unnamed criterion"
                assert len(c.get("test") or "") > 40, f"{niche}.{key}: thin test"


def test_a_criterion_that_names_exclusions_is_declared_not_inferred():
    """
    The escape hatch has to be explicit. Inferring "this criterion is an
    exclusion" from its wording is the same guessing that produced the
    contradiction in the first place.
    """
    for niche, cfg in niches.NICHES.items():
        flagged = [c for c in cfg["video_criteria"] if c.get("names_exclusions")]
        assert len(flagged) == 1, f"{niche}: expected exactly one, got {len(flagged)}"
        assert flagged[0].get("required") is True, \
            f"{niche}: an exclusion-naming criterion must be a veto"


def test_home_theater_text_criteria_ask_about_the_SPACE_not_the_GEAR():
    """
    The measured direction: across 96 labelled rows an equipment-focus score was
    INVERTED against the verdict (27% approved vs a 38% base rate; the five most
    equipment-focused channels were all Rejected). The reviewer buys an audience
    for home-entertainment FURNITURE.
    """
    blob = _criteria_text(niches.NICHES["Home Theater"], "text_criteria")
    assert any(w in blob for w in ("room", "space", "furniture", "seating")), blob
