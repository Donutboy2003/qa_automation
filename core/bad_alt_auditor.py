# core/bad_alt_auditor.py
#
# Scrapes a UAlberta site's sitemap, visits every page, and flags images
# with missing or poor-quality alt text. Generates LLM suggestions and
# writes a CSV report.
#
# No Cascade API calls — this is a read-only audit tool that works on the
# live public site.
#
# Batch usage:
#   from core.bad_alt_auditor import run_audit_site
#   run_audit_site(site="admissions-programs", output_mode="report")
#
# Run interactively: python -m core.bad_alt_auditor

from __future__ import annotations

import csv
import os
import sys
import traceback
from urllib.parse import urlparse

from dotenv import load_dotenv, find_dotenv
from openai import OpenAI
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from utils.alt_quality import check_alt_quality
from utils.report_helpers import report_path
from utils.http_helpers import SESSION, REQUEST_TIMEOUT, image_exists, within_llm_size_budget
from utils.image_filters import should_skip_image
from utils.image_scraper import route_blocker, extract_images_fast
from utils.sitemap_helpers import fetch_sitemap_paths
from utils.url_helpers import absolutize_src_url

# ── Config ────────────────────────────────────────────────────────────────────

load_dotenv(find_dotenv())
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_PROJECT = (os.getenv("OPENAI_PROJECT") or "").strip()

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY missing — add it to your .env file")

oai_client = OpenAI(api_key=OPENAI_API_KEY, project=(OPENAI_PROJECT or None))

DEFAULT_MODEL = "gpt-4.1-mini"
DEBUG         = True

# Short wait after DOMContentLoaded — we block heavy assets so pages load fast
EXTRA_WAIT_MS = 100


# ── LLM suggestion ────────────────────────────────────────────────────────────

# Module-level cache — same image URL is only sent to OpenAI once per process
_alt_cache: dict[str, str] = {}


def suggest_alt_for_url(image_url: str, src_name: str = "", model: str = DEFAULT_MODEL) -> str:
    """
    Ask the LLM for a short, descriptive alt text for the given image URL.
    Caches by URL so repeated calls for the same image are free.

    Args:
        image_url: Absolute URL of the image to describe
        src_name:  Filename hint passed to the model (e.g. "banner-hero.jpg")
        model:     OpenAI model to use (must support vision)

    Returns:
        Alt text string, or empty string on failure.
    """
    if image_url in _alt_cache:
        return _alt_cache[image_url]

    hint = f"\n- The file name is: {src_name}" if src_name else ""
    prompt = (
        "You are an accessibility helper. Suggest concise, descriptive alt text for the image.\n"
        "Rules:\n"
        "- 4–10 words preferred, objective, specific.\n"
        "- No 'Image of', no emojis, no trailing period.\n"
        "- 60 characters max."
        f"{hint}\n"
        "Return ONLY the alt text string."
    )
    try:
        resp = oai_client.responses.create(
            model=model,
            input=[{
                "role": "user",
                "content": [
                    {"type": "input_text",  "text": prompt},
                    {"type": "input_image", "image_url": image_url},
                ],
            }],
            temperature=0.2,
        )
        text = (getattr(resp, "output_text", "") or "").strip().strip('"')
        if text.endswith("."):
            text = text[:-1].strip()
        if len(text) > 140:
            text = text[:140].rstrip()
        _alt_cache[image_url] = text
        return text
    except Exception as e:
        if DEBUG:
            print(f"[LLM] Suggestion failed for {image_url}: {e}", file=sys.stderr)
        _alt_cache[image_url] = ""
        return ""


# ── Public API ────────────────────────────────────────────────────────────────

def run_audit_site(
    site:        str,
    output_mode: str = "report",
    model:       str = DEFAULT_MODEL,
) -> list[dict]:
    """
    Scrape every page in a site's sitemap and flag images with bad or missing alt text.
    Writes a CSV report file if output_mode includes report-saving.

    Args:
        site:        UAlberta site key (e.g. "admissions-programs", "arts")
        output_mode: "console"      — print findings only
                     "report"       — print + write CSV (default)
                     "cascade-dev"  — same as report (no Cascade writes for this tool)
                     "cascade-live" — same as report
        model:       OpenAI model for alt text suggestions

    Returns:
        List of finding dicts: {page_url, image_url, current_alt, suggested_alt, reason}
    """
    saves_report = output_mode in ("report", "cascade-dev", "cascade-live")
    site_root    = f"https://www.ualberta.ca/en/{site.strip('/')}"

    print(f"Fetching sitemap for: {site_root}")
    try:
        paths = fetch_sitemap_paths(site, debug=DEBUG)
    except Exception as e:
        print(f"[ERROR] Could not fetch sitemap: {e}")
        return []

    if not paths:
        print("[INFO] No paths found in sitemap.")
        return []

    print(f"Found {len(paths)} page(s) to audit.")

    findings: list[dict] = []
    out_file  = f"{site}_alt_audit.csv"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context()
        page    = context.new_page()
        page.route("**/*", route_blocker)

        for idx, rel_path in enumerate(paths, start=1):
            page_url = f"{site_root}{rel_path}"
            print(f"[{idx}/{len(paths)}] {page_url}")

            try:
                page.goto(page_url, wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT * 1000)
                try:
                    page.wait_for_selector("img", timeout=500)
                except PWTimeout:
                    pass
                if EXTRA_WAIT_MS:
                    page.wait_for_timeout(EXTRA_WAIT_MS)
                imgs = extract_images_fast(page)
            except Exception as e:
                if DEBUG:
                    print(f"  [WARN] Scrape failed: {e}", file=sys.stderr)
                continue

            seen_on_page: set[str] = set()

            for img in imgs:
                src = (img.get("src") or "").strip()
                alt = img.get("alt")
                w   = int(img.get("width") or 0)
                h   = int(img.get("height") or 0)

                if not src:
                    continue

                needs_attention, reason = check_alt_quality(alt)
                if not needs_attention:
                    continue

                abs_url = absolutize_src_url(page_url, src, site=site)
                if abs_url in seen_on_page:
                    continue
                seen_on_page.add(abs_url)

                if should_skip_image(abs_url, w, h):
                    continue
                if not image_exists(abs_url):
                    if DEBUG:
                        print(f"  - Skipping (not reachable): {abs_url}")
                    continue
                if not within_llm_size_budget(abs_url):
                    if DEBUG:
                        print(f"  - Skipping (over size budget): {abs_url}")
                    continue

                try:
                    src_name = (urlparse(abs_url).path or "").split("/")[-1]
                except Exception:
                    src_name = src

                suggested = suggest_alt_for_url(abs_url, src_name, model=model)
                finding = {
                    "page_url":      page_url,
                    "image_url":     abs_url,
                    "current_alt":   alt or "",
                    "suggested_alt": suggested,
                    "reason":        reason,
                }
                findings.append(finding)

                if DEBUG:
                    print(f"  ✓ {src_name}: {suggested[:80]} ({reason})")

        browser.close()

    if saves_report and findings:
        with open(report_path(out_file), "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["page_url", "image_url", "current_alt", "suggested_alt", "reason"])
            writer.writeheader()
            writer.writerows(findings)
        print(f"\nReport written to: {out_file}")
    elif saves_report:
        print("\nNo issues found — no report written.")

    print(f"\nDone. {len(findings)} image(s) flagged across {len(paths)} page(s).")
    return findings


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== Bad Alt Text Auditor ===")

    site = input("Enter UAlberta site key (e.g. admissions-programs): ").strip()
    if not site:
        print("No site provided.")
        sys.exit(1)

    print("\nOutput mode:")
    print("  1) console — print findings only")
    print("  2) report  — print + save CSV (default)")
    mode_choice = input("Mode (1/2) [2]: ").strip() or "2"
    output_mode = "console" if mode_choice == "1" else "report"

    try:
        run_audit_site(site=site, output_mode=output_mode)
    except KeyboardInterrupt:
        print("\nInterrupted.")
    except Exception:
        print("[FATAL]")
        print(traceback.format_exc())
