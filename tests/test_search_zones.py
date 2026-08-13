"""
The geographic search zones: US, Canada, UK, Europe, Australia — Ireland
excluded.

Two sources report a channel's country and they report it differently:
`channels.list` returns an ISO alpha-2 code, the public About panel renders
a country name. zone_verdict() has to take either, and has to distinguish
"declared, and outside" from "declared nothing" — only the first is a
reason to discard.
"""
import pytest


# --- the allowed zones ----------------------------------------------------


@pytest.mark.parametrize("code", ["US", "CA", "GB", "AU", "DE", "FR", "NL", "SE", "PL", "NO", "CH"])
def test_allowed_country_codes_are_inside(code):
    from search_zones import zone_verdict

    assert zone_verdict(code) is True


@pytest.mark.parametrize("code", ["IN", "PK", "PH", "BR", "NG", "JP", "NZ", "MX", "ID"])
def test_countries_outside_the_zones_are_rejected(code):
    from search_zones import zone_verdict

    assert zone_verdict(code) is False


def test_ireland_is_excluded():
    """
    "UK (except Ireland)" was explicit. IE is an EU member, so the Europe
    zone would otherwise readmit it through the back door.
    """
    from search_zones import zone_verdict

    assert zone_verdict("IE") is False
    assert zone_verdict("Ireland") is False


def test_northern_ireland_stays_inside_via_gb():
    """GB covers England, Scotland, Wales and Northern Ireland."""
    from search_zones import zone_verdict

    assert zone_verdict("GB") is True
    assert zone_verdict("Northern Ireland") is True


def test_a_lowercase_api_code_still_resolves():
    from search_zones import zone_verdict

    assert zone_verdict("us") is True
    assert zone_verdict(" gb ") is True


# --- About-panel country names -------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("United States", "US"),
        ("United States of America", "US"),
        ("United Kingdom", "GB"),
        ("Canada", "CA"),
        ("Australia", "AU"),
        ("Germany", "DE"),
        ("The Netherlands", "NL"),
        ("Czechia", "CZ"),
        ("Czech Republic", "CZ"),
    ],
)
def test_about_panel_names_resolve_to_codes(name, expected):
    from search_zones import country_code

    assert country_code(name) == expected


def test_names_are_matched_case_and_space_insensitively():
    from search_zones import country_code

    assert country_code("  united   states  ") == "US"
    assert country_code("UNITED KINGDOM") == "GB"


def test_a_country_name_gets_the_same_verdict_as_its_code():
    from search_zones import zone_verdict

    assert zone_verdict("United States") is True
    assert zone_verdict("India") is False


# --- unknown is not the same as outside -----------------------------------


@pytest.mark.parametrize("raw", ["", "   ", None, "Unknown", "unknown", "N/A", "-"])
def test_an_undeclared_country_has_no_verdict(raw):
    """
    get_channel_stats() defaults snippet.country to "Unknown" when a
    channel sets none. None (not False) is what keeps that channel in the
    pipeline for a human to look at.
    """
    from search_zones import zone_verdict

    assert zone_verdict(raw) is None


def test_an_unrecognised_country_name_has_no_verdict():
    """
    A name this module has no entry for is absent data, not evidence of
    being outside the zones — the same rule qualify() applies to an
    unknown channel age. Reading it as "outside" would silently discard
    real prospects every time YouTube relabels or relocalises the panel.
    """
    from search_zones import zone_verdict

    assert zone_verdict("Kingdom of Somewhere") is None
    assert zone_verdict("Åland-ish") is None


def test_none_is_falsy_so_callers_must_compare_identity():
    """
    `if not zone_verdict(c): drop` would discard every unknown-country
    channel — the exact failure this three-state return exists to prevent.
    main.process_candidate compares `is False`; this pins why.
    """
    from search_zones import zone_verdict

    assert zone_verdict("Unknown") is not False
    assert bool(zone_verdict("Unknown")) is False


# --- the code table itself ------------------------------------------------


def test_every_allowed_name_maps_into_the_allowed_code_set():
    """
    A typo in ALLOWED_COUNTRY_NAMES (a code that isn't in
    ALLOWED_COUNTRY_CODES) would make that country resolve and then be
    rejected — an About-panel-only channel silently discarded.
    """
    from search_zones import ALLOWED_COUNTRY_CODES, ALLOWED_COUNTRY_NAMES

    unmapped = {
        name: code for name, code in ALLOWED_COUNTRY_NAMES.items()
        if code not in ALLOWED_COUNTRY_CODES
    }
    assert unmapped == {}


def test_no_known_outside_name_maps_into_the_allowed_set():
    """The reverse mistake: a country listed as outside that resolves inside."""
    from search_zones import ALLOWED_COUNTRY_CODES, KNOWN_OUTSIDE_COUNTRY_NAMES

    leaked = {
        name: code for name, code in KNOWN_OUTSIDE_COUNTRY_NAMES.items()
        if code in ALLOWED_COUNTRY_CODES
    }
    assert leaked == {}


def test_the_two_name_tables_do_not_overlap():
    from search_zones import ALLOWED_COUNTRY_NAMES, KNOWN_OUTSIDE_COUNTRY_NAMES

    assert set(ALLOWED_COUNTRY_NAMES) & set(KNOWN_OUTSIDE_COUNTRY_NAMES) == set()


def test_names_in_both_tables_are_stored_normalised():
    """
    Lookup normalises the input, not the table, so an unnormalised key
    (leading space, capital letter) would be unreachable.
    """
    from search_zones import (
        ALLOWED_COUNTRY_NAMES, KNOWN_OUTSIDE_COUNTRY_NAMES, _normalize_name,
    )

    for table in (ALLOWED_COUNTRY_NAMES, KNOWN_OUTSIDE_COUNTRY_NAMES):
        for name in table:
            assert name == _normalize_name(name), f"{name!r} is not stored normalised"


# --- the content-language region subtag ----------------------------------
#
# The fallback for the ~15% of channels that set no snippet.country. Only
# the explicit REGION subtag counts — see the module docstring for the
# measurement that rejected mapping bare languages to countries.


@pytest.mark.parametrize(
    "tag,expected",
    [
        ("en-GB", "GB"),
        ("en-US", "US"),
        ("en-AU", "AU"),
        ("en-CA", "CA"),
        ("en-IN", "IN"),
        ("fr-FR", "FR"),
        ("pt-BR", "BR"),
        ("zh-Hant-TW", "TW"),   # script subtag sits between language and region
        ("en_GB", "GB"),        # underscore form, seen in some metadata
        ("EN-gb", "GB"),
    ],
)
def test_region_subtags_are_extracted(tag, expected):
    from search_zones import region_from_language_tag

    assert region_from_language_tag(tag) == expected


@pytest.mark.parametrize("tag", ["en", "hi", "ta", "es", "", None, "   "])
def test_a_bare_language_yields_no_region(tag):
    """
    A bare language is not a location. `ta` spans India, Sri Lanka and
    Singapore; `en`, `es` and `fr` straddle the zone boundary.
    """
    from search_zones import region_from_language_tag

    assert region_from_language_tag(tag) == ""


def test_numeric_un_m49_regions_are_ignored():
    """"en-419" is Latin America — several countries, not an alpha-2 code."""
    from search_zones import region_from_language_tag

    assert region_from_language_tag("en-419") == ""


def test_the_live_language_tags_get_the_right_verdicts():
    """
    Every distinct Content Language value across the two live tables. Only
    en-IN should be excluded; the rest are either inside the zones or carry
    no region at all.
    """
    from search_zones import region_from_language_tag, zone_verdict

    expected = {
        "en": None, "hi": None, "ta": None,          # no region subtag
        "en-US": True, "en-GB": True, "en-AU": True,
        "en-CA": True, "fr-FR": True,
        "en-IN": False,
    }
    for tag, want in expected.items():
        got = zone_verdict(region_from_language_tag(tag))
        assert got is want, f"{tag}: expected {want}, got {got}"


def test_country_code_passes_through_an_unrecognised_two_letter_code():
    """
    Resolving a country and judging its zone are separate questions — "IN"
    resolves fine, it just isn't allowed.
    """
    from search_zones import country_code, zone_verdict

    assert country_code("IN") == "IN"
    assert zone_verdict("IN") is False


# --------------------------------------------------------------------------
# description_location_outside_zone: catch a real non-zone location stated in
# the About even when snippet.country claims the US
# --------------------------------------------------------------------------

@pytest.mark.parametrize("description, expected", [
    ("Home theater reviews. Based in the Philippines 🇵🇭", "PH"),
    ("📍 India | AV gear on a budget", "IN"),
    ("Located in Pakistan, reviewing soundbars", "PK"),
    ("Location: Nigeria", "NG"),
    ("living in south korea, home cinema builds", "KR"),   # multi-word, longest-first
    ("based out of Brazil", "BR"),
])
def test_description_reveals_an_outside_location(description, expected):
    from search_zones import description_location_outside_zone

    assert description_location_outside_zone(description) == expected


@pytest.mark.parametrize("description", [
    "",
    "Home cinema and surround sound reviews",
    "Based in Los Angeles, USA",                 # in-zone city + country
    "based in Canada, eh",                       # in-zone country isn't matched
    "I review gear made in Japan and China",     # passing mention, no location cue
    "My parents are from India but I live in Chicago",  # 'from' is NOT a cue
    "Clips shot in Iceland last summer",         # 'shot in' isn't a cue (and IS is in-zone)
])
def test_description_does_not_trip_on_mentions_or_in_zone_locations(description):
    from search_zones import description_location_outside_zone

    assert description_location_outside_zone(description) == ""
