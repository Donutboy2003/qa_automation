# test_page_analyzer.py
#
# Classify pages across multiple UAlberta sites as Internal, External,
# Mixed, or Unclassified based on their target audience.
#
# Each site runs as a separate batch and writes its own CSV to reports/.
# A failure on one site is caught and logged — the run continues with the rest.
#
# Run: python test_page_analyzer.py

from core.page_analyzer import AnalysisConfig
from core.batch_analyzer import SourceConfig, run_batch

# ── Sites to audit ────────────────────────────────────────────────────────────

TEST_SITES = [
    "information-services-and-technology",
    "student-success-experience",
    "admissions",
    "undergraduate-programs",
    "engineering",
]

# ── Analysis config ───────────────────────────────────────────────────────────

config = AnalysisConfig(
    include_summary                 = True,
    include_audience_classification = True,
    model                           = "gpt-4.1-mini",
)

# ── Run options ───────────────────────────────────────────────────────────────

LIMIT       = None      # None = all pages; set e.g. 10 for a quick test
OUTPUT_MODE = "report"  # "console" | "report"

# ── Run ───────────────────────────────────────────────────────────────────────

all_site_results: dict[str, list] = {}
failed_sites:     list[str]       = []

for site in TEST_SITES:
    print(f"\n{'=' * 60}")
    print(f"Site: {site}")
    print(f"{'=' * 60}")

    source = SourceConfig(
        source_type    = "sitemap",
        site           = site,
        content_source = "live",
        limit          = LIMIT,
    )

    try:
        results = run_batch(source=source, config=config, output_mode=OUTPUT_MODE)
        all_site_results[site] = results
    except Exception as e:
        print(f"\n[FAILED] {site}: {e}")
        failed_sites.append(site)
        continue

# ── Summary across all sites ──────────────────────────────────────────────────

print(f"\n{'=' * 60}")
print("AUDIT SUMMARY")
print(f"{'=' * 60}")

labels = ("Internal", "External", "Mixed", "Unclassified")

for site, results in all_site_results.items():
    total  = len(results)
    errors = sum(1 for r in results if r.get("error"))
    counts = {label: 0 for label in labels}
    for r in results:
        label = r.get("audience_classification", "")
        if label in counts:
            counts[label] += 1
    breakdown = "  ".join(f"{lbl}: {counts[lbl]}" for lbl in labels)
    print(f"\n{site}")
    print(f"  Pages: {total}  Errors: {errors}")
    print(f"  {breakdown}")

if failed_sites:
    print(f"\nFailed sites ({len(failed_sites)}):")
    for s in failed_sites:
        print(f"  - {s}")