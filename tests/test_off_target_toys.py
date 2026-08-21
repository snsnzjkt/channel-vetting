"""
Toy / construction-brick / kids-doll channels must not read as home prospects.

From reviewer feedback 2026-08-22. Victor rejected two Home Theater rows and
neither was visible to `off_target_reason` at all — both scored 0.00 off-target
and passed every gate:

  Bricksie ............ a LEGO City channel
  Baby Doll Stories ... kids' doll roleplay

Baby Doll Stories is the one that shows why the category is global rather than
Home-Theater-only: its titles say "Room Makeover" and "DIY Cardboard ... Doll
Room", so it scored ON-TARGET for Lifestyle Sofa. A kids' doll channel was
reading as a home-decor prospect.

The titles below are the real ones from the reviewer's screenshots.
"""
import pytest

import main
import niches

HT = niches.NICHES["Home Theater"]
LS = niches.NICHES["Lifestyle Sofa"]

BRICKSIE = [
    "ALL NEW LEGO September 2026!",
    "LEGO City Canal Update! Boat Chaos!",
    "BIGGEST SET EVER! LumiBricks Town Life Station!",
    "Is this better? Too Much Road? LEGO City Rearranged!",
    "LEGO City Beach Update! Highly Detailed Sand & Water",
]

BABY_DOLL = [
    "Poor Vs Rich Yb Giga Rich Girl At Room Makeover! Chocolate, Bubble Gum, Mcdonald Girl",
    "Poor Vs Rich Yb Giga Rich Girl At School! Who Is The Best Student?",
    "DIY Cardboard Vs Silver Vs Golden Makeover Doll Room: Poor Vs Rich Giga Rich Doll",
]


@pytest.mark.parametrize("niche", [HT, LS], ids=["home_theater", "lifestyle"])
@pytest.mark.parametrize("titles", [BRICKSIE, BABY_DOLL], ids=["bricksie", "baby_doll"])
def test_reviewer_rejected_channels_are_dropped(niche, titles):
    reason, detail = main.off_target_reason(niche, "", titles)
    assert reason == main.DROP_OFF_TARGET, f"still passing the gate: {detail!r}"
    assert "toys_and_kids" in detail


# --- substring traps -------------------------------------------------------
#
# Matching is plain `term in title.lower()`, so the natural root words are all
# traps. Each of these is a REAL home-content topic that a careless term would
# kill. If someone "simplifies" the vocabulary to "doll" / "brick" / "toy",
# these fail.

@pytest.mark.parametrize("label,titles", [
    ("dollar store", ["Dollar Store Decor Haul", "5 Dollar Tree DIY Ideas",
                      "Dollar Store Organizing Hacks"]),
    ("exposed brick", ["Exposed Brick Fireplace Makeover",
                       "Brick Wall Accent in Our Living Room"]),
    ("toy storage", ["Toy Storage Ideas for the Playroom",
                     "Nursery Tour and Organization"]),
    ("toyota", ["Toyota Tundra Review", "Toyota Camry Road Trip"]),
])
@pytest.mark.parametrize("niche", [HT, LS], ids=["home_theater", "lifestyle"])
def test_substring_traps_are_not_dropped_as_toys(label, titles, niche):
    reason, detail = main.off_target_reason(niche, "", titles)
    if reason:
        assert "toys_and_kids" not in detail, (
            f"{label!r} was misread as toy content: {detail!r}"
        )


def test_no_toy_term_is_a_bare_root_word():
    """
    Pins the rule rather than the symptom. Every entry must be a brand or a
    multi-word phrase; the bare roots collide with real home-decor vocabulary.
    """
    banned = {"doll", "brick", "toy", "figure", "set", "play", "kid", "kids"}
    for term in niches.OFF_TARGET_TERMS["toys_and_kids"]:
        assert term not in banned, (
            f"{term!r} is a bare root word and will match real decor titles "
            "(dollar store / brick wall / toy storage). Use a brand or a phrase."
        )


# --- the "not all content is like that" case -------------------------------

def test_a_partly_on_topic_furniture_channel_is_kept():
    """
    The reviewer's actual ask: accept a channel that "did a room tour or mostly
    home furniture type of content", even when not all of its content is that.
    Two of these five videos are unrelated vlogs; that must not disqualify it.
    """
    titles = [
        "Our Basement Home Theater Room Tour",
        "Best Sectional Sofa for a Media Room",
        "What I Ate This Week",
        "Weekend Vlog: Coffee and Errands",
        "TV Wall Mount Install - Cable Management Tips",
    ]
    reason, detail = main.off_target_reason(HT, "", titles)
    assert reason is None, f"a partly on-topic furniture channel was dropped: {detail!r}"


def test_room_and_furniture_vocabulary_reaches_home_theater():
    """
    Home Theater's on-target list was pure AV EQUIPMENT, which the 96-row
    backtest found inverted against the reviewer's verdict. Room and furniture
    words must be present or a furniture-led channel cannot be rescued.
    """
    terms = set(niches.NICHES["Home Theater"]["on_target_terms"])
    for expected in ("room tour", "furniture", "sectional", "living room",
                     "entertainment center", "tv stand", "media console"):
        assert expected in terms, f"{expected!r} missing from Home Theater on_target_terms"


# --- widening on-target must not re-admit gaming ---------------------------

def test_home_theater_applies_only_the_toys_category():
    """
    The niche-level policy, asserted from the reviewer's labels.

    MEASURED 2026-08-22 over 21 approved and 31 rejected Home Theater rows,
    using each channel's real recent titles:

        whole gate    drops 67% of APPROVED, 29% of REJECTED   (-38%)
        gaming              48% approved vs 16% rejected
        phones_and_pcs      52%          vs 19%
        generic_gadgets     43%          vs 13%
        ai_and_crypto       19%          vs  6%

    Every category was more likely to kill a channel the reviewer wanted than
    to catch one he did not. The gate's own docstring named Bane Tech,
    DanKamYouKnow, Paul Antill and NFT TIGERS as "hand-verified off-target";
    the reviewer approved all four.

    Re-enabling a category here without a fresh backtest re-breaks this.
    """
    assert niches.NICHES["Home Theater"]["off_target_categories"] == ["toys_and_kids"]


def test_the_approved_tech_profile_survives_the_home_theater_gate():
    """
    The four channels the old calibration named as verified off-target are all
    approved by the reviewer. None may be dropped now.
    """
    profiles = {
        "DanKamYouKnow (PC builds)": ["Building a DOUBLE DECKER PC in the Thermaltake Capo X!"] * 43,
        "Paul Antill (phones)": ["Google Pixel 11 Pro Fold is $200 Cheaper vs Galaxy Z Fold 8"] * 21,
        "NFT TIGERS (crypto)": ["Fetra AI Review 2026: Best AI Automation Platform Demo"] * 17,
        "Bane Tech (gadgets)": ["Southern Humidity CRUSHED: Why You Need This Dehumidifier"] * 10,
    }
    for name, titles in profiles.items():
        reason, detail = main.off_target_reason(HT, "sharing my experiences about technology", titles)
        assert reason is None, f"{name} was dropped, but the reviewer approved it: {detail!r}"


def test_toy_content_still_drops_for_home_theater_despite_the_restriction():
    """The restriction must not disarm the reviewer's explicit instruction."""
    reason, detail = main.off_target_reason(HT, "", BRICKSIE)
    assert reason == main.DROP_OFF_TARGET
    assert "toys_and_kids" in detail


def test_a_niche_without_the_key_still_applies_every_category():
    """Backward compatibility: absent the key, historical behaviour is unchanged."""
    unrestricted = {k: v for k, v in HT.items() if k != "off_target_categories"}
    gaming = ["NEW FORTNITE *SEASON 4* UPDATE RIGHT NOW!! NEW MAP, BATTLE PASS"] * 38
    reason, detail = main.off_target_reason(unrestricted, "", gaming)
    assert reason == main.DROP_OFF_TARGET
    assert "gaming" in detail


def test_toy_story_is_not_a_term():
    """
    Removed 2026-08-22: it matched "Toy Story Gaming Laptop! MSI Cyborg 15
    Special Edition" on Paul Antill, a channel the reviewer APPROVED. A film
    brand appears on branded merchandise and is not evidence of kids' content.
    """
    assert "toy story" not in niches.OFF_TARGET_TERMS["toys_and_kids"]
