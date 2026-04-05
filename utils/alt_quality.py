# utils/alt_quality.py
# Heuristics for deciding whether an image's alt text is good enough.
# Used by the bad alt auditor to flag images that need attention.

import re
from typing import Optional

# Config — these match the auditor's policy
ALT_MAX_LEN = 140
ALT_MIN_WORDS = 4

BAD_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".tif", ".tiff"}
BAD_TOKENS = {"http://", "https://"}


def looks_like_filename(text: str) -> bool:
    """Return True if the alt text looks like it's just a file name pasted in."""
    if "." in text:
        for ext in BAD_EXTENSIONS:
            if text.lower().endswith(ext):
                return True
    # Slug-style names like "my-hero-image" or "banner_2023_v2"
    if re.search(r"[A-Za-z0-9]+[-_][A-Za-z0-9_-]+", text):
        if text.count("-") >= 2 or "_" in text:
            return True
    return False


def contains_forbidden_tokens(text: str) -> bool:
    """Return True if the alt text contains raw URLs (a common lazy mistake)."""
    low = text.lower()
    return any(tok in low for tok in BAD_TOKENS)


def has_bad_punctuation(text: str) -> bool:
    """Return True for alt text that ends in a period or has repeated punctuation."""
    if text.strip().endswith("."):
        return True
    if re.search(r"[!?:]{2,}", text):
        return True
    return False


def check_alt_quality(alt: Optional[str]) -> tuple[bool, str]:
    """
    Main entry point. Returns (needs_attention: bool, reason: str).

    Returns True (needs attention) if the alt is missing, empty, too short,
    too long, looks like a filename, contains raw URLs, or has bad punctuation.
    Returns False if the alt text looks fine.
    """
    if alt is None:
        return True, "missing"

    a = alt.strip()
    if a == "":
        return True, "missing"
    if looks_like_filename(a):
        return True, "filename-like"
    if contains_forbidden_tokens(a):
        return True, "forbidden-tokens"
    if len(a) > ALT_MAX_LEN:
        return True, "too-long"

    words = [w for w in re.split(r"\s+", a) if w]
    if len(words) < ALT_MIN_WORDS:
        return True, "too-short"

    if has_bad_punctuation(a):
        return True, "punctuation"

    return False, ""
