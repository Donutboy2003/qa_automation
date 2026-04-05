# test_patch_image_alts.py
#
# Update <img> alt attributes on a Cascade page via the API.
# All logic lives in core/img_alt_text.py and utils/.
#
# Run: python test_patch_image_alts.py

from core.img_alt_text import run_patch_alts

# ── Config ────────────────────────────────────────────────────────────────────

SITE = "arts"
PATH = "/faculty-news/2015/august/57-ways-to-screw-up-in-grad-school"

# Mode — controls which images are updated:
#   "missing"    — only images with no alt or alt=""  (default, safest)
#   "all"        — replace alt text on every image, including existing ones
#   "decorative" — set alt="" on every image (marks all as decorative, no OpenAI calls)
MODE = "all"

# Fetch method — how to get the image bytes to send to OpenAI:
#   "live"    — build the public URL and fetch over HTTP
#               e.g. https://www.ualberta.ca/en/arts/media-library/.../foo.jpg
#               Use this when the image is already published on the live site.
#   "cascade" — read the raw file asset directly from the Cascade API  (default)
#               Use this for unpublished images or when the live site is unreachable.
FETCH_METHOD = "cascade"

# Output mode — controls where results go:
#   "console"      — print to stdout only, no writes
#   "report"       — print + save a local JSON report, no Cascade writes
#   "cascade-dev"  — print + report + write to DEV server  (default)
#   "cascade-live" — print + report + write to LIVE server
OUTPUT_MODE = "cascade-live"

# ── Run ───────────────────────────────────────────────────────────────────────

result = run_patch_alts(
    site=SITE,
    path=PATH,
    mode=MODE,
    output_mode=OUTPUT_MODE,
    fetch_method=FETCH_METHOD,
)

print(f"\nSummary: {result}")
