# debug_sitemap.py
#
# Run from project root:
#   python debug_sitemap.py
#
# This calls your existing helper with debug=True and prints the resolved paths.

import traceback

from utils.sitemap_helpers import fetch_sitemap_paths

SITE = "information-services-and-technology"

if __name__ == "__main__":
    print(f"Checking sitemap for site: {SITE}\n")
    try:
        paths = fetch_sitemap_paths(SITE, debug=True)
        print("\n" + "=" * 80)
        print(f"Resolved {len(paths)} path(s)")
        print("=" * 80)

        for i, path in enumerate(paths[:25], start=1):
            print(f"{i:>2}. {path}")

        if len(paths) > 25:
            print(f"\n... and {len(paths) - 25} more")
    except Exception as e:
        print("\n" + "=" * 80)
        print("ERROR")
        print("=" * 80)
        print(str(e))
        print()
        traceback.print_exc()