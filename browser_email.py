"""
Step 5 of the email chain: follows a YouTube channel's public external
LINK LIST looking for a contact address on the creator's own site.

Do NOT add a country lookup here. `aboutChannelViewModel.country` exists
and does return a name ("United States"), but it is the SAME channel
setting `channels.list` returns in `snippet.country` — the About panel
just renders it. Measured over the 5 channels in the live tables whose
API country was empty, all 5 had no `country` key in the About payload
either, so the lookup recovered 0 and cost a page load per candidate. The
control in that measurement (Theater At Home, API country `US`) did return
"United States", which is how we know the key name was right and the
result is real. `search_zones.py` falls back to the content-language
region subtag instead, which is free.

Why the link list and not the About text: `channels.list` already returns
the channel's full, untruncated About description, and
`enrichment.get_channel_stats()` already runs `extract_business_email()`
over it (chain step 2) for a quota unit it was spending anyway. An earlier
version of this module loaded the About page in a browser and read
`body.inner_text()`, which could only ever re-find text step 2 already had.
Measured over every email-less row in the Home Theater table at the time
this was rewritten, 0 of 18 About descriptions contained an address, so that
step could not fire.

What the Data API does not expose is the channel's link list. It lives in
`ytInitialData.aboutChannelViewModel.links`, present in the page even though
`ytd-about-channel-renderer` never renders at the `/about` URL, with the
real destination in the `q=` parameter of YouTube's `/redirect` wrapper.
Following those links (and probing `/contact` on them) recovered an address
for 3 of those same 18 channels. Two of the three needed the `/contact`
probe, so it is the probe rather than the landing page that carries most of
the value.

This still only reads pages that are already public. It does not touch
YouTube's gated "View email address" button or the CAPTCHA in front of it.

One browser session serves the whole run, and one page serves each channel.
Every failure is soft: the chain continues without a browser-sourced email
rather than breaking the run.
"""
import logging
from urllib.parse import parse_qs, urlparse, quote

from enrichment import THIRD_PARTY_DOMAINS, extract_business_email

logger = logging.getLogger(__name__)

NAV_TIMEOUT_MS = 30000
EVAL_TIMEOUT_MS = 5000
# How long to wait for a JS-rendered page to paint something readable.
# Short on purpose: this runs per link per candidate, and a page with no
# text after 8s is not going to yield an address.
SETTLE_TIMEOUT_MS = 8000

BODY_HAS_TEXT_JS = "() => !!document.body && document.body.innerText.trim().length > 0"

# The one path worth guessing. Creator sites overwhelmingly use /contact,
# and every extra guess is another page load per candidate channel for a
# steeply falling return.
CONTACT_PATH = "/contact"

# Social platforms whose /about page leaks a contact address through the
# login wall. Facebook only, and measured rather than assumed: across the
# 6 Facebook links in the sample, the page ROOT yielded 0 while `/about`
# yielded 2, one of which (AV NIRVANA) no other path reaches.
#
# Instagram is deliberately absent: 8/8 of its links login-walled to a
# ~900-char interstitial with no address anywhere in the payload. X and
# Patreon likewise came up empty. Making those work would mean scraping
# logged-in with imported cookies, which is an access-control decision of
# the same kind as YouTube's CAPTCHA gate above — raise it, don't just do
# it.
SOCIAL_ABOUT_HOSTS = {"facebook.com"}

# Returns the channel's link list only. The About description is
# deliberately NOT returned: chain step 2 already scanned it via
# channels.list, and re-finding it here would misreport a step-2 hit as a
# step-4 one (see main.EMAIL_SOURCE_* and backfill_missing_emails.py).
# Nor is `country` — see the module docstring for why that measured out.
ABOUT_LINKS_JS = """() => {
  let vm = null;
  const walk = (node, depth) => {
    if (!node || vm || depth > 16 || typeof node !== 'object') return;
    if (node.aboutChannelViewModel) { vm = node.aboutChannelViewModel; return; }
    for (const key in node) walk(node[key], depth + 1);
  };
  walk(window.ytInitialData, 0);
  return vm ? { links: vm.links || [] } : null;
}"""

PAGE_CONTENT_JS = """() => ({
  text: document.body ? document.body.innerText : '',
  mailtos: Array.from(document.querySelectorAll('a[href^="mailto:"]'))
    .map((a) => (a.getAttribute('href') || '').replace(/^mailto:/i, '').split('?')[0])
    .filter(Boolean),
})"""


def _with_scheme(url: str) -> str:
    """aboutChannelViewModel links are often bare, e.g. "www.foo.com"."""
    return url if "//" in url else f"https://{url}"


def unwrap_youtube_redirect(url: str) -> str:
    """
    Return the real destination behind a youtube.com/redirect?...&q=<url>
    wrapper, or the URL unchanged when it isn't wrapped.
    """
    if not url:
        return ""
    parsed = urlparse(url)
    if (parsed.hostname or "").endswith("youtube.com") and parsed.path == "/redirect":
        destination = parse_qs(parsed.query).get("q", [])
        if destination:
            return destination[0]
    return url


def extract_link_urls(about_view_model) -> list[str]:
    """
    Pull the outbound link destinations out of an aboutChannelViewModel,
    in order, deduped. Returns [] for a channel with no links.

    The wrapper URL sits several levels down (links[] ->
    channelExternalLinkViewModel -> link -> commandRuns[] -> onTap ->
    innertubeCommand -> commandMetadata -> webCommandMetadata -> url), and
    that nesting is YouTube's to change, so this walks for any `url` that
    looks like a redirect rather than pinning the exact path.
    """
    if not about_view_model:
        return []

    found: list[str] = []

    def walk(node, depth: int = 0) -> None:
        if depth > 12:
            return
        if isinstance(node, dict):
            raw = node.get("url")
            if isinstance(raw, str) and "/redirect" in raw:
                destination = unwrap_youtube_redirect(raw)
                if destination:
                    found.append(destination)
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                walk(value, depth + 1)

    walk(about_view_model.get("links") or [])

    seen = set()
    ordered = []
    for url in found:
        if url not in seen:
            seen.add(url)
            ordered.append(url)
    return ordered


def link_host(url: str) -> str:
    """Registrable-ish host for screening, with any leading www. dropped."""
    if not url:
        return ""
    hostname = urlparse(_with_scheme(url)).hostname or ""
    return hostname.lower().removeprefix("www.")


def candidate_links(urls) -> list[str]:
    """
    Keep only links that could plausibly be the creator's own site.

    Screened with `enrichment.THIRD_PARTY_DOMAINS`, the same set the email
    extractor uses, so a social profile, a tip jar, a Linktree or a URL
    shortener is dropped here rather than fetched. Shorteners have to go:
    the shortener's own domain is all that can be screened, since the
    destination isn't known until it's followed.
    """
    seen = set()
    kept = []
    for url in urls or []:
        if not url:
            continue
        host = link_host(url)
        if not host or host in THIRD_PARTY_DOMAINS:
            continue
        normalized = _with_scheme(url)
        if normalized in seen:
            continue
        seen.add(normalized)
        kept.append(normalized)
    return kept


def contact_probe_url(url: str) -> str:
    """The /contact page on the same origin as `url`."""
    parsed = urlparse(_with_scheme(url))
    return f"{parsed.scheme}://{parsed.netloc}{CONTACT_PATH}"


def social_about_urls(urls) -> list[str]:
    """
    `/about` URLs for the social links worth probing (see
    SOCIAL_ABOUT_HOSTS). These are the links `candidate_links()`
    deliberately drops, so this reads the FULL link list, not the filtered
    one.

    Numeric profile URLs (facebook.com/profile.php?id=...) are skipped:
    there is no `/about` sibling to guess for them.
    """
    probes = []
    seen = set()
    for url in urls or []:
        if not url or link_host(url) not in SOCIAL_ABOUT_HOSTS:
            continue
        parsed = urlparse(_with_scheme(url))
        if parsed.query:
            continue
        path = parsed.path.rstrip("/")
        if not path:
            continue
        if not path.endswith("/about"):
            path = f"{path}/about"
        probe = f"{parsed.scheme}://{parsed.netloc}{path}"
        if probe not in seen:
            seen.add(probe)
            probes.append(probe)
    return probes


class BrowserEmailScraper:
    """
    Wraps a Playwright browser/context. Construct with `enabled=False` (or
    via null_scraper()) to get an inert scraper that always returns "".

    Usable as a context manager so the browser is closed even if the run
    raises.
    """

    def __init__(self, browser=None, context=None, playwright=None, enabled: bool = True):
        self._browser = browser
        self._context = context
        self._playwright = playwright
        self._enabled = enabled and (browser is not None or context is not None)

    @property
    def enabled(self) -> bool:
        """True if this scraper holds a live browser session."""
        return self._enabled

    @classmethod
    def launch(cls, headless: bool = True) -> "BrowserEmailScraper":
        """Start a Playwright session with stealth applied, or return an
        inert scraper if it can't be started."""
        try:
            from playwright.sync_api import sync_playwright
            from playwright_stealth import Stealth
        except ImportError:
            logger.warning("Playwright or playwright-stealth is not installed — browser email step disabled.")
            return cls(enabled=False)

        playwright = None
        browser = None
        context = None
        try:
            playwright = sync_playwright().start()
            browser = playwright.chromium.launch(headless=headless)
            context = browser.new_context()
            Stealth().apply_stealth_sync(context)
            return cls(browser=browser, context=context, playwright=playwright)
        except Exception as exc:
            logger.warning("Playwright failed to launch (%s) — browser email step disabled.", exc)
            for resource in (context, browser):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        pass
            if playwright is not None:
                try:
                    playwright.stop()
                except Exception:
                    pass
            return cls(enabled=False)

    def find_contact(self, channel_id: str) -> tuple[str, bool | None]:
        """
        Follow the channel's public link list ONCE, returning
        (email, has_external_links):

          - email: an address found on the creator's own site or a probed
            social /about page, or "".
          - has_external_links: True when the channel's About panel declares
            at least one outbound link (a website OR a social profile), False
            when it declares none, and None when the list couldn't be read at
            all (this scraper is inert, the page never opened, or the About
            view-model was absent).

        The flag is what lets the pipeline drop a channel with no web/social
        presence (main.DROP_NO_SOCIAL) WITHOUT a second page load — it falls
        out of the same fetch the email step already makes. None (not False)
        on an unreadable list is deliberate: "no links" is only reported when
        an empty list was actually SEEN, so the "absent data never
        disqualifies" rule the rest of the pipeline follows still holds. One
        page serves the whole channel.
        """
        if not self._enabled:
            return "", None

        page, opened = self._open_page(channel_id)
        if page is None:
            return "", None

        try:
            about = self._read_about_links(page, channel_id)
            if about is None:
                # The About view-model never loaded — unknown, not empty.
                return "", None
            # vm.links is the channel's declared link list; its emptiness is
            # the "no external presence" signal, independent of whether any
            # link below turns out to be email-scannable.
            has_links = bool(about.get("links"))
            links = extract_link_urls(about)
            if not links:
                return "", has_links

            # The creator's own domain first: it's the better signal, and
            # it's where 3 of the 4 measured hits came from.
            for link in candidate_links(links):
                email = self._email_from_link(page, link)
                if email:
                    return email, True

            # Then the social /about pages, which only get loaded for
            # channels every earlier step missed.
            for probe in social_about_urls(links):
                try:
                    email = self._email_from_page(page, probe)
                except Exception as exc:
                    logger.info("Could not read %s: %s", probe, exc)
                    continue
                if email:
                    return email, True
            return "", True
        except Exception as exc:
            logger.info("Playwright email lookup failed for %s: %s", channel_id, exc)
            return "", None
        finally:
            if opened:
                self._close_page(page)

    def find_email(self, channel_id: str) -> str:
        """
        Contact email found by following the channel's public link list, or
        "". Thin wrapper over find_contact() for callers that only need the
        address; the email chain itself calls find_contact() directly (it
        also needs the link-presence flag).
        """
        return self.find_contact(channel_id)[0]

    def _open_page(self, channel_id: str):
        """(page, opened) — (None, False) when a page can't be opened."""
        page_source = self._context or self._browser
        try:
            return page_source.new_page(), True
        except Exception as exc:
            logger.info("Playwright could not open a page for %s: %s", channel_id, exc)
            return None, False

    @staticmethod
    def _close_page(page) -> None:
        try:
            page.close()
        except Exception:
            pass

    def _read_about_links(self, page, channel_id: str):
        """The channel's aboutChannelViewModel links, or None."""
        about_url = f"https://www.youtube.com/channel/{quote(channel_id)}/about"
        try:
            page.goto(about_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            return page.evaluate(ABOUT_LINKS_JS)
        except Exception as exc:
            logger.info("Could not read the link list for %s: %s", channel_id, exc)
            return None

    def _email_from_link(self, page, link: str) -> str:
        """
        Scan a linked page, then its /contact sibling. A dead link yields ""
        rather than stopping the remaining links.
        """
        try:
            email = self._email_from_page(page, link)
        except Exception as exc:
            logger.info("Could not read %s: %s", link, exc)
            return ""
        if email:
            return email

        probe = contact_probe_url(link)
        if probe == link:
            return ""
        try:
            return self._email_from_page(page, probe)
        except Exception as exc:
            logger.info("Could not read %s: %s", probe, exc)
            return ""

    def _email_from_page(self, page, url: str) -> str:
        """
        Visible text plus mailto: hrefs on one page, screened by
        `extract_business_email()` so the shared domain blocklist applies.
        mailto goes first: an explicit mailto is a stronger signal than an
        address that merely appears in body copy.

        Waits for the body to actually have text before reading it.
        `domcontentloaded` fires before a JS-rendered page paints, and
        Facebook is exactly that: the first live run of the Facebook probe
        read an empty body and returned nothing, while three
        server-rendered creator sites passed and hid the bug.
        """
        page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        try:
            page.wait_for_function(BODY_HAS_TEXT_JS, timeout=SETTLE_TIMEOUT_MS)
        except Exception as exc:
            # A page that never paints still gets read for whatever it has —
            # falling through is strictly better than losing the candidate.
            logger.info("Body text never appeared on %s: %s", url, exc)
        content = page.evaluate(PAGE_CONTENT_JS) or {}
        mailtos = content.get("mailtos") or []
        text = content.get("text") or ""
        return extract_business_email("\n".join([*mailtos, text]))

    def close(self) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except Exception as exc:
                logger.info("Playwright context close failed: %s", exc)
            self._context = None

        if self._browser is not None:
            try:
                self._browser.close()
            except Exception as exc:
                logger.info("Playwright browser close failed: %s", exc)
            self._browser = None

        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception as exc:
                logger.info("Playwright shutdown failed: %s", exc)
            self._playwright = None

        self._enabled = False

    def __enter__(self) -> "BrowserEmailScraper":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def null_scraper() -> BrowserEmailScraper:
    """An inert scraper, for runs with the browser step turned off."""
    return BrowserEmailScraper(enabled=False)
