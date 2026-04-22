# QA Automation — User Guide

## Overview

This codebase automates accessibility and content quality tasks for the UAlberta Cascade WebCMS.
All scripts are designed to be run from the **project root** after configuring your `.env` file.

### Setup

1. Copy `.env.example` to `.env` and fill in your API keys
2. Install dependencies: `pip install -r requirements.txt`
3. After installing, run: `playwright install chromium`

---

## Core Scripts

These are the scripts you run directly. Each has a matching `test_*.py` file at the project root that is the simplest way to use it.

---

### `core/img_alt_text.py` — Image Alt Text Generator

Generates descriptive alt text for images on Cascade pages and writes it back to the CMS.

**Three modes:**
- **audit** — Suggests alt text and better filenames for every image on a page. No changes are written to Cascade. Good for reviewing before committing.
- **generate** — Generates alt text for images that are missing it and writes the updates back to the page.
- **file** — Generates alt text for a single image asset stored in the Cascade media library.

**Two image source options:**
- **live** — Scrapes the public URL using a headless browser (Playwright). Best for seeing exactly what users see.
- **cascade** — Reads the page JSON directly via the Cascade API. Faster and works on unpublished pages.

**Test script:** `test_patch_image_alts.py`

```python
from core.img_alt_text import run_patch_alts

run_patch_alts(
    site        = "parking-services",
    path        = "/regulations-citations/index",
    mode        = "missing",      # "missing" | "all" | "decorative"
    output_mode = "cascade-dev",  # "console" | "report" | "cascade-dev" | "cascade-live"
    fetch_method = "live",        # "live" | "cascade"
)
```

**Mode options:**
- `missing` — only generate alt text for images with no alt or an empty alt (safest, default)
- `all` — regenerate alt text for every image, replacing existing values
- `decorative` — set `alt=""` on every image, marking them all as decorative (no OpenAI calls)

---

### `core/link_aria_label.py` — Link Aria-Label Generator

Finds `<a>` tags on a Cascade page that are missing `aria-label` attributes, generates descriptive labels using the OpenAI API, and writes them back.

For each link, it fetches the link target page to gather context (title, h1, meta description) before generating a label. This makes labels more accurate than looking at the anchor text alone.

**Test script:** `test_link_aria_label.py`

```python
from core.link_aria_label import process_page

process_page(
    site        = "parking-services",
    path        = "/regulations-citations/index",
    output_mode = "report",   # "console" | "report" | "cascade-dev" | "cascade-live"
)
```

**Tip:** Run with `output_mode="report"` first to review `reports/page_updated.json` before writing to Cascade.

---

### `core/table_alt_text.py` — Table Alt Text Generator

Finds HTML tables on a page and generates a one-sentence alt text description for each using the OpenAI API. Prints results to console.

> Note: Tables live inside page HTML content — there is no dedicated Cascade metadata field for table alt text, so this tool is read-only. Use the output to manually update table captions or surrounding context.

**Two source options:**
- **live** — fetches the public URL over HTTP
- **cascade** — reads the page JSON directly from the Cascade API

**Test script:** `test_table_alt_text.py` *(or run interactively)*

```python
from core.table_alt_text import process_page

process_page(
    source      = "cascade",
    site        = "parking-services",
    path        = "/regulations-citations/index",
    output_mode = "report",
)
```

---

### `core/search_replace.py` — Search and Replace

Finds and replaces (or removes) text across one or more Cascade pages. Can operate on a single page or across an entire site by reading the sitemap.

**Test script:** `test_search_replace.py` *(or use the GUI)*

```python
from core.search_replace import run_search_replace, run_search_replace_site

# Single page
run_search_replace(
    site         = "parking-services",
    path         = "/regulations-citations/index",
    search_term  = "old text",
    replace_term = "new text",   # pass None or "" to remove instead of replace
    output_mode  = "cascade-dev",
)

# Entire site
run_search_replace_site(
    site         = "parking-services",
    search_term  = "old text",
    replace_term = "new text",
    folder_path  = "",           # optional: limit to a subfolder, e.g. "/about/"
    output_mode  = "cascade-dev",
)
```

**GUI:** `python search_replace_gui.py` — a desktop interface for the same functionality.

---

### `core/bad_alt_auditor.py` — Bad Alt Text Auditor

Scrapes a UAlberta site's public sitemap, visits every page using a headless browser, and flags images with missing or poor-quality alt text. For each flagged image, it calls OpenAI to suggest better alt text. Writes a CSV report.

> This tool reads the live public site only — no Cascade API calls.

**Test script:** `test_bad_alt_auditor.py`

```python
from core.bad_alt_auditor import run_audit_site

run_audit_site(
    site        = "parking-services",
    output_mode = "report",   # "console" | "report"
)
```

**Output:** `reports/{site}_alt_audit.csv` with columns: `page_url`, `image_url`, `current_alt`, `suggested_alt`, `reason`

---

### `core/decorative_alt_updater.py` — Decorative Alt Updater

Reads a Siteimprove audit CSV export (listing pages with missing image alt text), then for each page: reads the Cascade page JSON, sets `alt=""` on every `<img>` missing an alt attribute, and writes it back. Setting `alt=""` tells screen readers the image is decorative and should be skipped.

**Run via CLI:**
```bash
python -m core.decorative_alt_updater input.csv --output-mode cascade-dev --limit 10
```

**Or call directly:**
```python
from core.decorative_alt_updater import run_decorative_update

run_decorative_update(
    csv_path    = "siteimprove_export.csv",
    output_mode = "cascade-dev",
    limit       = 10,   # optional: process only the first N rows
)
```

---

### `core/page_analyzer.py` + `core/batch_analyzer.py` — Page Analyzer

Analyzes page content using the OpenAI API and generates any combination of: summary, description, call to action, main theme, target audience, keywords, meta tag categories, and relevance classification scoring.

**Test script:** `test_page_analyzer.py`

```python
from core.page_analyzer import AnalysisConfig
from core.batch_analyzer import SourceConfig, run_batch

config = AnalysisConfig(
    include_summary        = True,
    include_keywords       = True,
    include_classification = True,
    classification_prompt  = "Actionable resources for instructors and teaching assistants",
    classification_scale   = 4,
)

source = SourceConfig(
    source_type    = "sitemap",      # "url" | "csv" | "sitemap" | "cascade"
    site           = "parking-services",
    content_source = "live",         # "live" | "cascade"
    limit          = 5,
)

run_batch(source=source, config=config, output_mode="report")
```

**Source types:**
- `url` — single page URL
- `csv` — a CSV file with a column of URLs (set `csv_path` and `url_column`)
- `sitemap` — all pages from a site's sitemap
- `cascade` — same as sitemap but fetches content via Cascade API instead of browser

**Output:** `reports/page_analysis_{site}.csv` — one row per page with all requested fields.

---

## Output Modes

All core scripts share the same four output modes:

| Mode | Console | Local report file | Writes to Cascade |
|---|---|---|---|
| `console` | ✅ | ❌ | ❌ |
| `report` | ✅ | ✅ (`reports/`) | ❌ |
| `cascade-dev` | ✅ | ✅ | ✅ → DEV server |
| `cascade-live` | ✅ | ✅ | ✅ → LIVE server |

**Default is always `cascade-dev`.** Always test with `report` or `cascade-dev` before running on `cascade-live`.

---

## Utilities (`utils/`)

These are internal helper modules used by the core scripts. You generally don't call these directly.

| File | What it does |
|---|---|
| `CascadeClient.py` | All Cascade API interactions — read/edit/publish pages, read/write file assets, Java-signed byte conversion |
| `cascade_client.py` | File asset operations (images, PDFs) and byte conversion helpers |
| `html_helpers.py` | HTML parsing, alt attribute patching, extracting HTML snippets and images from Cascade page JSON |
| `http_helpers.py` | Browser-realistic HTTP session with retry logic and rate-limit handling for fetching live pages |
| `image_compressor.py` | Fetch and compress images with Pillow before sending to OpenAI (reduces API cost) |
| `image_filters.py` | Rules for deciding whether an image should be skipped (too small, decorative, tracking pixel, etc.) |
| `image_scraper.py` | Playwright-based scraper that collects image metadata from live pages |
| `sitemap_helpers.py` | Fetch and parse XML sitemaps, including sitemap indexes and gzip-compressed sitemaps |
| `url_helpers.py` | URL sanitization, UAlberta site/path parsing, image URL resolution, MIME type guessing |
| `text_helpers.py` | Find-and-replace or remove text across a Cascade page JSON tree |
| `alt_quality.py` | Heuristics for flagging bad alt text (too short, looks like a filename, contains a URL, etc.) |
| `llm_helpers.py` | Generic OpenAI wrapper — rate-limit retry, JSON parsing, text truncation |
| `page_content_extractor.py` | Extract clean readable text from either a live URL or Cascade page JSON |
| `report_helpers.py` | Ensures the `reports/` directory exists and returns the correct path for any report file |

---

## Environment Variables (`.env`)

| Variable | Required | Description |
|---|---|---|
| `CASCADE_API_KEY` | Yes | Production Cascade API key |
| `CASCADE_DEV_API_KEY` | Recommended | Dev/sandbox Cascade API key. Falls back to `CASCADE_API_KEY` if not set. |
| `OPENAI_API_KEY` | Yes | OpenAI API key |
| `OPENAI_PROJECT` | No | OpenAI project ID (only needed for `sk-proj-` style keys) |
| `ARIA_BASE_URL` | No | Base URL for resolving relative hrefs in `link_aria_label.py` |
