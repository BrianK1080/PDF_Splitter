# PDF Splitter v1.0 — README

## What It Does

A portable desktop application for splitting PDF files. Load a PDF, choose how
to split it, and save the output files to any folder. Everything runs locally —
no cloud, no installation wizard, no admin rights required.

### Split Modes

| Mode | Description |
|------|-------------|
| **TOC / Manual** | Split at bookmarks (auto-detected) or page numbers you enter manually |
| **By File Size** | Specify a max file size (e.g. 5 MB) — pages are packed greedily into files under the limit |
| **By File Size + Keep Chapters** | Same size limit, but whole chapters are never split across files |

### Features

- Auto-detects PDF table of contents (TOC/bookmarks) on load
- Manual split points: enter any page number + optional section name
- Add / Remove / Clear All split points
- Output files are numbered and named automatically
- Create a ZIP of all output files in one click
- Output folder is configurable (defaults to same folder as the source PDF)
- Progress bar with status messages
- Page sizes cached — re-splitting at a different size limit is fast

---

## Two Editions

### Edition 1: Portable (uses system Python)

For PCs that have Python installed, or where Python can be installed.

**To run:** Double-click `launch.bat`

The launcher will:
1. Check if Python 3.x is installed
2. Offer to install Python via Windows Package Manager (winget) if missing
3. Install the `pypdf` package automatically if needed (one-time, internet required)
4. Launch the app

### Edition 2: Standalone (bundled Python — no system Python needed)

For locked-down or corporate PCs where Python cannot be installed system-wide.
Includes a complete self-contained Python 3.12 runtime inside the app folder.

**First-time setup:** Double-click `setup_standalone.bat` (internet required, ~25 MB download, one-time only)

**To run after setup:** Double-click `launch_standalone.bat`

Setup will:
1. Download the official Python 3.12 embeddable package from python.org
2. Extract it into the `python\` subfolder
3. Install pip and the `pypdf` package into that local Python

After setup, the app runs entirely from the local `python\` folder — no internet
or system Python needed.

---

## Quick Start

### Portable Edition

1. Extract the zip to any folder
2. Double-click **`launch.bat`**
3. Click **Browse...** and select your PDF
4. Review or edit split points in the **TOC / Manual** tab
5. (Optional) Switch to a file size mode and set your limit
6. Click **Split PDF**
7. Click **Create ZIP of All Files** if needed

### Standalone Edition

1. Extract the zip to any folder
2. Double-click **`setup_standalone.bat`** (first time only)
3. Double-click **`launch_standalone.bat`**
4. Follow steps 3–7 above

---

## System Requirements

### Portable Edition

| Requirement | Details |
|-------------|---------|
| OS | Windows 10 (1709+) or Windows 11 |
| Python | 3.10 or later — auto-installed by `launch.bat` if missing |
| Internet | First run only (to install pypdf) |
| Admin rights | Not required if Python installed for current user |

### Standalone Edition

| Requirement | Details |
|-------------|---------|
| OS | Windows 10 (1709+) or Windows 11 (64-bit) |
| Python | Not required — bundled in `python\` subfolder |
| Internet | First run of `setup_standalone.bat` only (~25 MB) |
| Admin rights | Not required |

---

## Folder Structure

### Portable Edition

```
PDF_Splitter_v1.0_Portable/
├── launch.bat                  <-- DOUBLE-CLICK TO START
├── pdf_splitter.py             Main application
└── README.md                   This file
```

### Standalone Edition

```
PDF_Splitter_v1.0_Standalone/
├── setup_standalone.bat        <-- RUN FIRST (one-time setup)
├── launch_standalone.bat       <-- DOUBLE-CLICK TO START (after setup)
├── pdf_splitter.py             Main application
├── python\                     Bundled Python 3.12 (created by setup)
│   ├── python.exe
│   ├── Lib\site-packages\      pypdf installed here
│   └── ...
└── README.md                   This file
```

---

## Troubleshooting

**"Python not found" after running launch.bat**
- Restart the launcher after a fresh Python install (PATH needs to refresh)
- Or install Python manually from https://www.python.org/downloads/ — tick "Add Python to PATH"

**"Setup failed" in setup_standalone.bat**
- Check your internet connection
- Some corporate firewalls block python.org — try on a different network for setup

**PDF loads but no TOC sections appear**
- The PDF has no bookmarks embedded. Use the manual page number entry, or switch to a file size mode.

**File size mode is slow on first run**
- Each page is measured by rendering it as a standalone PDF. This is cached — subsequent splits are instant.

**Output files have the same name**
- Files are auto-numbered (01_, 02_, etc.) to prevent collisions. Check the Output Files list.

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | May 2026 | Initial release. TOC/manual, by file size, by file size + keep chapters. ZIP output. Portable + Standalone editions. |
