"""
gui.py — Page Analyzer (Tkinter)
=================================
Fully customizable LLM-powered batch page analysis GUI.
No asyncio in the GUI thread — Playwright runs in a daemon thread
with its own ProactorEventLoop, so it works on Windows.

Run:
    python gui.py
"""

from __future__ import annotations

import asyncio
import csv
import os
import queue
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from tkinter import filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
from tkinter import ttk
import tkinter as tk
from typing import Any, Optional


# ══════════════════════════════════════════════════════════════
#  Theme constants
# ══════════════════════════════════════════════════════════════

NAVY    = "#0f172a"
CYAN    = "#22d3ee"
WHITE   = "#ffffff"
OFF_W   = "#f8fafc"
LGRAY   = "#e2e8f0"
MGRAY   = "#94a3b8"
DGRAY   = "#475569"
YELLOW  = "#fef9c3"
YFORE   = "#78350f"
BLUE    = "#dbeafe"
BFORE   = "#1e40af"
RED     = "#ef4444"
GREEN   = "#22c55e"
AMBER   = "#f59e0b"

MONO   = ("Consolas", 9)
SANS   = ("Segoe UI", 10)
SANSB  = ("Segoe UI", 10, "bold")
SANSS  = ("Segoe UI", 9)
TITLE  = ("Segoe UI", 13, "bold")


# ══════════════════════════════════════════════════════════════
#  CustomField — the core data model
# ══════════════════════════════════════════════════════════════

OUTPUT_TYPES: list[tuple[str, str, str]] = [
    ("string",  "Text",         "Free-form text answer — any sentence or phrase"),
    ("boolean", "Yes / No",     "true or false — best for yes/no questions"),
    ("integer", "Whole number", "An integer — e.g. a count or category code"),
    ("float",   "Decimal",      "A decimal number — e.g. a ratio or probability"),
    ("list",    "List",         "A list of text items — comma-joined in the CSV"),
    ("scale",   "Score 1–N",    "An integer score from 1 to N (you choose N)"),
]
OUTPUT_TYPE_LABELS: dict[str, str] = {t[0]: t[1] for t in OUTPUT_TYPES}


@dataclass
class CustomField:
    name:        str    # CSV column key — must be a valid identifier
    label:       str    # GUI display name
    prompt:      str    # Instruction sent verbatim to the LLM
    output_type: str    # one of the OUTPUT_TYPES keys
    scale_max:   int  = 5
    is_preset:   bool = False

    @property
    def schema_line(self) -> str:
        """Fragment injected into the JSON schema prompt."""
        t, p = self.output_type, self.prompt
        if t == "string":
            return f'"{self.name}": string — {p}'
        if t == "boolean":
            return f'"{self.name}": boolean (return true or false, nothing else) — {p}'
        if t == "integer":
            return f'"{self.name}": integer — {p}'
        if t == "float":
            return f'"{self.name}": float (decimal number) — {p}'
        if t == "list":
            return f'"{self.name}": array of strings — {p}'
        if t == "scale":
            return (f'"{self.name}": integer 1\u2013{self.scale_max} '
                    f'(1=lowest, {self.scale_max}=highest) \u2014 {p}')
        return f'"{self.name}": string — {p}'

    def parse_value(self, raw: Any) -> Any:
        """Coerce the LLM raw value to the right Python type."""
        try:
            if self.output_type == "boolean":
                if isinstance(raw, bool):
                    return raw
                return str(raw).strip().lower() in ("true", "yes", "1")
            if self.output_type == "integer":
                return int(raw)
            if self.output_type == "float":
                return float(raw)
            if self.output_type == "scale":
                return max(1, min(self.scale_max, int(raw)))
            if self.output_type == "list":
                if isinstance(raw, list):
                    return raw
                return [s.strip() for s in str(raw).split(",") if s.strip()]
            return str(raw) if raw is not None else ""
        except Exception:
            return raw

    def to_csv_str(self, raw: Any) -> str:
        """Flatten a parsed value to a CSV-safe string."""
        val = self.parse_value(raw)
        if isinstance(val, list):
            return ", ".join(str(v) for v in val)
        if isinstance(val, bool):
            return "Yes" if val else "No"
        return str(val) if val is not None else ""


# ══════════════════════════════════════════════════════════════
#  Preset field library
# ══════════════════════════════════════════════════════════════

PRESET_FIELDS: list[CustomField] = [
    CustomField("summary", "Summary",
                "Write a one-paragraph summary of the page in 50 words or fewer",
                "string", is_preset=True),
    CustomField("description", "SEO Description",
                "Write a short page description suitable for CMS or SEO use (30 words max)",
                "string", is_preset=True),
    CustomField("call_to_action", "Call to Action",
                "What is the primary action this page asks users to take? Return null if none",
                "string", is_preset=True),
    CustomField("main_theme", "Main Theme",
                "What is the central topic of this page? Answer in one short phrase",
                "string", is_preset=True),
    CustomField("target_audience", "Target Audience",
                "Who is this page primarily written for? List all distinct audiences",
                "list", is_preset=True),
    CustomField("keywords", "Keywords",
                "List 5 to 10 important keyphrases from the content (not generic category names)",
                "list", is_preset=True),
    CustomField("audience_classification", "Audience Classification",
                "Classify as exactly one of: Internal (current students/staff), "
                "External (prospective/public), Mixed, or Unclassified",
                "string", is_preset=True),
    CustomField("relevance_score", "Relevance Score",
                "How relevant is this page to a university student seeking academic support?",
                "scale", scale_max=4, is_preset=True),
]
PRESET_BY_NAME: dict[str, CustomField] = {f.name: f for f in PRESET_FIELDS}


# ══════════════════════════════════════════════════════════════
#  LLM analysis — single prompt, all fields at once
# ══════════════════════════════════════════════════════════════

def analyze_page_combined(
    text: str,
    url: str,
    fields: list[CustomField],
    model: str = "gpt-4.1-mini",
    max_chars: int = 12_000,
) -> dict[str, Any]:
    from utils.llm_helpers import call_llm_json, truncate_text

    result: dict[str, Any] = {"url": url, "error": None}
    if not text or not text.strip():
        result["error"] = "No content to analyze"
        return result

    content = truncate_text(text.strip(), max_chars=max_chars)
    schema  = "{\n  " + ",\n  ".join(f.schema_line for f in fields) + "\n}"
    prompt  = (
        "Analyze the following webpage content and return a JSON object "
        "matching this schema exactly:\n\n"
        f"{schema}\n\n"
        "Rules:\n"
        "- Every key must be present even if the value is null.\n"
        "- For boolean fields return only true or false (no quotes).\n"
        "- For array fields return a JSON array of strings.\n"
        "- For score/scale fields return only an integer in the stated range.\n\n"
        f"URL: {url}\n\nPage Content:\n{content}\n\n"
        "Return ONLY the JSON object. No markdown fences, no extra commentary."
    )
    try:
        parsed = call_llm_json(prompt=prompt, model=model)
    except Exception as exc:
        result["error"] = str(exc)
        return result

    if parsed.get("_parse_error"):
        result["error"] = "JSON parse failed"
        return result

    for f in fields:
        if f.name in parsed:
            result[f.name] = f.parse_value(parsed[f.name])

    return result


# ══════════════════════════════════════════════════════════════
#  Batch runner
# ══════════════════════════════════════════════════════════════

def run_batch_combined(
    source,
    fields: list[CustomField],
    model: str = "gpt-4.1-mini",
    log_fn=None,
) -> tuple[list[dict], Optional[str]]:
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from utils.page_content_extractor import extract_text
    from utils.sitemap_helpers import fetch_sitemap_paths

    LLM_WORKERS = 5
    CHUNK       = LLM_WORKERS * 4

    def log(msg: str):
        print(msg)
        if log_fn:
            log_fn(msg)

    def resolve_urls(browser=None) -> list[str]:
        if source.source_type == "url":
            return [source.url]
        if source.source_type == "csv":
            import pandas as pd
            df = pd.read_csv(source.csv_path)
            return df[source.url_column].dropna().astype(str).tolist()
        paths = fetch_sitemap_paths(source.site, browser=browser)
        if source.folder_path:
            prefix = "/" + source.folder_path.strip("/") + "/"
            paths  = [p for p in paths if p.startswith(prefix)]
        root = f"https://www.ualberta.ca/en/{source.site.strip('/')}"
        return [f"{root}{p}" for p in paths]

    def do_run(browser=None) -> tuple[list[dict], Optional[str]]:
        log("Resolving page list...")
        try:
            urls = resolve_urls(browser)
        except Exception as exc:
            raise RuntimeError(f"Could not resolve URLs: {exc}") from exc

        if source.limit:
            urls = urls[:source.limit]

        total = len(urls)
        log(f"Pages to process: {total}\n")

        all_results: list[dict] = [{}] * total
        report_file = csv_writer = None
        csv_path: Optional[str] = None

        for chunk_start in range(0, total, CHUNK):
            chunk_urls = urls[chunk_start: chunk_start + CHUNK]

            chunk_contents: list[tuple[int, str, dict]] = []
            for li, url in enumerate(chunk_urls):
                gi = chunk_start + li
                log(f"[fetch {gi+1}/{total}] {url}")
                content = extract_text(
                    source=source.content_source, url=url, browser=browser
                )
                if content.get("error"):
                    log(f"  \u26a0 {content['error']}")
                chunk_contents.append((gi, url, content))

            def _analyze(args: tuple) -> tuple[int, dict]:
                idx, url, c = args
                if not c.get("text", "").strip():
                    return idx, {
                        "url":   url,
                        "title": c.get("title", ""),
                        "error": c.get("error") or "No content to analyze",
                    }
                res = analyze_page_combined(c["text"], url, fields, model=model)
                res["title"] = res.get("title") or c.get("title", "")
                return idx, res

            with ThreadPoolExecutor(max_workers=LLM_WORKERS) as pool:
                futures = {pool.submit(_analyze, item): item[0] for item in chunk_contents}
                for future in as_completed(futures):
                    idx, res = future.result()
                    all_results[idx] = res

            for li in range(len(chunk_urls)):
                gi  = chunk_start + li
                res = all_results[gi]

                if res.get("error"):
                    log(f"  [{gi+1}/{total}] \u2717 ERROR \u2014 {res['error']}")
                else:
                    log(f"  [{gi+1}/{total}] \u2713 {res.get('url', '')}")

                row: dict[str, Any] = {
                    "url":   res.get("url", ""),
                    "title": res.get("title", ""),
                    "error": res.get("error", ""),
                }
                for f in fields:
                    row[f.name] = f.to_csv_str(res.get(f.name, ""))

                if csv_writer is None:
                    os.makedirs("reports", exist_ok=True)
                    safe        = (source.site or "batch").replace("/", "_")
                    csv_path    = os.path.abspath(f"reports/page_analysis_{safe}.csv")
                    report_file = open(csv_path, "w", newline="", encoding="utf-8")
                    csv_writer  = csv.DictWriter(report_file, fieldnames=list(row.keys()))
                    csv_writer.writeheader()

                csv_writer.writerow(row)
                report_file.flush()

        if report_file:
            report_file.close()

        errors_count = sum(1 for r in all_results if r.get("error"))
        log(f"\n{'─' * 42}")
        log(f"Done \u2014 {total} page(s) \u00b7 {errors_count} error(s)")
        if csv_path:
            log(f"Report \u2192 {csv_path}")

        return all_results, csv_path

    if source.content_source == "live":
        from utils.browser_helpers import BrowserSession
        with BrowserSession() as browser:
            return do_run(browser)
    return do_run()


# ══════════════════════════════════════════════════════════════
#  Scrollable frame helper
# ══════════════════════════════════════════════════════════════

class ScrollableFrame(tk.Frame):
    def __init__(self, parent, bg=WHITE, **kw):
        super().__init__(parent, bg=bg, **kw)
        self._canvas = tk.Canvas(self, bg=bg, bd=0, highlightthickness=0)
        self._sb     = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self.inner   = tk.Frame(self._canvas, bg=bg)

        self.inner.bind(
            "<Configure>",
            lambda _: self._canvas.configure(scrollregion=self._canvas.bbox("all")),
        )
        self._win = self._canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self._canvas.configure(yscrollcommand=self._sb.set)
        self._canvas.bind("<Configure>",
                          lambda e: self._canvas.itemconfig(self._win, width=e.width))

        self._sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._canvas.bind_all(
            "<MouseWheel>",
            lambda e: self._canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"),
        )


# ══════════════════════════════════════════════════════════════
#  Field card widget
# ══════════════════════════════════════════════════════════════

class FieldCard(tk.Frame):
    def __init__(self, parent, field: CustomField, on_remove, **kw):
        super().__init__(parent, bg=WHITE, **kw)
        self.field = field

        badge_bg = BLUE   if field.is_preset else YELLOW
        badge_fg = BFORE  if field.is_preset else YFORE
        type_lbl = OUTPUT_TYPE_LABELS.get(field.output_type, field.output_type)
        if field.output_type == "scale":
            type_lbl = f"Score 1\u2013{field.scale_max}"

        left = tk.Frame(self, bg=WHITE)
        left.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 4), pady=7)

        icon = "\U0001f537" if field.is_preset else "\u2736"
        tk.Label(left, text=f"{icon}  {field.label}",
                 font=SANSB, bg=WHITE, fg=NAVY, anchor="w").pack(fill=tk.X)

        sub = tk.Frame(left, bg=WHITE)
        sub.pack(fill=tk.X, pady=(2, 0))
        tk.Label(sub, text=type_lbl, font=("Segoe UI", 8),
                 bg=badge_bg, fg=badge_fg, padx=6, pady=1).pack(side=tk.LEFT)

        if field.prompt:
            preview = field.prompt[:64] + ("\u2026" if len(field.prompt) > 64 else "")
            tk.Label(sub, text=f"  {preview}", font=("Segoe UI", 8),
                     bg=WHITE, fg=MGRAY, anchor="w").pack(side=tk.LEFT)

        tk.Button(
            self, text="\u2715", font=("Segoe UI", 9), fg=RED, bg=WHITE,
            bd=0, padx=6, cursor="hand2",
            activeforeground=RED, activebackground=WHITE,
            command=on_remove,
        ).pack(side=tk.RIGHT, padx=6)

        tk.Frame(self, bg=LGRAY, height=1).pack(side=tk.BOTTOM, fill=tk.X)


# ══════════════════════════════════════════════════════════════
#  Add Preset dialog
# ══════════════════════════════════════════════════════════════

class AddPresetDialog(tk.Toplevel):
    def __init__(self, parent, already_added: set[str]):
        super().__init__(parent)
        self.title("Add Preset Field")
        self.configure(bg=WHITE)
        self.resizable(False, False)
        self.result: Optional[CustomField] = None
        self._build(already_added)
        self.transient(parent)
        self.grab_set()
        _center(self, parent, 480, 540)
        self.wait_window()

    def _build(self, already_added: set[str]):
        tk.Label(self, text="Preset Fields", font=TITLE, bg=WHITE, fg=NAVY
                 ).pack(padx=20, pady=(18, 2), anchor="w")
        tk.Label(self,
                 text="Presets use carefully designed prompts that produce clean, parseable output.",
                 font=SANSS, bg=WHITE, fg=MGRAY, wraplength=440, justify=tk.LEFT,
                 ).pack(padx=20, pady=(0, 12), anchor="w")

        available = [f for f in PRESET_FIELDS if f.name not in already_added]

        if not available:
            tk.Label(self, text="All preset fields are already added.",
                     font=SANSS, bg=WHITE, fg=MGRAY).pack(pady=20)
        else:
            self._var = tk.StringVar(value="")
            sf = ScrollableFrame(self, bg=WHITE)
            sf.pack(fill=tk.BOTH, expand=True, padx=16, pady=4)

            for pf in available:
                row = tk.Frame(sf.inner, bg=WHITE)
                row.pack(fill=tk.X, pady=4)
                tk.Radiobutton(row, text="", variable=self._var, value=pf.name,
                               bg=WHITE, activebackground=WHITE).pack(side=tk.LEFT)
                info = tk.Frame(row, bg=WHITE)
                info.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
                tl = OUTPUT_TYPE_LABELS.get(pf.output_type, pf.output_type)
                if pf.output_type == "scale":
                    tl = f"Score 1\u2013{pf.scale_max}"
                tk.Label(info, text=pf.label, font=SANSB, bg=WHITE, fg=NAVY,
                         anchor="w").pack(fill=tk.X)
                tk.Label(info, text=f"{tl}  \u00b7  {pf.prompt}",
                         font=SANSS, bg=WHITE, fg=MGRAY, anchor="w",
                         wraplength=360, justify=tk.LEFT).pack(fill=tk.X)
                tk.Frame(sf.inner, bg=LGRAY, height=1).pack(fill=tk.X, pady=2)

        btn = tk.Frame(self, bg=WHITE)
        btn.pack(fill=tk.X, padx=20, pady=14)
        tk.Button(btn, text="Cancel", font=SANS, bg=LGRAY, fg=NAVY, bd=0,
                  padx=14, pady=6, cursor="hand2",
                  command=self.destroy).pack(side=tk.RIGHT, padx=(6, 0))
        if available:
            tk.Button(btn, text="Add Field", font=SANSB, bg=NAVY, fg=CYAN,
                      bd=0, padx=14, pady=6, cursor="hand2",
                      command=self._confirm).pack(side=tk.RIGHT)

    def _confirm(self):
        name = self._var.get()
        if not name:
            messagebox.showwarning("Select a field", "Please select a field first.", parent=self)
            return
        self.result = PRESET_BY_NAME.get(name)
        self.destroy()


# ══════════════════════════════════════════════════════════════
#  Add Custom dialog
# ══════════════════════════════════════════════════════════════

class AddCustomDialog(tk.Toplevel):
    """
    Form for building a custom LLM field from scratch.

    Safeguards:
      - Column name validated as a Python identifier
      - Duplicate name detected immediately
      - Prompt-length warning (<15 chars)
      - Context-sensitive tips per output type (boolean phrasing, list wording, etc.)
      - Live JSON schema preview shows exactly what gets sent to the LLM
      - Short-prompt soft-confirm before saving
    """

    def __init__(self, parent, existing_names: set[str]):
        super().__init__(parent)
        self.title("Custom Field Builder")
        self.configure(bg=WHITE)
        self.resizable(True, False)
        self.result: Optional[CustomField] = None
        self._existing = existing_names
        self._build()
        self.transient(parent)
        self.grab_set()
        _center(self, parent, 580, 680)
        self.wait_window()

    # ── Build ───────────────────────────────────────────────────────────────

    def _build(self):
        # ── Pinned header (never scrolls) ─────────────────────────────────
        hdr = tk.Frame(self, bg=WHITE)
        hdr.pack(fill=tk.X, padx=22, pady=(14, 0))
        tk.Label(hdr, text="Custom Field Builder", font=TITLE, bg=WHITE, fg=NAVY,
                 anchor="w").pack(fill=tk.X)
        tk.Label(hdr,
                 text="Define any question for the LLM to answer about each page. "
                      "The prompt + output type together determine what lands in the CSV.",
                 font=SANSS, bg=WHITE, fg=MGRAY, wraplength=530, justify=tk.LEFT,
                 ).pack(fill=tk.X, pady=(2, 8))

        tk.Frame(self, bg=LGRAY, height=1).pack(fill=tk.X)

        # ── Scrollable body ───────────────────────────────────────────────
        outer = tk.Frame(self, bg=WHITE)
        outer.pack(fill=tk.BOTH, expand=True)

        canvas = tk.Canvas(outer, bg=WHITE, bd=0, highlightthickness=0)
        vsb    = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)

        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        body = tk.Frame(canvas, bg=WHITE)
        win  = canvas.create_window((0, 0), window=body, anchor="nw")

        def _on_body_configure(_):
            canvas.configure(scrollregion=canvas.bbox("all"))
        def _on_canvas_configure(e):
            canvas.itemconfig(win, width=e.width)

        body.bind("<Configure>", _on_body_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        # Mouse-wheel scroll works everywhere inside the dialog
        def _on_mousewheel(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        self.bind_all("<MouseWheel>", _on_mousewheel)

        # Pad body contents
        pad = tk.Frame(body, bg=WHITE)
        pad.pack(fill=tk.X, padx=22, pady=4)

        # ── Column name ────────────────────────────────────────────────────
        self._section(pad, "Column name *",
                      "CSV header and JSON key. Letters, numbers, underscores only; "
                      "must start with a letter or underscore.")
        name_row = tk.Frame(pad, bg=WHITE)
        name_row.pack(fill=tk.X, pady=(2, 0))
        self._name_var = tk.StringVar()
        tk.Entry(name_row, textvariable=self._name_var, font=MONO, width=28,
                 bg=OFF_W, fg=NAVY, insertbackground=NAVY,
                 relief=tk.FLAT, highlightthickness=1,
                 highlightbackground=LGRAY).pack(side=tk.LEFT, ipady=5)
        self._name_warn = tk.Label(name_row, text="", font=SANSS, bg=WHITE, fg=RED, anchor="w")
        self._name_warn.pack(side=tk.LEFT, padx=8)

        # ── Display label ──────────────────────────────────────────────────
        self._section(pad, "Display label *", "Shown in the GUI. Can be anything.")
        self._label_var = tk.StringVar()
        tk.Entry(pad, textvariable=self._label_var, font=SANS, width=42,
                 bg=OFF_W, fg=NAVY, insertbackground=NAVY,
                 relief=tk.FLAT, highlightthickness=1,
                 highlightbackground=LGRAY).pack(fill=tk.X, ipady=5, pady=(2, 0))

        # ── LLM Prompt ────────────────────────────────────────────────────
        self._section(pad, "LLM Prompt / Question *",
                      "This exact text is sent to the model. "
                      "The output type below tells the model what format to return.")
        self._prompt_text = tk.Text(pad, height=3, font=SANS, wrap=tk.WORD,
                                    bg=OFF_W, fg=NAVY, insertbackground=NAVY,
                                    relief=tk.FLAT, highlightthickness=1,
                                    highlightbackground=LGRAY)
        self._prompt_text.pack(fill=tk.X, ipady=4, pady=(2, 2))
        self._prompt_warn = tk.Label(pad, text="", font=SANSS, bg=WHITE, fg=AMBER,
                                     anchor="w", wraplength=490, justify=tk.LEFT)
        self._prompt_warn.pack(fill=tk.X)

        # ── Output type ───────────────────────────────────────────────────
        self._section(pad, "Output type *",
                      "How the model's answer will be stored and formatted in the CSV.")
        grid = tk.Frame(pad, bg=WHITE)
        grid.pack(fill=tk.X, pady=(2, 4))
        self._type_var = tk.StringVar(value="string")
        for i, (key, lbl, desc) in enumerate(OUTPUT_TYPES):
            col = i % 2
            r   = i // 2
            cell = tk.Frame(grid, bg=WHITE)
            cell.grid(row=r, column=col, sticky="nw", padx=(0, 16), pady=2)
            tk.Radiobutton(cell, text="", variable=self._type_var, value=key,
                           bg=WHITE, activebackground=WHITE,
                           command=self._on_type_change).pack(side=tk.LEFT)
            info_f = tk.Frame(cell, bg=WHITE)
            info_f.pack(side=tk.LEFT)
            tk.Label(info_f, text=lbl, font=SANSB, bg=WHITE, fg=NAVY, anchor="w").pack(fill=tk.X)
            tk.Label(info_f, text=desc, font=("Segoe UI", 8), bg=WHITE, fg=MGRAY,
                     anchor="w").pack(fill=tk.X)

        # Scale N (hidden unless "scale" selected)
        self._scale_frame = tk.Frame(pad, bg=WHITE)
        tk.Label(self._scale_frame, text="Maximum N:", font=SANS, bg=WHITE,
                 fg=NAVY).pack(side=tk.LEFT)
        self._scale_var = tk.IntVar(value=5)
        tk.Spinbox(self._scale_frame, from_=2, to=10, textvariable=self._scale_var,
                   width=4, font=SANS, command=self._update).pack(side=tk.LEFT, padx=8)
        tk.Label(self._scale_frame, text="(LLM returns an integer from 1 to N)",
                 font=SANSS, bg=WHITE, fg=MGRAY).pack(side=tk.LEFT)

        # ── Schema preview ─────────────────────────────────────────────────
        tk.Label(pad, text="JSON schema line sent to the LLM:",
                 font=SANSS, bg=WHITE, fg=MGRAY).pack(anchor="w", pady=(10, 2))
        self._preview_var = tk.StringVar(value="\u2190 fill in name and prompt to see preview")
        tk.Label(pad, textvariable=self._preview_var,
                 font=MONO, bg=NAVY, fg=CYAN,
                 anchor="w", justify=tk.LEFT, wraplength=490,
                 padx=12, pady=10).pack(fill=tk.X, pady=(0, 12))

        # ── Wire live updates ──────────────────────────────────────────────
        for var in (self._name_var, self._label_var, self._type_var, self._scale_var):
            var.trace_add("write", lambda *_: self._update())
        self._prompt_text.bind("<KeyRelease>", lambda _: self._update())

        # ── Pinned footer buttons (always visible, never scroll) ───────────
        tk.Frame(self, bg=LGRAY, height=1).pack(fill=tk.X)
        btn_row = tk.Frame(self, bg=WHITE)
        btn_row.pack(fill=tk.X, padx=22, pady=12)
        tk.Button(btn_row, text="Cancel", font=SANS, bg=LGRAY, fg=NAVY, bd=0,
                  padx=14, pady=7, cursor="hand2",
                  command=self.destroy).pack(side=tk.RIGHT, padx=(6, 0))
        tk.Button(btn_row, text="Add Field \u2192", font=SANSB, bg=NAVY, fg=CYAN,
                  bd=0, padx=18, pady=7, cursor="hand2",
                  command=self._confirm).pack(side=tk.RIGHT)

        self._update()

    def _section(self, parent, title: str, subtitle: str = ""):
        tk.Label(parent, text=title, font=SANSB, bg=WHITE, fg=NAVY,
                 anchor="w").pack(fill=tk.X, pady=(10, 0))
        if subtitle:
            tk.Label(parent, text=subtitle, font=SANSS, bg=WHITE, fg=MGRAY,
                     anchor="w", wraplength=530).pack(fill=tk.X)

    # ── Callbacks ──────────────────────────────────────────────────────────

    def _on_type_change(self):
        if self._type_var.get() == "scale":
            self._scale_frame.pack(fill=tk.X, pady=(0, 4))
        else:
            self._scale_frame.pack_forget()
        self._update()

    def _update(self, *_):
        name   = self._name_var.get().strip()
        prompt = self._prompt_text.get("1.0", tk.END).strip()
        otype  = self._type_var.get()
        smax   = self._scale_var.get()

        # Column name validation
        if name:
            if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name):
                self._name_warn.config(
                    text="\u26a0 Letters, numbers, underscores; start with letter/underscore")
            elif name in self._existing:
                self._name_warn.config(text=f"\u26a0 '{name}' already exists")
            else:
                self._name_warn.config(text="")
        else:
            self._name_warn.config(text="")

        # Prompt tips
        tips: list[str] = []
        if prompt:
            if len(prompt) < 10:
                tips.append("\u26a0  Very short prompt \u2014 short prompts produce inconsistent output.")
            if otype == "boolean":
                q_starters = ("is ", "are ", "does ", "do ", "has ",
                               "have ", "can ", "will ", "should ", "was ", "were ")
                if not any(prompt.lower().startswith(w) for w in q_starters) and "?" not in prompt:
                    tips.append(
                        "\u2139  Tip: Yes/No prompts work best as questions, "
                        'e.g. "Is this page intended for prospective students?"'
                    )
            elif otype == "list":
                if not any(w in prompt.lower() for w in ("list", "all ", "array", "return")):
                    tips.append(
                        "\u2139  Tip: say \u201cList all\u2026\u201d so the model knows to return multiple items."
                    )
            elif otype == "float":
                if "0" not in prompt and "1" not in prompt and "percent" not in prompt.lower():
                    tips.append(
                        "\u2139  Tip: specify the range, e.g. \u201cReturn a value from 0.0 to 1.0.\u201d"
                    )
        self._prompt_warn.config(text="\n".join(tips))

        # Preview
        if name and prompt and re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name):
            tmp = CustomField(name=name, label="", prompt=prompt,
                              output_type=otype, scale_max=smax)
            self._preview_var.set(tmp.schema_line)
        else:
            self._preview_var.set("\u2190 fill in column name and prompt to see preview")

    def _confirm(self):
        name   = self._name_var.get().strip()
        label  = self._label_var.get().strip()
        prompt = self._prompt_text.get("1.0", tk.END).strip()
        otype  = self._type_var.get()
        smax   = self._scale_var.get()

        errors: list[str] = []
        if not name:
            errors.append("Column name is required.")
        elif not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name):
            errors.append("Column name: letters, numbers, underscores only; "
                          "must start with a letter or underscore.")
        elif name in self._existing:
            errors.append(f"A field named '{name}' already exists.")
        if not label:
            errors.append("Display label is required.")
        if not prompt:
            errors.append("LLM prompt is required.")
        elif len(prompt) < 5:
            errors.append("Prompt is too short \u2014 please be more descriptive.")

        if errors:
            messagebox.showerror(
                "Fix these issues before continuing",
                "\n\n".join(f"\u2022 {e}" for e in errors),
                parent=self,
            )
            return

        if len(prompt) < 20:
            ok = messagebox.askokcancel(
                "Short prompt \u2014 are you sure?",
                f'Your prompt is very short:\n\n    "{prompt}"\n\n'
                "Very short prompts often produce vague or inconsistent answers.\n\n"
                "Click OK to add it anyway, or Cancel to edit.",
                parent=self,
            )
            if not ok:
                return

        self.result = CustomField(
            name=name, label=label, prompt=prompt,
            output_type=otype, scale_max=smax, is_preset=False,
        )
        self.destroy()


# ══════════════════════════════════════════════════════════════
#  Main application window
# ══════════════════════════════════════════════════════════════

class PageAnalyzerApp(tk.Tk):

    _SPINNER = ["\u25d0", "\u25d3", "\u25d1", "\u25d2"]

    def __init__(self):
        super().__init__()
        self.title("Page Analyzer")
        self.configure(bg=NAVY)
        self.geometry("1140x740")
        self.minsize(900, 620)

        self._active_fields: list[CustomField] = []
        self._log_queue: queue.Queue           = queue.Queue()
        self._running    = False
        self._spin_idx   = 0
        self._last_csv: Optional[str] = None

        self._build_header()
        self._build_main()
        self._poll_log()

    # ── Header ──────────────────────────────────────────────────────────────

    def _build_header(self):
        h = tk.Frame(self, bg=NAVY, height=50)
        h.pack(fill=tk.X)
        h.pack_propagate(False)
        tk.Label(h, text="\u2b21  Page Analyzer",
                 font=("Consolas", 13, "bold"), bg=NAVY, fg=CYAN
                 ).pack(side=tk.LEFT, padx=20, pady=12)
        tk.Label(h, text="LLM-powered batch page analysis  \u00b7  fully customizable fields",
                 font=SANSS, bg=NAVY, fg=MGRAY).pack(side=tk.LEFT)

    # ── Main layout ──────────────────────────────────────────────────────────

    def _build_main(self):
        pane = tk.PanedWindow(self, orient=tk.HORIZONTAL,
                               bg=NAVY, sashwidth=6, sashpad=0, handlepad=0)
        pane.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))

        left  = tk.Frame(pane, bg=WHITE)
        right = tk.Frame(pane, bg=OFF_W)
        pane.add(left,  minsize=320)
        pane.add(right, minsize=440)
        pane.sash_place(0, 400, 0)

        self._build_left(left)
        self._build_right(right)

    # ── Left panel ──────────────────────────────────────────────────────────

    def _build_left(self, parent):

        # Source section
        src = tk.LabelFrame(parent, text=" Source ", font=SANSB,
                             bg=WHITE, fg=NAVY, bd=1, relief=tk.GROOVE,
                             padx=12, pady=8)
        src.pack(fill=tk.X, padx=10, pady=(10, 6))

        def lrow(text):
            f = tk.Frame(src, bg=WHITE)
            f.pack(fill=tk.X, pady=3)
            tk.Label(f, text=text, font=SANSS, bg=WHITE, fg=DGRAY,
                     width=10, anchor="w").pack(side=tk.LEFT)
            return f

        r = lrow("Type:")
        self._source_type = tk.StringVar(value="sitemap")
        cb = ttk.Combobox(r, textvariable=self._source_type, state="readonly",
                          width=20, values=["sitemap", "url", "csv"])
        cb.pack(side=tk.LEFT)
        cb.bind("<<ComboboxSelected>>", self._refresh_source_rows)

        # Source-specific rows via grid so show/hide preserves order
        sf = tk.Frame(src, bg=WHITE)
        sf.pack(fill=tk.X)
        sf.columnconfigure(1, weight=1)

        def grid_lbl(parent, text, row):
            tk.Label(parent, text=text, font=SANSS, bg=WHITE, fg=DGRAY,
                     width=10, anchor="w").grid(row=row, column=0, sticky="w", pady=3)

        # row 0: site slug
        grid_lbl(sf, "Site slug:", 0)
        self._site_var = tk.StringVar()
        tk.Entry(sf, textvariable=self._site_var, font=SANS, width=22,
                 relief=tk.FLAT, highlightthickness=1,
                 highlightbackground=LGRAY).grid(row=0, column=1, sticky="ew", ipady=4, pady=3)

        # row 1: folder
        grid_lbl(sf, "Folder:", 1)
        self._folder_var = tk.StringVar()
        tk.Entry(sf, textvariable=self._folder_var, font=SANS, width=22,
                 relief=tk.FLAT, highlightthickness=1,
                 highlightbackground=LGRAY).grid(row=1, column=1, sticky="ew", ipady=4, pady=3)

        # row 2: url
        grid_lbl(sf, "URL:", 2)
        self._url_var = tk.StringVar()
        tk.Entry(sf, textvariable=self._url_var, font=SANS, width=22,
                 relief=tk.FLAT, highlightthickness=1,
                 highlightbackground=LGRAY).grid(row=2, column=1, sticky="ew", ipady=4, pady=3)

        # row 3: csv path
        grid_lbl(sf, "CSV path:", 3)
        csv_inner = tk.Frame(sf, bg=WHITE)
        csv_inner.grid(row=3, column=1, sticky="ew", pady=3)
        self._csv_var = tk.StringVar()
        tk.Entry(csv_inner, textvariable=self._csv_var, font=SANS,
                 relief=tk.FLAT, highlightthickness=1,
                 highlightbackground=LGRAY).pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
        tk.Button(csv_inner, text="\u2026", font=SANS, bg=LGRAY, bd=0, padx=4,
                  command=self._browse_csv).pack(side=tk.LEFT, padx=4)

        # row 4: url column
        grid_lbl(sf, "URL column:", 4)
        self._col_var = tk.StringVar(value="url")
        tk.Entry(sf, textvariable=self._col_var, font=SANS, width=14,
                 relief=tk.FLAT, highlightthickness=1,
                 highlightbackground=LGRAY).grid(row=4, column=1, sticky="w", ipady=4, pady=3)

        # remember which grid rows belong to each source type
        self._src_grid_rows = {
            "sitemap": [0, 1],
            "url":     [2],
            "csv":     [3, 4],
        }
        self._src_grid_frame = sf

        # hide non-sitemap rows at start
        for key in ("url", "csv"):
            for row_i in self._src_grid_rows[key]:
                for w in sf.grid_slaves(row=row_i):
                    w.grid_remove()

        # content / limit / model
        r2 = lrow("Content:")
        self._content_src = tk.StringVar(value="live")
        for v, lbl in (("live", "Live (browser)"), ("cascade", "Cascade API")):
            tk.Radiobutton(r2, text=lbl, variable=self._content_src, value=v,
                           font=SANSS, bg=WHITE, activebackground=WHITE
                           ).pack(side=tk.LEFT, padx=3)

        r3 = lrow("Limit:")
        self._limit_var = tk.IntVar(value=0)
        tk.Spinbox(r3, from_=0, to=9999, textvariable=self._limit_var,
                   width=6, font=SANS).pack(side=tk.LEFT)
        tk.Label(r3, text="  pages  (0 = all)", font=SANSS, bg=WHITE, fg=MGRAY).pack(side=tk.LEFT)

        r4 = lrow("Model:")
        self._model_var = tk.StringVar(value="gpt-4.1-mini")
        ttk.Combobox(r4, textvariable=self._model_var, width=18,
                     values=["gpt-4.1-mini", "gpt-4.1", "gpt-4o", "gpt-4o-mini"]
                     ).pack(side=tk.LEFT)

        # Fields header + buttons
        fh = tk.Frame(parent, bg=WHITE)
        fh.pack(fill=tk.X, padx=10, pady=(10, 2))
        tk.Label(fh, text="Analysis Fields", font=SANSB, bg=WHITE, fg=NAVY).pack(side=tk.LEFT)

        btn_row = tk.Frame(parent, bg=WHITE)
        btn_row.pack(fill=tk.X, padx=10, pady=(0, 4))
        tk.Button(btn_row, text="\uff0b Preset", font=SANSS,
                  bg=BLUE, fg=BFORE, bd=0, padx=10, pady=4, cursor="hand2",
                  command=self._add_preset).pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(btn_row, text="\uff0b Custom", font=SANSS,
                  bg=YELLOW, fg=YFORE, bd=0, padx=10, pady=4, cursor="hand2",
                  command=self._add_custom).pack(side=tk.LEFT)

        # Scrollable field list
        list_border = tk.Frame(parent, bg=LGRAY, bd=1, relief=tk.SUNKEN)
        list_border.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 6))
        self._fields_scroll = ScrollableFrame(list_border, bg=WHITE)
        self._fields_scroll.pack(fill=tk.BOTH, expand=True)
        self._refresh_fields_ui()

        # Run button
        self._run_btn = tk.Button(
            parent, text="\u25b6  Run Analysis",
            font=("Segoe UI", 11, "bold"),
            bg=NAVY, fg=CYAN, bd=0, pady=10, cursor="hand2",
            activebackground="#1e293b", activeforeground=CYAN,
            command=self._run,
        )
        self._run_btn.pack(fill=tk.X, padx=10, pady=(0, 10))

    # ── Right panel ─────────────────────────────────────────────────────────

    def _build_right(self, parent):
        sb = tk.Frame(parent, bg=LGRAY, height=36)
        sb.pack(fill=tk.X)
        sb.pack_propagate(False)
        self._status_var = tk.StringVar(
            value="Ready \u2014 add fields and click \u25b6 Run Analysis.")
        tk.Label(sb, textvariable=self._status_var,
                 font=SANSS, bg=LGRAY, fg=NAVY, anchor="w"
                 ).pack(side=tk.LEFT, padx=12, pady=8)
        self._spinner_lbl = tk.Label(sb, text="",
                                     font=("Consolas", 11, "bold"), bg=LGRAY, fg=CYAN)
        self._spinner_lbl.pack(side=tk.RIGHT, padx=12)

        log_frame = tk.LabelFrame(parent, text=" Run Log ", font=SANSB,
                                   bg=OFF_W, fg=NAVY, bd=1, relief=tk.GROOVE)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(8, 4))

        self._log = ScrolledText(log_frame, font=MONO, bg=NAVY, fg="#94a3b8",
                                  insertbackground=CYAN, bd=0, relief=tk.FLAT,
                                  wrap=tk.WORD, state=tk.DISABLED)
        self._log.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self._log.tag_config("ok",    foreground=GREEN)
        self._log.tag_config("warn",  foreground=AMBER)
        self._log.tag_config("error", foreground=RED)
        self._log.tag_config("info",  foreground=CYAN)
        self._log.tag_config("done",  foreground="#e2e8f0")

        result_row = tk.Frame(parent, bg=OFF_W)
        result_row.pack(fill=tk.X, padx=10, pady=(0, 8))
        self._result_var = tk.StringVar(value="")
        tk.Label(result_row, textvariable=self._result_var,
                 font=SANSS, bg=OFF_W, fg=NAVY, anchor="w").pack(side=tk.LEFT)
        self._open_btn = tk.Button(result_row, text="\U0001f4c2  Open CSV",
                                   font=SANSS, bg=NAVY, fg=WHITE, bd=0, padx=10, pady=4,
                                   cursor="hand2", state=tk.DISABLED,
                                   command=self._open_csv)
        self._open_btn.pack(side=tk.RIGHT)

    # ── Source row toggling ──────────────────────────────────────────────────

    def _refresh_source_rows(self, *_):
        st    = self._source_type.get()
        sf    = self._src_grid_frame
        for key, rows in self._src_grid_rows.items():
            for row_i in rows:
                for w in sf.grid_slaves(row=row_i):
                    if key == st:
                        w.grid()
                    else:
                        w.grid_remove()

    def _browse_csv(self):
        path = filedialog.askopenfilename(
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if path:
            self._csv_var.set(path)

    # ── Field management ────────────────────────────────────────────────────

    def _refresh_fields_ui(self):
        for w in self._fields_scroll.inner.winfo_children():
            w.destroy()
        if not self._active_fields:
            tk.Label(
                self._fields_scroll.inner,
                text="No fields added yet.\n\n"
                     "Click  \uff0b Preset  for built-in fields\n"
                     "or  \uff0b Custom  to write your own prompt.",
                font=SANSS, bg=WHITE, fg=MGRAY, justify=tk.CENTER,
            ).pack(pady=30)
            return
        for i, f in enumerate(self._active_fields):
            FieldCard(self._fields_scroll.inner, f,
                      on_remove=lambda idx=i: self._remove_field(idx)
                      ).pack(fill=tk.X)

    def _remove_field(self, idx: int):
        if 0 <= idx < len(self._active_fields):
            self._active_fields.pop(idx)
            self._refresh_fields_ui()

    def _add_preset(self):
        existing = {f.name for f in self._active_fields}
        dlg = AddPresetDialog(self, already_added=existing)
        if dlg.result:
            self._active_fields.append(dlg.result)
            self._refresh_fields_ui()

    def _add_custom(self):
        existing = {f.name for f in self._active_fields}
        dlg = AddCustomDialog(self, existing_names=existing)
        if dlg.result:
            self._active_fields.append(dlg.result)
            self._refresh_fields_ui()

    # ── Validation ───────────────────────────────────────────────────────────

    def _validate(self) -> list[str]:
        errors: list[str] = []
        st = self._source_type.get()
        if st == "sitemap" and not self._site_var.get().strip():
            errors.append("Site slug is required (e.g. 'engineering').")
        if st == "url" and not self._url_var.get().strip():
            errors.append("URL is required.")
        if st == "csv" and not self._csv_var.get().strip():
            errors.append("CSV file path is required.")
        if not self._active_fields:
            errors.append("Add at least one analysis field.")
        return errors

    # ── Run ──────────────────────────────────────────────────────────────────

    def _run(self):
        errors = self._validate()
        if errors:
            messagebox.showerror("Cannot run",
                                  "\n\n".join(f"\u2022 {e}" for e in errors),
                                  parent=self)
            return

        if self._running:
            return

        self._log.config(state=tk.NORMAL)
        self._log.delete("1.0", tk.END)
        self._log.config(state=tk.DISABLED)
        self._result_var.set("")
        self._open_btn.config(state=tk.DISABLED)
        self._last_csv = None

        self._running = True
        self._run_btn.config(state=tk.DISABLED, text="\u23f3  Running\u2026")
        self._status_var.set("Running analysis\u2026")

        from core.batch_analyzer import SourceConfig
        lim = self._limit_var.get()
        source = SourceConfig(
            source_type    = self._source_type.get(),
            url            = self._url_var.get().strip(),
            csv_path       = self._csv_var.get().strip(),
            url_column     = self._col_var.get().strip() or "url",
            site           = self._site_var.get().strip(),
            folder_path    = self._folder_var.get().strip(),
            content_source = self._content_src.get(),
            limit          = lim if lim > 0 else None,
        )
        fields = list(self._active_fields)
        model  = self._model_var.get()
        log_q  = self._log_queue

        def _worker():
            if sys.platform == "win32":
                asyncio.set_event_loop(asyncio.ProactorEventLoop())
            else:
                asyncio.set_event_loop(asyncio.new_event_loop())
            try:
                results, csv_path = run_batch_combined(
                    source=source, fields=fields, model=model, log_fn=log_q.put)
                log_q.put(("__done__", results, csv_path))
            except Exception as exc:
                log_q.put(("__error__", str(exc)))

        threading.Thread(target=_worker, daemon=True).start()

    # ── Log polling ──────────────────────────────────────────────────────────

    def _poll_log(self):
        try:
            while True:
                item = self._log_queue.get_nowait()
                if isinstance(item, tuple):
                    if item[0] == "__done__":
                        self._on_done(item[1], item[2])
                    elif item[0] == "__error__":
                        self._on_error(item[1])
                else:
                    self._append_log(str(item) + "\n")
        except queue.Empty:
            pass

        if self._running:
            self._spin_idx = (self._spin_idx + 1) % len(self._SPINNER)
            self._spinner_lbl.config(text=self._SPINNER[self._spin_idx])

        self.after(120, self._poll_log)

    def _append_log(self, text: str, tag: str = ""):
        self._log.config(state=tk.NORMAL)
        if not tag:
            if "\u2713" in text or text.strip().startswith("Done"):
                tag = "ok"
            elif "\u26a0" in text or "WARN" in text:
                tag = "warn"
            elif "\u2717" in text or "ERROR" in text or "FATAL" in text:
                tag = "error"
            elif text.startswith("[fetch") or "Resolving" in text or "Pages to" in text:
                tag = "info"
            elif text.startswith("\u2500") or "Report" in text:
                tag = "done"
        self._log.insert(tk.END, text, tag or "")
        self._log.see(tk.END)
        self._log.config(state=tk.DISABLED)

    def _on_done(self, results: list[dict], csv_path: Optional[str]):
        self._running = False
        self._run_btn.config(state=tk.NORMAL, text="\u25b6  Run Analysis")
        self._spinner_lbl.config(text="")
        total  = len(results)
        errors = sum(1 for r in results if r.get("error"))
        self._status_var.set(
            f"Done \u2014 {total} pages  \u00b7  {total-errors} successful  \u00b7  {errors} error(s)")
        self._last_csv = csv_path
        if csv_path:
            self._result_var.set(f"Saved \u2192 {csv_path}")
            self._open_btn.config(state=tk.NORMAL)
        else:
            self._result_var.set("No pages written.")

    def _on_error(self, msg: str):
        self._running = False
        self._run_btn.config(state=tk.NORMAL, text="\u25b6  Run Analysis")
        self._spinner_lbl.config(text="")
        self._status_var.set("Run failed \u2014 see log for details.")
        self._append_log(f"\n[FATAL ERROR]\n{msg}\n", tag="error")
        messagebox.showerror("Run failed", msg, parent=self)

    def _open_csv(self):
        if not self._last_csv or not os.path.exists(self._last_csv):
            return
        if sys.platform == "win32":
            os.startfile(self._last_csv)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", self._last_csv])
        else:
            subprocess.Popen(["xdg-open", self._last_csv])


# ══════════════════════════════════════════════════════════════
#  Utilities
# ══════════════════════════════════════════════════════════════

def _center(win: tk.Toplevel, parent: tk.Tk, w: int, h: int):
    # Cap height to 90% of screen so dialogs never overflow on small monitors
    screen_h = parent.winfo_screenheight()
    h = min(h, int(screen_h * 0.90))
    px = parent.winfo_rootx() + (parent.winfo_width()  - w) // 2
    py = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
    win.geometry(f"{w}x{h}+{max(0, px)}+{max(0, py)}")


# ══════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app = PageAnalyzerApp()
    app.mainloop()