# utils/image_scraper.py
# Playwright-based scrapers that pull image metadata from live pages.
# Two modes:
#   scrape_page_images     — detailed per-image metadata (used by img_alt_text)
#   scrape_page_images_fast — lightweight batch extraction (used by bad_alt_auditor)

import sys
from typing import Any

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from utils.url_helpers import filename_from_src

# How long to wait after DOMContentLoaded for lazy-loaded images to settle
EXTRA_WAIT_MS = 700

# Resource types and ad/tracking hosts to block in fast mode —
# cuts page load time significantly when scraping many pages
BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}
BLOCKED_HOSTS = {
    "google-analytics.com", "googletagmanager.com", "doubleclick.net",
    "googlesyndication.com", "adsrvr.org", "facebook.net", "clarity.ms",
}


def route_blocker(route):
    """
    Playwright route handler that drops heavy/tracking resources.
    Pass this to page.route('**/*', route_blocker) before navigating.
    """
    req = route.request
    try:
        if req.resource_type in BLOCKED_RESOURCE_TYPES:
            return route.abort()
        host = req.url.split("/")[2].lower() if "://" in req.url else ""
        if any(blocked in host for blocked in BLOCKED_HOSTS):
            return route.abort()
    except Exception:
        pass
    return route.continue_()


def extract_images_fast(page) -> list[dict[str, Any]]:
    """
    Pull src, alt, and rendered dimensions for every image on the page
    in a single evaluate() call. Much faster than iterating with locators
    when you need to scan hundreds of pages.
    """
    return page.evaluate("""
        () => Array.from(document.images).map(img => ({
            src: img.getAttribute('src') || '',
            alt: img.getAttribute('alt'),
            width: (img.offsetWidth || img.naturalWidth || 0),
            height: (img.offsetHeight || img.naturalHeight || 0)
        }))
    """)


def scrape_page_images(page_url: str, max_images: int | None = None) -> list[dict[str, Any]]:
    """
    Launch a headless browser, navigate to page_url, and collect metadata
    for every <img> element on the page.

    Args:
        page_url:   The URL to scrape.
        max_images: If set, stop after this many images (useful for testing).

    Returns:
        List of dicts with keys: PageUrl, Index, Src, FileName, AltAttr,
        Role, AriaHidden, InLink, InButton, RenderedPx, ClassHints.
    """
    results = []

    with sync_playwright() as ctx:
        browser = ctx.chromium.launch()
        page = browser.new_page()

        try:
            page.goto(page_url, wait_until="domcontentloaded")
        except Exception as e:
            print(f"[ERROR] Couldn't navigate to {page_url}: {e}", file=sys.stderr)
            return results

        # Wait for at least one image to appear, then give lazy attrs a moment to settle
        try:
            page.wait_for_selector("img", timeout=1500)
        except PWTimeout:
            pass
        page.wait_for_timeout(EXTRA_WAIT_MS)

        images = page.locator("img")
        count = images.count()
        if max_images is not None:
            count = min(count, max_images)

        for index in range(count):
            img = images.nth(index)

            alt_attr   = img.get_attribute("alt")
            role       = img.get_attribute("role")
            aria_raw   = img.get_attribute("aria-hidden")
            aria_hidden = True if aria_raw == "true" else False if aria_raw == "false" else None
            in_link    = img.locator("xpath=ancestor::a").count() > 0
            in_button  = img.locator("xpath=ancestor::button|ancestor::*[@role='button']").count() > 0
            width      = img.evaluate("el => el.offsetWidth || 0")
            height     = img.evaluate("el => el.offsetHeight || 0")
            class_attr = (img.get_attribute("class") or "").strip()
            class_hints = [c for c in class_attr.split() if c]
            src        = img.get_attribute("src") or ""

            # data: URIs don't have a meaningful filename
            file_name = "data-uri" if src.startswith("data:") else (filename_from_src(src) or "unknown")

            results.append({
                "PageUrl":    page_url,
                "Index":      index,
                "Src":        src,
                "FileName":   file_name,
                "AltAttr":    alt_attr,
                "Role":       role,
                "AriaHidden": aria_hidden,
                "InLink":     in_link,
                "InButton":   in_button,
                "RenderedPx": {"Width": int(width), "Height": int(height)},
                "ClassHints": class_hints,
            })

        browser.close()

    return results
