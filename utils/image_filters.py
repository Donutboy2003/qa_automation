# utils/image_filters.py
# Logic for deciding whether an image is decorative, too small, or otherwise
# not worth sending to the LLM for alt text generation.

from typing import Any
from urllib.parse import urlparse

MIN_PX = 32  # images smaller than this in either dimension are considered decorative


def is_decorative_or_tiny(img: dict[str, Any], min_px: int = MIN_PX) -> bool:
    """
    Return True if this image should be skipped (decorative, hidden, too small, etc.)

    Skipped if any of these are true:
      - Rendered width or height is below min_px
      - role="presentation" or role="none"
      - aria-hidden="true"
      - No src, or src is a data URI
      - Src is from a known ad/tracking domain
    """
    w = int(img.get("RenderedPx", {}).get("Width") or 0)
    h = int(img.get("RenderedPx", {}).get("Height") or 0)
    if w < min_px or h < min_px:
        return True

    role = (img.get("Role") or "").strip().lower()
    if role in {"presentation", "none"}:
        return True

    if img.get("AriaHidden") is True:
        return True

    src = (img.get("Src") or "").strip()
    if not src or src.startswith("data:"):
        return True

    # Skip known ad/tracking pixels
    try:
        if "adsrvr.org" in (urlparse(src).netloc or ""):
            return True
    except Exception:
        pass

    return False


def should_skip_image(
    abs_url: str,
    width: int,
    height: int,
    allowed_host: str = "www.ualberta.ca",
    min_px: int = MIN_PX,
) -> bool:
    """
    Return True if an image should be skipped during a site-wide audit.

    Skips images that are:
      - Too small (below min_px in either dimension)
      - Not served from the allowed host (default: www.ualberta.ca)
      - From known ad/tracking domains
      - Named like a tracking pixel (1x1, pixel, adsct)
    """
    from urllib.parse import urlparse

    SKIP_DOMAINS = {
        "adsrvr.org", "doubleclick.net", "google-analytics.com",
        "googletagmanager.com", "googlesyndication.com",
    }

    if width < min_px or height < min_px:
        return True
    try:
        host = (urlparse(abs_url).netloc or "").lower()
        if host and host != allowed_host:
            return True
        if any(d in host for d in SKIP_DOMAINS):
            return True
    except Exception:
        pass
    try:
        fname = (urlparse(abs_url).path or "").split("/")[-1].lower()
        if fname in {"adsct", "pixel", "1x1"}:
            return True
    except Exception:
        pass
    return False
