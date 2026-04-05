# test_page_analyzer.py
#
# Analyze pages using configurable LLM outputs.
# All logic lives in core/page_analyzer.py and core/batch_analyzer.py.
#
# Run: python test_page_analyzer.py

from core.page_analyzer import AnalysisConfig
from core.batch_analyzer import SourceConfig, run_batch

# ── Analysis config — toggle any combination of outputs ───────────────────────

config = AnalysisConfig(

    # Basic content analysis
    include_summary     = True,   # ≤50 word summary of the page
    include_description = True,   # ≤30 word CMS/SEO description
    include_cta         = True,   # primary call to action on the page
    include_theme       = True,   # main topic in a short phrase
    include_audience    = True,   # who the page is written for
    include_keywords    = True,   # important keyphrases
    keyword_min         = 5,
    keyword_max         = 10,

    # Meta tag categorization
    # Set include_meta_tags=True and provide categories, or leave categories
    # empty to let the model pick freely.
    include_meta_tags   = True,
    meta_tag_categories = [
        "Health & Wellness",
        "Academic Support",
        "Financial",
        "Student Life",
        "Campus & Facilities",
        "Research & Innovation",
        "Career Development",
        "Governance & Policies",
        "Technology Services",
        "Events & Communications",
    ],

    # Relevance classification
    # Set include_classification=True and write a plain-language prompt
    # describing what you're looking for.
    include_classification          = True,
    classification_prompt           = "Lesson for graduate students",
    classification_scale            = 4,   # 1=not relevant, 4=highly relevant
    include_classification_reason      = True,
    include_classification_confidence  = True,

    model = "gpt-4.1-mini",
)

# ── Source config — pick ONE of the four source types ─────────────────────────

# --- Option 1: Single URL ---
# source = SourceConfig(
#     source_type    = "url",
#     url            = "https://www.ualberta.ca/en/parking-services/regulations-citations/index",
#     content_source = "live",   # "live" = fetch public URL | "cascade" = read via API
# )

# --- Option 2: CSV file ---
source = SourceConfig(
    source_type    = "csv",
    csv_path       = "test_urls.csv",
    url_column     = "url",        # which column holds the URLs
    content_source = "live",
)

# --- Option 3: Entire site via sitemap ---
# source = SourceConfig(
#     source_type    = "sitemap",
#     site           = "parking-services",
#     folder_path    = "",           # leave empty for whole site, or e.g. "/regulations-citations/"
#     content_source = "live",
#     limit          = 5,            # set a limit while testing; remove for full run
# )

# --- Option 4: Cascade API (reads page JSON directly, no browser needed) ---
# source = SourceConfig(
#     source_type    = "cascade",
#     site           = "parking-services",
#     content_source = "cascade",
#     use_dev        = True,         # True = DEV server, False = LIVE server
#     limit          = 5,
# )

# ── Output mode ───────────────────────────────────────────────────────────────

# "console"      — print results only
# "report"       — print + write CSV to reports/  (default)
# "cascade-dev"  — same as report (this script never writes to Cascade)
# "cascade-live" — same as report
OUTPUT_MODE = "report"

# ── Run ───────────────────────────────────────────────────────────────────────

results = run_batch(source=source, config=config, output_mode=OUTPUT_MODE)

print(f"\nTotal pages analyzed: {len(results)}")
