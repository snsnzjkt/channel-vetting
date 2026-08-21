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

def test_widening_on_target_did_not_re_admit_gaming_channels():
    """
    on_target_terms only RESCUE, so widening them makes the gate MORE
    permissive. This repo has been burned by that exact move before: "gaming
    setup" in the discovery query made 45% of a niche's rows gaming channels.

    A gamer who films his setup now matches "room tour" — he must still drop,
    because off_share > on_share does the real work.
    """
    gamer_with_setup = [
        "My Gaming Room Tour 2026",
        "Fortnite Victory Royale Montage",
        "New Gaming PC Build - RTX 5090",
        "Room Tour: RGB Setup Reveal",
        "Call of Duty Warzone Gameplay",
    ]
    reason, _ = main.off_target_reason(HT, "", gamer_with_setup)
    assert reason == main.DROP_OFF_TARGET, "a gaming channel was rescued by room-tour terms"


def test_a_genuine_home_theater_channel_with_one_gaming_video_is_kept():
    """The other side of the same threshold: one console video is not a verdict."""
    titles = [
        "Our Basement Home Theater Reveal",
        "Klipsch vs Polk - Surround Comparison",
        "Best Recliner Seating for a Media Room",
        "Projector Screen Install",
        "Playing Xbox on the new projector",
    ]
    reason, detail = main.off_target_reason(HT, "", titles)
    assert reason is None, f"dropped a genuine home theater channel: {detail!r}"
