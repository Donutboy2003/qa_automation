# search_replace_gui.py
#
# PyQt5 GUI for finding and replacing text across Cascade CMS pages.
# All logic lives in core/search_replace.py — this file is just the UI shell.
#
# Run: python search_replace_gui.py
# Requires: pip install PyQt5

import sys
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QTextEdit, QPushButton,
    QMessageBox, QCheckBox, QComboBox, QGroupBox,
)
from PyQt5.QtCore import Qt

from core.search_replace import run_search_replace, run_search_replace_site


class SearchReplaceTool(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Cascade CMS — Search & Replace")
        self.setGeometry(100, 100, 750, 600)
        layout = QVBoxLayout()

        # ── Site ──────────────────────────────────────────────────────────────
        layout.addWidget(QLabel("Site Name:"))
        self.site_input = QLineEdit()
        self.site_input.setPlaceholderText("e.g. parking-services")
        layout.addWidget(self.site_input)

        # ── Scope ─────────────────────────────────────────────────────────────
        scope_box = QGroupBox("Scope")
        scope_layout = QVBoxLayout()

        self.entire_site_cb = QCheckBox("Entire site")
        self.entire_site_cb.stateChanged.connect(self._toggle_scope)
        scope_layout.addWidget(self.entire_site_cb)

        scope_layout.addWidget(QLabel("Page Path (single page):"))
        self.page_path_input = QLineEdit()
        self.page_path_input.setPlaceholderText("e.g. /regulations-citations/index")
        scope_layout.addWidget(self.page_path_input)

        scope_layout.addWidget(QLabel("Folder Path (optional, limits entire-site run):"))
        self.folder_path_input = QLineEdit()
        self.folder_path_input.setPlaceholderText("e.g. /regulations-citations/  (leave blank for all pages)")
        self.folder_path_input.setDisabled(True)
        scope_layout.addWidget(self.folder_path_input)

        scope_box.setLayout(scope_layout)
        layout.addWidget(scope_box)

        # ── Search & Replace ──────────────────────────────────────────────────
        sr_box = QGroupBox("Search & Replace")
        sr_layout = QVBoxLayout()

        sr_layout.addWidget(QLabel("Search Term:"))
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Text to find")
        sr_layout.addWidget(self.search_input)

        sr_layout.addWidget(QLabel("Replace With (leave blank to remove matching fields):"))
        self.replace_input = QLineEdit()
        self.replace_input.setPlaceholderText("Replacement text — blank = remove")
        sr_layout.addWidget(self.replace_input)

        sr_box.setLayout(sr_layout)
        layout.addWidget(sr_box)

        # ── Output mode ───────────────────────────────────────────────────────
        mode_layout = QHBoxLayout()
        mode_layout.addWidget(QLabel("Output Mode:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItems([
            "cascade-dev   — write to DEV server (default)",
            "cascade-live  — write to LIVE server",
            "report        — save report only, no Cascade writes",
            "console       — print only, no files or writes",
        ])
        mode_layout.addWidget(self.mode_combo)
        layout.addLayout(mode_layout)

        # ── Report ────────────────────────────────────────────────────────────
        layout.addWidget(QLabel("Operation Log:"))
        self.output_area = QTextEdit()
        self.output_area.setReadOnly(True)
        layout.addWidget(self.output_area)

        # ── Run ───────────────────────────────────────────────────────────────
        self.run_button = QPushButton("Execute")
        self.run_button.clicked.connect(self._execute)
        layout.addWidget(self.run_button)

        self.setLayout(layout)

    # ── UI helpers ────────────────────────────────────────────────────────────

    def _toggle_scope(self):
        entire = self.entire_site_cb.isChecked()
        self.page_path_input.setDisabled(entire)
        self.folder_path_input.setDisabled(not entire)

    def _output_mode(self) -> str:
        return self.mode_combo.currentText().split()[0]

    def _log(self, text: str):
        self.output_area.append(text)
        QApplication.processEvents()

    # ── Execute ───────────────────────────────────────────────────────────────

    def _execute(self):
        site        = self.site_input.text().strip()
        search_term = self.search_input.text()
        replace_term = self.replace_input.text() or None  # None = remove mode
        output_mode = self._output_mode()

        if not site or not search_term:
            QMessageBox.warning(self, "Missing Input", "Site name and search term are required.")
            return

        self.output_area.clear()
        self.run_button.setDisabled(True)

        try:
            if self.entire_site_cb.isChecked():
                folder_path = self.folder_path_input.text().strip()
                self._log(f"Running site-wide replacement on '{site}'...")

                # Redirect print to the log area
                results = _run_with_logging(
                    self._log,
                    run_search_replace_site,
                    site=site,
                    search_term=search_term,
                    replace_term=replace_term,
                    folder_path=folder_path,
                    output_mode=output_mode,
                )

                matched = sum(1 for r in results if r["matches"] > 0)
                written = sum(1 for r in results if r["written"])
                errors  = sum(1 for r in results if r["error"])
                self._log(f"\nDone — {matched} page(s) had matches, {written} written, {errors} error(s)")

            else:
                path = self.page_path_input.text().strip()
                if not path:
                    QMessageBox.warning(self, "Missing Input", "Enter a page path or enable 'Entire site'.")
                    return

                self._log(f"Running on '{site}{path}'...")
                result = _run_with_logging(
                    self._log,
                    run_search_replace,
                    site=site,
                    path=path,
                    search_term=search_term,
                    replace_term=replace_term,
                    output_mode=output_mode,
                )

                if result.get("error"):
                    self._log(f"Error: {result['error']}")
                elif result["matches"] == 0:
                    self._log("No matches found.")
                else:
                    status = "Written to Cascade" if result["written"] else "Not written (output mode)"
                    self._log(f"Matches: {result['matches']} — {status}")

        except Exception as e:
            self._log(f"\n[FATAL ERROR] {e}")
            QMessageBox.critical(self, "Error", str(e))
        finally:
            self.run_button.setDisabled(False)


# ── Print capture helper ──────────────────────────────────────────────────────

def _run_with_logging(log_fn, func, **kwargs):
    """
    Call func(**kwargs) while redirecting stdout to log_fn so the core
    module's print() calls appear in the GUI log area.
    """
    import io

    class _Tee:
        def __init__(self, fn):
            self._fn = fn
            self._buf = ""

        def write(self, text):
            self._buf += text
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line:
                    self._fn(line)

        def flush(self):
            pass

    old_stdout = sys.stdout
    sys.stdout = _Tee(log_fn)
    try:
        return func(**kwargs)
    finally:
        sys.stdout = old_stdout


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = SearchReplaceTool()
    window.show()
    sys.exit(app.exec_())
