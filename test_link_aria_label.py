# test_link_aria_label.py
#
# Generate aria-labels for <a> tags missing them on a Cascade page.
# All logic lives in core/link_aria_label.py.
#
# Run: python test_link_aria_label.py

from core.link_aria_label import process_page

# ── Config ────────────────────────────────────────────────────────────────────

SITE = "parking-services"
PATH = "/regulations-citations/index"

# Output mode:
#   "console"      — print what would be added, no files written, no Cascade writes
#   "report"       — print + save page_raw.json and page_updated.json to reports/
#   "cascade-dev"  — print + report + write to DEV server  (default)
#   "cascade-live" — print + report + write to LIVE server
OUTPUT_MODE = "cascade-live"

# ── Run ───────────────────────────────────────────────────────────────────────

result = process_page(site=SITE, path=PATH, output_mode=OUTPUT_MODE)

print(f"\nSummary: {result}")
