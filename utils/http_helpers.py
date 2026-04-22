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
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

# How many times to retry a 202 before giving up.
_MAX_202_RETRIES = 4

# Base delay (seconds) for 202 retry backoff — doubles each attempt: 2, 4, 8, 16 s.
_202_BACKOFF_BASE = 2.0

# UAlberta root — hitting this seeds Akamai cookies for the whole session.
_WARMUP_URL = "https://www.ualberta.ca/"
_session_warmed_up = False

# Module-level session — rebuilt by reset_session(); shared across all callers.
# Declared as None here; assigned after make_session() is defined below.
SESSION: requests.Session


def make_session(total_retries: int = 5, backoff: float = 0.6) -> requests.Session:
    """
    Build a requests Session that mimics a real Chrome browser.
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


def reset_session() -> None:
    """
    Discard the current session and cookie jar and create a fresh one.

    Akamai tracks session age and request count. After ~15 requests from the
    same cookie jar it re-challenges the client. Resetting gives a clean slate
    that the next warm_up_session() call can seed with fresh cookies.

    Typical usage in a batch loop:
        if idx % RESET_EVERY == 0:
            reset_session()
            warm_up_session()
    """
    global SESSION, _session_warmed_up
    SESSION = make_session()
    _session_warmed_up = False


def warm_up_session() -> None:
    """
    Seed the session cookie jar by visiting the UAlberta root page.

    Akamai sets trust cookies on the first visit. Without them every subsequent
    request gets a 202 challenge page. No-op after the first successful call.
    """
    global _session_warmed_up
    if _session_warmed_up:
        return
    try:
        print(f"[INFO] Warming up session: {_WARMUP_URL}")
        SESSION.headers.update({"User-Agent": random.choice(_USER_AGENTS)})
        r = SESSION.get(_WARMUP_URL, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if r.status_code == 200:
            print(f"[INFO] Session warmed up — {len(SESSION.cookies)} cookie(s) set")
        else:
            print(f"[WARN] Warm-up returned {r.status_code} — proceeding anyway")
    except Exception as e:
        print(f"[WARN] Warm-up request failed: {e} — proceeding anyway")
    finally:
        _session_warmed_up = True


def _get(url: str, **kwargs) -> requests.Response:
    """
    Make a GET request with a human-paced delay, retrying up to _MAX_202_RETRIES
    times on Akamai 202 challenges with exponential backoff.

    Inter-request delay is 1.5–4 s — slow enough that Akamai's behavioral
    analysis doesn't flag a sustained machine-speed burst.
    """
    SESSION.headers.update({"User-Agent": random.choice(_USER_AGENTS)})
    time.sleep(random.uniform(1.5, 4.0))

    resp = SESSION.get(url, **kwargs)

    if resp.status_code != 202:
        return resp

    # 202 retry loop: 2 s, 4 s, 8 s, 16 s
    for attempt in range(1, _MAX_202_RETRIES + 1):
        wait = _202_BACKOFF_BASE * (2 ** (attempt - 1)) + random.uniform(0, 1)
        print(f"  [202] {url} — retry {attempt}/{_MAX_202_RETRIES} in {wait:.1f}s")
        time.sleep(wait)
        SESSION.headers.update({"User-Agent": random.choice(_USER_AGENTS)})
        resp = SESSION.get(url, **kwargs)
        if resp.status_code != 202:
            break

    return resp


# Initialise the module-level session.
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
        pass

    return ctx