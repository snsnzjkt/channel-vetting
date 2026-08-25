"""
Step 4 of the email chain reads the channel's external LINK LIST, not the
rendered About text.

Why: `enrichment.get_channel_stats()` already returns the full, untruncated
About description from `channels.list` and already runs
`extract_business_email()` over it (chain step 2), for a quota unit it was
spending anyway. Re-reading that same text in a browser cannot ever add an
address. Measured over every email-less row in the Home Theater table:
0/18 About descriptions contained an email.

What the Data API does NOT expose is the channel's link list. That lives in
`ytInitialData.aboutChannelViewModel.links`, with the real destination
buried in the `q=` parameter of YouTube's `/redirect` wrapper. Scraping
those destinations (and probing `/contact` on them) recovered an address for
3 of those same 18 channels.
"""
from urllib.parse import quote


def _redirect(destination):
    """A link URL shaped like YouTube's outbound redirect wrapper."""
    return (
        "https://www.youtube.com/redirect?event=channel_description"
        "&redir_token=QUNvcnJlY3Q%3D&q=" + quote(destination, safe="")
    )


def _about(links=(), description=""):
    """An aboutChannelViewModel with the real nesting depth."""
    return {
        "description": description,
        "links": [
            {
                "channelExternalLinkViewModel": {
                    "title": {"content": "Site"},
                    "link": {
                        "content": destination,
                        "commandRuns": [
                            {
                                "onTap": {
                                    "innertubeCommand": {
                                        "commandMetadata": {
                                            "webCommandMetadata": {
                                                "url": _redirect(destination)
                                            }
                                        }
                                    }
                                }
                            }
                        ],
                    },
                }
            }
            for destination in links
        ],
    }


def _page(text="", mailtos=()):
    """What the per-link evaluate() returns."""
    return {"text": text, "mailtos": list(mailtos)}


class _FakePage:
    """
    A tiny fake web: each URL substring maps to an evaluate() payload.

    Keys must be specific enough not to collide — the YouTube About page is
    keyed on "youtube.com" rather than "/about", since a Facebook page probe
    is also ".../about".
    """

    def __init__(self, site, fail_on=(), js_rendered=(), never_settles=()):
        self._site = site
        self._fail_on = tuple(fail_on)
        self._js_rendered = tuple(js_rendered)
        self._never_settles = tuple(never_settles)
        self._settled = False
        self.url = None
        self.visited = []
        self.waited = []

    def goto(self, url, **kwargs):
        self.url = url
        self.visited.append(url)
        self._settled = False
        if any(bad in url for bad in self._fail_on):
            raise RuntimeError("navigation failed")

    def wait_for_function(self, expression, timeout=None):
        """Stands in for waiting on a JS-rendered page to paint."""
        self.waited.append(self.url)
        if any(host in self.url for host in self._never_settles):
            raise RuntimeError("timeout waiting for body text")
        self._settled = True

    def evaluate(self, expression):
        for pattern, payload in self._site.items():
            if pattern in self.url:
                payload = dict(payload) if isinstance(payload, dict) else payload
                # A JS-rendered page has no body text until it settles.
                if (
                    isinstance(payload, dict)
                    and "text" in payload
                    and any(host in self.url for host in self._js_rendered)
                    and not self._settled
                ):
                    payload["text"] = ""
                    payload["mailtos"] = []
                return payload
        return None

    def close(self):
        pass


class _FakeBrowser:
    def __init__(self, site=None, fail=False, fail_on=(), js_rendered=(), never_settles=()):
        self._site = site or {}
        self._fail = fail
        self._fail_on = fail_on
        self._js_rendered = js_rendered
        self._never_settles = never_settles
        self.pages_created = 0
        self.closed = False
        self.last_page = None

    def new_page(self):
        self.pages_created += 1
        if self._fail:
            raise RuntimeError("browser exploded")
        self.last_page = _FakePage(
            self._site, fail_on=self._fail_on,
            js_rendered=self._js_rendered, never_settles=self._never_settles,
        )
        return self.last_page

    def close(self):
        self.closed = True


# --- unwrapping YouTube's redirect wrapper --------------------------------


def test_unwrap_redirect_returns_the_real_destination():
    from channel_vetting.enrichment.email_browser import unwrap_youtube_redirect

    wrapped = _redirect("https://andrewr.link/contactus")
    assert unwrap_youtube_redirect(wrapped) == "https://andrewr.link/contactus"


def test_unwrap_redirect_passes_through_a_plain_url():
    from channel_vetting.enrichment.email_browser import unwrap_youtube_redirect

    assert unwrap_youtube_redirect("https://theaterathome.com") == "https://theaterathome.com"


def test_extract_link_urls_digs_through_the_real_nesting():
    from channel_vetting.enrichment.email_browser import extract_link_urls

    view_model = _about(links=["https://iiwireviews.com/", "https://x.com/foo"])
    assert extract_link_urls(view_model) == [
        "https://iiwireviews.com/",
        "https://x.com/foo",
    ]


def test_extract_link_urls_dedupes_preserving_order():
    from channel_vetting.enrichment.email_browser import extract_link_urls

    view_model = _about(links=["https://a.com", "https://b.com", "https://a.com"])
    assert extract_link_urls(view_model) == ["https://a.com", "https://b.com"]


def test_extract_link_urls_tolerates_a_channel_with_no_links():
    from channel_vetting.enrichment.email_browser import extract_link_urls

    assert extract_link_urls(_about(links=[])) == []
    assert extract_link_urls({}) == []
    assert extract_link_urls(None) == []


# --- reusing the project's third-party domain screening -------------------


def test_candidate_links_drops_third_party_domains():
    """Social/platform links are never a creator's own contact page."""
    from channel_vetting.enrichment.email_browser import candidate_links

    urls = [
        "https://www.instagram.com/srba_iiwi/",
        "https://iiwireviews.com/",
        "https://www.patreon.com/iiwireviews",
        "https://www.facebook.com/hifibros/",
    ]
    assert candidate_links(urls) == ["https://iiwireviews.com/"]


def test_candidate_links_drops_url_shorteners():
    """The shortener's domain is what gets screened, not the destination."""
    from channel_vetting.enrichment.email_browser import candidate_links

    assert candidate_links(["https://bit.ly/m/ROBINSON-2025-FAVS"]) == []


def test_candidate_links_accepts_a_scheme_less_link():
    """aboutChannelViewModel link content is often bare, e.g. www.foo.com."""
    from channel_vetting.enrichment.email_browser import candidate_links

    assert candidate_links(["www.theaterathome.com"]) == ["https://www.theaterathome.com"]


# --- the /contact probe ---------------------------------------------------


def test_contact_probe_url_hangs_off_the_origin():
    from channel_vetting.enrichment.email_browser import contact_probe_url

    assert contact_probe_url("https://iiwireviews.com/some/page") == "https://iiwireviews.com/contact"


# --- find_email orchestration -------------------------------------------


def test_finds_email_on_the_linked_site():
    from channel_vetting.enrichment.email_browser import BrowserEmailScraper

    browser = _FakeBrowser({
        "youtube.com": _about(links=["https://www.theaterathome.com"]),
        "theaterathome.com": _page(text="Contact us at support@theaterathome.com"),
    })
    scraper = BrowserEmailScraper(browser=browser)
    assert scraper.find_email("UCFvcO") == "support@theaterathome.com"


def test_finds_email_in_a_mailto_href():
    """Plenty of sites only expose the address as a mailto link."""
    from channel_vetting.enrichment.email_browser import BrowserEmailScraper

    browser = _FakeBrowser({
        "youtube.com": _about(links=["https://hackshopgarage.com.au"]),
        "hackshopgarage": _page(text="Get in touch", mailtos=["hi@hackshopgarage.com.au"]),
    })
    scraper = BrowserEmailScraper(browser=browser)
    assert scraper.find_email("UCe6Gj") == "hi@hackshopgarage.com.au"


def test_probes_contact_when_the_landing_page_has_no_email():
    """2 of the 3 real hits needed this probe, not the landing page."""
    from channel_vetting.enrichment.email_browser import BrowserEmailScraper

    browser = _FakeBrowser({
        "youtube.com": _about(links=["https://iiwireviews.com/"]),
        "iiwireviews.com/contact": _page(text="mail: iiwireviewsmail@gmail.com"),
        "iiwireviews.com/": _page(text="Reviews of hi-fi gear"),
    })
    scraper = BrowserEmailScraper(browser=browser)
    assert scraper.find_email("UCVtYo") == "iiwireviewsmail@gmail.com"


def test_does_not_probe_a_link_that_is_already_a_contact_page():
    """
    Soft 404s are common (avnirvana.com serves byte-identical pages for
    /contact, /contact-us and /about), so a wasted probe is the normal case
    rather than the exception. When the link already points at the contact
    path there is nothing to gain from fetching it twice.
    """
    from channel_vetting.enrichment.email_browser import BrowserEmailScraper

    browser = _FakeBrowser({
        "youtube.com": _about(links=["https://andrewr.link/contact"]),
        "andrewr.link/contact": _page(text="use the form below"),
    })
    scraper = BrowserEmailScraper(browser=browser)
    assert scraper.find_email("UCBABC") == ""
    assert browser.last_page.visited.count("https://andrewr.link/contact") == 1


def test_gmail_addresses_are_kept():
    """Freemail must never be screened out — it is the commonest case."""
    from channel_vetting.enrichment.email_browser import BrowserEmailScraper

    browser = _FakeBrowser({
        "youtube.com": _about(links=["https://iiwireviews.com/"]),
        "iiwireviews.com/": _page(text="mail iiwireviewsmail@gmail.com"),
    })
    scraper = BrowserEmailScraper(browser=browser)
    assert scraper.find_email("UCVtYo") == "iiwireviewsmail@gmail.com"


def test_third_party_addresses_on_the_linked_page_are_rejected():
    """A tip-jar address on a creator's own site is still not their email."""
    from channel_vetting.enrichment.email_browser import BrowserEmailScraper

    browser = _FakeBrowser({
        "youtube.com": _about(links=["https://amazingworldbiketour.com"]),
        "amazingworldbiketour.com": _page(text="Tip me: donate@buymeacoffee.com"),
    })
    scraper = BrowserEmailScraper(browser=browser)
    assert scraper.find_email("UCEfriu") == ""


def test_returns_empty_when_the_channel_has_no_links():
    from channel_vetting.enrichment.email_browser import BrowserEmailScraper

    browser = _FakeBrowser({"youtube.com": _about(links=[])})
    scraper = BrowserEmailScraper(browser=browser)
    assert scraper.find_email("UCaSf_") == ""


# --- find_contact: the link-list-presence signal for the no-social drop ---
#
# find_email is just find_contact()[0]; these pin the second element, which
# pipeline.process_candidate turns into DROP_NO_SOCIAL. The rule is: only a
# POSITIVELY-empty list is False; anything unread is None (never disqualify
# on absent data).


def test_find_contact_reports_false_for_an_empty_link_list():
    """The one case that drives the no-social drop: links read, none present."""
    from channel_vetting.enrichment.email_browser import BrowserEmailScraper

    browser = _FakeBrowser({"youtube.com": _about(links=[])})
    scraper = BrowserEmailScraper(browser=browser)
    assert scraper.find_contact("UCaSf_") == ("", False)


def test_find_contact_reports_true_when_a_social_only_channel_has_links():
    """
    A channel with ONLY a social link (no own-domain site, no email) still
    HAS a social media page — presence is True, so it must NOT be dropped,
    even though candidate_links drops the social link for the email scan.
    """
    from channel_vetting.enrichment.email_browser import BrowserEmailScraper

    browser = _FakeBrowser({"youtube.com": _about(links=["https://www.instagram.com/creator/"])})
    scraper = BrowserEmailScraper(browser=browser)
    assert scraper.find_contact("UC1") == ("", True)


def test_find_contact_reports_true_alongside_a_found_email():
    from channel_vetting.enrichment.email_browser import BrowserEmailScraper

    browser = _FakeBrowser({
        "youtube.com": _about(links=["https://www.theaterathome.com"]),
        "theaterathome.com": _page(text="Contact us at support@theaterathome.com"),
    })
    scraper = BrowserEmailScraper(browser=browser)
    assert scraper.find_contact("UCFvcO") == ("support@theaterathome.com", True)


def test_find_contact_reports_none_when_the_about_panel_never_loads():
    """
    No aboutChannelViewModel at all (evaluate returns None) is UNKNOWN, not
    empty — presence is None so the channel is kept, not dropped.
    """
    from channel_vetting.enrichment.email_browser import BrowserEmailScraper

    browser = _FakeBrowser({})  # nothing keyed on youtube.com -> evaluate returns None
    scraper = BrowserEmailScraper(browser=browser)
    assert scraper.find_contact("UC1") == ("", None)


def test_null_scraper_find_contact_is_none():
    """An inert scraper reports None presence, keeping the no-social drop off."""
    from channel_vetting.enrichment.email_browser import null_scraper

    assert null_scraper().find_contact("UC123") == ("", None)


def test_does_not_rescan_the_about_description():
    """
    Chain step 2 already scanned this text via channels.list. Re-finding it
    here would double-count a step-2 hit as a step-4 hit and misreport which
    step actually moved email coverage.
    """
    from channel_vetting.enrichment.email_browser import BrowserEmailScraper

    browser = _FakeBrowser({
        "youtube.com": _about(links=[], description="Business: already@found.com"),
    })
    scraper = BrowserEmailScraper(browser=browser)
    assert scraper.find_email("UC123") == ""


def test_only_the_first_working_link_is_needed():
    """Stop at the first address rather than visiting every link."""
    from channel_vetting.enrichment.email_browser import BrowserEmailScraper

    browser = _FakeBrowser({
        "youtube.com": _about(links=["https://first.com", "https://second.com"]),
        "first.com": _page(text="hello@first.com"),
        "second.com": _page(text="hello@second.com"),
    })
    scraper = BrowserEmailScraper(browser=browser)
    assert scraper.find_email("UC1") == "hello@first.com"
    assert not any("second.com" in url for url in browser.last_page.visited)


# --- the Facebook /about probe -------------------------------------------
#
# Measured: Facebook's PAGE ROOT is login-walled and yields nothing, but
# `/about` leaks the page's contact email through the wall on 2 of the 6
# Facebook links in the sample. One of those two (AV NIRVANA,
# admin@avnirvana.com) is a channel no other path reaches. Instagram (0/8),
# X, and Patreon stay excluded — all measured, all walled or empty.


def test_social_about_urls_builds_the_facebook_about_path():
    from channel_vetting.enrichment.email_browser import social_about_urls

    assert social_about_urls(["https://www.facebook.com/avnirvana"]) == [
        "https://www.facebook.com/avnirvana/about"
    ]


def test_social_about_urls_normalises_a_trailing_slash():
    from channel_vetting.enrichment.email_browser import social_about_urls

    assert social_about_urls(["https://www.facebook.com/hifibros/"]) == [
        "https://www.facebook.com/hifibros/about"
    ]


def test_social_about_urls_ignores_platforms_measured_as_walled():
    """Instagram was 0/8, X and Patreon empty. Don't pay for those loads."""
    from channel_vetting.enrichment.email_browser import social_about_urls

    assert social_about_urls([
        "https://www.instagram.com/srba_iiwi/",
        "https://x.com/AV_NIRVANA",
        "https://www.patreon.com/iiwireviews",
    ]) == []


def test_social_about_urls_skips_a_numeric_profile_url():
    """facebook.com/profile.php?id=... has no /about sibling to guess."""
    from channel_vetting.enrichment.email_browser import social_about_urls

    assert social_about_urls(["https://www.facebook.com/profile.php?id=100081"]) == []


def test_social_about_urls_does_not_double_append_about():
    from channel_vetting.enrichment.email_browser import social_about_urls

    assert social_about_urls(["https://www.facebook.com/avnirvana/about"]) == [
        "https://www.facebook.com/avnirvana/about"
    ]


def test_facebook_about_is_used_when_no_website_yields_an_email():
    """AV NIRVANA's real case: site has no address, the FB page does."""
    from channel_vetting.enrichment.email_browser import BrowserEmailScraper

    browser = _FakeBrowser({
        "youtube.com": _about(links=[
            "www.avnirvana.com",
            "https://www.facebook.com/avnirvana",
            "https://x.com/AV_NIRVANA",
        ]),
        "facebook.com/avnirvana/about": _page(text="Email admin@avnirvana.com"),
        "avnirvana.com": _page(text="AV NIRVANA forum home"),
    })
    scraper = BrowserEmailScraper(browser=browser)
    assert scraper.find_email("UCr6mZ") == "admin@avnirvana.com"


def test_the_creators_own_site_wins_over_facebook():
    """Their own domain is the better signal when both have an address."""
    from channel_vetting.enrichment.email_browser import BrowserEmailScraper

    browser = _FakeBrowser({
        "youtube.com": _about(links=[
            "https://theaterathome.com",
            "https://www.facebook.com/theaterathome",
        ]),
        "theaterathome.com": _page(text="support@theaterathome.com"),
        "facebook.com/theaterathome/about": _page(text="fb@theaterathome.com"),
    })
    scraper = BrowserEmailScraper(browser=browser)
    assert scraper.find_email("UCFvcO") == "support@theaterathome.com"


def test_facebook_is_not_probed_when_a_website_already_answered():
    from channel_vetting.enrichment.email_browser import BrowserEmailScraper

    browser = _FakeBrowser({
        "youtube.com": _about(links=[
            "https://theaterathome.com",
            "https://www.facebook.com/theaterathome",
        ]),
        "theaterathome.com": _page(text="support@theaterathome.com"),
        "facebook.com": _page(text="fb@theaterathome.com"),
    })
    scraper = BrowserEmailScraper(browser=browser)
    scraper.find_email("UCFvcO")
    assert not any("facebook.com" in url for url in browser.last_page.visited)


# --- failure modes must stay soft ---------------------------------------


def test_browser_failure_is_soft():
    """A browser error must never break the pipeline."""
    from channel_vetting.enrichment.email_browser import BrowserEmailScraper

    scraper = BrowserEmailScraper(browser=_FakeBrowser(fail=True))
    assert scraper.find_email("UC123") == ""


def test_a_dead_link_does_not_stop_the_other_links():
    from channel_vetting.enrichment.email_browser import BrowserEmailScraper

    browser = _FakeBrowser(
        {
            "youtube.com": _about(links=["https://dead.example", "https://alive.com"]),
            "alive.com": _page(text="reach me at hi@alive.com"),
        },
        fail_on=("dead.example",),
    )
    scraper = BrowserEmailScraper(browser=browser)
    assert scraper.find_email("UC1") == "hi@alive.com"


def test_missing_about_view_model_is_soft():
    """If ytInitialData has no aboutChannelViewModel, return nothing."""
    from channel_vetting.enrichment.email_browser import BrowserEmailScraper

    browser = _FakeBrowser({})  # evaluate() returns None for every URL
    scraper = BrowserEmailScraper(browser=browser)
    assert scraper.find_email("UC123") == ""


def test_one_session_serves_many_channels():
    """Regression: the backfill launched a browser per channel."""
    from channel_vetting.enrichment.email_browser import BrowserEmailScraper

    browser = _FakeBrowser({"youtube.com": _about(links=[])})
    scraper = BrowserEmailScraper(browser=browser)
    for i in range(5):
        scraper.find_email(f"UC{i}")

    assert browser.pages_created == 5  # five pages...
    assert not browser.closed          # ...but the browser stayed open


def test_null_scraper_is_inert():
    from channel_vetting.enrichment.email_browser import null_scraper

    assert null_scraper().find_email("UC123") == ""


def test_the_scraper_does_not_do_country_lookups():
    """
    `aboutChannelViewModel.country` is real, but it is the SAME channel
    setting `channels.list` returns in snippet.country — the panel just
    renders it. All 5 live channels with an empty API country had no
    `country` key in the About payload either, so a lookup here recovers 0
    and costs a page load per candidate. `discovery/search_zones.py` uses the
    content-language region subtag instead. Pinned so it doesn't come back
    on the assumption that a browser must see more than the API.
    """
    from channel_vetting.enrichment.email_browser import BrowserEmailScraper

    assert not hasattr(BrowserEmailScraper, "find_country")


# --- chain integration ---------------------------------------------------


def test_resolve_email_prefers_free_steps_over_browser():
    """
    A free step's address wins, and the browser never FOLLOWS a link once one
    is in hand — but it does still read the About page for the link list, which
    is what the no-social drop runs on. One page load, no link navigations.
    """
    from channel_vetting import pipeline
    from channel_vetting.enrichment.email_browser import BrowserEmailScraper

    browser = _FakeBrowser({
        "youtube.com": _about(links=["https://site.com"]),
        "site.com": _page(text="browser@found.com"),
    })
    scraper = BrowserEmailScraper(browser=browser)

    stats = {"business_email": "about@page.com", "channel_id": "UC1"}
    performance = {"repeated_email": ""}
    email, source, has_links = pipeline.resolve_email_with_source(stats, performance, scraper)

    assert email == "about@page.com"
    assert source == pipeline.EMAIL_SOURCE_ABOUT, "the browser must not be credited"
    # The link list was read, so the no-social signal is KNOWN rather than None
    # — the whole point of the change. site.com was never visited.
    assert has_links is True
    assert browser.pages_created == 1


def test_the_link_list_is_read_even_when_an_earlier_step_found_the_address():
    """
    The defect this pins: a channel with NO external links whose address sits
    in its video descriptions was written as a prospect, because step 1
    short-circuited before the link list was ever fetched. Measured live on
    "Timber Time" (171k subs, empty link list) on 2026-08-15.
    """
    from channel_vetting import pipeline
    from channel_vetting.enrichment.email_browser import BrowserEmailScraper

    browser = _FakeBrowser({"youtube.com": _about(links=[])})
    scraper = BrowserEmailScraper(browser=browser)

    stats = {"business_email": "", "channel_id": "UC1"}
    performance = {"repeated_email": "indescription@creator.com"}
    email, source, has_links = pipeline.resolve_email_with_source(stats, performance, scraper)

    assert email == "indescription@creator.com"
    assert source == pipeline.EMAIL_SOURCE_REPEATED
    assert has_links is False, "an empty link list must be reported, not left unknown"


def test_resolve_email_falls_through_to_browser():
    from channel_vetting import pipeline
    from channel_vetting.enrichment.email_browser import BrowserEmailScraper

    browser = _FakeBrowser({
        "youtube.com": _about(links=["https://site.com"]),
        "site.com": _page(text="browser@found.com"),
    })
    scraper = BrowserEmailScraper(browser=browser)

    stats = {"business_email": "", "channel_id": "UC1"}
    performance = {"repeated_email": ""}
    assert pipeline.resolve_email(stats, performance, scraper) == "browser@found.com"


# --- JS-rendered pages ----------------------------------------------------
#
# Regression: Facebook paints its page with JS, so document.body.innerText is
# EMPTY at domcontentloaded. The first live run of the Facebook probe returned
# nothing for AV NIRVANA for exactly this reason, while three server-rendered
# creator sites passed and hid the bug. Every page fetch now waits for body
# text before reading it.


def test_waits_for_a_js_rendered_page_before_reading_it():
    from channel_vetting.enrichment.email_browser import BrowserEmailScraper

    browser = _FakeBrowser(
        {
            "youtube.com": _about(links=["https://www.facebook.com/avnirvana"]),
            "facebook.com/avnirvana/about": _page(text="Email admin@avnirvana.com"),
        },
        js_rendered=("facebook.com",),
    )
    scraper = BrowserEmailScraper(browser=browser)
    assert scraper.find_email("UCr6mZ") == "admin@avnirvana.com"
    assert any("facebook.com" in url for url in browser.last_page.waited)


def test_a_page_that_never_paints_is_not_fatal():
    """A settle timeout must fall through, not lose the remaining links."""
    from channel_vetting.enrichment.email_browser import BrowserEmailScraper

    browser = _FakeBrowser(
        {
            "youtube.com": _about(links=["https://slow.example", "https://fast.com"]),
            "slow.example": _page(text="hidden@slow.example"),
            "fast.com": _page(text="hi@fast.com"),
        },
        js_rendered=("slow.example",),
        never_settles=("slow.example",),
    )
    scraper = BrowserEmailScraper(browser=browser)
    assert scraper.find_email("UC1") == "hi@fast.com"
