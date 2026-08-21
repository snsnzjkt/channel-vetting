"""
Gaming and generic-tech channels are rejected; genuine prospects are not.

The brief: the tables were filling with gaming and generic-tech creators, and
"a YouTuber who reviews iPhones, laptops, gaming PCs and gadgets" is a bad match
however good the numbers are. Measured over the 147 rows live on 2026-08-21,
this gate rejects 29 of the 63 Home Theater rows (46%) and 0 of the 84 Lifestyle
rows.

EVERY case below is a real channel from those tables, with its real bio and real
title vocabulary, because the previous attempt at this gate was rejected on
measurements and a synthetic fixture would not have caught what did that:

  - A positive "must match an on-niche term" rule discarded "Jasper Tran -
    House Design Ideas" on a score of 0/50. Genuine prospects title videos
    "This Small House Will Make You Fall in Love", which no list anticipates.
  - Bio-only rules are unusable: four tracked channels have no bio worth
    reading, and "High-quality Tech, Unboxing, Reviews" is the bio of a real
    hi-fi channel (OCM Reviews).
"""
import main
from niches import NICHES

HT = NICHES["Home Theater"]
LS = NICHES["Lifestyle Sofa"]

# Home Theater restricted itself to the toys_and_kids category on 2026-08-22,
# because a backtest against the reviewer's own verdicts showed the gaming /
# phones_and_pcs / generic_gadgets / ai_and_crypto vocabulary was dropping 14 of
# his 21 APPROVED channels while catching only 9 of 31 he rejected.
#
# The vocabulary and the share arithmetic are still correct and still used by any
# niche that does not restrict, so the cases below keep testing them — against
# this unrestricted config rather than against live Home Theater. What changed is
# WHICH NICHE APPLIES THEM, not whether they work.
#
# The niche-level policy is asserted separately, from the labels, in
# test_off_target_toys.py.
ALL_CATEGORIES = {k: v for k, v in HT.items() if k != "off_target_categories"}


def _titles(*specs):
    """Expand ("text", n) pairs into a title list, so shares are exact."""
    out = []
    for text, n in specs:
        out.extend([text] * n)
    return out


# --- the channels the brief is about --------------------------------------


def test_a_fortnite_channel_is_rejected():
    """@grxnt, in the Home Theater table: 76% of titles are Fortnite."""
    reason, detail = main.off_target_reason(
        ALL_CATEGORIES, "I make Fortnite family friendly content and live streams.",
        _titles(("NEW FORTNITE *SEASON 4* UPDATE RIGHT NOW!! NEW MAP, BATTLE PASS", 38),
                ("Some other video", 12)),
    )
    assert reason == main.DROP_OFF_TARGET
    assert "gaming" in detail


def test_a_pc_building_channel_is_rejected():
    """@dankamyouknow: 86% PC builds. The brief names PC hardware explicitly."""
    reason, _ = main.off_target_reason(
        ALL_CATEGORIES, "Collab: dankamcontact@gmail.com",
        _titles(("Building a DOUBLE DECKER PC in the Thermaltake Capo X!", 43),
                ("Watch until the end", 7)),
    )
    assert reason == main.DROP_OFF_TARGET


def test_a_phone_and_camera_reviewer_is_rejected():
    """@paulantill was IN the Home Theater table reviewing Pixels and DJI gear.
    The brief's own bad-match example."""
    reason, _ = main.off_target_reason(
        ALL_CATEGORIES, "Tech and camera reviews",
        _titles(("Google Pixel 11 Pro Fold is $200 Cheaper vs Galaxy Z Fold 8", 21),
                ("DJI Osmo Pocket 4P vs Insta360 Luna Ultra Zoom", 15),
                ("A home cinema video", 14)),
    )
    assert reason == main.DROP_OFF_TARGET


def test_an_ai_and_crypto_channel_is_rejected():
    """@cryptonfttigers_spoton reached the Home Theater table."""
    reason, _ = main.off_target_reason(
        ALL_CATEGORIES, "#1 Crypto Youtuber, NFT TIGERS SPOTON",
        _titles(("Fetra AI Review 2026: Best AI Automation Platform Demo", 17),
                ("Unrelated upload", 33)),
    )
    assert reason == main.DROP_OFF_TARGET


def test_a_generic_gadget_channel_is_rejected():
    """@banetech: treadmills, dehumidifiers, earbuds, phone cases. Its own bio
    says "sharing my experiences about technology"."""
    reason, _ = main.off_target_reason(
        ALL_CATEGORIES, "Simply sharing my experiences about technology.",
        _titles(("UREVO Strol 2E Pro Review: Smart Treadmill for Working From Home?", 5),
                ("Southern Humidity CRUSHED: Why You Need This Dehumidifier", 5),
                ("Neutral upload title", 40)),
    )
    assert reason == main.DROP_OFF_TARGET


# --- the persona rule, and why it has to exist ---------------------------


def test_a_gaming_bio_convicts_a_channel_the_share_alone_would_miss():
    """
    @dragstertv: bio "money glitches on games such as Forza Horizon 5". At the
    low end the title share alone cannot separate this from a real hi-fi
    channel — see the next test, which scores almost the same and is KEPT.
    """
    reason, detail = main.off_target_reason(
        ALL_CATEGORIES, "I post the most up to date money glitches on games such as Forza Horizon 5",
        _titles(("*LIVE* Clearing House on Rainbow Six Siege", 2),
                ("Some driving video", 48)),
    )
    assert reason == main.DROP_OFF_TARGET
    assert "bio says" in detail


def test_a_real_hifi_channel_is_rescued_at_the_same_off_target_score():
    """
    @ocmreviews: Fosi Audio DACs, IEMs, an Ultimea Atmos soundbar. Scores 0.06
    off-target — nearly identical to DragsterTV above — and is KEPT because its
    on-target share is 0.60. This single test is the reason on-target terms
    exist, and the reason they rescue rather than admit.
    """
    reason, _ = main.off_target_reason(
        HT, "High-quality Tech, Unboxing, Reviews, Comparisons.",
        _titles(("Budget Soundbar with Premium Atmos? Ultimea X40 Tested", 30),
                ("This Tiny DAC Made My Phone Sound Better - Fosi Audio MD3", 17),
                ("iPhone accessory roundup", 3)),
    )
    assert reason is None


# --- what must never be dropped -----------------------------------------


def test_a_prospect_with_no_recognisable_vocabulary_is_kept():
    """
    @jaspertran8016 — "Jasper Tran - House Design Ideas". The channel the
    2026-08-15 positive-scoring attempt discarded at 0/50. Nothing here matches
    the on-target list either, and that must not matter: the gate needs positive
    evidence of being something ELSE.
    """
    reason, _ = main.off_target_reason(
        HT, "House design and small home ideas.",
        _titles(("This Small House Will Make You Fall in Love Instantly!", 25),
                ("Stop Overpaying for Space You Never Use", 25)),
    )
    assert reason is None


def test_a_channel_with_no_titles_is_kept():
    """Absent data never disqualifies — the pipeline's standing rule."""
    assert main.off_target_reason(HT, "gamer playing games all day", [])[0] is None
    assert main.off_target_reason(HT, "", None)[0] is None


def test_a_niche_without_rescue_vocabulary_disables_the_gate():
    """
    Missing on_target_terms would pin on_share at 0, so ANY off-target term
    would outweigh it and the gate would run far harder than it was calibrated
    to. Disabled is the safe reading of a missing key.
    """
    bare = {k: v for k, v in HT.items() if k != "on_target_terms"}
    reason, _ = main.off_target_reason(
        bare, "gamer", _titles(("FORTNITE BATTLE PASS", 50)))
    assert reason is None


def test_an_on_niche_home_theater_channel_is_kept():
    """@zerofidelity — a 34-character bio and unambiguous hi-fi titles."""
    reason, _ = main.off_target_reason(
        HT, "Hi-Fi on a budget",
        _titles(("Why Waste your Money on Expensive Speakers?", 25),
                ("An Excellent, Reliable CD-Player for Audiophiles!", 25)),
    )
    assert reason is None


def test_lifestyle_channels_are_untouched():
    """
    Zero of the 84 live Lifestyle rows are flagged (the highest scores 0.04), so
    the threshold has real headroom rather than sitting on the distribution.
    """
    for bio, title in (
        ("Home decor and styling", "AESTHETIC BATHROOM MAKEOVER pinterest inspired, deep cleaning, DIY"),
        ("Interior designer", "House Tour - Renovating in Point Grey Vancouver VLOG"),
        ("Mum of two", "cozy girl morning routine, decorating my apartment"),
    ):
        assert main.off_target_reason(LS, bio, [title] * 50)[0] is None


# --- placement, which is a credit-safety property ------------------------


def test_the_relevance_gate_runs_before_the_paid_email_chain():
    """
    The brief: "irrelevant creators are filtered out before they consume a
    creator credit". Step 4 of the email chain is a 0.2-credit lookup, so the
    ORDER here is the feature, not the gate alone.
    """
    import inspect
    src = inspect.getsource(main.process_candidate)
    assert src.index("off_target_reason(") < src.index("resolve_email_with_source"), (
        "the relevance gate must run BEFORE the paid email chain"
    )


def test_the_relevance_gate_runs_before_the_longform_paging():
    """The other expensive step: confirming 30 non-Shorts uploads can page for
    2 quota units a page."""
    import inspect
    src = inspect.getsource(main.process_candidate)
    assert src.index("off_target_reason(") < src.index("count_longform_in_older_videos")


def test_an_off_target_drop_is_cached_as_a_durable_rejection():
    """Not transient, so the creator is excluded server-side next run and the
    0.01 discovery credit is never paid for them twice."""
    assert main.DROP_OFF_TARGET not in main.TRANSIENT_DROP_REASONS
