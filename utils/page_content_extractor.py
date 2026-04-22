# utils/page_content_extractor.py
#
# Extract clean readable text from a page, either by fetching the live URL
# or by reading the Cascade page JSON.
#
# Live fetches use Playwright (real Chromium) so that Akamai JS challenges
# are resolved naturally.  A BrowserSession must be passed in for live mode;
# the caller (batch_analyzer) owns the session lifetime.
#
# Used by page_analyzer and batch_analyzer as the content source step.

from __future__ import annotations

import re
from typing import Any, Optional

from bs4 import BeautifulSoup

from utils.html_helpers import extract_html_snippets


# ── Live URL extraction ───────────────────────────────────────────────────────

def extract_text_from_url(url: str, browser=None) -> dict[str, str]:
    """
    Fetch a live page and extract clean text content.

    Args:
        url:     The page URL to fetch.
        browser: A BrowserSession instance (from utils.browser_helpers).
                 Required — raises RuntimeError if not provided.

    Returns a dict with keys: url, title, text, error.
    """
    result = {"url": url, "title": "", "text": "", "error": ""}

    if browser is None:
        result["error"] = "BrowserSession required for live extraction"
        return result

    html_text = browser.get_html(url)

    if not html_text:
        result["error"] = "Browser returned empty response"
        return result

    # Detect Akamai challenge pages that didn't resolve
    if _is_challenge_page(html_text):
        result["error"] = "Akamai challenge page — JS challenge did not resolve"
        return result

    # Try trafilatura first — best at extracting article-quality text
    try:
        import trafilatura
        extracted = trafilatura.extract(html_text, include_formatting=False, include_tables=True)
        if extracted:
            result["text"] = extracted
    except ImportError:
        pass
    except Exception:
        pass

    # Fall back to BeautifulSoup if trafilatura gave nothing
    if not result["text"]:
        try:
            soup = BeautifulSoup(html_text, "html.parser")
            for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
                tag.decompose()
            main = soup.find(["article", "main"]) or soup
            result["text"] = main.get_text(" ", strip=True)
        except Exception as e:
            result["error"] = f"Parse failed: {e}"

    # Always try to get the title
    try:
        soup = BeautifulSoup(html_text, "html.parser")
        if soup.title and soup.title.string:
            result["title"] = soup.title.string.strip()
        if not result["title"]:
            m = re.search(r"<title>(.*?)</title>", html_text, re.I | re.S)
            if m:
                result["title"] = m.group(1).strip()
    except Exception:
        pass

    return result


def _is_challenge_page(html: str) -> bool:
    """
    Return True if the HTML looks like an unresolved Akamai/CDN challenge page
    rather than real content. Checks for known challenge signatures.
    """
    lower = html.lower()
    signals = [
        "you have been blocked",
        "please enable cookies",
        "checking your browser",
        "ray id:",                   # Cloudflare challenge
        "akamai-error",
        "access denied",
    ]
    # Also flag pages with almost no content — challenge pages are typically tiny
    text_length = len(BeautifulSoup(html, "html.parser").get_text())
    if text_length < 200:
        return True
    return any(s in lower for s in signals)


# ── Cascade JSON extraction ───────────────────────────────────────────────────

def extract_text_from_cascade_json(page_json: dict[str, Any], url: str = "") -> dict[str, str]:
    """
    Extract clean text from a Cascade page JSON response.

    Returns a dict with keys: url, title, text, error.
    """
    result = {"url": url, "title": "", "text": "", "error": ""}

    try:
        snippets = extract_html_snippets(page_json)
        if not snippets:
            result["error"] = "No HTML content found in page JSON"
            return result

        try:
            meta = page_json.get("asset", {}).get("page", {}).get("metadata", {})
            result["title"] = (
                meta.get("title") or meta.get("displayName") or ""
            ).strip()
        except Exception:
            pass

        parts: list[str] = []
        for html_snippet in snippets:
            try:
                soup = BeautifulSoup(html_snippet, "html.parser")
                text = soup.get_text(" ", strip=True)
                if text:
                    parts.append(text)
            except Exception:
                parts.append(html_snippet)

        result["text"] = "\n\n".join(parts)

    except Exception as e:
        result["error"] = f"Extraction failed: {e}"

    return result


# ── Combined helper ───────────────────────────────────────────────────────────

def extract_text(
    source:    str = "live",
    url:       str = "",
    page_json: dict[str, Any] | None = None,
    browser=None,
) -> dict[str, str]:
    """
    Extract page text from either a live URL or Cascade JSON.

    Args:
        source:    "live"    — fetch and parse the URL via Playwright
                   "cascade" — extract from page_json
        url:       The page URL
        page_json: Cascade read response — required when source="cascade"
        browser:   BrowserSession instance — required when source="live"

    Returns:
        Dict with keys: url, title, text, error
    """
    if source == "cascade":
        if page_json is None:
            return {"url": url, "title": "", "text": "", "error": "page_json is required for cascade source"}
        return extract_text_from_cascade_json(page_json, url=url)
    else:
        if not url:
            return {"url": "", "title": "", "text": "", "error": "url is required for live source"}
        return extract_text_from_url(url, browser=browser)