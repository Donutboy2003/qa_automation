# utils/sitemap_helpers.py
#
# Fetch and parse XML sitemaps for UAlberta sites via BrowserSession.
# All known UAlberta sitemaps are flat <urlset> documents (no sitemap indexes).

from __future__ import annotations

import gzip
import xml.etree.ElementTree as ET
from io import BytesIO
from typing import Optional
from urllib.parse import urlparse

from utils.browser_helpers import BrowserSession


def _live_site_root(site: str) -> str:
    return f"https://www.ualberta.ca/en/{site.strip().strip('/')}"


def _sitemap_url_candidates(site: str) -> list[str]:
    root = _live_site_root(site)
    return [
        f"{root}/sitemap.xml",
        f"{root}/sitemap_index.xml",
        f"{root}/sitemap-index.xml",
    ]


def _to_xml_bytes(raw: bytes) -> Optional[bytes]:
    """
    Given raw bytes from the browser, return clean XML bytes or None.

    Handles three cases:
      1. Raw XML bytes straight off the wire  (ideal — get_bytes response listener)
      2. Gzip-compressed XML
      3. Chrome rendered the XML into HTML    (fallback — extract text from <pre>)
    """
    if not raw:
        return None

    # Decompress gzip if needed
    if raw.startswith(b"\x1f\x8b"):
        try:
            raw = gzip.GzipFile(fileobj=BytesIO(raw)).read()
        except OSError:
            pass

    stripped = raw.strip()

    # Case 1: already valid XML
    if stripped.startswith(b"<") and b"<urlset" in stripped[:500]:
        return stripped

    # Case 2: Chrome wrapped the XML in an HTML page (XML viewer or challenge page)
    # Pull text out of <pre> tags (Chrome's XML viewer) or the whole body
    if stripped.lower().startswith(b"<!doctype") or stripped.lower().startswith(b"<html"):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(stripped, "html.parser")
        pre  = soup.find("pre")
        text = (pre.get_text() if pre else soup.get_text()).strip()
        if text.startswith("<") and "<urlset" in text[:500]:
            return text.encode("utf-8")
        # Looks like a challenge / error page — not XML
        return None

    return None


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


def fetch_sitemap_paths(
    site:    str,
    debug:   bool = False,
    browser: Optional[BrowserSession] = None,
) -> list[str]:
    """
    Fetch the sitemap for a UAlberta site and return relative page paths.

    Args:
        site:    Site slug, e.g. "human-resources-health-safety-environment"
        debug:   Print extra diagnostics if True
        browser: Open BrowserSession to reuse. Creates a temporary one if None.

    Returns paths as '/some/page/path' (no .html, no host).
    Raises RuntimeError if no sitemap can be fetched.
    """
    base_path = f"/en/{site.strip().strip('/')}"

    def _do_fetch(b: BrowserSession) -> list[str]:
        raw_bytes: Optional[bytes] = None
        used_url:  Optional[str]   = None

        for cand in _sitemap_url_candidates(site):
            data = b.get_bytes(cand, delay=True)
            xml  = _to_xml_bytes(data)

            if debug:
                print(f"[DEBUG] {cand} → raw={len(data)}B  xml={'ok' if xml else 'None'}")
                if not xml:
                    print(f"[DEBUG] First 200B: {data[:200]}")

            if xml:
                raw_bytes = xml
                used_url  = cand
                break

            print(f"[WARN] {cand} — could not extract valid XML, trying next candidate")

        if raw_bytes is None:
            raise RuntimeError(
                f"Could not fetch any sitemap for site '{site}' after trying all candidates."
            )

        xml   = raw_bytes.strip()
        head  = xml[:2000].lower()
        is_index = b"<sitemapindex" in head or b":sitemapindex" in head

        if debug:
            print(f"[DEBUG] Sitemap source: {used_url}  is_index={is_index}")

        all_urls: list[str] = []
        if is_index:
            children = _parse_sitemapindex(xml)
            for child_url in children:
                child_raw = b.get_bytes(child_url, delay=True)
                child_xml = _to_xml_bytes(child_raw)
                if child_xml:
                    try:
                        all_urls.extend(_parse_urlset(child_xml))
                    except ET.ParseError:
                        pass
        else:
            all_urls.extend(_parse_urlset(xml))

        if debug:
            print(f"[DEBUG] Total URLs in sitemap: {len(all_urls)}")

        return all_urls

    if browser is not None:
        all_urls = _do_fetch(browser)
    else:
        with BrowserSession() as tmp:
            all_urls = _do_fetch(tmp)

    # Convert full URLs to site-relative paths, deduplicated
    paths: list[str] = []
    seen:  set[str]  = set()

    for full_url in all_urls:
        try:
            pth = urlparse(full_url).path
        except Exception:
            continue
        if not pth.startswith(base_path):
            continue
        rel = pth[len(base_path):] or "/"
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
        print(f"[DEBUG] Paths kept for '{site}': {len(paths)}")

    return paths