"""
Reads a YouTube channel's public About page in CloakBrowser and scans the
rendered text for a contact email.

This only reads text already visible on the public page. It does not
attempt to reveal YouTube's gated "View email address" button.

One browser instance serves the whole run. The earlier implementation in
backfill_missing_emails.py launched and closed a browser per channel,
which at 40+ channels per niche per day is both slow and a stronger
automation signal than a single session.

Every failure is soft: the chain continues without a browser-sourced
email rather than breaking the run.
"""
import logging
from urllib.parse import quote

from enrichment import extract_business_email

logger = logging.getLogger(__name__)

NAV_TIMEOUT_MS = 30000
TEXT_TIMEOUT_MS = 5000


class BrowserEmailScraper:
    """
    Wraps a CloakBrowser instance. Construct with `enabled=False` (or via
    null_scraper()) to get an inert scraper that always returns "".

    Usable as a context manager so the browser is closed even if the run
    raises.
    """

    def __init__(self, browser=None, enabled: bool = True):
        self._browser = browser
        self._enabled = enabled and browser is not None

    @property
    def enabled(self) -> bool:
        """True if this scraper holds a live browser session."""
        return self._enabled

    @classmethod
    def launch(cls, headless: bool = True) -> "BrowserEmailScraper":
        """Start a CloakBrowser session, or return an inert scraper if it
        can't be started."""
        try:
            from cloakbrowser import launch
        except ImportError:
            logger.warning("CloakBrowser is not installed — browser email step disabled.")
            return cls(enabled=False)

        try:
            return cls(browser=launch(headless=headless))
        except Exception as exc:
            logger.warning("CloakBrowser failed to launch (%s) — browser email step disabled.", exc)
            return cls(enabled=False)

    def find_email(self, channel_id: str) -> str:
        """Return an email found in the channel's About page text, or ""."""
        if not self._enabled:
            return ""

        about_url = f"https://www.youtube.com/channel/{quote(channel_id)}/about"
        page = None
        try:
            page = self._browser.new_page()
            page.goto(about_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            visible_text = page.locator("body").inner_text(timeout=TEXT_TIMEOUT_MS)
            return extract_business_email(visible_text)
        except Exception as exc:
            logger.info("Browser email lookup failed for %s: %s", channel_id, exc)
            return ""
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass

    def close(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception as exc:
                logger.info("Browser close failed: %s", exc)
            self._browser = None
            self._enabled = False

    def __enter__(self) -> "BrowserEmailScraper":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def null_scraper() -> BrowserEmailScraper:
    """An inert scraper, for runs with the browser step turned off."""
    return BrowserEmailScraper(enabled=False)
