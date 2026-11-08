"""Tool 7: OCR (image / scanned PDF -> text). Offline, no AI, no API key.

Uses the Tesseract OCR program (installed once on Windows). Language data (English, Urdu, Arabic) is downloaded
once into <ToolBox>/models/tessdata/ and not committed to git. Output: TXT, Word (with RTL for Urdu/Arabic),
searchable PDF.
"""
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import suppress
from functools import wraps
from pathlib import Path

import cv2
import numpy as np
from flask import Blueprint, jsonify, render_template, request, send_from_directory
from PIL import Image, ImageOps, ImageSequence

try:
    import pymupdf as fitz
except ImportError:  # older PyMuPDF
    import fitz

META = {"order": 7, "name": "OCR (Text nikalo)", "slug": "ocr", "icon": "🔎",
        "desc": "Image ya scanned PDF se text nikalo (English, Urdu, Arabic). TXT, Word, searchable PDF."}

bp = Blueprint("ocr", __name__, url_prefix="/ocr")

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "Output" / "OCR"
TESSDATA = ROOT / "models" / "tessdata"
IS_WIN = sys.platform.startswith("win")
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DATA_URL = "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/{}.traineddata"
MIN_DATA = 500_000
IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
MAX_PAGES = 200
MAX_SIDE = 6000
PREVIEW = 20000

LANGS = {
    "eng": {"label": "English", "human": "4 MB"},
    "urd": {"label": "Urdu (اردو)", "human": "1.4 MB"},
    "ara": {"label": "Arabic (العربية)", "human": "1.4 MB"},
}
PSMS = {"3": "Auto (aam page)", "6": "Ek block", "7": "Ek line", "11": "Bikhra hua text"}


class Err(Exception):
    """Error with a user-readable message."""


# ---------------------------------------------------------------- helpers
def api(fn):
    @wraps(fn)
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except Err as e:
            return jsonify(error=str(e)), 400
        except Exception as e:  # noqa
            return jsonify(error=f"Kuch masla aaya: {e}"), 500
    return wrapper


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def stem_of(name):
    s = re.sub(r"[^\w\-. ]+", "_", Path(name or "file").stem)[:80].strip()
    return s or "file"


def unique_path(name):
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / name
    stem, suf, i = p.stem, p.suffix, 1
    while p.exists():
        p = OUT / f"{stem} ({i}){suf}"
        i += 1
    return p


# ---------------------------------------------------------------- tesseract + language data
def _candidates():
    env = os.environ.get("TESSERACT_CMD")
    if env:
        yield env
    w = shutil.which("tesseract")
    if w:
        yield w
    if IS_WIN:
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
            if base:
                yield os.path.join(base, "Tesseract-OCR", "tesseract.exe")
        yield str(ROOT / "Tesseract-OCR" / "tesseract.exe")


def find_tesseract():
    for c in _candidates():
        if c and os.path.isfile(c):
            return c
    return ""


def tess_version(exe):
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, timeout=20, creationflags=NOWIN)
        txt = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
        return txt.splitlines()[0] if txt else ""
    except Exception:  # noqa
        return ""


def lang_path(name):
    return TESSDATA / f"{name}.traineddata"


def lang_ready(name):
    p = lang_path(name)
    return p.exists() and p.stat().st_size >= MIN_DATA


def fetch_lang(name):
    TESSDATA.mkdir(parents=True, exist_ok=True)
    dst = lang_path(name)
    part = dst.with_suffix(".part")
    last = ""
    for _ in range(4):
        try:
            req = urllib.request.Request(DATA_URL.format(name), headers={"User-Agent": "ArslanToolbox/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                total = int(r.headers.get("Content-Length") or 0)
                with open(part, "wb") as fh:
                    shutil.copyfileobj(r, fh)
            size = part.stat().st_size
            if size < MIN_DATA or (total and size != total):
                last = f"file poori nahi aayi ({size} / {total or '?'} bytes)"
                time.sleep(2)
                continue
            os.replace(part, dst)
            return
        except Exception as e:  # noqa
            last = str(e)
            time.sleep(2)
    with suppress(Exception):
        part.unlink()
    raise Err(f"Language data download nahi hua: {last[:200]}. Internet check karke dobara dabao.")


def langs_state():
    return [{"name": n, "label": m["label"], "human": m["human"], "ready": lang_ready(n)} for n, m in LANGS.items()]


# ---------------------------------------------------------------- image preparation
def to_gray(im, upscale=True, clean=False):
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info:
        im = im.convert("RGBA")
        flat = Image.new("RGB", im.size, "white")
        flat.paste(im, mask=im.split()[3])
        im = flat
    g = ImageOps.grayscale(im.convert("RGB"))
    long = max(g.size)
    if long > MAX_SIDE:
        g.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
    elif upscale and long < 1600:  # Tesseract likes bigger text
        f = min(3.0, 2000 / long)
        g = g.resize((round(g.width * f), round(g.height * f)), Image.Resampling.LANCZOS)
    g = ImageOps.autocontrast(g, cutoff=1)
    if clean:  # for dim / uneven photos: blur a bit, then adaptive threshold
        arr = cv2.GaussianBlur(np.array(g), (3, 3), 0)
        block = max(31, (min(arr.shape) // 30) | 1)
        arr = cv2.adaptiveThreshold(arr, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, block, 15)
        g = Image.fromarray(arr)
    return g


def pages_of(name, data, dpi, clean):
    """Yield (gray PIL image, dpi) for each page of an image or PDF, one at a time."""
    ext = Path(name).suffix.lower()
    if ext == ".pdf":
        try:
            doc = fitz.open(stream=data, filetype="pdf")
        except Exception:
            raise Err("PDF khul nahi saki (kharab hai).")
        with doc:
            if doc.needs_pass:
                raise Err("PDF password wali hai. Pehle PDF Toolkit mein unlock karo.")
            if doc.page_count > MAX_PAGES:
                raise Err(f"PDF mein {doc.page_count} pages hain, ek baar mein {MAX_PAGES} tak chalte hain.")
            for page in doc:
                pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY, alpha=False)
                g = Image.frombytes("L", (pix.width, pix.height), pix.samples)
                yield to_gray(g, upscale=False, clean=clean), dpi
        return
    if ext not in IMG_EXTS:
        raise Err(f"ye type nahi chalti ({ext or 'koi nahi'}). Chalti hain: JPG, PNG, WebP, BMP, TIFF, PDF.")
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        raise Err("image khul nahi saki (kharab hai ya supported nahi).")
    frames = list(ImageSequence.Iterator(im))[:MAX_PAGES] if getattr(im, "n_frames", 1) > 1 else [im]
    for fr in frames:
        yield to_gray(fr.copy(), upscale=True, clean=clean), 300


# ---------------------------------------------------------------- running tesseract
def run_tess(exe, img, base, langs, psm, want_pdf):
    cmd = [exe, str(img), str(base), "-l", "+".join(langs), "--psm", str(psm), "--tessdata-dir", str(TESSDATA),
           "-c", "tessedit_create_txt=1", "-c", "tessedit_create_tsv=1"]
    if want_pdf:
        cmd += ["-c", "tessedit_create_pdf=1"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=300, creationflags=NOWIN)
    except subprocess.TimeoutExpired:
        raise Err("Tesseract ko bohat der lag gayi (5 minute). Chhoti image ya kam pages try karo.")
    if not Path(f"{base}.txt").exists():
        msg = (r.stderr or r.stdout).decode("utf-8", "replace").strip()[-300:]
        raise Err(f"Tesseract chalne mein masla: {msg or 'unknown error'}")


def read_result(base):
    text = Path(f"{base}.txt").read_text(encoding="utf-8", errors="replace").replace("\x0c", "")
    text = re.sub(r"\r\n?", "\n", text).strip()
    confs = 0.0
    words = 0
    tsv = Path(f"{base}.tsv")
    if tsv.exists():
        for line in tsv.read_text(encoding="utf-8", errors="replace").splitlines()[1:]:
            p = line.split("\t", 11)
            if len(p) == 12 and p[0] == "5" and p[11].strip():
                with suppress(ValueError):
                    c = float(p[10])
                    if c >= 0:
                        confs += c
                        words += 1
    return text, words, (confs / words if words else None)


# ---------------------------------------------------------------- outputs
def _rtl_para(par, size_pt=14):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    ppr = par._p.get_or_add_pPr()
    ppr.append(OxmlElement("w:bidi"))
    for run in par.runs:
        rpr = run._r.get_or_add_rPr()
        fonts = rpr.find(qn("w:rFonts"))
        if fonts is None:
            fonts = OxmlElement("w:rFonts")
            rpr.append(fonts)
        fonts.set(qn("w:cs"), "Arial")
        rpr.append(OxmlElement("w:rtl"))
        szcs = OxmlElement("w:szCs")
        szcs.set(qn("w:val"), str(size_pt * 2))
        rpr.append(szcs)


def build_docx(pages, dst, rtl, join):
    from docx import Document
    doc = Document()
    for i, text in enumerate(pages):
        if i:
            doc.add_page_break()
        blocks = re.split(r"\n\s*\n", text) if text else [""]
        for b in blocks:
            lines = [ln.strip() for ln in b.split("\n") if ln.strip()]
            if not lines:
                continue
            par = doc.add_paragraph()
            if join:
                par.add_run(" ".join(lines))
            else:
                for j, ln in enumerate(lines):
                    run = par.add_run(ln)
                    if j < len(lines) - 1:
                        run.add_break()
            if rtl:
                _rtl_para(par)
    doc.save(dst)


def merge_pdfs(paths, dst):
    out = fitz.open()
    for p in paths:
        with fitz.open(p) as d:
            out.insert_pdf(d)
    out.save(str(dst), garbage=3, deflate=True)
    out.close()


# ---------------------------------------------------------------- routes
@bp.route("/")
def page():
    return render_template("ocr.html", folder=str(OUT), tessdata=str(TESSDATA), psms=PSMS)


@bp.route("/status")
def status():
    exe = find_tesseract()
    return jsonify(found=bool(exe), exe=exe, version=tess_version(exe) if exe else "", langs=langs_state())


@bp.route("/lang/download", methods=["POST"])
@api
def lang_download():
    name = (request.get_json(silent=True) or {}).get("name", "")
    if name not in LANGS:
        raise Err("Ghalat language.")
    if not lang_ready(name):
        fetch_lang(name)
        exe = find_tesseract()
        if exe:  # make sure Tesseract can actually load it
            r = subprocess.run([exe, "--list-langs", "--tessdata-dir", str(TESSDATA)], capture_output=True,
                               timeout=30, creationflags=NOWIN)
            if name not in (r.stdout + r.stderr).decode("utf-8", "replace").split():
                with suppress(Exception):
                    lang_path(name).unlink()
                raise Err("Download hui file Tesseract se load nahi hui. Dobara koshish karo.")
    return jsonify(langs=langs_state())


@bp.route("/run", methods=["POST"])
@api
def run():
    exe = find_tesseract()
    if not exe:
        raise Err("Tesseract install nahi mila. Upar diye gaye install steps karo, phir page refresh karo.")
    langs = [l for l in request.form.getlist("lang") if l in LANGS]
    if not langs:
        raise Err("Kam az kam ek language chuno.")
    missing = [LANGS[l]["label"] for l in langs if not lang_ready(l)]
    if missing:
        raise Err("Pehle language data download karo: " + ", ".join(missing))
    psm = request.form.get("psm", "3")
    if psm not in PSMS:
        psm = "3"
    try:
        dpi = int(request.form.get("dpi", "300"))
    except ValueError:
        dpi = 300
    dpi = min(max(dpi, 150), 400)
    clean = request.form.get("clean") == "on"
    outs = set(request.form.getlist("out")) & {"txt", "docx", "pdf"} or {"txt"}
    join = request.form.get("join") == "on"
    rtl = request.form.get("rtl") == "on"
    files = [f for f in request.files.getlist("files") if f.filename]
    if not files:
        raise Err("Pehle image ya PDF chuno.")

    results, fails = [], []
    t0 = time.time()
    for f in files:
        label = f.filename
        try:
            data = f.read()
            texts, pdfs, wsum, csum = [], [], 0, 0.0
            with tempfile.TemporaryDirectory(prefix="ocr_") as tmp:
                for i, (g, pdpi) in enumerate(pages_of(label, data, dpi, clean), 1):
                    img = Path(tmp) / (f"p{i}.png" if clean else f"p{i}.jpg")
                    if clean:
                        g.save(img, "PNG", dpi=(pdpi, pdpi))
                    else:
                        g.save(img, "JPEG", quality=92, dpi=(pdpi, pdpi))
                    base = Path(tmp) / f"p{i}"
                    run_tess(exe, img, base, langs, psm, "pdf" in outs)
                    text, words, conf = read_result(base)
                    texts.append(text)
                    wsum += words
                    csum += (conf or 0) * words
                    if "pdf" in outs and Path(f"{base}.pdf").exists():
                        pdfs.append(f"{base}.pdf")
                    os.remove(img)
                npages = len(texts)
                if not npages:
                    raise Err("koi page nahi mila.")
                stem = stem_of(label)
                files_out = []
                if npages > 1:
                    full = "\n\n".join(f"===== Page {i} =====\n\n{t}" for i, t in enumerate(texts, 1))
                else:
                    full = texts[0]
                if "txt" in outs:
                    p = unique_path(f"{stem}.txt")
                    p.write_text(full, encoding="utf-8")
                    files_out.append(p)
                if "docx" in outs:
                    p = unique_path(f"{stem}.docx")
                    build_docx(texts, p, rtl, join)
                    files_out.append(p)
                if "pdf" in outs and pdfs:
                    p = unique_path(f"{stem}_searchable.pdf")
                    merge_pdfs(pdfs, p)
                    files_out.append(p)
            conf = csum / wsum if wsum else None
            if not full.strip():
                fails.append(f"'{label}': koi text nahi mila. Photo saaf/seedhi ho, sahi language chuno, ya 'saaf karo' try karo.")
            elif conf is not None and conf < 60:
                fails.append(f"'{label}': accuracy kam hai ({conf:.0f}%). Photo saaf aur seedhi ho, 'saaf karo' try karo, "
                             "ya sahi language/page-type chuno. Nateeja check kar lo.")
            results.append({"name": label, "pages": npages, "words": wsum,
                            "conf": None if conf is None else round(conf),
                            "text": full[:PREVIEW], "truncated": len(full) > PREVIEW,
                            "files": [{"name": p.name, "size": human(p.stat().st_size)} for p in files_out]})
        except Err as e:
            fails.append(f"'{label}': {e}")
        except Exception as e:  # noqa
            fails.append(f"'{label}': {e}")
    if not results:
        raise Err(" | ".join(fails) or "Kuch nahi bana.")
    return jsonify(ok=True, note=f"{len(results)} file(en) ho gayin ({time.time() - t0:.1f} second).",
                   results=results, warnings=fails)


@bp.route("/file/<path:rel>")
def serve(rel):
    return send_from_directory(OUT, rel, as_attachment=False)


@bp.route("/open-folder", methods=["POST"])
def open_folder():
    OUT.mkdir(parents=True, exist_ok=True)
    if IS_WIN:
        os.startfile(OUT)  # noqa
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(OUT)])
    else:
        subprocess.Popen(["xdg-open", str(OUT)])
    return jsonify(ok=True)
