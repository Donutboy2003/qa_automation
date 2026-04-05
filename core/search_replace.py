# core/search_replace.py
#
# Find and replace (or remove) text across one or more Cascade CMS pages.
#
# Two public functions:
#   run_search_replace       — operate on a single page
#   run_search_replace_site  — operate on every page in a site (via sitemap)
#
# Output modes:
#   "console"      — print results only, no Cascade writes
#   "report"       — print + save a JSON report to reports/, no Cascade writes
#   "cascade-dev"  — print + report + write to DEV server  (default)
#   "cascade-live" — print + report + write to LIVE server
#
# Batch usage:
#   from core.search_replace import run_search_replace, run_search_replace_site
#
#   run_search_replace("parking-services", "/regulations-citations/index",
#                      search_term="old text", replace_term="new text",
#                      output_mode="cascade-dev")
#
#   run_search_replace_site("parking-services", search_term="old text",
#                           replace_term="new text", output_mode="cascade-dev")

from __future__ import annotations

import json
import os
from typing import Optional

from dotenv import load_dotenv, find_dotenv

from utils.CascadeClient import CascadeClient
from utils.report_helpers import report_path
from utils.sitemap_helpers import fetch_sitemap_paths
from utils.text_helpers import replace_in_page_json, remove_from_page_json

# ── Config ────────────────────────────────────────────────────────────────────

load_dotenv(find_dotenv())
CASCADE_API_KEY     = (os.getenv("CASCADE_API_KEY") or "").strip()
CASCADE_DEV_API_KEY = (os.getenv("CASCADE_DEV_API_KEY") or CASCADE_API_KEY).strip()


# ── Output mode helpers ───────────────────────────────────────────────────────

def _get_client(output_mode: str) -> CascadeClient:
    use_dev = output_mode != "cascade-live"
    api_key = CASCADE_DEV_API_KEY if use_dev else CASCADE_API_KEY
    if not api_key:
        raise RuntimeError(f"{'CASCADE_DEV_API_KEY' if use_dev else 'CASCADE_API_KEY'} missing")
    return CascadeClient(api_key, testing=use_dev)


def _writes_to_cascade(output_mode: str) -> bool:
    return output_mode in ("cascade-dev", "cascade-live")


def _saves_report(output_mode: str) -> bool:
    return output_mode in ("report", "cascade-dev", "cascade-live")


# ── Core operation ────────────────────────────────────────────────────────────

def _apply_and_write(
    cascade:      CascadeClient,
    site:         str,
    path:         str,
    search_term:  str,
    replace_term: Optional[str],
    output_mode:  str,
) -> dict:
    """
    Read one page, apply the replacement, optionally write back.
    Returns a result dict describing what happened.
    """
    result = {
        "site":         site,
        "path":         path,
        "search_term":  search_term,
        "replace_term": replace_term,
        "matches":      0,
        "written":      False,
        "error":        None,
    }

    try:
        resp = cascade.readByPath(site, path)
        if resp.status_code != 200:
            raise RuntimeError(f"Read failed: {resp.status_code} — {resp.text[:200]}")
        page_json = resp.json()
    except Exception as e:
        result["error"] = str(e)
        print(f"  [ERROR] {path}: {e}")
        return result

    # Apply replacement or removal
    if replace_term is None or replace_term == "":
        updated_json, matches = remove_from_page_json(page_json, search_term)
        action = f"removed {matches} field(s) containing '{search_term}'"
    else:
        updated_json, matches = replace_in_page_json(page_json, search_term, replace_term)
        action = f"replaced {matches} occurrence(s) of '{search_term}' → '{replace_term}'"

    result["matches"] = matches

    if matches == 0:
        print(f"  {path}: no matches found")
        return result

    print(f"  {path}: {action}")

    if _writes_to_cascade(output_mode) and matches > 0:
        try:
            edit_resp = cascade.editAsset(updated_json.get("asset") or updated_json)
            edit_result = edit_resp.json()
            if edit_result.get("success"):
                result["written"] = True
                server = "DEV" if output_mode == "cascade-dev" else "LIVE"
                print(f"    → Written to Cascade ({server})")
            else:
                raise RuntimeError(f"Edit failed: {edit_resp.text[:200]}")
        except Exception as e:
            result["error"] = str(e)
            print(f"    → [ERROR] Write failed: {e}")

    return result


# ── Public API ────────────────────────────────────────────────────────────────

def run_search_replace(
    site:         str,
    path:         str,
    search_term:  str,
    replace_term: Optional[str] = None,
    output_mode:  str = "cascade-dev",
) -> dict:
    """
    Find and replace (or remove) text on a single Cascade page.

    Args:
        site:         Cascade site name (e.g. "parking-services")
        path:         Page path (e.g. "/regulations-citations/index")
        search_term:  Text to search for
        replace_term: Text to replace with. Pass None or "" to remove
                      all fields containing the search term instead.
        output_mode:  "console" | "report" | "cascade-dev" | "cascade-live"

    Returns:
        Dict: {site, path, search_term, replace_term, matches, written, error}
    """
    server  = "DEV" if output_mode != "cascade-live" else "LIVE"
    cascade = _get_client(output_mode)

    print(f"\n=== Search & Replace ({server}) ===")
    print(f"  Site:    {site}")
    print(f"  Path:    {path}")
    print(f"  Search:  '{search_term}'")
    print(f"  Replace: '{replace_term}'" if replace_term else f"  Action:  remove fields containing '{search_term}'")

    result = _apply_and_write(cascade, site, path, search_term, replace_term, output_mode)

    if _saves_report(output_mode):
        fname = f"search_replace_{site}_{path.strip('/').replace('/', '_')}.json"
        with open(report_path(fname), "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"  Report: reports/{fname}")

    return result


def run_search_replace_site(
    site:         str,
    search_term:  str,
    replace_term: Optional[str] = None,
    folder_path:  str = "",
    output_mode:  str = "cascade-dev",
) -> list[dict]:
    """
    Find and replace (or remove) text across every page in a site's sitemap.

    Args:
        site:         Cascade site name (e.g. "parking-services")
        search_term:  Text to search for
        replace_term: Text to replace with. Pass None or "" to remove instead.
        folder_path:  Optional path prefix to limit scope (e.g. "/regulations-citations/").
                      Only pages whose path starts with this prefix are processed.
        output_mode:  "console" | "report" | "cascade-dev" | "cascade-live"

    Returns:
        List of result dicts (one per page processed).
    """
    server  = "DEV" if output_mode != "cascade-live" else "LIVE"
    cascade = _get_client(output_mode)

    print(f"\n=== Site-wide Search & Replace ({server}) ===")
    print(f"  Site:    {site}")
    print(f"  Search:  '{search_term}'")
    print(f"  Replace: '{replace_term}'" if replace_term else f"  Action:  remove fields containing '{search_term}'")

    # Fetch all page paths from the sitemap
    try:
        all_paths = fetch_sitemap_paths(site, debug=False)
    except Exception as e:
        raise RuntimeError(f"Could not fetch sitemap for '{site}': {e}")

    # Filter by folder prefix if provided
    if folder_path:
        prefix = "/" + folder_path.strip("/") + "/"
        paths  = [p for p in all_paths if p.startswith(prefix) or p == prefix.rstrip("/")]
        print(f"  Folder:  {prefix} ({len(paths)} of {len(all_paths)} pages)")
    else:
        paths = all_paths
        print(f"  Pages:   {len(paths)}")

    results: list[dict] = []

    for idx, path in enumerate(paths, 1):
        print(f"\n[{idx}/{len(paths)}] {path}")
        result = _apply_and_write(cascade, site, path, search_term, replace_term, output_mode)
        results.append(result)

    # Summary
    matched = sum(1 for r in results if r["matches"] > 0)
    written = sum(1 for r in results if r["written"])
    errors  = sum(1 for r in results if r["error"])
    print(f"\nDone — {matched} page(s) had matches, {written} written, {errors} error(s)")

    if _saves_report(output_mode):
        safe_search = search_term[:30].replace(" ", "_").replace("/", "_")
        fname = f"search_replace_site_{site}_{safe_search}.json"
        with open(report_path(fname), "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"Report: reports/{fname}")

    return results
