# core/decorative_alt_updater.py
#
# Reads a Siteimprove-style CSV listing pages with "Image missing a text alternative",
# then for each page: reads the Cascade page JSON, inserts alt="" on any <img>
# that has no alt attribute, and writes it back.
#
# alt="" marks an image as decorative — screen readers will skip it entirely.
# This is different from generating descriptive alt text.
#
# Output modes:
#   "console"      — parse the CSV and print what would change, no writes
#   "report"       — print + save a CSV report, no Cascade writes
#   "cascade-dev"  — print + report + write to DEV server (default)
#   "cascade-live" — print + report + write to LIVE server
#
# Batch usage:
#   from core.decorative_alt_updater import run_decorative_update
#   run_decorative_update("siteimprove_export.csv", output_mode="cascade-dev", limit=10)
#
# Run via CLI: python -m core.decorative_alt_updater input.csv [--output-mode cascade-dev] [--limit N]

from __future__ import annotations

import argparse
import csv
import os
import sys
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv, find_dotenv

from utils.CascadeClient import CascadeClient
from utils.report_helpers import report_path
from utils.html_helpers import apply_decorative_alts

# ── Config ────────────────────────────────────────────────────────────────────

load_dotenv(find_dotenv())
CASCADE_API_KEY     = (os.getenv("CASCADE_API_KEY") or "").strip()
CASCADE_DEV_API_KEY = (os.getenv("CASCADE_DEV_API_KEY") or CASCADE_API_KEY).strip()

DEBUG = True


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class AuditRow:
    title:       str
    url:         str
    occurrences: int
    page_views:  Optional[int] = None


@dataclass
class ProcessResult:
    url:                  str
    expected_occurrences: int
    updated_count:        int
    matched:              bool
    read_status:          Optional[int]
    write_status:         Optional[int]
    error:                Optional[str]
    img_missing_before:   Optional[int]
    img_missing_after:    Optional[int]


# ── CSV parsing ───────────────────────────────────────────────────────────────

def parse_audit_csv(csv_path: str) -> list[AuditRow]:
    """
    Parse a Siteimprove audit export CSV.

    These files have a metadata preamble before the real header row, so we
    scan forward until we find the line starting with "Title"/"URL"/etc.
    Handles both tab-delimited UTF-16 (Siteimprove default) and regular UTF-8.
    """
    for encoding in ("utf-16", "utf-8-sig", "utf-8"):
        try:
            with open(csv_path, "r", encoding=encoding) as f:
                lines = f.readlines()
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    else:
        raise RuntimeError(f"Could not decode {csv_path} — tried utf-16, utf-8-sig, utf-8")

    # Find the real header row — scan for a line that has both URL and Occurrences
    header_idx = None
    for i, line in enumerate(lines):
        clean = line.strip()
        if clean.startswith('"Title"') and "URL" in clean:
            header_idx = i
            break
        if "URL" in clean and "Occurrences" in clean:
            header_idx = i
            break

    if header_idx is None:
        raise RuntimeError(
            f"Could not find a header row with 'URL'/'Occurrences' in {csv_path}.\n"
            "Expected a Siteimprove-style tab-delimited export."
        )

    # Detect delimiter (Siteimprove uses tab, but some exports use comma)
    delimiter = "\t" if "\t" in lines[header_idx] else ","

    reader = csv.DictReader(lines[header_idx:], delimiter=delimiter, quotechar='"')
    rows: list[AuditRow] = []
    for r in reader:
        if not r:
            continue
        url = (r.get("URL") or "").strip('" ')
        if not url:
            continue
        title   = (r.get("Title") or "").strip('" ')
        occ_raw = r.get("Occurrences") or r.get("Occurences") or r.get("Occurence") or ""
        pv_raw  = r.get("Page views") or r.get("Page Views") or ""
        try:
            occurrences = int(str(occ_raw).strip().replace(",", "")) if str(occ_raw).strip() else 0
        except ValueError:
            occurrences = 0
        try:
            page_views = int(str(pv_raw).strip().replace(",", "")) if str(pv_raw).strip() else None
        except ValueError:
            page_views = None
        rows.append(AuditRow(title=title, url=url, occurrences=occurrences, page_views=page_views))

    return rows


# ── Output mode helpers ───────────────────────────────────────────────────────

def _get_cascade_client(output_mode: str) -> CascadeClient:
    use_dev = output_mode != "cascade-live"
    api_key = CASCADE_DEV_API_KEY if use_dev else CASCADE_API_KEY
    if not api_key:
        raise RuntimeError(f"{'CASCADE_DEV_API_KEY' if use_dev else 'CASCADE_API_KEY'} missing")
    return CascadeClient(apiKey=api_key, testing=use_dev)


def _writes_to_cascade(output_mode: str) -> bool:
    return output_mode in ("cascade-dev", "cascade-live")


def _saves_report(output_mode: str) -> bool:
    return output_mode in ("report", "cascade-dev", "cascade-live")


# ── Public API ────────────────────────────────────────────────────────────────

def run_decorative_update(
    csv_path:    str,
    output_mode: str = "cascade-dev",
    limit:       Optional[int] = None,
    out_file:    str = "decorative-alt-report.csv",
) -> list[ProcessResult]:
    """
    For each page in a Siteimprove audit CSV, add alt="" to any <img> missing
    an alt attribute, then write the updated page back to Cascade.

    Args:
        csv_path:    Path to the Siteimprove export CSV
        output_mode: "console" | "report" | "cascade-dev" | "cascade-live"
        limit:       If set, process only the first N rows (useful for testing)
        out_file:    Filename for the output report CSV

    Returns:
        List of ProcessResult objects (one per page processed).
    """
    rows = parse_audit_csv(csv_path)
    if limit is not None:
        rows = rows[:limit]

    print(f"[INFO] {len(rows)} row(s) from {os.path.basename(csv_path)}")
    server = "DEV" if output_mode != "cascade-live" else "LIVE"

    cascade  = _get_cascade_client(output_mode) if _writes_to_cascade(output_mode) else None
    results: list[ProcessResult] = []

    for i, row in enumerate(rows, 1):
        print(f"\n[{i}/{len(rows)}] {row.url}")

        if not _writes_to_cascade(output_mode):
            # Console/report mode — nothing to read or write
            print(f"  Would process {row.occurrences} missing alt(s) — skipping (output_mode='{output_mode}')")
            results.append(ProcessResult(
                url=row.url, expected_occurrences=row.occurrences,
                updated_count=0, matched=False,
                read_status=None, write_status=None, error=None,
                img_missing_before=None, img_missing_after=None,
            ))
            continue

        read_status   = None
        write_status  = None
        error_msg     = None
        changes_applied = 0
        missing_before  = None

        # Step 1: Read the page
        try:
            resp = cascade.read(row.url)
            read_status = resp.status_code
            if resp.status_code != 200:
                raise RuntimeError(f"READ {resp.status_code}: {resp.text[:200]}")
            data       = resp.json()
            asset_json = data.get("asset") or data
        except Exception as e:
            error_msg = str(e)
            print(f"  [ERROR] Read failed: {error_msg}")
            results.append(ProcessResult(
                url=row.url, expected_occurrences=row.occurrences,
                updated_count=0, matched=False,
                read_status=read_status, write_status=None, error=error_msg,
                img_missing_before=None, img_missing_after=None,
            ))
            continue

        # Step 2: Apply alt="" to all <img> missing an alt
        try:
            updated_json, missing_before, changes_applied = apply_decorative_alts(asset_json)
        except Exception as e:
            results.append(ProcessResult(
                url=row.url, expected_occurrences=row.occurrences,
                updated_count=0, matched=False,
                read_status=read_status, write_status=None, error=f"Apply error: {e}",
                img_missing_before=None, img_missing_after=None,
            ))
            continue

        print(f"  Missing before: {missing_before} | Applied: {changes_applied}")

        # Step 3: Write back if anything changed
        if changes_applied > 0:
            try:
                resp_w = cascade.editAsset(updated_json)
                write_status = resp_w.status_code
                if resp_w.status_code != 200:
                    error_msg = f"WRITE {resp_w.status_code}: {resp_w.text[:200]}"
                    print(f"  [ERROR] Write failed: {error_msg}")
                else:
                    print(f"  Written to Cascade ({server}).")
            except Exception as e:
                error_msg = f"Write error: {e}"
                print(f"  [ERROR] {error_msg}")
        else:
            print("  No <img> tags missing alt — nothing to write.")

        matched = (changes_applied == row.occurrences) if row.occurrences is not None else False
        results.append(ProcessResult(
            url=row.url, expected_occurrences=row.occurrences,
            updated_count=changes_applied, matched=matched,
            read_status=read_status, write_status=write_status, error=error_msg,
            img_missing_before=missing_before,
            img_missing_after=0 if missing_before is not None else None,
        ))

    # Write report
    if _saves_report(output_mode):
        fieldnames = [
            "url", "expected_occurrences", "updated_count", "matched",
            "read_status", "write_status", "error",
            "img_missing_before", "img_missing_after",
        ]
        with open(report_path(out_file), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in results:
                w.writerow({
                    "url":                  r.url,
                    "expected_occurrences": r.expected_occurrences,
                    "updated_count":        r.updated_count,
                    "matched":              r.matched,
                    "read_status":          r.read_status,
                    "write_status":         r.write_status,
                    "error":                r.error,
                    "img_missing_before":   r.img_missing_before,
                    "img_missing_after":    r.img_missing_after,
                })
        print(f"\nReport written to: {out_file}")

    total    = len(results)
    ok       = sum(1 for r in results if r.matched and not r.error)
    mismatch = sum(1 for r in results if not r.matched and not r.error)
    errors   = sum(1 for r in results if r.error)
    print(f"\nDone — matched: {ok}, mismatched: {mismatch}, errors: {errors}, total: {total}")

    return results


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Mark images missing alt text as decorative (alt='') in Cascade."
    )
    parser.add_argument("csv_path", help="Path to the Siteimprove audit CSV export.")
    parser.add_argument(
        "--output-mode",
        default="cascade-dev",
        choices=["console", "report", "cascade-dev", "cascade-live"],
        help="console=print only, report=save CSV, cascade-dev/live=write to Cascade (default: cascade-dev)",
    )
    parser.add_argument("--limit", type=int, default=None, help="Process only the first N rows.")
    parser.add_argument("--out",   default="decorative-alt-report.csv", help="Output report filename.")
    args = parser.parse_args()

    run_decorative_update(
        csv_path=args.csv_path,
        output_mode=args.output_mode,
        limit=args.limit,
        out_file=args.out,
    )
