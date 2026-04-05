# core/table_alt_text.py
#
# Finds all HTML tables on a page and generates a one-sentence alt text for each.
#
# Output modes:
#   "console"      — print generated alt text to stdout only (default)
#   "report"       — also save a JSON report file locally
#   "cascade-dev"  — print + report (no Cascade write; tables don't live in CMS metadata)
#   "cascade-live" — same as cascade-dev (included for API consistency)
#
# Note: Tables are content inside page HTML — there's no Cascade metadata field to write
# alt text back to, so all modes behave like "report" at most. The output_mode parameter
# is still accepted so this function works cleanly in a batch runner alongside the other scripts.
#
# Source modes:
#   "live"    — fetch the live public URL (no Cascade credentials needed)
#   "cascade" — read the page asset directly from Cascade CMS
#
# Batch usage example:
#   from core.table_alt_text import process_page
#
#   process_page(source="cascade", site="campus-community-recreation",
#                path="/our-programs/club-sports/tabletennis", output_mode="report")

from __future__ import annotations

import json
import os
import sys

from dotenv import load_dotenv
from openai import OpenAI

from utils.CascadeClient import CascadeClient
from utils.html_helpers import TableHTMLParser, extract_html_snippets, scrape_tables_from_url
from utils.report_helpers import report_path
from utils.url_helpers import is_valid_http_url, sanitize_url

# ── Config ────────────────────────────────────────────────────────────────────

load_dotenv()

CASCADE_KEY     = os.getenv("CASCADE_API_KEY", "").strip()
CASCADE_DEV_KEY = os.getenv("CASCADE_DEV_API_KEY", CASCADE_KEY).strip()
OPENAI_KEY      = os.getenv("OPENAI_API_KEY", "").strip()

if not OPENAI_KEY:
    raise RuntimeError("OPENAI_API_KEY not found in .env")

client = OpenAI(api_key=OPENAI_KEY)


# ── Output mode helpers ───────────────────────────────────────────────────────

def _saves_report(output_mode: str) -> bool:
    return output_mode in ("report", "cascade-dev", "cascade-live")


# ── LLM ───────────────────────────────────────────────────────────────────────

def generate_table_alt_text(table_matrix: list[list[str]], model: str = "gpt-4.1") -> str:
    """
    Convert a table (list of rows, each a list of cell strings) to Markdown
    and ask the model for a concise one-sentence alt text.
    """
    md = "\n".join("| " + " | ".join(row) + " |" for row in table_matrix)
    prompt = (
        "Generate a concise one-sentence alt text in less than 20 words for this table:\n\n"
        "```\n" + md + "\n```"
    )
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.choices[0].message.content.strip()


# ── Table collection helpers ──────────────────────────────────────────────────

def _get_tables_live(url: str) -> list[list[list[str]]]:
    """Fetch the live page over HTTP and extract all tables from the HTML."""
    print(f"Fetching live page: {url}")
    tables = scrape_tables_from_url(url)
    print(f"Found {len(tables)} table(s) on live page.")
    return tables


def _get_tables_cascade(site: str, path: str, output_mode: str) -> list[list[list[str]]]:
    """Read the Cascade page JSON and extract all tables from its HTML snippets."""
    use_dev = output_mode != "cascade-live"
    api_key = CASCADE_DEV_KEY if use_dev else CASCADE_KEY
    if not api_key:
        raise RuntimeError(f"{'CASCADE_DEV_API_KEY' if use_dev else 'CASCADE_API_KEY'} missing")

    cascade = CascadeClient(api_key, testing=use_dev)
    resp = cascade.readByPath(site, path)
    if resp.status_code != 200:
        raise RuntimeError(f"Cascade read failed: {resp.status_code} — {resp.text[:300]}")

    snippets = extract_html_snippets(resp.json())
    parser = TableHTMLParser()
    for html in snippets:
        parser.feed(html)

    print(f"Found {len(parser.tables)} table(s) in Cascade page JSON.")
    return parser.tables


# ── Public API ────────────────────────────────────────────────────────────────

def process_page(
    source: str = "cascade",
    page_url: str = "",
    site: str = "",
    path: str = "",
    output_mode: str = "console",
) -> list[dict]:
    """
    Find all tables on a page and generate a one-sentence alt text for each.

    Args:
        source:      "live" (fetch public URL) or "cascade" (read page JSON)
        page_url:    Full URL — required when source="live"
        site:        Cascade site name — required when source="cascade"
        path:        Cascade page path — required when source="cascade"
        output_mode: "console" | "report" | "cascade-dev" | "cascade-live"
                     (no Cascade writes happen — tables have no CMS alt text field)

    Returns:
        List of dicts: [{index, table_matrix, alt_text}]
    """
    if source == "live":
        if not page_url:
            raise ValueError("page_url is required when source='live'")
        page_url = sanitize_url(page_url)
        if not is_valid_http_url(page_url):
            raise ValueError(f"Invalid URL: {page_url}")
        tables = _get_tables_live(page_url)
        label  = page_url
    else:
        if not site or not path:
            raise ValueError("site and path are required when source='cascade'")
        tables = _get_tables_cascade(site, path, output_mode)
        label  = f"{site}{path}"

    if not tables:
        print(f"No tables found on {label}.")
        return []

    results = []
    print(f"\n=== Table Alt Text: {label} ===")
    for idx, table in enumerate(tables, start=1):
        if not table:
            print(f"  Table #{idx}: empty, skipping.")
            continue
        alt = generate_table_alt_text(table)
        print(f"  Table #{idx} → {alt}")
        results.append({"index": idx, "table_matrix": table, "alt_text": alt})

    if _saves_report(output_mode):
        safe_label = label.replace("/", "_").replace(":", "").strip("_")
        fname = f"table_alt_{safe_label}.json"
        with open(report_path(fname), "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"Written: {fname}")

    return results


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== Table Alt Text Generator ===")
    print("\nSource:")
    print("  1) Scrape live URL")
    print("  2) Read from Cascade")
    source_choice = input("Source (1/2): ").strip()

    print("\nOutput mode:")
    print("  1) console  — print results only (default)")
    print("  2) report   — also save a local JSON file")
    mode_choice = input("Mode (1/2) [1]: ").strip() or "1"
    output_mode = "report" if mode_choice == "2" else "console"

    if source_choice == "1":
        url = input("Page URL: ").strip()
        if not url:
            print("No URL provided.")
            sys.exit(1)
        process_page(source="live", page_url=url, output_mode=output_mode)

    elif source_choice == "2":
        site = input("Site name (e.g. campus-community-recreation): ").strip()
        path = input("Page path (e.g. /our-programs/club-sports/tabletennis): ").strip()
        if not site or not path:
            print("Site name and page path are both required.")
            sys.exit(1)
        print("\nCascade server:")
        print("  1) DEV (default)")
        print("  2) LIVE")
        server = input("Server (1/2) [1]: ").strip() or "1"
        output_mode = "cascade-live" if server == "2" else "cascade-dev"
        if mode_choice != "2":
            output_mode = "console"  # user chose console, override server flag
        process_page(source="cascade", site=site, path=path, output_mode=output_mode)

    else:
        print("Invalid choice.")
        sys.exit(1)
