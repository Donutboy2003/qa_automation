# utils/html_helpers.py
# Functions for reading and updating alt attributes inside HTML strings
# embedded in Cascade page JSON.

import copy
import json
from typing import Any

from bs4 import BeautifulSoup


def count_missing_alts(html: str) -> int:
    """Count how many <img> tags in an HTML snippet are missing or have an empty alt."""
    soup = BeautifulSoup(html or "", "html.parser")
    return sum(
        1 for img in soup.find_all("img")
        if img.get("alt") is None or str(img.get("alt")).strip() == ""
    )


def insert_empty_alts(html: str) -> tuple[str, int]:
    """
    Set alt="" on every <img> that has no alt or an empty alt.
    Returns (updated_html, number_of_changes).
    This is used to mark decorative images explicitly rather than leaving them
    with a missing alt, which is a different accessibility violation.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    changes = 0
    for img in soup.find_all("img"):
        alt = img.get("alt")
        if alt is None or str(alt).strip() == "":
            img["alt"] = ""
            changes += 1
    return str(soup), changes


def walk_page_json_html_nodes(page_json: dict[str, Any], callback) -> dict[str, Any]:
    """
    Deep-copy a Cascade page JSON dict and call callback(html_string) on every
    text node that contains <img> tags. The callback should return (new_html, changes_int).

    Returns (updated_json, total_changes).
    """
    updated = copy.deepcopy(page_json)
    total_changes = 0

    def visit(node: Any):
        nonlocal total_changes
        if isinstance(node, dict):
            if node.get("type") == "text" and "<img" in (node.get("text") or ""):
                new_html, n = callback(node["text"])
                if n > 0:
                    node["text"] = new_html
                    total_changes += n
            # Always recurse — structured data nodes are nested inside regular dicts
            for k, v in node.items():
                if k == "structuredDataNodes" and isinstance(v, list):
                    for child in v:
                        visit(child)
                else:
                    visit(v)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(updated)
    return updated, total_changes


def apply_decorative_alts(page_json: dict[str, Any]) -> tuple[dict[str, Any], int, int]:
    """
    Walk the page JSON and insert alt="" on every <img> missing an alt attribute.
    Returns (updated_json, missing_before, changes_applied).
    """
    # First pass: count how many are missing before we change anything
    missing_before = 0

    def count_only(html: str):
        n = count_missing_alts(html)
        nonlocal missing_before
        missing_before += n
        return html, 0  # no changes, just counting

    walk_page_json_html_nodes(page_json, count_only)

    # Second pass: actually apply the empty alts
    updated_json, changes = walk_page_json_html_nodes(page_json, insert_empty_alts)
    return updated_json, missing_before, changes


def extract_html_snippets(page_json: dict[str, Any]) -> list[str]:
    """
    Walk a Cascade page JSON tree and collect every string value that looks like
    HTML (i.e. contains a '<' character). This is the equivalent of the old
    cascadeFunctions.extractTextFromJson helper.

    Returns a flat list of HTML strings — one per text/xhtml node found.
    """
    snippets: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key in ("text", "xhtml"):
                val = node.get(key)
                if isinstance(val, str) and "<" in val:
                    snippets.append(val)
            for v in node.values():
                if isinstance(v, (dict, list)):
                    visit(v)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(page_json)
    return snippets


class TableHTMLParser:
    """
    Lightweight HTML table parser built on Python's stdlib HTMLParser.
    Walks an HTML string and extracts every <table> as a list of rows,
    where each row is a list of cell strings.

    Usage:
        parser = TableHTMLParser()
        parser.feed(html_string)
        for table in parser.tables:
            # table is a list[list[str]]
    """

    from html.parser import HTMLParser as _HTMLParser

    class _Inner(_HTMLParser):
        def __init__(self):
            super().__init__()
            self.tables: list[list[list[str]]] = []
            self._in_table  = False
            self._in_row    = False
            self._in_cell   = False
            self._curr_table: list[list[str]] = []
            self._curr_row:   list[str]       = []
            self._curr_data:  str             = ""

        def handle_starttag(self, tag, attrs):
            t = tag.lower()
            if t == "table":
                self._in_table = True
                self._curr_table = []
            elif t == "tr" and self._in_table:
                self._in_row = True
                self._curr_row = []
            elif t in ("td", "th") and self._in_row:
                self._in_cell = True
                self._curr_data = ""

        def handle_data(self, data):
            if self._in_cell:
                self._curr_data += data

        def handle_endtag(self, tag):
            t = tag.lower()
            if t in ("td", "th") and self._in_cell:
                self._in_cell = False
                self._curr_row.append(self._curr_data.strip())
            elif t == "tr" and self._in_row:
                self._in_row = False
                self._curr_table.append(self._curr_row)
            elif t == "table" and self._in_table:
                self._in_table = False
                self.tables.append(self._curr_table)

    def __init__(self):
        self._parser = self._Inner()

    @property
    def tables(self) -> list[list[list[str]]]:
        return self._parser.tables

    def feed(self, html: str) -> None:
        """Feed an HTML string into the parser. Can be called multiple times."""
        self._parser.feed(html)


def scrape_tables_from_url(url: str) -> list[list[list[str]]]:
    """
    Fetch a live page and extract all HTML tables from it.
    Returns a list of tables, each table being a list of rows,
    each row being a list of cell strings.
    Uses the shared SESSION from http_helpers for consistent retries/headers.
    """
    from utils.http_helpers import SESSION

    resp = SESSION.get(url, timeout=15)
    resp.raise_for_status()

    parser = TableHTMLParser()
    parser.feed(resp.text)
    return parser.tables


def extract_images_from_page_json(page_json: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Pull image data out of a Cascade page JSON tree by parsing <img> tags from
    every HTML snippet found in text/xhtml nodes.

    Returns a list of dicts in the same shape as scrape_page_images() so that
    the rest of the alt text pipeline can treat both sources the same way.
    Note: RenderedPx will be 0x0 since we can't know rendered size from JSON alone —
    the image_filters.is_decorative_or_tiny check is skipped for this source.
    """
    soup_module = BeautifulSoup  # already imported at top of file
    results = []
    index = 0

    for html in extract_html_snippets(page_json):
        soup = BeautifulSoup(html, "html.parser")
        for img in soup.find_all("img"):
            src       = img.get("src") or ""
            alt_attr  = img.get("alt")          # None means missing, "" means explicitly empty
            role      = img.get("role")
            aria_raw  = img.get("aria-hidden")
            aria_hidden = True if aria_raw == "true" else False if aria_raw == "false" else None
            class_hints = [c for c in (img.get("class") or []) if c]

            # Try to get a filename from the src
            try:
                from urllib.parse import urlparse as _up
                file_name = (_up(src).path or src).split("/")[-1].split("?")[0] or "unknown"
            except Exception:
                file_name = "unknown"

            results.append({
                "PageUrl":    "",           # not available from JSON
                "Index":      index,
                "Src":        src,
                "FileName":   file_name,
                "AltAttr":    alt_attr,
                "Role":       role,
                "AriaHidden": aria_hidden,
                "InLink":     False,        # not detectable from static HTML
                "InButton":   False,
                "RenderedPx": {"Width": 0, "Height": 0},  # unknown from JSON
                "ClassHints": class_hints,
                "Source":     "cascade",    # flag so callers know size filter should be skipped
            })
            index += 1

    return results


def scrape_tables_from_url(url: str) -> list[list[list[str]]]:
    """
    Fetch a live URL and extract all HTML tables from the page content.
    Uses the shared SESSION from http_helpers for consistent retry behavior.

    Returns a list of tables, each table being a list of rows,
    each row being a list of cell strings.
    """
    from utils.http_helpers import SESSION
    resp = SESSION.get(url, timeout=15)
    resp.raise_for_status()
    parser = TableHTMLParser()
    parser.feed(resp.text)
    return parser.tables
