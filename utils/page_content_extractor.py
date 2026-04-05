# utils/page_content_extractor.py
#
# Extract clean readable text from a page, either by fetching the live URL
# or by reading the Cascade page JSON.
#
# Used by page_analyzer and batch_analyzer as the content source step.

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

from utils.html_helpers import extract_html_snippets
from utils.http_helpers import _get, REQUEST_TIMEOUT


# ── Live URL extraction ───────────────────────────────────────────────────────

def extract_text_from_url(url: str) -> dict[str, str]:
    """
    Fetch a live page and extract clean text content.

    Tries trafilatura first (best quality), falls back to BeautifulSoup.
    Returns a dict with keys: url, title, text, error.
    """
    result = {"url": url, "title": "", "text": "", "error": ""}

    try:
        resp = _get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if resp.status_code != 200:
            result["error"] = f"HTTP {resp.status_code}"
            return result
        html_text = resp.text
    except Exception as e:
        result["error"] = f"Fetch failed: {e}"
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


# ── Cascade JSON extraction ───────────────────────────────────────────────────

def extract_text_from_cascade_json(page_json: dict[str, Any], url: str = "") -> dict[str, str]:
    """
    Extract clean text from a Cascade page JSON response.

    Pulls all HTML snippets from the structured data, strips tags,
    and joins into a single readable string.

    Returns a dict with keys: url, title, text, error.
    """
    result = {"url": url, "title": "", "text": "", "error": ""}

    try:
        snippets = extract_html_snippets(page_json)
        if not snippets:
            result["error"] = "No HTML content found in page JSON"
            return result

        # Pull title from metadata if available
        try:
            meta = page_json.get("asset", {}).get("page", {}).get("metadata", {})
            result["title"] = (
                meta.get("title")
                or meta.get("displayName")
                or ""
            ).strip()
        except Exception:
            pass

        # Strip HTML tags from each snippet and join
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
    source: str = "live",
    url: str = "",
    page_json: dict[str, Any] | None = None,
) -> dict[str, str]:
    """
    Extract page text from either a live URL or Cascade JSON.

    Args:
        source:    "live" — fetch and parse the URL
                   "cascade" — extract from page_json
        url:       The page URL (used for live fetch, and as label for cascade)
        page_json: Cascade read response — required when source="cascade"

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
        return extract_text_from_url(url)
