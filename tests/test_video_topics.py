"""
Topic evidence from creator-declared tags — and the rules it must not break.

The vocabularies here (firearms, toys_and_kids) already existed; only the INPUT
is new. So these tests are mostly about the two standing rules that every
relevance signal in this repo has to obey, because three of them have been
caught pointing the wrong way: absent data never disqualifies, and nothing may
admit a channel on positive evidence alone.
"""
from channel_vetting.discovery import niches
from channel_vetting.verification import video_topics as vt

FIREARMS = {"firearms": niches.EXCLUDED_TOPIC_TERMS["firearms"]}
TOYS = {"toys_and_kids": niches.OFF_TARGET_TERMS["toys_and_kids"]}
BOTH = {**FIREARMS, **TOYS}


# --- the standing rule: absent data never disqualifies ---

def test_no_tags_is_no_verdict():
    for empty in (None, [], ["", "   "]):
        ev = vt.topic_evidence(empty, BOTH)
        assert ev["hits"] == {}
        assert ev["tags_seen"] == 0
        assert vt.dominant_topic(ev, 0.1) == (None, 0.0)


def test_no_vocabulary_is_no_verdict():
    """A missing config key must disable the check, not run it unbounded."""
    ev = vt.topic_evidence(["lego", "handgun"], {})
    assert ev["hits"] == {}
    assert vt.dominant_topic(ev, 0.0) == (None, 0.0)


def test_tags_that_match_nothing_produce_no_topic():
    ev = vt.topic_evidence(["home theater", "projector", "atmos"], BOTH)
    assert ev["hits"] == {}
    assert vt.dominant_topic(ev, 0.1) == (None, 0.0)


# --- the signal the module exists for ---

def test_a_lego_channel_is_legible_from_tags_alone():
    """
    The case the title gates miss: not one of these titles-shaped strings would
    have matched, but the creator tagged the topic themselves.
    """
    tags = ["lego", "lego moc", "minifigure", "brickheadz", "afol"]
    ev = vt.topic_evidence(tags, TOYS)
    assert ev["hits"]["toys_and_kids"] == 4
    assert ev["share"]["toys_and_kids"] == 4 / 5
    category, share = vt.dominant_topic(ev, 0.5)
    assert category == "toys_and_kids" and share == 4 / 5


def test_a_firearms_channel_is_legible_from_tags_alone():
    tags = ["handgun review", "ammo test", "range day", "firearm safety"]
    ev = vt.topic_evidence(tags, FIREARMS)
    assert ev["hits"]["firearms"] == 3
    assert vt.dominant_topic(ev, 0.5)[0] == "firearms"


def test_share_is_over_tags_so_one_stray_tag_is_not_a_topic():
    """
    The whole point of share over count. A home-theatre channel that tagged one
    video "lego" is not a Lego channel, and a raw hit count cannot tell it from
    one that is.
    """
    tags = ["lego"] + [f"home theater {i}" for i in range(39)]
    ev = vt.topic_evidence(tags, TOYS)
    assert ev["hits"]["toys_and_kids"] == 1
    assert ev["share"]["toys_and_kids"] == 1 / 40
    assert vt.dominant_topic(ev, 0.25) == (None, 0.0), "one tag in 40 is noise"


def test_word_boundaries_stop_the_substring_false_positives():
    """
    'iem' inside 'item' and 'dac' inside 'dachshund' are the reason this matches
    on boundaries. The title gates carry leading spaces on those same terms to
    work around exactly this.
    """
    vocab = {"av": ["iem", "dac", "sonos"]}
    ev = vt.topic_evidence(["item review", "dachshund grooming", "predacon"], vocab)
    assert ev["hits"] == {}, ev
    assert vt.topic_evidence(["iem review"], vocab)["hits"] == {"av": 1}


def test_the_reported_term_is_the_specific_one():
    """Longest-first: a channel tagged 'bookshelf speaker' should say so."""
    vocab = {"av": ["speaker", "bookshelf speaker"]}
    ev = vt.topic_evidence(["bookshelf speaker"], vocab)
    assert ev["terms"]["av"] == ["bookshelf speaker"]


# --- determinism and record-safety ---

def test_dominant_topic_is_stable_when_two_topics_tie():
    ev = vt.topic_evidence(["lego", "handgun"], BOTH)
    assert ev["share"] == {"toys_and_kids": 0.5, "firearms": 0.5}
    # Alphabetical on a tie, so the same input never yields two answers.
    assert vt.dominant_topic(ev, 0.5) == ("firearms", 0.5)
    assert vt.dominant_topic(ev, 0.5) == vt.dominant_topic(ev, 0.5)


def test_terms_are_deduped_and_capped_for_the_record():
    """This string reaches Airtable; 400 repeats of 'lego' must not."""
    ev = vt.topic_evidence(["lego"] * 400, TOYS)
    assert ev["terms"]["toys_and_kids"] == ["lego"]
    assert ev["hits"]["toys_and_kids"] == 400
    assert len(vt.summarise(ev)) < 300


def test_summarise_is_empty_when_there_is_nothing_to_say():
    assert vt.summarise({}) == ""
    assert vt.summarise(vt.topic_evidence([], TOYS)) == ""


def test_summarise_leads_with_the_share_and_names_the_evidence():
    ev = vt.topic_evidence(["lego", "minifigure", "home theater"], TOYS)
    line = vt.summarise(ev, ["20", "20", "26"])
    assert "toys_and_kids" in line and "67%" in line
    assert "lego" in line and "minifigure" in line
    assert "Gaming" in line, "category names, not bare IDs"


# --- categories are reported, never gated ---

def test_unknown_categories_surface_rather_than_vanish():
    assert vt.category_name("999") == "category 999"
    assert vt.category_name("20") == "Gaming"
    assert vt.category_name(None) == "" and vt.category_name("") == ""


def test_category_distribution_is_most_common_first():
    assert vt.category_distribution(["20", "20", "26"]) == [("Gaming", 2), ("Howto & Style", 1)]
    assert vt.category_distribution([]) == []
    assert vt.category_distribution(None) == []
