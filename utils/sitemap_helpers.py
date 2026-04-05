# utils/sitemap_helpers.py
# Fetch and parse XML sitemaps for UAlberta sites.
# Handles sitemap indexes, gzip-compressed sitemaps, and the
# /en/<site>/ path prefix convention.

import gzip
import xml.etree.ElementTree as ET
from io import BytesIO
from typing import Optional
from urllib.parse import urlparse

from utils.http_helpers import SESSION, REQUEST_TIMEOUT, _get


def _live_site_root(site: str) -> str:
    return f"https://www.ualberta.ca/en/{site.strip().strip('/')}"


def _sitemap_url_candidates(site: str) -> list[str]:
    """Try these URLs in order until one responds."""
    root = _live_site_root(site)
    return [
        f"{root}/sitemap.xml",
        f"{root}/sitemap_index.xml",
        f"{root}/sitemap-index.xml",
    ]


def _read_xml_bytes(resp) -> bytes:
    """Handle gzip-compressed responses transparently."""
    data = resp.content or b""
    is_gzip = data.startswith(b"\x1f\x8b") or (resp.url or "").lower().endswith(".gz")
    if is_gzip:
        try:
            return gzip.GzipFile(fileobj=BytesIO(data)).read()
        except OSError:
            return data
    return data


def _parse_urlset(xml_bytes: bytes) -> list[str]:
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ET.fromstring(xml_bytes)
    return [
        loc.text.strip()
        for loc in root.findall("sm:url/sm:loc", ns)
        if loc is not None and loc.text
    ]


def _parse_sitemapindex(xml_bytes: bytes) -> list[str]:
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ET.fromstring(xml_bytes)
    return [
        loc.text.strip()
        for loc in root.findall("sm:sitemap/sm:loc", ns)
        if loc is not None and loc.text
    ]


def _safe_parse_urlset(xml_bytes: bytes) -> list[str]:
    """Parse a urlset, with a fallback decompression attempt on parse errors."""
    try:
        return _parse_urlset(xml_bytes)
    except ET.ParseError:
        try:
            decompressed = gzip.GzipFile(fileobj=BytesIO(xml_bytes)).read()
            return _parse_urlset(decompressed)
        except Exception:
            head = xml_bytes[:200].decode("utf-8", errors="replace")
            raise RuntimeError(f"Sitemap is not valid XML. First 200 bytes:\n{head}")


def fetch_sitemap_paths(site: str, debug: bool = False) -> list[str]:
    """
    Fetch the sitemap for a UAlberta site and return a list of relative page paths.

    Paths are returned as '/some/page/path' (no .html, no host).
    Handles both flat sitemaps and sitemap index files.

    Raises RuntimeError if no sitemap can be fetched.
    """
    base_path = f"/en/{site.strip().strip('/')}"
    all_urls: list[str] = []

    # Try each candidate URL until one works
    raw_bytes: Optional[bytes] = None
    used_url: Optional[str] = None
    for cand in _sitemap_url_candidates(site):
        try:
            r = _get(cand, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            if debug:
                print(f"[DEBUG] Tried {cand} → {r.status_code} (final: {r.url})")
            if r.status_code == 200 and r.content:
                raw_bytes = _read_xml_bytes(r)
                used_url = r.url
                break
            else:
                print(f"[WARN] {cand} returned {r.status_code} — trying next candidate")
        except Exception as e:
            print(f"[WARN] {cand} failed: {e} — trying next candidate")
            continue

    if raw_bytes is None:
        raise RuntimeError(f"Could not fetch any sitemap for site '{site}' after trying all candidates.")

    xml = raw_bytes.strip()
    head = xml[:2000].lower()
    is_index = b"<sitemapindex" in head or b":sitemapindex" in head

    if is_index:
        # Sitemap index — fetch and combine all child sitemaps
        children = _parse_sitemapindex(xml)
        for child_url in children:
            try:
                rr = _get(child_url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
                if rr.status_code != 200 or not rr.content:
                    continue
                all_urls.extend(_safe_parse_urlset(_read_xml_bytes(rr)))
            except Exception:
                continue
    else:
        all_urls.extend(_safe_parse_urlset(xml))

    # Convert full URLs to site-relative paths, deduplicated
    paths: list[str] = []
    seen: set[str] = set()
    for full_url in all_urls:
        try:
            pth = urlparse(full_url).path
        except Exception:
            continue
        if not pth.startswith(base_path):
            continue
        rel = pth[len(base_path):] or "/"
        # Normalize: strip .html, ensure /index for root/trailing-slash
        if rel.endswith(".html"):
            rel = rel[:-5]
        if rel == "" or rel.endswith("/"):
            rel = f"{rel}index"
        if not rel.startswith("/"):
            rel = "/" + rel
        if rel not in seen:
            seen.add(rel)
            paths.append(rel)

    if debug:
        print(f"[DEBUG] Sitemap source: {used_url}")
        print(f"[DEBUG] Total URLs in sitemap: {len(all_urls)}, paths kept for '{site}': {len(paths)}")

    return paths
