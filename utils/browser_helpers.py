# utils/browser_helpers.py
#
# Playwright-based page fetcher that runs a real Chromium engine.
#
# Why this exists:
#   Akamai Bot Manager serves a JavaScript challenge page to any client that
#   cannot execute JS.  requests/urllib can never pass it.  Playwright runs
#   actual Chromium, so the challenge executes and resolves as in a real browser.
#
# Key design decisions:
#   get_html()  — wait_until="networkidle", returns page.content() (rendered HTML)
#   get_bytes() — registers a response listener BEFORE navigation and always
#                 overwrites with the LATEST response for the target URL.
#
#                 Why latest, not first?
#                 Akamai's flow for a challenged request is:
#                   1. Browser GETs sitemap.xml
#                   2. Server returns 202 + HTML challenge  (response 1, url=sitemap.xml)
#                   3. Embedded JS executes, sets trust cookie, redirects back
#                   4. Server returns 200 + real XML        (response 2, url=sitemap.xml)
#                 Both responses share the same URL.  Capturing only the first gives
#                 the challenge HTML every time.  Overwriting with the latest gives
#                 the real content after the challenge resolves.

from __future__ import annotations

import random
import time
from typing import Optional

from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page, Playwright, Response

_VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
    {"width": 1280, "height": 800},
]

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

_WAIT_UNTIL       = "networkidle"   # ensures Akamai JS challenge completes
_NAV_TIMEOUT      = 30_000          # ms
_INTER_PAGE_DELAY = (0.5, 1.5)      # seconds between requests


class BrowserSession:
    """
    A reusable Playwright browser context that behaves like a real Chrome tab.

    Use as a context manager:

        with BrowserSession() as browser:
            html = browser.get_html("https://www.ualberta.ca/en/...")
            raw  = browser.get_bytes("https://.../sitemap.xml")

    The same BrowserContext (and cookie jar) is reused for every call so
    Akamai trust cookies set on the first request carry over to all subsequent ones.
    """

    def __init__(self, headless: bool = True):
        self._headless    = headless
        self._playwright: Optional[Playwright]     = None
        self._browser:    Optional[Browser]        = None
        self._context:    Optional[BrowserContext] = None

    def __enter__(self) -> "BrowserSession":
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=self._headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        self._context = self._browser.new_context(
            user_agent=random.choice(_USER_AGENTS),
            viewport=random.choice(_VIEWPORTS),
            locale="en-CA",
            timezone_id="America/Edmonton",
            extra_http_headers={
                "Accept-Language": "en-CA,en-US;q=0.9,en;q=0.8",
                "DNT":             "1",
            },
        )
        self._context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins',   { get: () => [1, 2, 3] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-CA', 'en'] });
        """)
        return self

    def __exit__(self, *_) -> None:
        try:
            if self._context:    self._context.close()
            if self._browser:    self._browser.close()
            if self._playwright: self._playwright.stop()
        except Exception:
            pass

    def _new_page(self) -> Page:
        if self._context is None:
            raise RuntimeError("BrowserSession must be used as a context manager")
        return self._context.new_page()

    def get_html(self, url: str, delay: bool = True) -> str:
        """
        Navigate to url and return fully-rendered HTML after JS has executed.
        Use for normal web pages.
        """
        if delay:
            time.sleep(random.uniform(*_INTER_PAGE_DELAY))
        page = self._new_page()
        try:
            page.goto(url, wait_until=_WAIT_UNTIL, timeout=_NAV_TIMEOUT)
            return page.content()
        except Exception as e:
            print(f"  [BROWSER] Failed to load {url}: {e}")
            return ""
        finally:
            page.close()

    def get_bytes(self, url: str, delay: bool = True) -> bytes:
        """
        Fetch url and return the raw response body bytes.

        Use for XML/binary resources (sitemaps) where you need bytes off the
        wire, not the HTML Chrome renders around them.

        The response listener always overwrites with the LATEST body for the
        target URL.  This handles Akamai's challenge→redirect flow where two
        responses share the same URL and only the second contains real content.
        After wait_until="networkidle" the challenge is guaranteed to have
        finished, so the final overwrite is always the real response.
        """
        if delay:
            time.sleep(random.uniform(*_INTER_PAGE_DELAY))

        page = self._new_page()
        # Use a list so the closure can overwrite: latest_body[0] = new_body
        latest_body: list[bytes] = []

        def _on_response(resp: Response) -> None:
            if resp.url == url:
                try:
                    # Always replace — we want the LAST response, not the first
                    body = resp.body()
                    if body:
                        if latest_body:
                            latest_body[0] = body
                        else:
                            latest_body.append(body)
                except Exception:
                    pass

        page.on("response", _on_response)
        try:
            page.goto(url, wait_until=_WAIT_UNTIL, timeout=_NAV_TIMEOUT)
            if latest_body:
                return latest_body[0]
            # Fallback: encode rendered content (challenge already resolved at this point)
            return page.content().encode("utf-8")
        except Exception as e:
            print(f"  [BROWSER] Failed to fetch bytes {url}: {e}")
            return b""
        finally:
            page.close()

    def get_text(self, url: str, delay: bool = True) -> str:
        """Navigate to url and return visible text (no HTML tags)."""
        from bs4 import BeautifulSoup
        html = self.get_html(url, delay=delay)
        if not html:
            return ""
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
            tag.decompose()
        return soup.get_text(" ", strip=True)