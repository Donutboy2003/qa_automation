# test_alt_write.py
#
# Writes a test alt text string directly into the <img> tags inside the
# page's HTML content node, then saves it back to the LIVE Cascade server.
#
# After running: publish the page in Cascade, inspect the image on the live
# site (right-click → inspect), and confirm the alt attribute shows up.
#
# Run from the project root: python test_alt_write.py

from __future__ import annotations

import os
import sys

from bs4 import BeautifulSoup
from dotenv import load_dotenv

from utils.CascadeClient import CascadeClient

# ── Config ────────────────────────────────────────────────────────────────────

PAGE_SITE = "arts"
PAGE_PATH = "/faculty-news/2015/august/57-ways-to-screw-up-in-grad-school"

TEST_ALT  = "[TEST] Alt text written via Cascade API into HTML content node"

# ── Setup ─────────────────────────────────────────────────────────────────────

load_dotenv()
API_KEY = os.getenv("CASCADE_API_KEY", "").strip()
if not API_KEY:
    print("CASCADE_API_KEY not set in .env")
    sys.exit(1)

cascade = CascadeClient(API_KEY, testing=False)  # live server

# ── Read the page ─────────────────────────────────────────────────────────────

print(f"Reading: {PAGE_SITE}{PAGE_PATH}")
resp = cascade.readByPath(PAGE_SITE, PAGE_PATH)
if resp.status_code != 200:
    print(f"Read failed: {resp.status_code} — {resp.text[:300]}")
    sys.exit(1)

page_json  = resp.json()
page_asset = page_json["asset"]["page"]

# ── Patch <img> alt attributes in all HTML content nodes ─────────────────────

def patch_img_alts(node, count=0) -> int:
    """Recursively find HTML content nodes and set alt on every <img> tag."""
    if isinstance(node, dict):
        if node.get("identifier") == "content" and node.get("type") == "text":
            html = node.get("text") or ""
            if "<img" in html:
                soup = BeautifulSoup(html, "html.parser")
                for img in soup.find_all("img"):
                    old = img.get("alt", "<not set>")
                    img["alt"] = TEST_ALT
                    print(f"  img src : {img.get('src', '')}")
                    print(f"  old alt : {old}")
                    print(f"  new alt : {TEST_ALT}\n")
                    count += 1
                node["text"] = str(soup)
        for v in node.values():
            if isinstance(v, (dict, list)):
                count = patch_img_alts(v, count)
    elif isinstance(node, list):
        for item in node:
            count = patch_img_alts(item, count)
    return count

print()
patched = patch_img_alts(page_asset.get("structuredData", {}))

if patched == 0:
    print("No <img> tags found in any content node — nothing to write.")
    sys.exit(0)

print(f"Patched {patched} <img> tag(s). Writing back to Cascade (LIVE)...")

# ── Write back ────────────────────────────────────────────────────────────────

edit_resp = cascade.editAsset(page_json["asset"])
result    = edit_resp.json()

if result.get("success"):
    print("Write succeeded.")
    print("Next: publish the page in Cascade, then inspect the image alt on the live site.")
else:
    print(f"Write failed: {edit_resp.status_code} — {edit_resp.text[:300]}")