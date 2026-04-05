# core/batch_analyzer.py
#
# Run page analysis across multiple pages from various input sources.
#
# Input sources:
#   "url"      — a single page URL
#   "csv"      — a CSV file; you specify which column holds URLs
#   "sitemap"  — all pages in a site's sitemap
#   "cascade"  — all pages in a Cascade site (reads JSON directly, no browser)
#
# Content sources (how to get text for each page):
#   "live"     — fetch and parse the live public URL
#   "cascade"  — read the page JSON from Cascade API
#
# Output modes:
#   "console"      — print results only
#   "report"       — print + write CSV to reports/
#   "cascade-dev"  — same as report (this tool reads but never writes to Cascade)
#   "cascade-live" — same as report
#
# Batch usage:
#   from core.batch_analyzer import run_batch, SourceConfig
#   from core.page_analyzer import AnalysisConfig
#
#   source = SourceConfig(source_type="sitemap", site="parking-services", content_source="live")
#   config = AnalysisConfig(include_summary=True, include_classification=True,
#                           classification_prompt="Resources for students with disabilities")
#   run_batch(source, config, output_mode="report")

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv, find_dotenv

from utils.CascadeClient import CascadeClient
from utils.page_content_extractor import extract_text
from utils.report_helpers import report_path
from utils.sitemap_helpers import fetch_sitemap_paths
from core.page_analyzer import AnalysisConfig, analyze_page

load_dotenv(find_dotenv())
CASCADE_API_KEY     = (os.getenv("CASCADE_API_KEY") or "").strip()
CASCADE_DEV_API_KEY = (os.getenv("CASCADE_DEV_API_KEY") or CASCADE_API_KEY).strip()


# ── Source config ─────────────────────────────────────────────────────────────

@dataclass
class SourceConfig:
    """
    Describes where to get the list of pages and how to fetch their content.
    """

    source_type:    str = "url"
    """
    Where the list of page URLs comes from:
      "url"      — single URL, set url field
      "csv"      — CSV file, set csv_path and url_column
      "sitemap"  — all pages in a site's sitemap, set site
      "cascade"  — all pages from Cascade sitemap, set site; content fetched via Cascade API
    """

    # For source_type="url"
    url: str = ""

    # For source_type="csv"
    csv_path:   str = ""
    url_column: str = "url"

    # For source_type="sitemap" or "cascade"
    site:        str = ""
    folder_path: str = ""
    """Optional path prefix to limit which pages are processed, e.g. '/about/'"""

    content_source: str = "live"
    """
    How to get the text content for each page:
      "live"    — fetch the public URL (default)
      "cascade" — read via Cascade API (requires CASCADE_API_KEY, site must be set)
    """

    use_dev: bool = True
    """
    When content_source="cascade": True = DEV server, False = LIVE server.
    """

    # Optional: limit how many pages to process (useful for testing)
    limit: Optional[int] = None


# ── URL resolution ────────────────────────────────────────────────────────────

def _resolve_urls(source: SourceConfig) -> list[str]:
    """Return the flat list of page URLs to process."""

    if source.source_type == "url":
        if not source.url:
            raise ValueError("source.url is required when source_type='url'")
        return [source.url]

    if source.source_type == "csv":
        if not source.csv_path:
            raise ValueError("source.csv_path is required when source_type='csv'")
        import pandas as pd
        df = pd.read_csv(source.csv_path)
        if source.url_column not in df.columns:
            cols = list(df.columns)
            raise ValueError(f"Column '{source.url_column}' not found. Available: {cols}")
        urls = df[source.url_column].dropna().astype(str).tolist()
        return [u.strip() for u in urls if u.strip()]

    if source.source_type in ("sitemap", "cascade"):
        if not source.site:
            raise ValueError("source.site is required when source_type='sitemap' or 'cascade'")
        paths = fetch_sitemap_paths(source.site, debug=False)
        # Filter by folder prefix if set
        if source.folder_path:
            prefix = "/" + source.folder_path.strip("/") + "/"
            paths = [p for p in paths if p.startswith(prefix)]
        # Build live URLs
        site_root = f"https://www.ualberta.ca/en/{source.site.strip('/')}"
        return [f"{site_root}{p}" for p in paths]

    raise ValueError(f"Unknown source_type: {source.source_type!r}")


# ── Content fetching ──────────────────────────────────────────────────────────

def _get_cascade_client(source: SourceConfig) -> CascadeClient:
    api_key = CASCADE_DEV_API_KEY if source.use_dev else CASCADE_API_KEY
    if not api_key:
        raise RuntimeError("CASCADE_API_KEY missing — add it to your .env file")
    return CascadeClient(api_key, testing=source.use_dev)


def _fetch_content(url: str, source: SourceConfig, cascade: Optional[CascadeClient] = None) -> dict[str, str]:
    """Fetch page text using the configured content source."""
    if source.content_source == "cascade":
        if not cascade:
            raise RuntimeError("CascadeClient required for cascade content source")
        if not source.site:
            raise RuntimeError("source.site is required for cascade content source")

        # Derive the Cascade path from the live URL
        # Live URL: https://www.ualberta.ca/en/{site}/{path}
        # Cascade path: /{path}
        site_root = f"/en/{source.site.strip('/')}"
        from urllib.parse import urlparse
        parsed_path = urlparse(url).path
        if parsed_path.startswith(site_root):
            cascade_path = parsed_path[len(site_root):] or "/index"
        else:
            cascade_path = parsed_path

        # Strip .html if present
        if cascade_path.endswith(".html"):
            cascade_path = cascade_path[:-5]

        try:
            resp = cascade.readByPath(source.site, cascade_path)
            if resp.status_code != 200:
                return {"url": url, "title": "", "text": "", "error": f"Cascade read failed: {resp.status_code}"}
            return extract_text(source="cascade", url=url, page_json=resp.json())
        except Exception as e:
            return {"url": url, "title": "", "text": "", "error": str(e)}
    else:
        return extract_text(source="live", url=url)


# ── Output ────────────────────────────────────────────────────────────────────

def _result_to_csv_row(result: dict[str, Any], config: AnalysisConfig) -> dict[str, Any]:
    """Flatten a result dict to a CSV-friendly row."""
    row: dict[str, Any] = {
        "url":   result.get("url", ""),
        "title": result.get("title", ""),
        "error": result.get("error", ""),
    }

    if config.include_summary:
        row["summary"] = result.get("summary", "")
    if config.include_description:
        row["description"] = result.get("description", "")
    if config.include_cta:
        row["call_to_action"] = result.get("call_to_action", "")
    if config.include_theme:
        row["main_theme"] = result.get("main_theme", "")
    if config.include_audience:
        val = result.get("target_audience", [])
        row["target_audience"] = ", ".join(val) if isinstance(val, list) else str(val)
    if config.include_keywords:
        val = result.get("keywords", [])
        row["keywords"] = ", ".join(val) if isinstance(val, list) else str(val)
    if config.include_meta_tags:
        val = result.get("meta_tags", [])
        row["meta_tags"] = ", ".join(val) if isinstance(val, list) else str(val)
    if config.include_classification:
        row["relevance_score"]      = result.get("relevance_score", "")
        if config.include_classification_reason:
            row["relevance_reason"] = result.get("relevance_reason", "")
        if config.include_classification_confidence:
            row["relevance_confidence"] = result.get("relevance_confidence", "")

    return row


# ── Public API ────────────────────────────────────────────────────────────────

def run_batch(
    source:      SourceConfig,
    config:      AnalysisConfig | None = None,
    output_mode: str = "report",
) -> list[dict[str, Any]]:
    """
    Analyze multiple pages and collect results.

    Args:
        source:      SourceConfig — where pages come from and how to fetch content
        config:      AnalysisConfig — what the LLM should produce per page
        output_mode: "console" | "report" | "cascade-dev" | "cascade-live"

    Returns:
        List of result dicts (one per page).
    """
    if config is None:
        config = AnalysisConfig(include_summary=True)

    saves_report = output_mode in ("report", "cascade-dev", "cascade-live")

    # Resolve URL list
    print("Resolving page list...")
    try:
        urls = _resolve_urls(source)
    except Exception as e:
        raise RuntimeError(f"Failed to resolve URLs: {e}")

    if source.limit:
        urls = urls[:source.limit]

    print(f"Pages to process: {len(urls)}")
    if config.include_classification:
        print(f"Classification prompt: \"{config.classification_prompt}\"")

    # Set up Cascade client if needed
    cascade = _get_cascade_client(source) if source.content_source == "cascade" else None

    # Prepare report file
    report_file = None
    csv_writer  = None
    fieldnames  = None

    safe_label = (source.site or "batch").replace("/", "_")
    report_filename = f"page_analysis_{safe_label}.csv"

    all_results: list[dict[str, Any]] = []

    for idx, url in enumerate(urls, 1):
        print(f"\n[{idx}/{len(urls)}] {url}")

        # Step 1: fetch content
        content = _fetch_content(url, source, cascade)
        if content.get("error"):
            print(f"  [WARN] Content fetch failed: {content['error']}")

        # Step 2: analyze
        result = analyze_page(
            text=content.get("text", ""),
            url=url,
            config=config,
        )
        result["title"] = result.get("title") or content.get("title", "")

        # Log summary to console
        if result.get("error"):
            print(f"  [ERROR] {result['error']}")
        else:
            if config.include_summary:
                print(f"  Summary: {str(result.get('summary', ''))[:120]}")
            if config.include_classification:
                print(
                    f"  Relevance: {result.get('relevance_score')}/{config.classification_scale}"
                    + (f" (conf={result.get('relevance_confidence', '?')})" if config.include_classification_confidence else "")
                    + (f" — {str(result.get('relevance_reason', ''))[:100]}" if config.include_classification_reason else "")
                )

        all_results.append(result)

        # Write to CSV progressively (open on first row)
        if saves_report:
            row = _result_to_csv_row(result, config)
            if csv_writer is None:
                fieldnames  = list(row.keys())
                report_file = open(report_path(report_filename), "w", newline="", encoding="utf-8")
                csv_writer  = csv.DictWriter(report_file, fieldnames=fieldnames)
                csv_writer.writeheader()
            csv_writer.writerow(row)
            report_file.flush()

    if report_file:
        report_file.close()
        print(f"\nReport written to: reports/{report_filename}")

    print(f"\nDone — {len(all_results)} page(s) analyzed.")
    errors = sum(1 for r in all_results if r.get("error"))
    if errors:
        print(f"  {errors} page(s) had errors.")

    return all_results
