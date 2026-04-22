# core/batch_analyzer.py
#
# Run page analysis across multiple pages from various input sources.
#
# Speed design:
#   - Page fetching is sequential (Playwright sync API is single-threaded)
#   - LLM analysis is parallelised with ThreadPoolExecutor(max_workers=5)
#     because each analyze_page() call is pure network I/O with no shared state
#   - Results are written to CSV in URL order regardless of completion order

from __future__ import annotations

import csv
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Optional

from dotenv import load_dotenv, find_dotenv

from utils.CascadeClient import CascadeClient
from utils.browser_helpers import BrowserSession
from utils.page_content_extractor import extract_text
from utils.report_helpers import report_path
from utils.sitemap_helpers import fetch_sitemap_paths
from core.page_analyzer import AnalysisConfig, analyze_page

load_dotenv(find_dotenv())
CASCADE_API_KEY     = (os.getenv("CASCADE_API_KEY") or "").strip()
CASCADE_DEV_API_KEY = (os.getenv("CASCADE_DEV_API_KEY") or CASCADE_API_KEY).strip()

LLM_WORKERS = 5   # parallel LLM calls — safe because analyze_page() is stateless I/O


# ── Source config ─────────────────────────────────────────────────────────────

@dataclass
class SourceConfig:
    source_type:    str           = "url"
    url:            str           = ""
    csv_path:       str           = ""
    url_column:     str           = "url"
    site:           str           = ""
    folder_path:    str           = ""
    content_source: str           = "live"
    use_dev:        bool          = True
    limit:          Optional[int] = None


# ── URL resolution ────────────────────────────────────────────────────────────

def _resolve_urls(source: SourceConfig, browser: Optional[BrowserSession] = None) -> list[str]:
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
            raise ValueError(f"Column '{source.url_column}' not found. Available: {list(df.columns)}")
        urls = df[source.url_column].dropna().astype(str).tolist()
        return [u.strip() for u in urls if u.strip()]

    if source.source_type in ("sitemap", "cascade"):
        if not source.site:
            raise ValueError("source.site is required when source_type='sitemap' or 'cascade'")
        paths = fetch_sitemap_paths(source.site, debug=False, browser=browser)
        if source.folder_path:
            prefix = "/" + source.folder_path.strip("/") + "/"
            paths = [p for p in paths if p.startswith(prefix)]
        site_root = f"https://www.ualberta.ca/en/{source.site.strip('/')}"
        return [f"{site_root}{p}" for p in paths]

    raise ValueError(f"Unknown source_type: {source.source_type!r}")


# ── Content fetching ──────────────────────────────────────────────────────────

def _get_cascade_client(source: SourceConfig) -> CascadeClient:
    api_key = CASCADE_DEV_API_KEY if source.use_dev else CASCADE_API_KEY
    if not api_key:
        raise RuntimeError("CASCADE_API_KEY missing — add it to your .env file")
    return CascadeClient(api_key, testing=source.use_dev)


def _fetch_content(
    url:     str,
    source:  SourceConfig,
    cascade: Optional[CascadeClient]  = None,
    browser: Optional[BrowserSession] = None,
) -> dict[str, str]:
    if source.content_source == "cascade":
        if not cascade:
            raise RuntimeError("CascadeClient required for cascade content source")
        site_root = f"/en/{source.site.strip('/')}"
        from urllib.parse import urlparse
        parsed_path  = urlparse(url).path
        cascade_path = (parsed_path[len(site_root):] or "/index") if parsed_path.startswith(site_root) else parsed_path
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
        return extract_text(source="live", url=url, browser=browser)


# ── Output ────────────────────────────────────────────────────────────────────

def _result_to_csv_row(result: dict[str, Any], config: AnalysisConfig) -> dict[str, Any]:
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
        row["relevance_score"] = result.get("relevance_score", "")
        if config.include_classification_reason:
            row["relevance_reason"] = result.get("relevance_reason", "")
        if config.include_classification_confidence:
            row["relevance_confidence"] = result.get("relevance_confidence", "")
    if config.include_audience_classification:
        row["audience_classification"]     = result.get("audience_classification", "")
        row["audience_confidence_score"]   = result.get("audience_confidence_score", "")
        indicators = result.get("audience_primary_indicators", [])
        row["audience_primary_indicators"] = (
            " | ".join(indicators) if isinstance(indicators, list) else str(indicators)
        )
        row["audience_reasoning"] = result.get("audience_reasoning", "")
    return row


def _log_result(result: dict[str, Any], config: AnalysisConfig, idx: int, total: int) -> None:
    """Print a single result line to the console."""
    prefix = f"[{idx}/{total}]"
    if result.get("error"):
        print(f"{prefix} [ERROR] {result['url']} — {result['error']}")
        return
    print(f"{prefix} {result['url']}")
    if config.include_summary:
        print(f"  Summary: {str(result.get('summary', ''))[:120]}")
    if config.include_classification:
        print(
            f"  Relevance: {result.get('relevance_score')}/{config.classification_scale}"
            + (f" (conf={result.get('relevance_confidence', '?')})" if config.include_classification_confidence else "")
            + (f" — {str(result.get('relevance_reason', ''))[:100]}" if config.include_classification_reason else "")
        )
    if config.include_audience_classification:
        ind = result.get("audience_primary_indicators", [])
        ind_str = " | ".join(ind) if isinstance(ind, list) else str(ind)
        print(
            f"  Audience: {result.get('audience_classification', '?')}"
            f" (conf={result.get('audience_confidence_score', '?')})"
            f" [{ind_str}]"
            f"\n    Reason: {str(result.get('audience_reasoning', ''))[:120]}"
        )


# ── Public API ────────────────────────────────────────────────────────────────

def run_batch(
    source:      SourceConfig,
    config:      AnalysisConfig | None = None,
    output_mode: str = "report",
) -> list[dict[str, Any]]:
    """
    Analyze multiple pages and collect results.

    Pipeline:
      1. Open one BrowserSession for the entire batch (keeps Akamai cookies warm)
      2. Fetch page content sequentially (browser is single-threaded)
      3. Skip pages with no content immediately — don't waste an LLM call
      4. Submit LLM analysis jobs to a ThreadPoolExecutor(max_workers=5)
      5. Collect results in URL order and write CSV progressively

    Args:
        source:      SourceConfig — where pages come from and how to fetch content
        config:      AnalysisConfig — what the LLM should produce per page
        output_mode: "console" | "report" | "cascade-dev" | "cascade-live"

    Returns:
        List of result dicts in the original URL order.
    """
    if config is None:
        config = AnalysisConfig(include_summary=True)

    saves_report = output_mode in ("report", "cascade-dev", "cascade-live")
    uses_browser = source.content_source == "live"

    def _run(browser: Optional[BrowserSession]) -> list[dict[str, Any]]:
        # ── Step 1: resolve URLs ───────────────────────────────────────────────
        print("Resolving page list...")
        try:
            urls = _resolve_urls(source, browser=browser)
        except Exception as e:
            raise RuntimeError(f"Failed to resolve URLs: {e}")

        if source.limit:
            urls = urls[:source.limit]

        total = len(urls)
        print(f"Pages to process: {total}")
        if config.include_classification:
            print(f"Classification prompt: \"{config.classification_prompt}\"")
        if config.include_audience_classification:
            print("Audience classification: enabled (Internal / External / Mixed / Unclassified)")
        print(f"LLM workers: {LLM_WORKERS}")

        cascade = _get_cascade_client(source) if source.content_source == "cascade" else None

        # ── Step 2: fetch all content sequentially ────────────────────────────
        # We collect (url, content_dict) pairs so the LLM pool can start
        # immediately once the first few pages are ready.  To keep memory sane
        # and allow progressive CSV writes we process in chunks.
        CHUNK = LLM_WORKERS * 4   # fetch this many pages before draining the LLM pool

        report_file = None
        csv_writer  = None
        safe_label  = (source.site or "batch").replace("/", "_")
        report_filename = f"page_analysis_{safe_label}.csv"

        all_results: list[dict[str, Any]] = [{}] * total  # pre-size for in-order assembly

        print(f"\nFetching pages and running LLM analysis (chunk size {CHUNK})...\n")

        for chunk_start in range(0, total, CHUNK):
            chunk_urls = urls[chunk_start: chunk_start + CHUNK]

            # Fetch chunk sequentially
            chunk_contents: list[tuple[int, str, dict]] = []
            for local_i, url in enumerate(chunk_urls):
                global_i = chunk_start + local_i
                print(f"[fetch {global_i + 1}/{total}] {url}")
                content = _fetch_content(url, source, cascade=cascade, browser=browser)
                if content.get("error"):
                    print(f"  [WARN] {content['error']}")
                chunk_contents.append((global_i, url, content))

            # Analyze chunk in parallel
            def _analyze(args: tuple[int, str, dict]) -> tuple[int, dict[str, Any]]:
                idx, url, content = args
                if not content.get("text", "").strip():
                    # No usable content — skip LLM call entirely
                    result: dict[str, Any] = {
                        "url":   url,
                        "title": content.get("title", ""),
                        "error": content.get("error") or "No content to analyze",
                    }
                else:
                    result = analyze_page(text=content["text"], url=url, config=config)
                    result["title"] = result.get("title") or content.get("title", "")
                return idx, result

            with ThreadPoolExecutor(max_workers=LLM_WORKERS) as pool:
                futures = {pool.submit(_analyze, item): item[0] for item in chunk_contents}
                for future in as_completed(futures):
                    idx, result = future.result()
                    all_results[idx] = result

            # Write chunk to CSV in URL order (not completion order)
            for local_i in range(len(chunk_urls)):
                global_i = chunk_start + local_i
                result   = all_results[global_i]
                _log_result(result, config, global_i + 1, total)

                if saves_report:
                    row = _result_to_csv_row(result, config)
                    if csv_writer is None:
                        report_file = open(report_path(report_filename), "w", newline="", encoding="utf-8")
                        csv_writer  = csv.DictWriter(report_file, fieldnames=list(row.keys()))
                        csv_writer.writeheader()
                    csv_writer.writerow(row)
                    report_file.flush()

        if report_file:
            report_file.close()
            print(f"\nReport written to: reports/{report_filename}")

        print(f"\nDone — {total} page(s) analyzed.")
        errors = sum(1 for r in all_results if r.get("error"))
        if errors:
            print(f"  {errors} page(s) had errors.")

        return all_results

    if uses_browser:
        with BrowserSession() as browser:
            return _run(browser)
    else:
        return _run(browser=None)