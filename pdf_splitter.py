#!/usr/bin/env python3
"""
PDF Splitter v1.0
Split PDFs by TOC/manual page numbers, file size, or file size keeping chapters whole.
Double-click launch.bat (or launch_standalone.bat) to start.
"""

import sys
import os
import io
import re
import zipfile
import threading
import subprocess
from pathlib import Path

APP_NAME    = "PDF Splitter"
APP_VERSION = "1.2"
WIN_W, WIN_H = 840, 720

# ── Dependency auto-install ────────────────────────────────────────────────────

def _check_deps():
    missing = []
    try:
        import pypdf  # noqa
    except ImportError:
        missing.append("pypdf")

    if not missing:
        return True

    try:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk(); r.withdraw()
        ok = messagebox.askyesno(
            f"{APP_NAME} — Install Required Packages",
            "The following package is required but not installed:\n\n"
            + "\n".join(f"  •  {p}" for p in missing)
            + "\n\nInstall now? (internet required, one-time only)"
        )
        r.destroy()
        if not ok:
            return False
    except Exception:
        ans = input(f"Required: {missing}. Install now? [y/n]: ")
        if ans.strip().lower() != "y":
            return False

    for pkg in missing:
        print(f"Installing {pkg}…")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", pkg])
    return True


if not _check_deps():
    sys.exit(0)

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pypdf import PdfReader, PdfWriter

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_DND = True
except ImportError:
    HAS_DND = False

SETTINGS_FILE    = Path(__file__).parent / "pdf_splitter_settings.json"

# Claude's 30 MB limit is SI megabytes (1 MB = 1,000,000 bytes), NOT binary MiB.
# Use 29.5 MB as the hard ceiling to leave a small safety margin for PDF metadata.
CLAUDE_MAX_BYTES = 29_500_000   # 29.5 SI MB  →  guaranteed under Claude's 30 MB limit

# ── Utilities ──────────────────────────────────────────────────────────────────

def fmt_size(b: int) -> str:
    """Format bytes using SI units (1 MB = 1,000,000 bytes) — matches what
    Windows Explorer, Claude, and most file-size displays show."""
    if b >= 1_000_000:
        return f"{b / 1_000_000:.1f} MB"
    if b >= 1_000:
        return f"{b / 1_000:.0f} KB"
    return f"{b} B"


def safe_filename(s: str, fallback: str = "Section") -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(s)).strip()
    return s or fallback


def measure_page_bytes(reader: PdfReader, page_idx: int) -> int:
    """Return byte size of a single page rendered as a standalone PDF."""
    w = PdfWriter()
    w.add_page(reader.pages[page_idx])
    buf = io.BytesIO()
    w.write(buf)
    return len(buf.getvalue())


def extract_toc(reader: PdfReader) -> list:
    """Return sorted [(1-based page, title)] from PDF outline/bookmarks."""
    seen: dict = {}

    def walk(items, depth=0):
        if depth > 8:
            return
        for item in items:
            if isinstance(item, list):
                walk(item, depth + 1)
            else:
                try:
                    pg = reader.get_destination_page_number(item) + 1
                    if pg not in seen:
                        seen[pg] = (item.title or f"Section p.{pg}").strip()
                except Exception:
                    pass

    try:
        walk(reader.outline)
    except Exception:
        pass

    return sorted(seen.items())


def greedy_pack_pages(page_sizes: list, max_bytes: int) -> list:
    """Pack pages into groups <= max_bytes. Returns [(start_0idx, end_0idx)]."""
    OVERHEAD = 4096
    groups, start, running = [], 0, 0
    for i, s in enumerate(page_sizes):
        if running + s + OVERHEAD > max_bytes and i > start:
            groups.append((start, i - 1))
            start, running = i, s
        else:
            running += s
    groups.append((start, len(page_sizes) - 1))
    return groups


def pack_pages_exact(reader: PdfReader, page_indices: list, max_bytes: int) -> list:
    """
    Pack pages into groups where each group's real PDF output is guaranteed
    <= max_bytes.  Builds each group incrementally, serialising to a buffer
    after every page to get the true size (accounts for shared fonts/images).

    page_indices: list of 0-based page indices to pack (in order)
    Returns:      list of lists of 0-based page indices, one sub-list per group
    """
    groups: list = []
    current: list = []

    for idx in page_indices:
        candidate = current + [idx]

        # Measure real size of candidate group
        w = PdfWriter()
        for p in candidate:
            w.add_page(reader.pages[p])
        buf = io.BytesIO()
        w.write(buf)
        real_size = len(buf.getvalue())

        if real_size > max_bytes and current:
            # This page tips us over — finalise current group, start fresh
            groups.append(current)
            current = [idx]
        else:
            current = candidate

    if current:
        groups.append(current)

    return groups
    """Pack chapter dicts into groups <= max_bytes. Returns list of lists."""
    groups, cur, cur_size = [], [], 0
    for ch in chapters:
        if cur_size + ch["size"] > max_bytes and cur:
            groups.append(cur)
            cur, cur_size = [ch], ch["size"]
        else:
            cur.append(ch)
            cur_size += ch["size"]
    if cur:
        groups.append(cur)
    return groups


# ── Main Application ───────────────────────────────────────────────────────────

class PDFSplitterApp:

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"{APP_NAME}  v{APP_VERSION}")
        self.root.geometry(f"{WIN_W}x{WIN_H}")
        self.root.minsize(720, 600)

        self.pdf_path:          str | None       = None
        self.reader:            PdfReader | None = None
        self.total_pages:       int              = 0
        self.split_points:      list             = []   # [[page_1based, name], ...]
        self.output_files:      list             = []
        self.cached_page_sizes: list | None      = None

        # Size vars for each size-based tab (slots 1-3)
        self.sz1_var  = None  # By File Size
        self.sz1_unit = None
        self.sz2_var  = None  # TOC + Max Size
        self.sz2_unit = None
        self.sz3_var  = None  # By File Size + Keep Chapters
        self.sz3_unit = None

        # Split for Claude tab
        self.md_var       = None   # BooleanVar — generate MD index
        self.apikey_var   = None   # StringVar  — Anthropic API key
        self.show_key_var = None   # BooleanVar — show/hide key

        self._build_ui()
        self._load_settings()
        self.notebook.bind("<<NotebookTabChanged>>", lambda _: self._update_split_btn())

    # ── UI Construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)

        # PDF file selection + drag-and-drop zone
        f_file = ttk.LabelFrame(self.root, text="PDF File", padding=8)
        f_file.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 0))
        f_file.columnconfigure(0, weight=1)

        # Drop zone (visual target)
        self._drop_zone = tk.Label(
            f_file,
            text="⬇  Drop a PDF here  ⬇",
            font=("", 11, "bold"),
            relief="groove",
            bd=2,
            pady=12,
            cursor="hand2",
        )
        self._drop_zone.grid(row=0, column=0, sticky="ew", padx=(0, 6), pady=(0, 6))
        self._drop_zone.bind("<Button-1>", lambda _: self.browse_pdf())

        if HAS_DND:
            self._drop_zone.drop_target_register(DND_FILES)
            self._drop_zone.dnd_bind("<<Drop>>",   self._on_drop)
            self._drop_zone.dnd_bind("<<DragEnter>>", self._on_drag_enter)
            self._drop_zone.dnd_bind("<<DragLeave>>", self._on_drag_leave)
            self._drop_zone.config(text="⬇  Drop a PDF here, or click to browse  ⬇")
        else:
            self._drop_zone.config(
                text="Click to browse for a PDF\n"
                     "(install tkinterdnd2 to enable drag-and-drop)",
                font=("", 10),
                pady=8,
            )

        # Path + browse row
        path_row = ttk.Frame(f_file)
        path_row.grid(row=1, column=0, sticky="ew")
        path_row.columnconfigure(0, weight=1)

        self.pdf_var = tk.StringVar(value="No file selected")
        ttk.Entry(path_row, textvariable=self.pdf_var, state="readonly").grid(
            row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(path_row, text="Browse...", command=self.browse_pdf).grid(row=0, column=1)

        self.info_var = tk.StringVar()
        ttk.Label(self.root, textvariable=self.info_var, foreground="#555").grid(
            row=1, column=0, sticky="w", padx=14, pady=(3, 0))

        # Mode notebook
        f_mode = ttk.LabelFrame(self.root, text="Split Mode", padding=8)
        f_mode.grid(row=2, column=0, sticky="nsew", padx=10, pady=(8, 0))
        f_mode.columnconfigure(0, weight=1)
        f_mode.rowconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        self.notebook = ttk.Notebook(f_mode)
        self.notebook.grid(sticky="nsew")

        self._build_tab_manual()
        self._build_tab_toc_maxsize()   # TOC chapters + size cap
        self._build_tab_size()
        self._build_tab_sizechap()
        self._build_tab_claude()        # NEW: Split for Claude + MD index

        # Output folder
        f_out = ttk.LabelFrame(self.root, text="Output Folder", padding=8)
        f_out.grid(row=3, column=0, sticky="ew", padx=10, pady=(8, 0))
        f_out.columnconfigure(0, weight=1)

        self.out_var = tk.StringVar(value="Same folder as source PDF")
        ttk.Entry(f_out, textvariable=self.out_var, state="readonly").grid(
            row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(f_out, text="Browse...", command=self.browse_output).grid(row=0, column=1)
        ttk.Button(f_out, text="Reset",
                   command=lambda: self.out_var.set("Same folder as source PDF")).grid(
            row=0, column=2, padx=(4, 0))

        # Action buttons
        f_act = ttk.Frame(self.root, padding=(10, 8, 10, 0))
        f_act.grid(row=4, column=0, sticky="ew")

        self.split_btn = ttk.Button(f_act, text="Split PDF",
                                    command=self.do_split, state="disabled")
        self.split_btn.pack(side="left", ipadx=16, ipady=4)

        self.zip_btn = ttk.Button(f_act, text="Create ZIP of All Files",
                                  command=self.create_zip, state="disabled")
        self.zip_btn.pack(side="left", padx=(8, 0), ipadx=10, ipady=4)

        self.open_btn = ttk.Button(f_act, text="Open Output Folder",
                                   command=self.open_output_folder, state="disabled")
        self.open_btn.pack(side="right", ipadx=10, ipady=4)

        # Progress
        f_prog = ttk.Frame(self.root, padding=(10, 6, 10, 0))
        f_prog.grid(row=5, column=0, sticky="ew")
        f_prog.columnconfigure(0, weight=1)

        self.progress = ttk.Progressbar(f_prog, mode="determinate", maximum=100)
        self.progress.grid(row=0, column=0, sticky="ew")
        self.status_var = tk.StringVar()
        ttk.Label(f_prog, textvariable=self.status_var, foreground="#555").grid(
            row=1, column=0, sticky="w", pady=(2, 0))

        # Results list
        f_res = ttk.LabelFrame(self.root, text="Output Files", padding=8)
        f_res.grid(row=6, column=0, sticky="ew", padx=10, pady=(6, 10))
        f_res.columnconfigure(0, weight=1)

        self.result_list = tk.Listbox(f_res, height=6, font=("Consolas", 10))
        self.result_list.grid(row=0, column=0, sticky="ew")
        sb = ttk.Scrollbar(f_res, command=self.result_list.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.result_list.config(yscrollcommand=sb.set)

    # ── Tab 1: TOC / Manual ────────────────────────────────────────────────────

    def _build_tab_manual(self):
        f = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(f, text="  TOC / Manual  ")
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)

        ttk.Label(f, text=(
            "Each split point marks the start of a new output file. "
            "TOC bookmarks are auto-detected when a PDF is loaded."
        )).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))

        cols = ("page", "name", "range")
        self.sp_tree = ttk.Treeview(f, columns=cols, show="headings", height=7)
        self.sp_tree.heading("page", text="Start Page")
        self.sp_tree.heading("name", text="Section Name")
        self.sp_tree.heading("range", text="Page Range")
        self.sp_tree.column("page", width=90,  anchor="center", stretch=False)
        self.sp_tree.column("name", width=360)
        self.sp_tree.column("range", width=180, anchor="center", stretch=False)
        self.sp_tree.grid(row=1, column=0, sticky="nsew")
        sb = ttk.Scrollbar(f, command=self.sp_tree.yview)
        sb.grid(row=1, column=1, sticky="ns")
        self.sp_tree.config(yscrollcommand=sb.set)

        f_add = ttk.Frame(f)
        f_add.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        ttk.Label(f_add, text="Page #:").pack(side="left")
        self.sp_page_var = tk.StringVar()
        e_pg = ttk.Entry(f_add, textvariable=self.sp_page_var, width=7)
        e_pg.pack(side="left", padx=(4, 12))
        e_pg.bind("<Return>", lambda _: self.add_split_point())

        ttk.Label(f_add, text="Name:").pack(side="left")
        self.sp_name_var = tk.StringVar()
        e_nm = ttk.Entry(f_add, textvariable=self.sp_name_var, width=30)
        e_nm.pack(side="left", padx=(4, 12))
        e_nm.bind("<Return>", lambda _: self.add_split_point())

        ttk.Button(f_add, text="Add",             command=self.add_split_point).pack(side="left")
        ttk.Button(f_add, text="Remove Selected", command=self.remove_split_point).pack(side="left", padx=(8, 0))
        ttk.Button(f_add, text="Clear All",       command=self.clear_split_points).pack(side="left", padx=(8, 0))

        self.sp_err_var = tk.StringVar()
        ttk.Label(f, textvariable=self.sp_err_var, foreground="red").grid(
            row=3, column=0, sticky="w", pady=(3, 0))

    # ── Tab 2: TOC + Max File Size (chapters split into parts if oversized) ────

    def _build_tab_toc_maxsize(self):
        f = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(f, text="  TOC + Max File Size  ")

        ttk.Label(f, text="Maximum size per output file:", font=("", 10, "bold")).pack(anchor="w")

        f_sz = ttk.Frame(f)
        f_sz.pack(anchor="w", pady=(8, 0))
        self.sz2_var = tk.StringVar(value="5")
        ttk.Entry(f_sz, textvariable=self.sz2_var, width=8).pack(side="left")
        self.sz2_unit = tk.StringVar(value="MB")
        ttk.Radiobutton(f_sz, text="MB", variable=self.sz2_unit, value="MB").pack(side="left", padx=(10, 0))
        ttk.Radiobutton(f_sz, text="KB", variable=self.sz2_unit, value="KB").pack(side="left", padx=(6, 0))

        ttk.Label(f, text=(
            "\nHow it works:\n"
            "  Chapters are defined by TOC bookmarks or your manual split points.\n"
            "  Each chapter is output as a single file where it fits within the limit.\n"
            "  If a chapter exceeds the limit, it is automatically split into\n"
            "  numbered parts using the chapter name as the base:\n\n"
            "      Cooling.pdf  -->  Cooling_1.pdf, Cooling_2.pdf, Cooling_3.pdf\n\n"
            "  This gives clean chapter-based output while enforcing a maximum file size.\n\n"
            "Tip: check the TOC / Manual tab to review or edit chapter definitions first."
        ), foreground="#555", justify="left").pack(anchor="w", pady=(12, 0))

    # ── Tab 3: By File Size ────────────────────────────────────────────────────

    def _build_tab_size(self):
        f = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(f, text="  By File Size  ")

        ttk.Label(f, text="Maximum size per output file:", font=("", 10, "bold")).pack(anchor="w")

        f_sz = ttk.Frame(f)
        f_sz.pack(anchor="w", pady=(8, 0))
        self.sz1_var = tk.StringVar(value="5")
        ttk.Entry(f_sz, textvariable=self.sz1_var, width=8).pack(side="left")
        self.sz1_unit = tk.StringVar(value="MB")
        ttk.Radiobutton(f_sz, text="MB", variable=self.sz1_unit, value="MB").pack(side="left", padx=(10, 0))
        ttk.Radiobutton(f_sz, text="KB", variable=self.sz1_unit, value="KB").pack(side="left", padx=(6, 0))

        ttk.Label(f, text=(
            "\nHow it works:\n"
            "  Each page is measured individually to determine its real compressed size.\n"
            "  Pages are then packed greedily so each output file stays under the limit.\n"
            "  Chapter/section boundaries are not respected in this mode.\n\n"
            "Note: measuring page sizes can take a moment on large PDFs.\n"
            "      Page sizes are cached -- re-splitting at a different size limit is fast."
        ), foreground="#555", justify="left").pack(anchor="w", pady=(12, 0))

    # ── Tab 4: By File Size + Keep Chapters ───────────────────────────────────

    def _build_tab_sizechap(self):
        f = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(f, text="  By File Size + Keep Chapters  ")

        ttk.Label(f, text="Maximum size per output file:", font=("", 10, "bold")).pack(anchor="w")

        f_sz = ttk.Frame(f)
        f_sz.pack(anchor="w", pady=(8, 0))
        self.sz3_var = tk.StringVar(value="5")
        ttk.Entry(f_sz, textvariable=self.sz3_var, width=8).pack(side="left")
        self.sz3_unit = tk.StringVar(value="MB")
        ttk.Radiobutton(f_sz, text="MB", variable=self.sz3_unit, value="MB").pack(side="left", padx=(10, 0))
        ttk.Radiobutton(f_sz, text="KB", variable=self.sz3_unit, value="KB").pack(side="left", padx=(6, 0))

        ttk.Label(f, text=(
            "\nHow it works:\n"
            "  Chapters are defined by TOC bookmarks or your manual split points.\n"
            "  Whole chapters are combined into output files up to the size limit.\n"
            "  A single chapter that exceeds the limit gets its own file (not split).\n"
            "  Falls back to page-level packing if no split points are defined.\n\n"
            "Tip: check the TOC / Manual tab to review or edit chapter definitions first."
        ), foreground="#555", justify="left").pack(anchor="w", pady=(12, 0))

    # ── Tab 5: Split for Claude ────────────────────────────────────────────────

    def _build_tab_claude(self):
        f = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(f, text="  Split for Claude  ")

        # Fixed size info
        info = ttk.LabelFrame(f, text="Split Settings", padding=8)
        info.pack(fill="x", pady=(0, 10))
        ttk.Label(info, text="Max file size:  29.5 MB  (hard limit — guarantees files stay under Claude's 30 MB cap)",
                  font=("", 10, "bold")).pack(anchor="w")
        ttk.Label(info, text=(
            "Uses SI megabytes (1 MB = 1,000,000 bytes) — matching Windows Explorer\n"
            "and Claude's upload checker. Previously 30 MiB (binary) caused overruns."
        ), foreground="#555", justify="left").pack(anchor="w", pady=(4, 0))
        ttk.Label(info, text=(
            "Logic: TOC chapters as individual files. Chapters over 30 MB are\n"
            "split into numbered parts — original chapter file is also kept:\n\n"
            "    02_Cooling.pdf  +  02_Cooling_1.pdf,  02_Cooling_2.pdf, ..."
        ), foreground="#555", justify="left").pack(anchor="w", pady=(6, 0))

        # MD index section
        md_frame = ttk.LabelFrame(f, text="Claude Project Index  (.md file)", padding=10)
        md_frame.pack(fill="x", pady=(0, 0))

        self.md_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(md_frame, text="Generate .md index file after splitting",
                        variable=self.md_var,
                        command=self._refresh_md_options).pack(anchor="w")

        ttk.Label(md_frame, text=(
            "Creates a markdown file listing every output PDF with page range,\n"
            "file size, contents summary, and keywords — ready to upload to a\n"
            "Claude project so Claude knows what's in each file."
        ), foreground="#555", justify="left").pack(anchor="w", pady=(4, 0))

        ttk.Separator(md_frame, orient="horizontal").pack(fill="x", pady=(10, 8))

        # Summary method radio buttons
        ttk.Label(md_frame, text="Summary method:", font=("", 9, "bold")).pack(anchor="w")

        self.md_method_var = tk.StringVar(value="python")
        self._md_options_frame = ttk.Frame(md_frame)
        self._md_options_frame.pack(fill="x", pady=(6, 0))

        ttk.Radiobutton(self._md_options_frame, text="Python auto-generate  (no API key needed)",
                        variable=self.md_method_var, value="python",
                        command=self._refresh_md_options).pack(anchor="w")
        ttk.Label(self._md_options_frame,
                  text="    Extracts text from each PDF and uses word-frequency analysis\n"
                       "    to write Contents and Keywords automatically.",
                  foreground="#555").pack(anchor="w", pady=(0, 6))

        ttk.Radiobutton(self._md_options_frame, text="Claude API  (best quality summaries)",
                        variable=self.md_method_var, value="api",
                        command=self._refresh_md_options).pack(anchor="w")

        # API key sub-frame (shown/hidden based on radio)
        self._api_key_frame = ttk.Frame(self._md_options_frame)
        self._api_key_frame.pack(fill="x", pady=(4, 6), padx=(20, 0))
        self._api_key_frame.columnconfigure(0, weight=1)

        self.apikey_var = tk.StringVar()
        self.show_key_var = tk.BooleanVar(value=False)
        self._key_entry = ttk.Entry(self._api_key_frame, textvariable=self.apikey_var,
                                    show="•", width=48)
        self._key_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Checkbutton(self._api_key_frame, text="Show",
                        variable=self.show_key_var,
                        command=self._toggle_key_visibility).grid(row=0, column=1)
        ttk.Button(self._api_key_frame, text="Save Key",
                   command=self._save_settings).grid(row=0, column=2, padx=(4, 0))

        ttk.Label(self._api_key_frame,
                  text="Get an API key at:  console.anthropic.com  (separate from Claude.ai subscription,\n"
                       "pay-per-use billing — Haiku is very cheap, ~$0.001 per section).",
                  foreground="#555").grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))

        if not HAS_ANTHROPIC:
            ttk.Label(self._api_key_frame,
                      text="Note: 'anthropic' package not installed — run  pip install anthropic  then restart.",
                      foreground="#a04000").grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

        ttk.Radiobutton(self._md_options_frame, text="Manual placeholders  (fill in yourself)",
                        variable=self.md_method_var, value="manual",
                        command=self._refresh_md_options).pack(anchor="w")
        ttk.Label(self._md_options_frame,
                  text="    Generates the index with blank Contents and Keywords for you to complete.",
                  foreground="#555").pack(anchor="w", pady=(0, 2))

        self._refresh_md_options()

    def _refresh_md_options(self):
        """Show/hide the API key frame based on current radio + checkbox state."""
        if not self.md_var or not self.md_method_var:
            return
        md_on  = self.md_var.get()
        method = self.md_method_var.get()
        # Enable/disable the whole options frame
        state = "normal" if md_on else "disabled"
        for child in self._md_options_frame.winfo_children():
            try:
                child.config(state=state)
            except Exception:
                pass
        # Show API key frame only when method == api and md is on
        if md_on and method == "api":
            self._api_key_frame.pack(fill="x", pady=(4, 6), padx=(20, 0))
        else:
            self._api_key_frame.pack_forget()

    def _toggle_key_visibility(self):
        self._key_entry.config(show="" if self.show_key_var.get() else "•")

    # ── Settings persistence ───────────────────────────────────────────────────

    def _load_settings(self):
        try:
            import json
            if SETTINGS_FILE.exists():
                data = json.loads(SETTINGS_FILE.read_text())
                if self.apikey_var and data.get("api_key"):
                    self.apikey_var.set(data["api_key"])
        except Exception:
            pass

    def _save_settings(self):
        try:
            import json
            data = {"api_key": self.apikey_var.get().strip() if self.apikey_var else ""}
            SETTINGS_FILE.write_text(json.dumps(data, indent=2))
            self.status_var.set("API key saved.")
        except Exception as e:
            messagebox.showerror("Save Error", str(e))

    # ── Drag and drop handlers ─────────────────────────────────────────────────

    def _on_drag_enter(self, event):
        self._drop_zone.config(relief="sunken", background="#d0e8ff")

    def _on_drag_leave(self, event):
        self._drop_zone.config(relief="groove", background="")

    def _on_drop(self, event):
        self._drop_zone.config(relief="groove", background="")
        # event.data may contain curly-braced paths with spaces, or space-separated paths
        raw = event.data.strip()
        # Handle Windows {path with spaces} format
        if raw.startswith("{"):
            path = raw[1:raw.index("}")]
        else:
            path = raw.split()[0]
        path = path.strip()
        if not path.lower().endswith(".pdf"):
            messagebox.showwarning("Wrong File Type",
                f'"{Path(path).name}" is not a PDF file.\nPlease drop a .pdf file.')
            return
        self.load_pdf(path)

    # ── File loading ───────────────────────────────────────────────────────────

    def browse_pdf(self):
        path = filedialog.askopenfilename(
            title="Select PDF",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")]
        )
        if path:
            self.load_pdf(path)

    def load_pdf(self, path: str):
        try:
            self.reader = PdfReader(path)
            self.total_pages = len(self.reader.pages)
        except Exception as e:
            messagebox.showerror("Cannot Open PDF", str(e))
            return

        self.pdf_path = path
        self.cached_page_sizes = None
        self.output_files = []
        self.result_list.delete(0, "end")
        self.zip_btn.config(state="disabled")
        self.open_btn.config(state="disabled")
        self.progress.config(value=0)

        # Flash the drop zone green briefly
        self._drop_zone.config(background="#c8f0c8")
        self.root.after(600, lambda: self._drop_zone.config(background=""))

        name = Path(path).name
        size = os.path.getsize(path)
        self.pdf_var.set(path)
        self.info_var.set(
            f"{name}   |   {self.total_pages} pages   |   {fmt_size(size)}")

        # Auto-detect TOC
        self.split_points = []
        toc = extract_toc(self.reader)
        if toc:
            for pg, title in toc:
                if 1 <= pg <= self.total_pages:
                    self.split_points.append([pg, title])
            self.status_var.set(
                f"TOC detected: {len(self.split_points)} section(s) loaded automatically.")
        else:
            self.status_var.set(
                "No TOC found — add split points manually, or use the file size modes.")

        self.refresh_sp_tree()
        self._update_split_btn()
        self.out_var.set(str(Path(path).parent))

    def browse_output(self):
        d = filedialog.askdirectory(title="Select Output Folder")
        if d:
            self.out_var.set(d)

    # ── Split points ───────────────────────────────────────────────────────────

    def add_split_point(self):
        self.sp_err_var.set("")
        raw = self.sp_page_var.get().strip()
        if not raw.isdigit():
            self.sp_err_var.set("Enter a valid page number.")
            return
        pg = int(raw)
        if self.total_pages and not (1 <= pg <= self.total_pages):
            self.sp_err_var.set(f"Page must be between 1 and {self.total_pages}.")
            return
        if any(sp[0] == pg for sp in self.split_points):
            self.sp_err_var.set(f"Page {pg} is already a split point.")
            return
        name = self.sp_name_var.get().strip() or f"Section (p.{pg})"
        self.split_points.append([pg, name])
        self.split_points.sort(key=lambda x: x[0])
        self.sp_page_var.set("")
        self.sp_name_var.set("")
        self.refresh_sp_tree()
        self._update_split_btn()

    def remove_split_point(self):
        sel = self.sp_tree.selection()
        if not sel:
            return
        indices = sorted([self.sp_tree.index(i) for i in sel], reverse=True)
        for idx in indices:
            if 0 <= idx < len(self.split_points):
                self.split_points.pop(idx)
        self.refresh_sp_tree()
        self._update_split_btn()

    def clear_split_points(self):
        if self.split_points and messagebox.askyesno("Clear All", "Remove all split points?"):
            self.split_points = []
            self.refresh_sp_tree()
            self._update_split_btn()

    def refresh_sp_tree(self):
        self.sp_tree.delete(*self.sp_tree.get_children())
        for i, (pg, name) in enumerate(self.split_points):
            end  = (self.split_points[i + 1][0] - 1
                    if i + 1 < len(self.split_points) else self.total_pages)
            span = end - pg + 1
            self.sp_tree.insert("", "end", values=(
                pg, name,
                f"pp. {pg} - {end}  ({span} page{'s' if span != 1 else ''})"
            ))

    def _update_split_btn(self):
        mode = self.notebook.index("current") if self.notebook.tabs() else 0
        # Modes 0, 1, 4 are TOC-based and need split points
        needs_points = mode in (0, 1, 4)
        enabled = bool(self.pdf_path) and (not needs_points or len(self.split_points) > 0)
        self.split_btn.config(state="normal" if enabled else "disabled")

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _get_output_dir(self) -> str:
        v = self.out_var.get()
        return str(Path(self.pdf_path).parent) if v == "Same folder as source PDF" else v

    def _get_max_bytes(self, slot: int) -> int:
        """slot 1 = By File Size, slot 2 = TOC+MaxSize, slot 3 = By File Size + Keep Chapters.
        Uses SI units: 1 MB = 1,000,000 bytes, 1 KB = 1,000 bytes."""
        var  = {1: self.sz1_var,  2: self.sz2_var,  3: self.sz3_var }.get(slot, self.sz1_var)
        unit = {1: self.sz1_unit, 2: self.sz2_unit, 3: self.sz3_unit}.get(slot, self.sz1_unit)
        try:
            v = float(var.get())
        except (ValueError, AttributeError):
            v = 5.0
        return int(v * (1_000_000 if unit.get() == "MB" else 1_000))

    def _set_progress(self, pct: int, msg: str):
        self.root.after(0, lambda: (
            self.progress.config(value=min(pct, 100)),
            self.status_var.set(msg)
        ))

    # ── Page size measurement ──────────────────────────────────────────────────

    def _get_page_sizes(self, reader: PdfReader) -> list:
        if self.cached_page_sizes:
            return self.cached_page_sizes
        sizes = []
        for i in range(self.total_pages):
            self._set_progress(
                int(5 + (i / self.total_pages) * 42),
                f"Measuring page sizes...  ({i + 1} of {self.total_pages})"
            )
            sizes.append(measure_page_bytes(reader, i))
        self.cached_page_sizes = sizes
        return sizes

    # ── Split ──────────────────────────────────────────────────────────────────

    def do_split(self):
        if not self.pdf_path:
            return
        self.split_btn.config(state="disabled")
        self.zip_btn.config(state="disabled")
        self.open_btn.config(state="disabled")
        self.result_list.delete(0, "end")
        self.output_files = []
        threading.Thread(target=self._split_thread, daemon=True).start()

    def _split_thread(self):
        try:
            mode    = self.notebook.index("current")
            out_dir = self._get_output_dir()
            os.makedirs(out_dir, exist_ok=True)
            reader  = PdfReader(self.pdf_path)
            groups  = []   # list of dicts: start, end, name, part (None or int)

            # ── Mode 0: TOC / Manual ──────────────────────────────────────
            if mode == 0:
                for i, (pg, name) in enumerate(self.split_points):
                    end = (self.split_points[i + 1][0] - 1
                           if i + 1 < len(self.split_points) else self.total_pages)
                    groups.append({"start": pg, "end": end, "name": name, "part": None})

            # ── Mode 1: TOC + Max File Size (split oversized chapters) ────
            elif mode == 1:
                max_b    = self._get_max_bytes(2)
                chap_pad = len(str(len(self.split_points)))
                for chap_idx, (pg, name) in enumerate(self.split_points, start=1):
                    end = (self.split_points[chap_idx][0] - 1
                           if chap_idx < len(self.split_points) else self.total_pages)
                    # Test real size of full chapter
                    self._set_progress(
                        int(5 + (chap_idx / len(self.split_points)) * 40),
                        f"Measuring chapter {chap_idx}: {name}...")
                    w = PdfWriter()
                    for p in range(pg - 1, end):
                        w.add_page(reader.pages[p])
                    buf = io.BytesIO(); w.write(buf)
                    chap_real_size = len(buf.getvalue())

                    if chap_real_size <= max_b:
                        groups.append({"start": pg, "end": end, "name": name,
                                       "part": None, "chap_idx": chap_idx,
                                       "chap_pad": chap_pad})
                    else:
                        # Full chapter first (oversized but kept for reference)
                        groups.append({"start": pg, "end": end, "name": name,
                                       "part": None, "chap_idx": chap_idx,
                                       "chap_pad": chap_pad})
                        # Exact split into parts — guaranteed under limit
                        page_indices = list(range(pg - 1, end))
                        sub_groups   = pack_pages_exact(reader, page_indices, max_b)
                        for part_num, sub_idx in enumerate(sub_groups, start=1):
                            groups.append({
                                "start":    sub_idx[0] + 1,
                                "end":      sub_idx[-1] + 1,
                                "name":     name,
                                "part":     part_num,
                                "chap_idx": chap_idx,
                                "chap_pad": chap_pad,
                            })

            # ── Mode 2: By File Size (pure page packing) ──────────────────
            elif mode == 2:
                sizes = self._get_page_sizes(reader)
                max_b = self._get_max_bytes(1)
                for i, (s, e) in enumerate(greedy_pack_pages(sizes, max_b)):
                    groups.append({"start": s + 1, "end": e + 1,
                                   "name": f"Part {i + 1:02d}", "part": None})

            # ── Mode 3: By File Size + Keep Chapters whole ────────────────
            elif mode == 3:
                sizes = self._get_page_sizes(reader)
                max_b = self._get_max_bytes(3)
                if self.split_points:
                    chaps = []
                    for i, (pg, name) in enumerate(self.split_points):
                        end = (self.split_points[i + 1][0] - 1
                               if i + 1 < len(self.split_points) else self.total_pages)
                        chaps.append({
                            "start": pg, "end": end, "name": name,
                            "size":  sum(sizes[pg - 1: end])
                        })
                    for i, grp in enumerate(greedy_pack_chapters(chaps, max_b)):
                        groups.append({
                            "start": grp[0]["start"],
                            "end":   grp[-1]["end"],
                            "name":  grp[0]["name"] if len(grp) == 1
                                     else f"Part {i + 1:02d}",
                            "part":  None,
                        })
                else:
                    for i, (s, e) in enumerate(greedy_pack_pages(sizes, max_b)):
                        groups.append({"start": s + 1, "end": e + 1,
                                       "name": f"Part {i + 1:02d}", "part": None})

            # ── Mode 4: Split for Claude — hard 30 MB limit ───────────────
            else:
                max_b    = CLAUDE_MAX_BYTES
                chap_pad = len(str(len(self.split_points)))
                n_chaps  = len(self.split_points)
                for chap_idx, (pg, name) in enumerate(self.split_points, start=1):
                    end = (self.split_points[chap_idx][0] - 1
                           if chap_idx < len(self.split_points) else self.total_pages)
                    self._set_progress(
                        int(5 + (chap_idx / n_chaps) * 40),
                        f"Checking chapter {chap_idx} of {n_chaps}: {name}...")
                    # Measure real combined size of full chapter
                    w = PdfWriter()
                    for p in range(pg - 1, end):
                        w.add_page(reader.pages[p])
                    buf = io.BytesIO(); w.write(buf)
                    chap_real_size = len(buf.getvalue())

                    if chap_real_size <= max_b:
                        groups.append({"start": pg, "end": end, "name": name,
                                       "part": None, "chap_idx": chap_idx,
                                       "chap_pad": chap_pad})
                    else:
                        # Keep full chapter file (may be over 30 MB — labelled as oversized)
                        groups.append({"start": pg, "end": end, "name": name,
                                       "part": None, "chap_idx": chap_idx,
                                       "chap_pad": chap_pad,
                                       "oversized": True})
                        # Parts — guaranteed ≤ 30 MB using real measurement
                        page_indices = list(range(pg - 1, end))
                        self._set_progress(
                            int(5 + (chap_idx / n_chaps) * 40),
                            f"Splitting chapter {chap_idx} into parts (measuring real sizes)...")
                        sub_groups = pack_pages_exact(reader, page_indices, max_b)
                        for part_num, sub_idx in enumerate(sub_groups, start=1):
                            groups.append({
                                "start":    sub_idx[0] + 1,
                                "end":      sub_idx[-1] + 1,
                                "name":     name,
                                "part":     part_num,
                                "chap_idx": chap_idx,
                                "chap_pad": chap_pad,
                            })

            # ── Write output PDFs ─────────────────────────────────────────
            total = len(groups)
            pad   = len(str(total))

            for i, g in enumerate(groups):
                part_label = (f"  (part {g['part']})" if g.get("part") else "")
                self._set_progress(
                    int(50 + (i / total) * 46),
                    f"Writing file {i + 1} of {total}:  {g['name']}{part_label}..."
                )
                writer = PdfWriter()
                for p in range(g["start"] - 1, g["end"]):
                    writer.add_page(reader.pages[p])

                base_name = safe_filename(g["name"], f"Part_{i + 1:02d}")

                # Modes 1 & 4: prefix is chapter number (same for all parts)
                if "chap_idx" in g:
                    prefix = str(g["chap_idx"]).zfill(g["chap_pad"])
                else:
                    prefix = str(i + 1).zfill(pad)

                if g.get("part") is not None:
                    file_name = f"{prefix}_{base_name}_{g['part']}.pdf"
                else:
                    file_name = f"{prefix}_{base_name}.pdf"

                out_path = os.path.join(out_dir, file_name)
                with open(out_path, "wb") as fh:
                    writer.write(fh)

                fsize = os.path.getsize(out_path)
                pages = g["end"] - g["start"] + 1
                self.output_files.append(out_path)

                over_flag = "  *** OVERSIZED — full chapter kept for reference ***" \
                            if g.get("oversized") else ""
                row = (f"{file_name:<46}  pp. {g['start']}-{g['end']}"
                       f"  ({pages}p)   {fmt_size(fsize)}{over_flag}")
                self.root.after(0, lambda r=row: self.result_list.insert("end", r))

            # ── Generate MD index (mode 4 only, if checkbox ticked) ───────
            md_path = None
            if mode == 4 and self.md_var and self.md_var.get():
                self._set_progress(98, "Generating .md index file...")
                md_path = self._generate_md_index(groups, out_dir)

            # ── Done ──────────────────────────────────────────────────────
            done_msg = (f"Done -- {total} file{'s' if total != 1 else ''} saved to:  {out_dir}")
            if md_path:
                done_msg += f"   |   Index: {Path(md_path).name}"
            self._set_progress(100, done_msg)
            self.root.after(0, lambda: [
                self.zip_btn.config(state="normal"),
                self.open_btn.config(state="normal"),
            ])

        except Exception as e:
            import traceback; traceback.print_exc()
            self._set_progress(0, f"Error: {e}")

        self.root.after(0, lambda: self.split_btn.config(state="normal"))
        self.root.after(0, self._update_split_btn)

    # ── MD Index generation ────────────────────────────────────────────────────

    def _extract_text_sample(self, pdf_path: str, max_chars: int = 3000) -> str:
        """Extract a text sample from a PDF for summarisation."""
        try:
            reader = PdfReader(pdf_path)
            text = ""
            for page in reader.pages:
                text += (page.extract_text() or "") + "\n"
                if len(text) >= max_chars:
                    break
            return text[:max_chars].strip()
        except Exception:
            return ""

    def _python_summarise(self, file_name: str, text: str) -> tuple:
        """
        Pure-Python summarisation — no API key required.
        Uses word frequency to extract keywords, and the first clean sentence
        as the contents description.
        Returns (contents_str, keywords_str).
        """
        import string
        import re

        # ── Keywords via word frequency ────────────────────────────────────
        STOPWORDS = {
            "the","a","an","and","or","but","in","on","at","to","for","of","with",
            "is","are","was","were","be","been","being","have","has","had","do",
            "does","did","will","would","could","should","may","might","shall",
            "that","this","these","those","it","its","as","by","from","into",
            "than","then","so","if","not","also","which","when","where","how",
            "all","any","each","per","page","figure","table","note","see","ref",
            "section","chapter","appendix","part","item","items","number","no",
        }
        words = re.findall(r"[a-zA-Z]{3,}", text.lower())
        freq: dict = {}
        for w in words:
            if w not in STOPWORDS:
                freq[w] = freq.get(w, 0) + 1
        top_words = sorted(freq, key=freq.get, reverse=True)[:8]
        keywords = ", ".join(top_words) if top_words else "—"

        # ── Contents: first clean sentence of 6+ words ─────────────────────
        contents = "—"
        sentences = re.split(r"(?<=[.!?])\s+", text.replace("\n", " "))
        for sent in sentences:
            sent = sent.strip()
            word_count = len(sent.split())
            # Skip headings, page numbers, very short or very long sentences
            if 6 <= word_count <= 30 and not sent.isupper() and sent[0].isupper():
                # Remove trailing punctuation clutter
                contents = sent.rstrip(string.punctuation) + "."
                break

        # Fallback: use top words as a phrase
        if contents == "—" and top_words:
            contents = "Topics: " + ", ".join(top_words[:5]) + "."

        return contents, keywords

    def _claude_summarise(self, api_key: str, file_name: str, text: str) -> tuple:
        """Call Claude Haiku to produce a contents description and keywords."""
        try:
            client = anthropic.Anthropic(api_key=api_key)
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=120,
                messages=[{
                    "role": "user",
                    "content": (
                        f'PDF section: "{file_name}"\n\n'
                        f"Text extract:\n{text}\n\n"
                        "Reply ONLY in this exact format, no other text:\n"
                        "Contents: [5-8 word comma-separated topic description]\n"
                        "Keywords: [5-8 comma-separated keywords]"
                    )
                }]
            )
            raw = response.content[0].text.strip()
            contents, keywords = "", ""
            for line in raw.splitlines():
                if line.lower().startswith("contents:"):
                    contents = line.split(":", 1)[1].strip()
                elif line.lower().startswith("keywords:"):
                    keywords = line.split(":", 1)[1].strip()
            return contents or "—", keywords or "—"
        except Exception as e:
            return f"(API error: {e})", "—"

    def _generate_md_index(self, groups: list, out_dir: str) -> str | None:
        """
        Generate a .md index file for all output PDFs.
        Method is determined by self.md_method_var: 'python' | 'api' | 'manual'
        Returns path to the .md file, or None on failure.
        """
        try:
            import datetime
            stem    = Path(self.pdf_path).stem
            md_path = os.path.join(out_dir, f"{stem}_claude_index.md")
            method  = self.md_method_var.get() if self.md_method_var else "manual"
            api_key = (self.apikey_var.get().strip()
                       if self.apikey_var else "")

            # Validate API method
            if method == "api" and (not api_key or not HAS_ANTHROPIC):
                method = "python"  # graceful fallback

            today = datetime.date.today().strftime("%d %b %Y")
            method_label = {
                "python": "Python auto-generated",
                "api":    "Claude API (claude-haiku)",
                "manual": "manual — please fill in",
            }.get(method, method)

            pad = len(str(len(groups)))

            lines = [
                f"# Claude Project Index — {stem}",
                f"*Split for Claude Projects (30 MB limit) · Generated {today}*",
                f"*{len(self.output_files)} files · source: {Path(self.pdf_path).name}*",
                f"*Summaries: {method_label}*",
                "",
                "---",
                "",
                "> **How to use:** Upload all PDF files and this index to a Claude project.",
                "> Ask Claude to consult the index to locate relevant sections before answering.",
                "",
                "---",
                "",
            ]

            file_num = 0
            for i, g in enumerate(groups):
                base_name = safe_filename(g["name"], f"Part_{i + 1:02d}")
                if "chap_idx" in g:
                    prefix = str(g["chap_idx"]).zfill(g["chap_pad"])
                else:
                    prefix = str(i + 1).zfill(pad)

                file_name = (f"{prefix}_{base_name}_{g['part']}.pdf"
                             if g.get("part") is not None
                             else f"{prefix}_{base_name}.pdf")

                out_path = os.path.join(out_dir, file_name)
                if not os.path.exists(out_path):
                    continue

                file_num += 1
                fsize = os.path.getsize(out_path)
                pages = g["end"] - g["start"] + 1
                part_note = f" *(part {g['part']})*" if g.get("part") else ""

                lines.append(f"## {file_num}. {file_name}{part_note}")
                lines.append(f"pp. {g['start']}–{g['end']} · "
                             f"{pages} page{'s' if pages != 1 else ''} · {fmt_size(fsize)}  ")

                if method in ("python", "api"):
                    self._set_progress(98,
                        f"Generating index: analysing {file_name}...")
                    text_sample = self._extract_text_sample(out_path)
                    if method == "python":
                        contents, keywords = self._python_summarise(file_name, text_sample)
                    else:
                        contents, keywords = self._claude_summarise(api_key, file_name, text_sample)
                    lines.append(f"**Contents:** {contents}  ")
                    lines.append(f"**Keywords:** {keywords}")
                else:
                    lines.append("**Contents:**   ")
                    lines.append("**Keywords:**   ")

                lines.append("")

            Path(md_path).write_text("\n".join(lines), encoding="utf-8")

            md_display = f"{Path(md_path).name:<46}  (Claude project index — {method_label})"
            self.root.after(0, lambda d=md_display: self.result_list.insert("end", d))
            return md_path

        except Exception as e:
            import traceback; traceback.print_exc()
            self._set_progress(98, f"MD generation error: {e}")
            return None

    # ── ZIP ────────────────────────────────────────────────────────────────────

    def create_zip(self):
        if not self.output_files:
            return
        stem     = Path(self.pdf_path).stem
        zip_path = os.path.join(self._get_output_dir(), f"{stem}.zip")
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
                for f in self.output_files:
                    if os.path.exists(f):
                        zf.write(f, Path(f).name)
            fsize = os.path.getsize(zip_path)
            self.status_var.set(
                f"ZIP created:  {Path(zip_path).name}   ({fmt_size(fsize)})")
            messagebox.showinfo("ZIP Created",
                f"All {len(self.output_files)} files zipped to:\n\n"
                f"{zip_path}\n\nSize: {fmt_size(fsize)}")
        except Exception as e:
            messagebox.showerror("ZIP Error", str(e))

    # ── Open folder ────────────────────────────────────────────────────────────

    def open_output_folder(self):
        d = self._get_output_dir()
        if os.path.isdir(d):
            os.startfile(d)


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    if HAS_DND:
        root = TkinterDnD.Tk()
    else:
        root = tk.Tk()
    PDFSplitterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
