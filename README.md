# Arslan Toolbox

Local, free tools. No AI, no API key (Part 1). Runs in the browser at http://127.0.0.1:5000

## Run (Windows)
Double-click `run.bat` (first run creates `.venv` and installs requirements). Python 3.9+ required.

## Run (Mac/Linux)
```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

## Structure
- `app.py` - auto-loads every module in `tools/` (needs `bp` Blueprint + `META` dict)
- `tools/<name>.py` - one file per tool
- `templates/<name>.html` - one page per tool, extends `base.html`

## Adding a tool
Copy `tools/downloader.py`, change `META` (order, name, slug, icon, desc) and routes, add a template. No change in `app.py` needed.

## Tools
| # | Tool | Status |
|---|------|--------|
| 1 | Video/Audio Downloader (yt-dlp + ffmpeg) | Done |
| 2 | PDF Toolkit (PyMuPDF + Pillow; 12 tabs incl. visual editor) | Built, user testing pending |
| 3 | Document Converter | Pending |
| 4 | CV / Resume Maker | Pending |
| 5 | Image Tools | Pending |
| 6 | Background Remover | Pending |
| 7 | OCR | Pending |
| 8 | QR / Barcode | Pending |
| 9 | Audio Tools | Pending |
| 10 | Bulk Renamer + Duplicate Finder | Pending |

## Notes
- Downloads save to `~/Downloads/ArslanToolbox`.
- If a site stops working, use the "yt-dlp update karo" button on the Downloader page.
- ffmpeg: uses system ffmpeg if installed, else the one bundled by `imageio-ffmpeg`.
"# ToolBox" 
"# ToolBox" 
