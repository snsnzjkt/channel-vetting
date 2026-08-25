"""
The 2026-08-20 criteria change: a REQUIRED and NOT-BLINDLY-TRUSTED channel
location, a narrowed search zone, and a broadcaster/TV-show gate.

Three instructions drove it:

  - "I noticed the AI is taking a lot of channels that don't have a specific
    location listed on YouTube ... around 90% of them are from Africa or
    Europe. Please don't include channels unless they have a specific location
    listed on YouTube."
  - "Europe is not in our search zone for lifestyle. Only UK USA CANADA and
    AUS."
  - "For some reason some TV channels are on the list please remove them."

Everything asserted here was calibrated against the FULL set of 144 rows
already tracked in the two niche tables (their real titles, About
descriptions and declared countries, pulled from channels.list). The channel
names in these tests are those live rows, not invented ones, and the
zero-false-positive claims are measurements over that corpus rather than
aspirations. If you widen any pattern, re-run that corpus — the failure mode
these gates guard against is not "misses a bad channel", it is "silently
starts discarding real prospects", and only the corpus can tell you which.

The single most important thing recorded here: **requiring a declared country
fixes far less than it appears to.** Only 8 of 144 rows declare nothing, while
NINE of the twelve genuinely out-of-zone rows declare an IN-ZONE country —
`Linet_ke` ("a girl from a village in Kenya Africa") declares US, `LIV KENYA`
declares GB. YouTube's country field is self-declared and unverified and
creators set it to their market. That is why three title/description signals
outrank it, and why deleting any of them re-opens the original complaint even
though the "specific location listed on YouTube" rule would still be enforced.
"""
import inspect

import pytest

from channel_vetting import pipeline
from channel_vetting.pipeline import (
    DROP_BROADCAST_TV,
    DROP_NO_DECLARED_COUNTRY,
    DROP_OUTSIDE_SEARCH_ZONE,
    REQUIRED_NICHE_KEYS,
    broadcast_tv_reason,
    location_drop_reason,
)
from channel_vetting.discovery.niches import (
    BROADCAST_TV_NAME_TERMS,
    BROADCAST_TV_PHRASE_TERMS,
    EXCLUDED_TOPIC_KEYWORDS,
    NICHES,
)
from channel_vetting.discovery.search_zones import (
    ALLOWED_COUNTRY_CODES,
    EUROPE_COUNTRY_CODES,
    ZONE_CORE,
    description_location_outside_zone,
    flag_country_outside_zone,
    title_country_outside_zone,
    zone_verdict,
)


class _NullBlocklist:
    def match(self, handle="", email="", name=""):
        return ""


# --- the zone itself ------------------------------------------------------

def test_the_core_zone_is_exactly_the_four_countries_asked_for():
    """
    "Only UK USA CANADA and AUS". Ireland stays excluded from every zone, as
    it always has been — GB covers Northern Ireland, IE is the Republic.
    """
    assert set(ZONE_CORE) == {"US", "CA", "GB", "AU"}
    assert "IE" not in ZONE_CORE


@pytest.mark.parametrize("code", ["DE", "FR", "NO", "CH", "UA", "IT", "ES", "PL"])
def test_european_countries_are_now_outside_the_zone(code):
    """
    The change itself. Each of these is still a country search_zones KNOWS
    (so the verdict is a confident False, not an unknown None) and is still
    inside the module's widest set — it is the NICHE's zone that narrowed,
    not the module's vocabulary.
    """
    assert zone_verdict(code, ZONE_CORE) is False
    assert zone_verdict(code, ALLOWED_COUNTRY_CODES) is True


def test_europe_is_still_defined_so_it_can_be_restored():
    """
    EUROPE_COUNTRY_CODES is wired to nothing today. It stays defined so that
    putting Europe back for one niche is `ZONE_CORE | EUROPE_COUNTRY_CODES`
    rather than forty codes retyped from memory — and so the deliberate
    omissions (IE, RU, BY, TR) survive with it.
    """
    assert "DE" in EUROPE_COUNTRY_CODES
    assert not EUROPE_COUNTRY_CODES & ZONE_CORE, "the two blocks must not overlap"
    for omitted in ("IE", "RU", "BY", "TR"):
        assert omitted not in EUROPE_COUNTRY_CODES


def test_both_niches_run_on_the_core_zone():
    for name, config in NICHES.items():
        assert config["allowed_country_codes"] == ZONE_CORE, name


def test_allowed_country_codes_is_a_required_niche_key():
    """
    Required rather than defaulted. `zone_verdict`'s default argument is
    ALLOWED_COUNTRY_CODES — the WIDEST zone, Europe included — so a niche that
    forgot this key would silently get back exactly what was just removed. A
    missing required key skips that niche with a logged error instead.
    """
    assert "allowed_country_codes" in REQUIRED_NICHE_KEYS


# --- signal 1: a flag emoji in the title ----------------------------------

def test_a_flag_emoji_in_the_title_places_the_channel():
    """
    `Daichi` + the Japanese flag, live in the Lifestyle table, declares
    `country: US` and posts Japanese travel content. A regional-indicator
    pair IS an ISO code, so this is exact rather than heuristic.
    """
    assert flag_country_outside_zone("Daichi\U0001F1EF\U0001F1F5", ZONE_CORE) == "JP"


def test_an_in_zone_flag_does_not_fire():
    """`Vroon & Britt TV` flies the Canadian flag. CA is in zone."""
    assert flag_country_outside_zone("Vroon & Britt \U0001F1E8\U0001F1E6", ZONE_CORE) == ""


def test_the_flag_signal_reads_the_title_and_not_the_description():
    """
    TITLE only, deliberately. A flag in the About text is usually a trip list
    on a travel channel — and both niches legitimately contain travel
    creators — so reading it there is a false positive waiting to happen.
    This test is the guard against someone "improving" the signal by feeding
    it the bio.
    """
    travel_bio = "Next up \U0001F1EF\U0001F1F5 \U0001F1F9\U0001F1ED \U0001F1EE\U0001F1F3 — subscribe!"
    _drop, _detail = location_drop_reason("Wander With Me", travel_bio, "US", ZONE_CORE)
    assert _drop is None, "a flag in the About text must not place the creator"


# --- signal 2: a country name in the title --------------------------------

@pytest.mark.parametrize("title,expected", [
    ("LIV KENYA", "KE"),            # live row, declares GB
    ("Inside Japan Living", "JP"),  # live row, declares US
])
def test_a_country_named_in_the_title_places_the_channel(title, expected):
    assert title_country_outside_zone(title) == expected


@pytest.mark.parametrize("title", [
    # The word-boundary match does real work here for free: the trailing \b
    # needs a non-word character, so the adjectival forms never match.
    "Japanese Joinery Woodworking",
    "Chinese Cooking At Home",
    # Ordinary in-niche titles from the live tables.
    "Home Theater Reviews",
    "Escape To The Country",
    "Apartment Therapy",
])
def test_the_title_signal_does_not_fire_on_ordinary_titles(title):
    assert title_country_outside_zone(title) == ""


# --- signal 3: the repaired description cue -------------------------------

@pytest.mark.parametrize("description,expected", [
    # THE BUG. "lives in"/"living in" were matched but plain "live in" was
    # not, so the most common first-person phrasing never fired. Both of
    # these are live rows that declare US.
    ("A channel about simple village life! My name is Olesya. I live in Belarus.", "BY"),
    ("We are Thai Girl Gift & Foreigner Joe. We live in Mueang Prachuap "
     "Khiri Khan Thailand where we built our dream home.", "TH"),
    # THE WINDOW. The old pattern allowed 30 characters AND no comma between
    # cue and country; both of these cross one or both limits.
    ("Welcome to my world. This is a girl from a village in Kenya Africa.", "KE"),
    ("sharing peaceful African homestead life from our dream home in rural Kenya, Africa.", "KE"),
    # Unchanged behaviour, kept so the repair cannot regress the original.
    ("Hi! I'm a creator based in the Philippines.", "PH"),
])
def test_the_description_cue_places_the_channel(description, expected):
    assert description_location_outside_zone(description) == expected


@pytest.mark.parametrize("description", [
    # A cue is still REQUIRED. Bare country names anywhere in the bio was the
    # rejected wider variant: measured over the same 144 rows it fires 11
    # times and is plainly wrong at least twice ("jordan" as a person's name).
    "We review gear from Japan and Korea every week.",
    "Shot in Iceland over three weeks.",
    "My parents are from India.",
    "Clips from India and Pakistan.",
    # Only OUTSIDE names are matched, so an in-zone location never fires.
    "Based in Canada, building furniture.",
    "I live in the United Kingdom.",
    "",
])
def test_the_description_cue_does_not_fire_on_mentions_or_in_zone_locations(description):
    assert description_location_outside_zone(description) == ""


def test_the_description_cue_is_not_inert():
    """
    The regression that motivated the repair, stated as an invariant rather
    than a case: before 2026-08-20 this gate fired on ZERO of the 144 tracked
    rows. It was shipped, documented, and did nothing. A cue set that cannot
    match the single most common phrasing a creator uses is not a gate.
    """
    assert description_location_outside_zone("I live in Kenya") == "KE"
    assert description_location_outside_zone("we live in Vietnam") == "VN"


# --- the combined gate, and its precedence --------------------------------

def test_a_stated_location_outranks_a_declared_country():
    """
    The finding that makes this whole change work. Nine of the twelve
    genuinely out-of-zone tracked rows declare US, GB or CA — the country
    field is self-declared, unverified, and set to the creator's MARKET. If
    the declared country won, requiring it would fix 8 rows and leave the
    reviewer's actual complaint untouched.
    """
    drop, detail = location_drop_reason(
        "Linet_ke", "This is a girl from a village in Kenya Africa.", "US", ZONE_CORE,
    )
    assert drop == DROP_OUTSIDE_SEARCH_ZONE
    assert "KE" in detail


def test_a_missing_country_and_an_out_of_zone_country_report_different_reasons():
    """
    Both discard; the reasons stay distinct on purpose. A run summary that
    cannot tell "we looked and they're in Kenya" from "they told us nothing"
    cannot tell a badly-targeted discovery query from a thin-metadata one, and
    those need opposite responses.
    """
    assert location_drop_reason("Chan", "", "", ZONE_CORE)[0] == DROP_NO_DECLARED_COUNTRY
    assert location_drop_reason("Chan", "", "Unknown", ZONE_CORE)[0] == DROP_NO_DECLARED_COUNTRY
    assert location_drop_reason("Chan", "", "DE", ZONE_CORE)[0] == DROP_OUTSIDE_SEARCH_ZONE


def test_an_in_zone_channel_with_a_clean_bio_survives():
    assert location_drop_reason(
        "Anna Home", "Home decor and interior styling.", "US", ZONE_CORE,
    ) == (None, "")


def test_the_zone_gate_runs_before_the_paid_performance_fetch(monkeypatch):
    """
    Pins the quota saving, which is the one thing about this refactor that is
    silent when it breaks. The gate's inputs are all free — title, About
    description and snippet.country, every one of them already on the
    channels.list response — so an out-of-zone candidate must be discarded
    WITHOUT spending the ~3 units of get_recent_video_performance (plus any
    long-form paging behind it).

    It used to sit after that fetch, because its language-region-subtag
    fallback needed content_language. That fallback is gone; if someone
    restores it, this test fails rather than the bill quietly going up.
    """
    calls = []

    monkeypatch.setattr(pipeline, "get_channel_stats", lambda cid: {
        "channel_id": "UC1", "channel_title": "Chan", "handle": "chan",
        "published_at": "", "subscriber_count": 10_000,
        "uploads_playlist_id": "PL1", "business_email": "",
        "video_count": 100, "country": "DE", "description": "",
    })

    def _boom(cid, pl):
        calls.append(cid)
        raise AssertionError("performance must not be fetched for an out-of-zone channel")

    monkeypatch.setattr(pipeline, "get_recent_video_performance", _boom)
    monkeypatch.setattr(pipeline.time, "sleep", lambda s: None)

    niche_config = {
        "min_avg_views": 10_000, "min_channel_age_months": None,
        "allowed_country_codes": ZONE_CORE,
    }
    record, reason = pipeline.process_candidate(
        {"channel_id": "UC1", "channel_title": "Chan", "matched_keywords": []},
        {}, _NullBlocklist(), niche_config, None,
    )

    assert record is None
    assert reason == DROP_OUTSIDE_SEARCH_ZONE
    assert calls == []


# --- broadcasters and TV shows --------------------------------------------

@pytest.mark.parametrize("title,description,expected", [
    # The three live rows that prompted the instruction, all Qualified in the
    # Lifestyle table before this gate existed.
    ("HGTV", "Welcome to the official HGTV YouTube channel! ... See the full "
             "HGTV television programming schedule now to watch all our shows",
     "broadcast_tv_name"),
    ("Entertainment Tonight", "Entertainment begins and ends with ET.",
     "broadcast_tv_name"),
    # Caught by a PHRASE, not by its name — which is the point of having the
    # phrase list at all. Show names are unbounded; "a British daytime
    # television property-buying programme" is how a show describes itself.
    ("Escape To The Country",
     "Escape to the Country is a British daytime television property-buying "
     "programme, first airing in 2002", "broadcast_tv_phrase"),
])
def test_broadcasters_and_tv_shows_are_dropped(title, description, expected):
    assert broadcast_tv_reason(title, description) == expected


@pytest.mark.parametrize("title,description", [
    # MEASURED FALSE POSITIVES, and the reason network names are matched
    # against the TITLE only. Both are genuine creator channels citing a
    # credit; a creator MENTIONS a network, a network IS one.
    ("Drew & Jonathan",
     "You probably know us from our HGTV shows like Property Brothers, "
     "Forever Home, Celebrity IOU, and Brother Vs. Brother"),
    ("Traveling with Kristin",
     "Kristin's work has been featured by the BBC, CNBC and Business Insider."),
    # Bare "television" was measured and rejected as a phrase: this is a live
    # film-and-TV fan-review channel and a legitimate creator.
    ("CritiX tv", "FILM * TELEVISION * COMIC CON * DISCUSSION"),
    # "official youtube channel of" was measured and dropped from the phrase
    # list — it caught this speaker manufacturer, which is a brand channel but
    # not a TV one, and this gate is scoped to TV.
    ("ADAM Audio",
     "The official YouTube channel of Berlin-based monitor manufacturer, ADAM Audio."),
    # A creator with "TV" in their own name is not a broadcaster.
    ("Vroon & Britt TV", "Canada Wide House Tours. You may have seen us on VroonTV."),
    ("Anna Home", "Home decor and interior styling."),
])
def test_ordinary_creators_survive_the_broadcast_gate(title, description):
    assert broadcast_tv_reason(title, description) is None


# --- networks past television (2026-08-25) ---------------------------------
#
# `Fox Sports Radio` (241K) reached the Home Theater table as Qualified: the
# name list carried "fox news" and "sky news" but neither network's sports arm,
# and it had no radio or masthead vocabulary at all. Every case below is a live
# row, and each is annotated with WHICH half of the gate has to catch it.

@pytest.mark.parametrize("title,description,expected", [
    # A network's sports arm. The pre-2026-08-25 list would have caught
    # "Fox News" and missed both of these.
    ("Fox Sports Radio",
     "Welcome to Fox Sports Radio's official YouTube channel. Fox Sports "
     "Radio brings you the latest sports news coverage 24/7!",
     "broadcast_tv_name"),
    ("Sky Sports Premier League",
     "Sky Sports Premier League is the home of Sky Sports' Premier League "
     "videos on YouTube featuring highlights from every game of the season!",
     "broadcast_tv_name"),
    # NAME half is useless here — the title is a person's name. Only the bio
    # gives it away, which is the whole reason the phrase list exists.
    ("The Herd with Colin Cowherd",
     "The Herd with Colin Cowherd and Jason McIntyre is a three-hour sports "
     "television and radio show on FS1 and iHeartRadio.",
     "broadcast_tv_phrase"),
    # A staffed newsroom with no broadcast brand in its title at all.
    ("DNVR Sports",
     "DNVR is a digital media company for die-hard Denver sports fans. We "
     "have credentialed reporters and analysts covering the Broncos.",
     "broadcast_tv_phrase"),
    # A masthead. This is the half of the 2026-08-20 "corporate channels stay
    # admitted" decision that 2026-08-25 deliberately reversed.
    ("The Verge",
     "Welcome to the YouTube channel for TheVerge.com, a team of journalists "
     "that examines how technology will change life in the future.",
     "broadcast_tv_name"),
    ("House Beautiful UK",
     "House Beautiful champions modern living and affordable style.",
     "broadcast_tv_name"),
])
def test_networks_radio_and_mastheads_are_dropped(title, description, expected):
    assert broadcast_tv_reason(title, description) == expected


@pytest.mark.parametrize("title,description", [
    # THE OTHER HALF OF THE 2026-08-25 DECISION, and the line the widening
    # must not cross: a manufacturer's brand channel is not a media outlet.
    # Both are live rows and both must still reach a human reviewer.
    ("Dolby", "Dolby Laboratories - experience entertainment in Dolby."),
    ("ADAM Audio",
     "The official YouTube channel of Berlin-based monitor manufacturer, ADAM Audio."),
    # PODCASTS ARE THE POINT OF THE PIPELINE and every one of these is a live
    # row. "radio show" is the one added phrase that could plausibly cost a
    # real prospect, so the podcasts in the pool are pinned here directly.
    ("The Big Podcast with Shaq",
     "The biggest podcast in the world is here. Watch new episodes every Friday!"),
    ("Club 520 Podcast", "Official YouTube Page for Club520 Podcast!! "
                         "Episodes Drop Weekly! Like Share & Subscribe"),
    ("Nightcap", "Come for the sports, stay for the stories. You've never "
                 "heard Shannon Sharpe and Chad Johnson like this."),
    ("The Joel Klatt Show: A College Football Podcast", ""),
    # MEASURED FALSE POSITIVES of terms considered and rejected on 2026-08-25.
    # Bare "magazine" caught this Approved row; "home of" caught this one.
    ("Penny Modern", "A magazine-style look at modern interiors."),
    ("Cozy DIY Home", "The home of cosy, affordable DIY projects."),
    # "sky sports" must not fire on an ordinary use of either word.
    ("Jsky", "Interviews and reviews."),
])
def test_creators_podcasters_and_manufacturers_survive_the_widened_gate(title, description):
    assert broadcast_tv_reason(title, description) is None


def test_both_halves_of_the_gate_still_fire():
    """
    The health check the function's own docstring asks for: it returns WHICH
    half fired because the name list is unbounded whack-a-mole and the phrase
    list is meant to generalise. A suite where only name hits appear means the
    phrase list has gone stale — which is exactly how radio was missed until
    2026-08-25.
    """
    assert broadcast_tv_reason("Sky Sports Cricket", "") == "broadcast_tv_name"
    assert broadcast_tv_reason(
        "Some Show", "A nationally syndicated talk radio programme.",
    ) == "broadcast_tv_phrase"


def test_network_names_are_matched_against_the_title_only():
    """
    The scope rule, stated directly rather than via a sample. Same network
    name, two positions: in the title it is the broadcaster, in the bio it is
    a credit. Collapsing the two arguments into one blob — the way
    excluded_topic_reason does — reintroduces both measured false positives.
    """
    assert broadcast_tv_reason("BBC Earth", "") == "broadcast_tv_name"
    assert broadcast_tv_reason("Kate Makes Things", "As seen on BBC Two") is None


def test_the_broadcast_terms_are_never_sent_to_the_discovery_vendor():
    """
    EXCLUDED_TOPIC_KEYWORDS goes to influencers.club as
    `keywords_not_in_description`, which is a BIO negation — exactly the scope
    proved unsafe above. Sending "bbc" would withhold `Traveling with Kristin`
    from discovery entirely. The forgone credit saving is trivial (0.01 per
    creator, on 3 of 144 rows); the pool damage would not have been.
    """
    vendor = {term.lower() for term in EXCLUDED_TOPIC_KEYWORDS}
    for term in BROADCAST_TV_NAME_TERMS + BROADCAST_TV_PHRASE_TERMS:
        assert term.lower() not in vendor, f"{term!r} must not reach the vendor's bio negation"


def test_the_broadcast_gate_runs_before_the_paid_performance_fetch(monkeypatch):
    """Same quota rule as the zone gate: free inputs, so discard before paying."""
    monkeypatch.setattr(pipeline, "get_channel_stats", lambda cid: {
        "channel_id": "UC1", "channel_title": "HGTV", "handle": "hgtv",
        "published_at": "", "subscriber_count": 1_070_000,
        "uploads_playlist_id": "PL1", "business_email": "",
        "video_count": 100, "country": "US",
        "description": "Welcome to the official HGTV YouTube channel!",
    })

    def _boom(cid, pl):
        raise AssertionError("performance must not be fetched for a broadcaster")

    monkeypatch.setattr(pipeline, "get_recent_video_performance", _boom)
    monkeypatch.setattr(pipeline.time, "sleep", lambda s: None)

    record, reason = pipeline.process_candidate(
        {"channel_id": "UC1", "channel_title": "HGTV", "matched_keywords": []},
        {}, _NullBlocklist(),
        {"min_avg_views": 10_000, "min_channel_age_months": None,
         "allowed_country_codes": ZONE_CORE},
        None,
    )

    assert record is None
    assert reason == DROP_BROADCAST_TV


def test_broadcast_tv_reason_takes_title_and_description_positionally():
    """
    Not `*texts`. The two arguments are matched against different patterns and
    are NOT interchangeable — a signature change to a varargs blob would
    silently collapse the scope distinction this whole gate rests on.
    """
    assert list(inspect.signature(broadcast_tv_reason).parameters) == [
        "channel_title", "description",
    ]
