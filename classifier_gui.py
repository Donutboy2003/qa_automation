"""
classifier_gui.py — Page Classifier (Tkinter)
==============================================
Structured around the UAlberta audience classification prompt.

What the user can customise
  1. Classification criteria  — free-text textarea (role, definitions, rules, any
                                keyword signals if needed — written directly in the text)
  2. Output categories        — editable pill list (Internal, External, Mixed, …)

What is fixed
  Output schema per page:
    classification       — one of the user's category labels
    confidence_score     — float 0.0–1.0
    primary_indicators   — 3 phrases from the page that drove the decision
    reasoning            — one sentence explanation

Run:
    python classifier_gui.py
"""

from __future__ import annotations

import asyncio
import csv
import os
import queue
import subprocess
import sys
import threading
from tkinter import filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
from tkinter import ttk
import tkinter as tk
from typing import Any, Optional


# ══════════════════════════════════════════════════════════════
#  Theme
# ══════════════════════════════════════════════════════════════

NAVY   = "#0f172a"
CYAN   = "#22d3ee"
WHITE  = "#ffffff"
OFF_W  = "#f8fafc"
LGRAY  = "#e2e8f0"
MGRAY  = "#94a3b8"
DGRAY  = "#475569"
YELLOW = "#fef9c3"
YFORE  = "#78350f"
BLUE   = "#dbeafe"
BFORE  = "#1e40af"
RED    = "#ef4444"
GREEN  = "#22c55e"
AMBER  = "#f59e0b"
PURPLE = "#f3e8ff"
PFORE  = "#7c3aed"

MONO  = ("Consolas", 9)
SANS  = ("Segoe UI", 10)
SANSB = ("Segoe UI", 10, "bold")
SANSS = ("Segoe UI", 9)
SANS8 = ("Segoe UI", 8)
TITLE = ("Segoe UI", 13, "bold")


# ══════════════════════════════════════════════════════════════
#  Defaults  (match the user's prompt exactly)
# ══════════════════════════════════════════════════════════════

DEFAULT_CATEGORIES = ["Internal", "External", "Mixed", "Unclassified"]

DEFAULT_CRITERIA = """\
### Role
You are an expert Content Strategist and Web Auditor for a major University. \
Your task is to classify web pages based on their target audience using the \
provided URL and scraped page content.

### Definitions

INTERNAL CONTENT:
  Audience: Current employees (support staff, researchers, instructors, \
librarians, faculty) and current students.
  Purpose: Navigating internal university processes, accessing private services, \
HR forms, internal policy documents, and student portal information.

EXTERNAL CONTENT:
  Audience: Prospective students, prospective employees (job-seekers), partners, \
funders (governments, donors), alumni, and the general public.
  Purpose: Marketing, admissions requirements, application processes, job listings, \
research impact showcases, donation/giving pages, and public-facing university services.

### Classification Rules
1. Internal:      Aimed >80% at current students/staff.
2. External:      Aimed >80% at prospective/public audiences.
3. Mixed:         Use if the page content is split. If no single audience clearly \
controls at least 80% of the intent, classify as "Mixed."
4. Unclassified:  Use only if the page contains insufficient information to make \
a determination.

### Keyword Red Flags (Priority Indicators)
  - Internal Indicators: [Canvas, Blackboard, Employee Benefits Portal, Faculty Handbook]
  - External Indicators: [Apply Now, Donate, Alumni Association, Campus Tours]\
"""


# ══════════════════════════════════════════════════════════════
#  Prompt builder
# ══════════════════════════════════════════════════════════════

def build_prompt(
    text:       str,
    url:        str,
    criteria:   str,
    categories: list[str],
) -> str:
    cats_inline = ", ".join(f'"{c}"' for c in categories)

    schema = (
        "{\n"
        f'  "classification": one of {cats_inline},\n'
        '  "confidence_score": float 0.0-1.0,\n'
        '  "primary_indicators": array of exactly 3 strings - keywords/phrases '
        'from the page that drove the decision,\n'
        '  "reasoning": string - one concise sentence explaining the classification\n'
        "}"
    )

    return (
        f"{criteria.strip()}\n\n"
        f"### Valid Categories\n"
        f"  {cats_inline}\n\n"
        f"### Output Format\n"
        f"You must respond strictly in JSON format with the following keys:\n"
        f"{schema}\n\n"
        f"No markdown fences. No extra commentary.\n\n"
        f"URL: {url}\n\n"
        f"Page Content:\n{text}"
    )


# ══════════════════════════════════════════════════════════════
#  Per-page analysis
# ══════════════════════════════════════════════════════════════

def analyze_page(
    text:       str,
    url:        str,
    criteria:   str,
    categories: list[str],
    model:      str,
    max_chars:  int = 12_000,
) -> dict[str, Any]:
    from utils.llm_helpers import call_llm_json, truncate_text

    result: dict[str, Any] = {"url": url, "error": None}
    if not text.strip():
        result["error"] = "No content"
        return result
    try:
        content = truncate_text(text.strip(), max_chars=max_chars)
        prompt  = build_prompt(content, url, criteria, categories)
        parsed  = call_llm_json(prompt=prompt, model=model)
    except Exception as exc:
        result["error"] = str(exc)
        return result
    if parsed.get("_parse_error"):
        result["error"] = f"JSON parse error. Raw: {parsed.get('_raw', '')}"
        return result
    result.update(parsed)
    return result


# ══════════════════════════════════════════════════════════════
#  Batch runner
# ══════════════════════════════════════════════════════════════

OUTPUT_FIELDS = ["classification", "confidence_score", "primary_indicators", "reasoning"]


def run_batch(
    source,
    criteria:   str,
    categories: list[str],
    model:      str,
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

            chunk_data: list[tuple[int, str, dict]] = []
            for li, url in enumerate(chunk_urls):
                gi = chunk_start + li
                log(f"[fetch {gi+1}/{total}] {url}")
                content = extract_text(source=source.content_source, url=url, browser=browser)
                if content.get("error"):
                    log(f"  \u26a0 {content['error']}")
                chunk_data.append((gi, url, content))

            def _task(args: tuple) -> tuple[int, dict]:
                idx, url, c = args
                txt = c.get("text", "")
                if not txt.strip():
                    return idx, {
                        "url":   url,
                        "title": c.get("title", ""),
                        "error": c.get("error") or "No content",
                    }
                res = analyze_page(
                    txt, url, criteria, categories, model,
                )
                res.setdefault("title", c.get("title", ""))
                return idx, res

            with ThreadPoolExecutor(max_workers=LLM_WORKERS) as pool:
                futures = {pool.submit(_task, item): item[0] for item in chunk_data}
                for future in as_completed(futures):
                    idx, res = future.result()
                    all_results[idx] = res

            for li in range(len(chunk_urls)):
                gi  = chunk_start + li
                res = all_results[gi]
                if res.get("error"):
                    log(f"  [{gi+1}/{total}] \u2717 ERROR \u2014 {res['error']}")
                else:
                    log(f"  [{gi+1}/{total}] \u2713 {res.get('url', '')}  \u2192  {res.get('classification', '?')}")

                row: dict[str, Any] = {
                    "url":   res.get("url",   ""),
                    "title": res.get("title", ""),
                    "error": res.get("error", ""),
                }
                for key in OUTPUT_FIELDS:
                    val = res.get(key, "")
                    if isinstance(val, list):
                        val = " | ".join(str(v) for v in val)
                    row[key] = str(val) if val is not None else ""

                if csv_writer is None:
                    os.makedirs("reports", exist_ok=True)
                    safe        = (source.site or "batch").replace("/", "_")
                    csv_path    = os.path.abspath(f"reports/classification_{safe}.csv")
                    report_file = open(csv_path, "w", newline="", encoding="utf-8")
                    csv_writer  = csv.DictWriter(report_file, fieldnames=list(row.keys()))
                    csv_writer.writeheader()

                csv_writer.writerow(row)
                report_file.flush()

        if report_file:
            report_file.close()
        errors_count = sum(1 for r in all_results if r.get("error"))
        log(f"\n{chr(8212)*42}")
        log(f"Done \u2014 {total} page(s)  \u00b7  {errors_count} error(s)")
        if csv_path:
            log(f"Report \u2192 {csv_path}")
        return all_results, csv_path

    if source.content_source == "live":
        from utils.browser_helpers import BrowserSession
        with BrowserSession() as browser:
            return do_run(browser)
    return do_run()


# ══════════════════════════════════════════════════════════════
#  PillList widget  (reusable for categories + keywords)
# ══════════════════════════════════════════════════════════════

class PillList(tk.Frame):
    """
    Editable list of labels rendered as coloured pills.
      pill_colors : list of (bg, fg) tuples, cycled by index
      min_items   : removal is blocked when len == min_items
    """

    def __init__(
        self,
        parent,
        initial:     list[str],
        pill_colors: list[tuple] | None = None,
        min_items:   int = 0,
        placeholder: str = "Type and press Enter or click Add",
        **kw,
    ):
        super().__init__(parent, bg=WHITE, **kw)
        self._items      = list(initial)
        self._colors     = pill_colors or [
            (BLUE, BFORE), (YELLOW, YFORE), (PURPLE, PFORE), ("#dcfce7", "#166534"),
        ]
        self._min        = min_items
        self._placeholder = placeholder
        self._change_cb  = None

        # Pill area
        pill_wrap = tk.Frame(self, bg=LGRAY, bd=0)
        pill_wrap.pack(fill=tk.X)
        self._pill_inner = tk.Frame(pill_wrap, bg=WHITE)
        self._pill_inner.pack(fill=tk.X, padx=1, pady=1)

        # Add row
        add_row = tk.Frame(self, bg=WHITE)
        add_row.pack(fill=tk.X, pady=(5, 0))
        self._entry_var = tk.StringVar()
        self._entry = tk.Entry(
            add_row, textvariable=self._entry_var, font=SANS,
            relief=tk.FLAT, highlightthickness=1, highlightbackground=LGRAY,
        )
        self._entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=5)
        self._entry.bind("<Return>", lambda _: self._add())
        tk.Button(
            add_row, text="\uff0b Add",
            font=SANSS, bg=BLUE, fg=BFORE, bd=0, padx=10, pady=5,
            cursor="hand2", command=self._add,
        ).pack(side=tk.LEFT, padx=(6, 0))

        self._refresh()

    def _refresh(self):
        for w in self._pill_inner.winfo_children():
            w.destroy()

        if not self._items:
            tk.Label(self._pill_inner, text=f"  {self._placeholder}",
                     font=SANSS, bg=WHITE, fg=MGRAY).pack(anchor="w", pady=6)
        else:
            row = tk.Frame(self._pill_inner, bg=WHITE)
            row.pack(fill=tk.X, padx=6, pady=6)
            for i, item in enumerate(self._items):
                bg, fg = self._colors[i % len(self._colors)]
                pill = tk.Frame(row, bg=bg)
                pill.pack(side=tk.LEFT, padx=(0, 6), pady=2)
                tk.Label(pill, text=item, font=SANSB,
                         bg=bg, fg=fg, padx=8, pady=3).pack(side=tk.LEFT)
                tk.Button(
                    pill, text="\u2715",
                    font=SANS8, bg=bg, fg=fg, bd=0,
                    cursor="hand2", activeforeground=RED,
                    command=lambda idx=i: self._remove(idx),
                ).pack(side=tk.LEFT, padx=(0, 4))

        if self._change_cb:
            self._change_cb()

    def _add(self):
        name = self._entry_var.get().strip()
        if not name:
            return
        if name in self._items:
            messagebox.showwarning("Duplicate", f'"{name}" is already in the list.',
                                   parent=self)
            return
        self._items.append(name)
        self._entry_var.set("")
        self._refresh()

    def _remove(self, idx: int):
        if len(self._items) <= self._min:
            messagebox.showwarning(
                "Cannot remove",
                f"At least {self._min} item(s) required.",
                parent=self)
            return
        self._items.pop(idx)
        self._refresh()

    def get(self) -> list[str]:
        return list(self._items)

    def reset(self, defaults: list[str]):
        self._items = list(defaults)
        self._refresh()

    def on_change(self, cb):
        self._change_cb = cb


# ══════════════════════════════════════════════════════════════
#  CollapsibleSection
# ══════════════════════════════════════════════════════════════

class CollapsibleSection(tk.Frame):
    def __init__(self, parent, title: str, subtitle: str = "",
                 start_open: bool = True, **kw):
        super().__init__(parent, bg=WHITE, **kw)
        self._open = start_open

        hdr = tk.Frame(self, bg=LGRAY, cursor="hand2")
        hdr.pack(fill=tk.X)
        hdr.bind("<Button-1>", self._toggle)

        self._arrow = tk.Label(hdr, text="\u25be" if start_open else "\u25b8",
                               font=SANSB, bg=LGRAY, fg=NAVY)
        self._arrow.pack(side=tk.LEFT, padx=(10, 4), pady=6)
        self._arrow.bind("<Button-1>", self._toggle)

        col = tk.Frame(hdr, bg=LGRAY)
        col.pack(side=tk.LEFT, fill=tk.X, expand=True, pady=4)
        col.bind("<Button-1>", self._toggle)
        tk.Label(col, text=title, font=SANSB, bg=LGRAY, fg=NAVY,
                 anchor="w").pack(anchor="w")
        if subtitle:
            tk.Label(col, text=subtitle, font=SANS8, bg=LGRAY, fg=DGRAY,
                     anchor="w").pack(anchor="w")

        self._body = tk.Frame(self, bg=WHITE)
        if start_open:
            self._body.pack(fill=tk.X)

    def _toggle(self, _=None):
        if self._open:
            self._body.pack_forget()
            self._arrow.config(text="\u25b8")
        else:
            self._body.pack(fill=tk.X)
            self._arrow.config(text="\u25be")
        self._open = not self._open

    @property
    def body(self) -> tk.Frame:
        return self._body


# ══════════════════════════════════════════════════════════════
#  Main application
# ══════════════════════════════════════════════════════════════

class ClassifierApp(tk.Tk):

    _SPINNER = "\u25d0\u25d3\u25d1\u25d2"

    def __init__(self):
        super().__init__()
        self.title("Page Classifier")
        self.configure(bg=WHITE)
        self.geometry("1320x860")
        self.minsize(960, 660)

        self._running   = False
        self._log_queue: queue.Queue = queue.Queue()
        self._last_csv: Optional[str] = None
        self._spin_idx  = 0

        self._build_ui()
        self.after(200, self._update_preview)

    # ── Top-level layout ─────────────────────────────────────────────────────

    def _build_ui(self):
        bar = tk.Frame(self, bg=NAVY, height=50)
        bar.pack(fill=tk.X)
        bar.pack_propagate(False)
        tk.Label(bar, text="\u2b21  Page Classifier",
                 font=("Consolas", 13, "bold"), bg=NAVY, fg=CYAN,
                 ).pack(side=tk.LEFT, padx=20, pady=12)
        tk.Label(bar,
                 text="Audience classification  \u00b7  edit criteria, keywords & output categories",
                 font=SANSS, bg=NAVY, fg=MGRAY).pack(side=tk.LEFT)

        body = tk.Frame(self, bg=LGRAY)
        body.pack(fill=tk.BOTH, expand=True)

        # Three columns: config (fixed) | preview (fixed) | log (expands)
        left   = tk.Frame(body, bg=WHITE, width=500)
        middle = tk.Frame(body, bg=OFF_W, width=360)
        right  = tk.Frame(body, bg=OFF_W)
        left.pack(side=tk.LEFT, fill=tk.BOTH)
        middle.pack(side=tk.LEFT, fill=tk.BOTH)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        left.pack_propagate(False)
        middle.pack_propagate(False)

        self._build_config(left)
        self._build_preview(middle)
        self._build_log(right)

    # ══════════════════════════════════════════════════════════
    #  Config column
    # ══════════════════════════════════════════════════════════

    def _build_config(self, parent):
        # Scrollable wrapper for the entire left column
        outer  = tk.Frame(parent, bg=WHITE)
        outer.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(outer, bg=WHITE, bd=0, highlightthickness=0)
        vsb    = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        body = tk.Frame(canvas, bg=WHITE)
        win  = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>",
                  lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(win, width=e.width))
        canvas.bind_all(
            "<MouseWheel>",
            lambda e: canvas.yview_scroll(int(-1*(e.delta/120)), "units"))

        P = 12   # horizontal pad inside sections

        # ── Source ────────────────────────────────────────────────────────

        s1 = CollapsibleSection(body, "Source",
                                "Where to fetch pages from", start_open=True)
        s1.pack(fill=tk.X, pady=(0, 1))
        sp = tk.Frame(s1.body, bg=WHITE, padx=P, pady=8)
        sp.pack(fill=tk.X)
        self._build_source(sp)

        # ── Output Categories ─────────────────────────────────────────────

        s2 = CollapsibleSection(
            body, "Output Categories",
            "The LLM returns exactly one of these labels per page",
            start_open=True)
        s2.pack(fill=tk.X, pady=(0, 1))
        cp = tk.Frame(s2.body, bg=WHITE, padx=P, pady=8)
        cp.pack(fill=tk.X)

        tk.Label(cp,
                 text="Add, remove or rename to match your classification scheme. "
                      "Changes are reflected instantly in the preview.",
                 font=SANS8, bg=WHITE, fg=MGRAY, wraplength=450,
                 justify=tk.LEFT).pack(anchor="w", pady=(0, 6))

        self._cat_list = PillList(
            cp, DEFAULT_CATEGORIES, min_items=1,
            placeholder="e.g. Vendor Information",
            pill_colors=[(BLUE, BFORE), (YELLOW, YFORE),
                         (PURPLE, PFORE), ("#dcfce7", "#166534")],
        )
        self._cat_list.pack(fill=tk.X)
        self._cat_list.on_change(lambda: self.after_idle(self._update_preview))
        tk.Button(cp, text="\u21ba Reset to defaults",
                  font=SANSS, bg=LGRAY, fg=NAVY, bd=0, padx=8, pady=3,
                  cursor="hand2",
                  command=lambda: self._cat_list.reset(DEFAULT_CATEGORIES),
                  ).pack(anchor="w", pady=(6, 0))

        # ── Classification Criteria ───────────────────────────────────────

        s4 = CollapsibleSection(
            body, "Classification Criteria",
            "Role, definitions & rules — sent verbatim at the top of every prompt",
            start_open=True)
        s4.pack(fill=tk.X, pady=(0, 1))
        crp = tk.Frame(s4.body, bg=WHITE, padx=P, pady=8)
        crp.pack(fill=tk.X)

        tk.Label(crp,
                 text="Write any keyword signals directly here if your classification "
                      "scheme needs them (e.g. Internal Indicators: Canvas, Blackboard).",
                 font=SANS8, bg=WHITE, fg=MGRAY,
                 wraplength=450, justify=tk.LEFT).pack(anchor="w", pady=(0, 6))

        self._criteria_text = tk.Text(
            crp, height=17, font=SANS, wrap=tk.WORD,
            bg=OFF_W, fg=NAVY, insertbackground=NAVY,
            relief=tk.FLAT, highlightthickness=1, highlightbackground=LGRAY,
        )
        self._criteria_text.pack(fill=tk.X, ipady=6)
        self._criteria_text.insert("1.0", DEFAULT_CRITERIA)
        self._criteria_text.bind("<KeyRelease>",
                                  lambda _: self.after_idle(self._update_preview))
        tk.Button(crp, text="\u21ba Reset to default",
                  font=SANSS, bg=LGRAY, fg=NAVY, bd=0, padx=8, pady=3,
                  cursor="hand2", command=self._reset_criteria,
                  ).pack(anchor="w", pady=(6, 0))

        # ── Run button ────────────────────────────────────────────────────

        tk.Frame(body, bg=LGRAY, height=1).pack(fill=tk.X, pady=(8, 0))
        self._run_btn = tk.Button(
            body, text="\u25b6  Run Classification",
            font=("Segoe UI", 11, "bold"),
            bg=NAVY, fg=CYAN, bd=0, pady=10, cursor="hand2",
            activebackground="#1e293b", activeforeground=CYAN,
            command=self._run,
        )
        self._run_btn.pack(fill=tk.X, padx=10, pady=(8, 14))

    # ── Source sub-form ──────────────────────────────────────────────────────

    def _build_source(self, parent):
        def lrow(label):
            r = tk.Frame(parent, bg=WHITE)
            r.pack(fill=tk.X, pady=2)
            tk.Label(r, text=label, font=SANSS, bg=WHITE, fg=DGRAY,
                     width=11, anchor="w").pack(side=tk.LEFT)
            return r

        r0 = lrow("Type:")
        self._source_type = tk.StringVar(value="sitemap")
        for v, lbl in (("sitemap", "Sitemap"), ("url", "Single URL"), ("csv", "CSV file")):
            tk.Radiobutton(r0, text=lbl, variable=self._source_type, value=v,
                           font=SANSS, bg=WHITE, activebackground=WHITE,
                           command=self._refresh_source_rows,
                           ).pack(side=tk.LEFT, padx=3)

        sg = tk.Frame(parent, bg=WHITE)
        sg.pack(fill=tk.X, pady=(2, 0))
        sg.columnconfigure(1, weight=1)

        def glbl(txt, row):
            tk.Label(sg, text=txt, font=SANSS, bg=WHITE, fg=DGRAY,
                     width=11, anchor="w").grid(row=row, column=0, sticky="w", pady=2)

        def gentry(row, var):
            e = tk.Entry(sg, textvariable=var, font=SANS,
                         relief=tk.FLAT, highlightthickness=1, highlightbackground=LGRAY)
            e.grid(row=row, column=1, sticky="ew", ipady=4, pady=2)
            return e

        self._site_var   = tk.StringVar()
        self._folder_var = tk.StringVar()
        self._url_var    = tk.StringVar()
        self._csv_var    = tk.StringVar()
        self._col_var    = tk.StringVar(value="url")

        glbl("Site slug:", 0);  gentry(0, self._site_var)
        glbl("Folder:",    1);  gentry(1, self._folder_var)
        glbl("URL:",       2);  gentry(2, self._url_var)

        glbl("CSV path:", 3)
        cr = tk.Frame(sg, bg=WHITE)
        cr.grid(row=3, column=1, sticky="ew", pady=2)
        tk.Entry(cr, textvariable=self._csv_var, font=SANS,
                 relief=tk.FLAT, highlightthickness=1, highlightbackground=LGRAY,
                 ).pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)
        tk.Button(cr, text="\u2026", font=SANS, bg=LGRAY, bd=0, padx=4,
                  command=self._browse_csv).pack(side=tk.LEFT, padx=4)

        glbl("URL column:", 4);  gentry(4, self._col_var)

        self._src_grid_rows  = {"sitemap": [0, 1], "url": [2], "csv": [3, 4]}
        self._src_grid_frame = sg
        for key in ("url", "csv"):
            for row_i in self._src_grid_rows[key]:
                for w in sg.grid_slaves(row=row_i):
                    w.grid_remove()

        rc = lrow("Content:")
        self._content_src = tk.StringVar(value="live")
        for v, lbl in (("live", "Live (browser)"), ("cascade", "Cascade API")):
            tk.Radiobutton(rc, text=lbl, variable=self._content_src, value=v,
                           font=SANSS, bg=WHITE, activebackground=WHITE,
                           ).pack(side=tk.LEFT, padx=3)

        rl = lrow("Limit:")
        self._limit_var = tk.IntVar(value=0)
        tk.Spinbox(rl, from_=0, to=9999, textvariable=self._limit_var,
                   width=6, font=SANS).pack(side=tk.LEFT)
        tk.Label(rl, text=" pages  (0 = all)", font=SANSS, bg=WHITE, fg=MGRAY,
                 ).pack(side=tk.LEFT)

        rm = lrow("Model:")
        self._model_var = tk.StringVar(value="gpt-4.1-mini")
        ttk.Combobox(rm, textvariable=self._model_var, width=18,
                     values=["gpt-4.1-mini", "gpt-4.1", "gpt-4o", "gpt-4o-mini"],
                     ).pack(side=tk.LEFT)

    # ══════════════════════════════════════════════════════════
    #  Preview column
    # ══════════════════════════════════════════════════════════

    def _build_preview(self, parent):
        hdr = tk.Frame(parent, bg=LGRAY, height=50)
        hdr.pack(fill=tk.X)
        hdr.pack_propagate(False)
        tk.Label(hdr, text="Live Prompt Preview",
                 font=SANSB, bg=LGRAY, fg=NAVY).pack(side=tk.LEFT, padx=12, pady=10)
        tk.Label(hdr, text="read-only",
                 font=SANSS, bg=LGRAY, fg=MGRAY).pack(side=tk.LEFT)

        tk.Label(parent,
                 text="Exactly what is sent to the LLM for every page. "
                      "Updates as you edit.",
                 font=SANS8, bg=OFF_W, fg=MGRAY,
                 wraplength=320, justify=tk.LEFT,
                 ).pack(anchor="w", padx=10, pady=(8, 4))

        self._preview_text = ScrolledText(
            parent, font=("Consolas", 8), bg=NAVY, fg="#94a3b8",
            insertbackground=CYAN, bd=0, relief=tk.FLAT,
            wrap=tk.WORD, state=tk.DISABLED,
        )
        self._preview_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        self._preview_text.tag_config("hed",    foreground=CYAN)
        self._preview_text.tag_config("schema", foreground=AMBER)
        self._preview_text.tag_config("cats",   foreground=GREEN)
        self._preview_text.tag_config("kw",     foreground="#f9a8d4")

    def _update_preview(self):
        criteria   = self._criteria_text.get("1.0", tk.END).strip()
        categories = self._cat_list.get()

        cats_inline = ", ".join(f'"{c}"' for c in categories) if categories else "(none)"

        schema = (
            "{\n"
            f'  "classification": one of {cats_inline},\n'
            '  "confidence_score": float 0.0-1.0,\n'
            '  "primary_indicators": array of exactly 3 strings,\n'
            '  "reasoning": string - one sentence\n'
            "}"
        )

        self._preview_text.config(state=tk.NORMAL)
        self._preview_text.delete("1.0", tk.END)

        def w(text, tag=""):
            self._preview_text.insert(tk.END, text, tag)

        w(criteria + "\n\n")
        w("### Valid Categories\n", "hed")
        w(f"  {cats_inline}\n\n", "cats")
        w("### Output Format\n", "hed")
        w("You must respond strictly in JSON format:\n")
        w(schema + "\n\n", "schema")
        w("No markdown fences. No extra commentary.\n\n")
        w("URL: <page url>\n\n")
        w("Page Content:\n<scraped text...>")
        self._preview_text.config(state=tk.DISABLED)

    # ══════════════════════════════════════════════════════════
    #  Log column
    # ══════════════════════════════════════════════════════════

    def _build_log(self, parent):
        # Fixed output schema info bar
        info = tk.Frame(parent, bg=BLUE, height=50)
        info.pack(fill=tk.X)
        info.pack_propagate(False)
        tk.Label(info,
                 text="Fixed output columns: "
                      " classification  \u00b7  confidence_score  "
                      "\u00b7  primary_indicators  \u00b7  reasoning",
                 font=SANSS, bg=BLUE, fg=BFORE, anchor="w",
                 ).pack(side=tk.LEFT, padx=14, pady=16)

        sb = tk.Frame(parent, bg=LGRAY, height=34)
        sb.pack(fill=tk.X)
        sb.pack_propagate(False)
        self._status_var = tk.StringVar(
            value="Ready \u2014 configure and click \u25b6 Run Classification.")
        tk.Label(sb, textvariable=self._status_var, font=SANSS,
                 bg=LGRAY, fg=NAVY, anchor="w").pack(side=tk.LEFT, padx=12, pady=7)
        self._spinner_lbl = tk.Label(sb, text="",
                                      font=("Consolas", 11, "bold"), bg=LGRAY, fg=CYAN)
        self._spinner_lbl.pack(side=tk.RIGHT, padx=12)

        log_frame = tk.LabelFrame(parent, text=" Run Log ", font=SANSB,
                                   bg=OFF_W, fg=NAVY, bd=1, relief=tk.GROOVE)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(8, 4))
        self._log = ScrolledText(
            log_frame, font=MONO, bg=NAVY, fg="#94a3b8",
            insertbackground=CYAN, bd=0, relief=tk.FLAT,
            wrap=tk.WORD, state=tk.DISABLED,
        )
        self._log.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        for tag, fg in (("ok", GREEN), ("warn", AMBER), ("error", RED),
                         ("info", CYAN), ("done", "#e2e8f0")):
            self._log.tag_config(tag, foreground=fg)

        result_row = tk.Frame(parent, bg=OFF_W)
        result_row.pack(fill=tk.X, padx=10, pady=(0, 8))
        self._result_var = tk.StringVar(value="")
        tk.Label(result_row, textvariable=self._result_var,
                 font=SANSS, bg=OFF_W, fg=NAVY, anchor="w").pack(side=tk.LEFT)
        self._open_btn = tk.Button(
            result_row, text="\U0001f4c2  Open CSV",
            font=SANSS, bg=NAVY, fg=WHITE, bd=0, padx=10, pady=4,
            cursor="hand2", state=tk.DISABLED, command=self._open_csv,
        )
        self._open_btn.pack(side=tk.RIGHT)

    # ══════════════════════════════════════════════════════════
    #  Source row toggling
    # ══════════════════════════════════════════════════════════

    def _refresh_source_rows(self, *_):
        st = self._source_type.get()
        sg = self._src_grid_frame
        for key, rows in self._src_grid_rows.items():
            for row_i in rows:
                for w in sg.grid_slaves(row=row_i):
                    if key == st:
                        w.grid()
                    else:
                        w.grid_remove()

    def _browse_csv(self):
        path = filedialog.askopenfilename(
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if path:
            self._csv_var.set(path)

    def _reset_criteria(self):
        self._criteria_text.delete("1.0", tk.END)
        self._criteria_text.insert("1.0", DEFAULT_CRITERIA)
        self._update_preview()

    # ══════════════════════════════════════════════════════════
    #  Validation + Run
    # ══════════════════════════════════════════════════════════

    def _validate(self) -> list[str]:
        errors: list[str] = []
        st = self._source_type.get()
        if st == "sitemap" and not self._site_var.get().strip():
            errors.append("Site slug is required (e.g. 'engineering').")
        if st == "url" and not self._url_var.get().strip():
            errors.append("URL is required.")
        if st == "csv" and not self._csv_var.get().strip():
            errors.append("CSV file path is required.")
        if not self._cat_list.get():
            errors.append("Add at least one output category.")
        if not self._criteria_text.get("1.0", tk.END).strip():
            errors.append("Classification criteria cannot be empty.")
        return errors

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
        self._running  = True
        self._run_btn.config(state=tk.DISABLED, text="\u23f3  Running\u2026")
        self._status_var.set("Running classification\u2026")
        self.after(120, self._poll_log)

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
        criteria   = self._criteria_text.get("1.0", tk.END).strip()
        categories = self._cat_list.get()
        model      = self._model_var.get()
        log_q      = self._log_queue

        def _worker():
            if sys.platform == "win32":
                asyncio.set_event_loop(asyncio.ProactorEventLoop())
            else:
                asyncio.set_event_loop(asyncio.new_event_loop())
            try:
                results, csv_path = run_batch(
                    source=source,
                    criteria=criteria,
                    categories=categories,
                    model=model,
                    log_fn=log_q.put,
                )
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
            if "\u2713" in text or text.strip().startswith("Done"):   tag = "ok"
            elif "\u26a0" in text or "WARN" in text:                  tag = "warn"
            elif "\u2717" in text or "ERROR" in text or "FATAL" in text: tag = "error"
            elif text.startswith("[fetch") or "Resolving" in text:    tag = "info"
            elif text.startswith(chr(8212)) or "Report" in text:      tag = "done"
        self._log.insert(tk.END, text, tag or "")
        self._log.see(tk.END)
        self._log.config(state=tk.DISABLED)

    def _on_done(self, results: list[dict], csv_path: Optional[str]):
        self._running = False
        self._run_btn.config(state=tk.NORMAL, text="\u25b6  Run Classification")
        self._spinner_lbl.config(text="")
        total  = len(results)
        errors = sum(1 for r in results if r.get("error"))
        self._status_var.set(
            f"Done \u2014 {total} pages  \u00b7  {total - errors} successful"
            f"  \u00b7  {errors} error(s)")
        self._last_csv = csv_path
        if csv_path:
            self._result_var.set(f"Saved \u2192 {csv_path}")
            self._open_btn.config(state=tk.NORMAL)
        else:
            self._result_var.set("No pages written.")

    def _on_error(self, msg: str):
        self._running = False
        self._run_btn.config(state=tk.NORMAL, text="\u25b6  Run Classification")
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
#  Entry point
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app = ClassifierApp()
    app.mainloop()