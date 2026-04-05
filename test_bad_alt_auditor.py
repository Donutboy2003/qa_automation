# test_bad_alt_auditor.py
#
# Audit a UAlberta site for images with missing or low-quality alt text.
# Scrapes the live site via sitemap — no Cascade API calls needed.
#
# Run: python test_bad_alt_auditor.py

from core.bad_alt_auditor import run_audit_site

# ── Config ────────────────────────────────────────────────────────────────────

# UAlberta site key — the segment after /en/ in the URL
# e.g. "arts", "admissions-programs", "botanic-garden"
SITE = "actm"

# Output mode:
#   "console" — print findings only, no files written
#   "report"  — print + write a CSV to reports/  (default)
OUTPUT_MODE = "report"

# ── Run ───────────────────────────────────────────────────────────────────────

findings = run_audit_site(site=SITE, output_mode=OUTPUT_MODE)

print(f"\nTotal flagged: {len(findings)}")
