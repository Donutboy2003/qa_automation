# utils/url_helpers.py
# Shared URL parsing, sanitization, and site info extraction for Cascade/UAlberta pages.

import re
from urllib.parse import urlparse, urlunparse, urljoin


def sanitize_url(raw: str) -> str:
    """
    Fix common copy-paste mistakes before we try to use the URL.
    Handles things like missing 'h' in https, single slashes, missing scheme, etc.
    """
    s = (raw or "").strip()
    if not s:
        return s
    low = s.lower()

    # Someone dropped the 'h' off https://
    if low.startswith("ttps://"):
        s = "h" + s
        low = s.lower()

    # Single slash instead of double (http:/ or https:/)
    if low.startswith("http:/") and not low.startswith("http://"):
        s = "http://" + s[len("http:/"):]
        low = s.lower()
    if low.startswith("https:/") and not low.startswith("https://"):
        s = "https://" + s[len("https:/"):]
        low = s.lower()

    # No scheme at all — assume https
    p = urlparse(s)
    if not p.scheme:
        s = "https://" + s
        p = urlparse(s)

    # Some totally wrong scheme — replace it
    if p.scheme not in ("http", "https"):
        s = re.sub(r"^[a-zA-Z]+://", "https://", s)
        p = urlparse(s)

    # Still no netloc (e.g. "example.com/path" slipped through)
    if not p.netloc and p.path:
        s = "https://" + p.path
        p = urlparse(s)

    return urlunparse(p)


def is_valid_http_url(url: str) -> bool:
    """Quick check that a URL has a proper http/https scheme and a host."""
    try:
        p = urlparse(url)
        return bool(p.scheme in ("http", "https") and p.netloc)
    except Exception:
        return False


def extract_site_info(page_url: str) -> tuple[str, str, str, str, str]:
    """
    Pull apart a UAlberta Cascade URL into its components.

    Returns (site_name, page_path, scheme, host, site_root)
      - site_name: the segment right after '/en/'
      - page_path: the rest of the path, .html stripped, 'index' appended if needed
      - site_root: 'https://{host}/en/{site_name}'

    Raises RuntimeError if the URL doesn't follow the /en/<site>/... pattern.
    """
    u = urlparse(page_url)
    host = (u.netloc or "").lower()
    scheme = u.scheme or "https"
    segs = [p for p in (u.path or "/").split("/") if p]

    if "en" not in segs or segs.index("en") == len(segs) - 1:
        raise RuntimeError(
            f"Can't figure out site name from this URL (expected '/en/<site>/...'): {page_url}"
        )

    en_idx = segs.index("en")
    site_name = segs[en_idx + 1]
    rest_segs = segs[en_idx + 2:]
    rest_path = "/" + "/".join(rest_segs) if rest_segs else "/"

    # Cascade paths don't include .html or trailing slashes
    if rest_path.endswith(".html"):
        rest_path = rest_path[:-5]
    if rest_path == "" or rest_path.endswith("/"):
        rest_path = f"{rest_path}index"

    site_root = f"{scheme}://{host}/en/{site_name}"
    return site_name, rest_path, scheme, host, site_root


def absolutize_image_url(page_url: str, src: str) -> str:
    """
    Turn a relative image src into an absolute URL using UAlberta's CMS path convention.

    Rules:
      1. If src is already http/https, use it as-is.
      2. Otherwise, build https://{host}/en/{site_name}/{cleaned_src}
         stripping any leading /, ./, ../ from src first.

    Use this for CMS-sourced paths. Use absolutize_src_url for live-site scraping.
    """
    if not src:
        return src

    s = src.strip()
    if s.startswith("http://") or s.startswith("https://"):
        return s

    site_name, _, scheme, host, _ = extract_site_info(page_url)

    while s.startswith("../"):
        s = s[3:]
    if s.startswith("./"):
        s = s[2:]
    if s.startswith("/"):
        s = s[1:]

    return f"{scheme}://{host}/en/{site_name}/{s}"


def absolutize_src_url(page_url: str, src: str, site: str = "", allowed_host: str = "www.ualberta.ca") -> str:
    """
    Resolve a relative <img src> to an absolute URL using the page URL as base.

    Also fixes a common UAlberta pattern where image paths are stored without
    the /en/ language prefix (e.g. /arts/media-library/ → /en/arts/media-library/).

    Use this for live-site scraping. Use absolutize_image_url for CMS path resolution.

    Args:
        page_url:     The URL of the page the image appears on
        src:          The raw src attribute value
        site:         The site key (e.g. "arts") — used to detect missing /en/ prefix
        allowed_host: Only apply the /en/ fix for this host
    """
    if not src:
        return src
    url = urljoin(page_url, src.strip())
    if site:
        try:
            p = urlparse(url)
            if p.netloc == allowed_host and p.path.startswith(f"/{site}/"):
                url = p._replace(path="/en" + p.path).geturl()
        except Exception:
            pass
    return url


def filename_from_src(src: str) -> str:
    """Pull just the filename out of a URL or path (no query string)."""
    try:
        return (urlparse(src).path or src).split("/")[-1].split("?")[0]
    except Exception:
        return "unknown"


def normalize_src(src: str, site_name: str) -> str:
    """
    Strip the /en/{site_name} prefix from a src path so we can compare
    image references that might be written differently in different contexts.
    """
    if not src:
        return ""
    p = urlparse(src).path or src
    prefix = f"/en/{site_name}"
    if p.startswith(prefix + "/") or p == prefix:
        p = p[len(prefix):] or "/"
    return p.lower()


def normalize_asset_path(src: str) -> str | None:
    """
    Convert any <img src> format into a clean Cascade file path.

    Handles all the ways an image src can appear:
      /media-library/news/foo.jpg
      https://www.ualberta.ca/media-library/news/foo.jpg
      https://www.ualberta.ca/arts/media-library/news/foo.jpg

    Always returns the path starting from /media-library/ when that segment
    is present, otherwise returns the raw path. Returns None if src is empty.
    """
    if not src:
        return None
    path = (urlparse(src).path or "").strip()
    if not path:
        return None
    media_idx = path.find("/media-library/")
    if media_idx != -1:
        return path[media_idx:]
    return path if path.startswith("/") else "/" + path.lstrip("/")


def guess_mime_type(asset_path: str) -> str:
    """
    Guess the MIME type of an image from its file extension.
    Defaults to image/jpeg for anything unrecognised.
    """
    lower = (asset_path or "").lower()
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith(".webp"):
        return "image/webp"
    if lower.endswith(".gif"):
        return "image/gif"
    if lower.endswith(".svg"):
        return "image/svg+xml"
    return "image/jpeg"


def build_live_image_url(
    site_name:  str,
    asset_path: str,
    host:       str = "www.ualberta.ca",
) -> str:
    """
    Build the public live URL for a Cascade image asset.

    Cascade stores image paths as /media-library/... (no site prefix).
    The live URL includes the /en/{site_name} prefix that the CMS adds
    when publishing.

    Example:
        site_name  = "arts"
        asset_path = "/media-library/news/archived-images/2015/foo.jpg"
        → "https://www.ualberta.ca/en/arts/media-library/news/archived-images/2015/foo.jpg"

    Args:
        site_name:  Cascade site name (e.g. "arts", "admissions-programs")
        asset_path: Cascade asset path starting with / (e.g. "/media-library/...")
        host:       Public hostname (default: www.ualberta.ca)
    """
    # Ensure asset_path starts with exactly one /
    clean_path = "/" + asset_path.lstrip("/")
    return f"https://{host}/en/{site_name}{clean_path}"
