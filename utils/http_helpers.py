# utils/http_helpers.py
# HTTP utilities: a retrying requests session and helpers for checking
# whether an image URL is reachable and within a size budget for LLM calls.

import time
import random
from typing import Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

MAX_BYTES_FOR_LLM = 10 * 1024 * 1024  # 10 MB — skip anything larger
REQUEST_TIMEOUT = 30  # seconds

# Cache so we don't HEAD the same URL twice in a run
_head_cache: dict[str, tuple[bool, Optional[int], Optional[str]]] = {}

# Rotate through a handful of realistic Chrome user agent strings.
# Using a single UA forever is a bot signal — rotating helps.
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


def make_session(total_retries: int = 5, backoff: float = 0.6) -> requests.Session:
    """
    Build a requests Session that mimics a real Chrome browser.

    Uses a rotating user agent and the full set of headers a browser sends,
    which avoids bot-detection filters that key on missing or suspicious headers.
    """
    s = requests.Session()
    retry = Retry(
        total=total_retries,
        connect=total_retries,
        read=total_retries,
        status=total_retries,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=50)
    s.mount("http://", adapter)
    s.mount("https://", adapter)

    # Full browser header set — missing any of these is a common bot signal
    s.headers.update({
        "User-Agent":                random.choice(_USER_AGENTS),
        "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language":           "en-CA,en-US;q=0.9,en;q=0.8",
        "Accept-Encoding":           "gzip, deflate, br",
        "Connection":                "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest":            "document",
        "Sec-Fetch-Mode":            "navigate",
        "Sec-Fetch-Site":            "none",
        "Sec-Fetch-User":            "?1",
        "Sec-CH-UA":                 '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "Sec-CH-UA-Mobile":          "?0",
        "Sec-CH-UA-Platform":        '"Windows"',
        "DNT":                       "1",
    })
    return s


def _get(url: str, **kwargs) -> requests.Response:
    """
    Make a GET request with a small random delay to avoid rate limiting.
    Rotates the User-Agent header per request.
    """
    SESSION.headers.update({"User-Agent": random.choice(_USER_AGENTS)})
    time.sleep(random.uniform(0.3, 0.9))
    return SESSION.get(url, **kwargs)


# Module-level session — shared across all callers in a single run
SESSION = make_session()


def head_info(url: str) -> tuple[bool, Optional[int], Optional[str]]:
    """
    Run a HEAD request and return (reachable, content_length, content_type).
    Results are cached for the lifetime of the process.
    """
    if url in _head_cache:
        return _head_cache[url]
    try:
        r = SESSION.head(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        ok = r.status_code == 200
        size = int(r.headers.get("Content-Length", "0") or 0) or None
        ctype = (r.headers.get("Content-Type") or "").lower() or None
        _head_cache[url] = (ok, size, ctype)
    except requests.RequestException:
        _head_cache[url] = (False, None, None)
    return _head_cache[url]


def image_exists(url: str) -> bool:
    """
    Return True if the image URL responds with a 200.
    Falls back to a GET if HEAD fails (some servers don't support HEAD).
    """
    ok, _, _ = head_info(url)
    if ok:
        return True
    try:
        r = SESSION.get(url, timeout=REQUEST_TIMEOUT, stream=True)
        return r.status_code == 200
    except requests.RequestException:
        return False


def within_llm_size_budget(url: str) -> bool:
    """
    Return True if the image is small enough and of a type we can send to the LLM.
    Skips TIFFs (not supported by vision APIs) and anything over MAX_BYTES_FOR_LLM.
    """
    _, size, ctype = head_info(url)
    if ctype and "tiff" in ctype:
        return False
    if size is not None and size > MAX_BYTES_FOR_LLM:
        return False
    return True


def fetch_link_context(href: str) -> dict:
    """
    Fetch a link target page and return compact context for aria-label generation:
    final_url, title, og_title, h1, meta_desc.

    Only attempts absolute http(s) URLs — returns empty fields for anything else.
    Uses the shared SESSION so retries and headers are handled consistently.
    """
    from bs4 import BeautifulSoup

    ctx = {"final_url": "", "title": "", "og_title": "", "h1": "", "meta_desc": ""}

    parsed = urlparse(href)
    if not parsed.scheme or parsed.scheme not in ("http", "https"):
        return ctx

    try:
        r = _get(href, timeout=(5, 8))
        r.raise_for_status()
        ctx["final_url"] = r.url

        soup = BeautifulSoup(r.text, "html.parser")

        if soup.title and soup.title.string:
            ctx["title"] = (soup.title.string or "").strip()

        og = soup.find("meta", attrs={"property": "og:title"})
        if og and og.get("content"):
            ctx["og_title"] = (og["content"] or "").strip()

        h1 = soup.find("h1")
        if h1:
            ctx["h1"] = h1.get_text(separator=" ", strip=True)

        md = soup.find("meta", attrs={"name": "description"})
        if md and md.get("content"):
            ctx["meta_desc"] = (md["content"] or "").strip()

    except Exception:
        pass  # caller gets empty context — not a hard failure

    return ctx
