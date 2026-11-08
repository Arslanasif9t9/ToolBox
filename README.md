# Arslan Toolbox

Local, free tools. No AI, no API key (Part 1). Runs in the browser at http://127.0.0.1:5000

## Run (Windows)
Double-click `run.bat` (first run creates `.venv` and installs requirements). Python 3.11+ required (PyMuPDF).
Tool 3's Word/PowerPoint/Excel to PDF needs MS Office installed (Windows only); the rest of the Toolbox needs nothing extra.
After pulling new tools, run `.venv\Scripts\pip install -r requirements.txt` once so new packages get installed.

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
| 3 | Document Converter (PDF→Word, Word/PPT/Excel→PDF via MS Office, Text/MD→Word) | Built, user testing pending |
| 4 | CV / Resume Maker (3 PDF templates, Word export, photo, save/load JSON) | Built, user testing pending |
| 5 | Image Tools (Pillow: compress incl. target KB, resize, convert, passport photo + print sheet) | Built, user testing pending |
| 6 | Background Remover (U2-Net / ISNet via OpenCV DNN; models downloaded once into `models/`) | Built, user testing pending |
| 7 | OCR (Tesseract; English/Urdu/Arabic; image + scanned PDF -> TXT / Word / searchable PDF) | Built, user testing pending |
| 8 | QR / Barcode (styled QR with gradient/logo/frame, barcodes, camera + photo reader, bulk) | Built, user testing pending |
| 9 | Audio Tools (ffmpeg: video->audio, convert, cut/remove part, join, speed/volume/fade/silence trim) | Built, user testing pending |
| 10 | Bulk Renamer + Duplicate Finder | Pending |

## Notes
- Background Remover uses OpenCV's DNN module instead of onnxruntime/rembg (onnxruntime has no wheels for Python 3.15). Models (4.6-178 MB) are downloaded on demand from the rembg GitHub release into `models/` (git-ignored) with resume support. Quantized models (e.g. silueta) are not supported by OpenCV.
- OCR needs the Tesseract program installed once (`winget install UB-Mannheim.TesseractOCR`). Language data (eng/urd/ara, ~7 MB) is downloaded from the tessdata_fast repo into `models/tessdata/` (git-ignored) via buttons on the OCR page.
- QR/Barcode: own renderer on the `qrcode` matrix (PNG/SVG/PDF); barcodes via `python-barcode`; reading via `zxing-cpp` (OpenCV fallback). Camera scanning needs Chrome/Edge on http://127.0.0.1:5000 and camera permission.
- Downloads save to `~/Downloads/ArslanToolbox`.
- If a site stops working, use the "yt-dlp update karo" button on the Downloader page.
- ffmpeg: uses system ffmpeg if installed, else the one bundled by `imageio-ffmpeg`.
