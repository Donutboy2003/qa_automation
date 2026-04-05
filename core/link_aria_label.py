# core/link_aria_label.py
#
# Reads a Cascade page, finds any <a> tags missing aria-labels, generates them
# using OpenAI, and optionally writes the updates back to Cascade.
#
# Output modes:
#   "console"      — print what would be added, no files written, no Cascade writes
#   "report"       — save page_raw.json and page_updated.json locally, no Cascade writes
#   "cascade-dev"  — write changes to the DEV server + save report files (default)
#   "cascade-live" — write changes to the PRODUCTION server + save report files
#
# Batch usage example:
#   from core.link_aria_label import process_page
#
#   process_page(site="ualberta", path="/about/index", output_mode="cascade-dev")
#   process_page(site="botanic-garden", path="/visit/index", output_mode="report")

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Iterator
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI

from utils.CascadeClient import CascadeClient
from utils.http_helpers import fetch_link_context
from utils.report_helpers import report_path
from utils.url_helpers import is_valid_http_url

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("debug.log", mode="w", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

load_dotenv()

CASCADE_API_KEY = os.getenv("CASCADE_API_KEY", "").strip()
CASCADE_DEV_KEY = os.getenv("CASCADE_DEV_API_KEY", CASCADE_API_KEY).strip()
OPENAI_KEY      = os.getenv("OPENAI_API_KEY", "").strip()

if not OPENAI_KEY:
    logger.critical("OPENAI_API_KEY not found in environment.")
    sys.exit(1)

client = OpenAI(api_key=OPENAI_KEY)

# Set ARIA_BASE_URL in .env if your page HTML uses relative hrefs
BASE_URL = os.getenv("ARIA_BASE_URL", "").strip() or None


# ── Output mode helpers ───────────────────────────────────────────────────────

def _cascade_client(output_mode: str) -> CascadeClient:
    use_dev = output_mode != "cascade-live"
    api_key = CASCADE_DEV_KEY if use_dev else CASCADE_API_KEY
    if not api_key:
        raise RuntimeError(f"{'CASCADE_DEV_API_KEY' if use_dev else 'CASCADE_API_KEY'} missing")
    return CascadeClient(api_key, testing=use_dev)


def _writes_to_cascade(output_mode: str) -> bool:
    return output_mode in ("cascade-dev", "cascade-live")


def _saves_report(output_mode: str) -> bool:
    return output_mode in ("report", "cascade-dev", "cascade-live")


# ── LLM ───────────────────────────────────────────────────────────────────────

def generate_aria_label(anchor_html: str, link_ctx: dict) -> str:
    """
    Ask the model for an aria-label for a given <a> element.
    Passes the full anchor HTML plus context scraped from the link target page.
    """
    link_summary = json.dumps({
        "final_url": link_ctx.get("final_url", ""),
        "title":     link_ctx.get("title", ""),
        "og_title":  link_ctx.get("og_title", ""),
        "h1":        link_ctx.get("h1", ""),
        "meta_desc": link_ctx.get("meta_desc", ""),
    }, ensure_ascii=False)

    system_msg = (
        "You generate clear, concise aria-labels for <a> elements to improve accessibility. "
        "Aria-labels should help a screen reader user understand the purpose/destination of the link."
    )

    user_msg = f"""
You are given:
1) The FULL anchor HTML (including attributes and inner text).
2) A compact summary of the target page (if fetched successfully).

Instructions for the aria-label:
- Use THREE sources of truth together:
  (A) the anchor's display text (inner text between <a>...</a>),
  (B) the anchor's attributes (e.g., title, rel, data-*; do NOT just repeat the raw URL),
  (C) the target page summary (final URL, page title, og:title, first H1, meta description).
- Be short (ideally ≤ 12 words), specific, and action-oriented:
  Start with verbs like "Open", "Visit", "View", "Register for", "Download", etc., when appropriate.
- If the display text already fully describes the destination, you may keep it but improve clarity if needed.
- Avoid redundancy: do not repeat the same phrase twice, and do not include raw tracking querystrings or fragments.
- Do NOT include quotes or brackets. Output ONLY the aria-label text.

Anchor HTML:
{anchor_html}

Target Page Summary (JSON):
{link_summary}
""".strip()

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user",   "content": user_msg},
            ],
            max_tokens=50,
            temperature=0.2,
        )
        aria = resp.choices[0].message.content.strip().strip('"')
        logger.debug(f"Generated aria-label: '{aria}'")
        return aria
    except Exception as e:
        logger.error(f"Failed to generate aria-label: {e}")
        raise


# ── Structured data traversal ─────────────────────────────────────────────────

def iter_text_nodes(node: Any, path: list | None = None) -> Iterator[tuple[dict, str, str, str]]:
    """
    Walk a Cascade page JSON tree and yield every field that might contain HTML.
    Checks both 'text' and 'xhtml' keys.

    Yields: (node_ref, key, path_str, identifier)
    """
    if path is None:
        path = []

    if isinstance(node, dict):
        ntype = node.get("type")
        ident = node.get("identifier")

        for key in ("text", "xhtml"):
            if key in node and isinstance(node[key], str):
                yield (node, key, " / ".join(path + [f"{ntype}:{ident}:{key}"]), ident or "")

        for idx, child in enumerate(node.get("structuredDataNodes") or []):
            yield from iter_text_nodes(child, path + [f"{ntype}:{ident}[{idx}]"])

        for k, v in node.items():
            if k == "structuredDataNodes":
                continue
            if isinstance(v, (dict, list)):
                yield from iter_text_nodes(v, path + [k])

    elif isinstance(node, list):
        for idx, item in enumerate(node):
            yield from iter_text_nodes(item, path + [f"[{idx}]"])


# ── HTML mutation ─────────────────────────────────────────────────────────────

def _resolve_href(href: str) -> str:
    """Resolve a relative href against BASE_URL if set, otherwise return as-is."""
    if not href or is_valid_http_url(href):
        return href
    return urljoin(BASE_URL, href) if BASE_URL else href


def process_html_block(html: str) -> tuple[str, int]:
    """
    Add aria-labels to all unlabeled <a> tags in an HTML block.
    Returns (updated_html, number_added).
    """
    soup = BeautifulSoup(html or "", "html.parser")
    added = 0

    for i, a in enumerate(soup.find_all("a"), start=1):
        href_raw = a.get("href", "")
        display  = a.get_text(strip=True)
        existing = a.get("aria-label")

        logger.debug(f"  Link #{i}: href='{href_raw}', text='{display}', aria='{existing}'")

        if existing or not href_raw or not display:
            continue

        link_ctx = fetch_link_context(_resolve_href(href_raw))

        try:
            aria = generate_aria_label(str(a), link_ctx)
            if aria:
                a["aria-label"] = aria
                logger.info(f"  Added aria-label to {href_raw}: {aria}")
                added += 1
        except Exception as e:
            logger.warning(f"  Error generating aria-label for {href_raw}: {e}")

    return str(soup), added


# ── Public API ────────────────────────────────────────────────────────────────

def process_page(
    site: str,
    path: str,
    output_mode: str = "cascade-dev",
) -> dict:
    """
    Read a Cascade page, add aria-labels to unlabeled <a> tags, and optionally write back.

    Args:
        site:        Cascade site name (e.g. "ualberta", "_dev-abdul")
        path:        Page path (e.g. "/student-services/awards-forms-for-students")
        output_mode: "console" | "report" | "cascade-dev" | "cascade-live"

    Returns:
        Dict: {site, path, nodes_modified, aria_labels_added, server}
    """
    cascade = _cascade_client(output_mode)
    server  = "DEV" if output_mode != "cascade-live" else "LIVE"

    logger.info(f"Reading from Cascade ({server}): site='{site}', path='{path}'")

    try:
        res = cascade.readByPath(site, path)
        if res.status_code != 200:
            logger.error(f"Cascade read failed: {res.status_code} — {res.text[:300]}")
            return {"site": site, "path": path, "nodes_modified": 0, "aria_labels_added": 0, "server": server}
        page_json = res.json()
    except Exception as e:
        logger.error(f"Failed to read page: {e}")
        return {"site": site, "path": path, "nodes_modified": 0, "aria_labels_added": 0, "server": server}

    if _saves_report(output_mode):
        with open(report_path("page_raw.json"), "w", encoding="utf-8") as f:
            json.dump(page_json, f, indent=2, ensure_ascii=False)
        logger.info("Saved: page_raw.json")

    candidates     = list(iter_text_nodes(page_json))
    total_added    = 0
    nodes_modified = 0

    for node_ref, key, path_str, ident in candidates:
        html = node_ref.get(key) or ""
        if "<a" not in html:
            continue

        updated_html, added = process_html_block(html)

        if added > 0:
            # Always update in-memory so the updated JSON reflects reality,
            # even in console/report mode (the JSON just won't be written to Cascade)
            node_ref[key] = updated_html
            nodes_modified += 1
            total_added    += added
            logger.info(f"Node updated: {path_str} (identifier='{ident}') — {added} aria-label(s) added")

    # Console output summary
    print(f"\n=== Aria Labels: {site}{path} — {total_added} label(s) added across {nodes_modified} node(s) ===")

    if _saves_report(output_mode):
        with open(report_path("page_updated.json"), "w", encoding="utf-8") as f:
            json.dump(page_json, f, indent=2, ensure_ascii=False)
        logger.info("Saved: page_updated.json")

    if nodes_modified == 0:
        logger.info(f"No unlabeled links found on {path}")
    elif _writes_to_cascade(output_mode):
        try:
            cascade.editAsset(page_json["asset"])
            logger.info(f"Written to Cascade ({server}): {path} — {nodes_modified} node(s), {total_added} label(s)")
        except Exception as e:
            logger.error(f"Failed to write to Cascade: {e}")
    else:
        logger.info(f"Output mode is '{output_mode}' — skipping Cascade write")

    return {
        "site":             site,
        "path":             path,
        "nodes_modified":   nodes_modified,
        "aria_labels_added": total_added,
        "server":           server,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== Cascade Aria-Label Generator ===")

    site = input("Site name (e.g. _dev-abdul or actm): ").strip()
    path = input("Page path (e.g. /student-services/awards-forms-for-students): ").strip()

    if not site or not path:
        logger.critical("Site name and page path are required.")
        sys.exit(1)

    print("\nOutput mode:")
    print("  1) console      — print results only")
    print("  2) report       — save local JSON files, no Cascade writes")
    print("  3) cascade-dev  — write to DEV server (default)")
    print("  4) cascade-live — write to PRODUCTION server")
    mode_choice = input("Mode (1/2/3/4) [3]: ").strip() or "3"
    output_mode = {"1": "console", "2": "report", "3": "cascade-dev", "4": "cascade-live"}.get(mode_choice, "cascade-dev")

    process_page(site=site, path=path, output_mode=output_mode)
