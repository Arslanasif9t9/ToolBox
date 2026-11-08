"""Tool 3: Document Converter. Offline, no API key, no AI.

- Word / PowerPoint / Excel -> PDF : uses the MS Office installed on this PC (COM automation, Windows only)
- PDF -> Word                      : pdf2docx (text PDFs; scanned PDFs need OCR, Tool 7)
- Text / Markdown -> Word          : python-docx
"""
import os
import re
import subprocess
import sys
import tempfile
import threading
import uuid
from functools import wraps
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request, send_from_directory

META = {"order": 3, "name": "Document Converter", "slug": "converter", "icon": "🔄",
        "desc": "PDF → Word, Word/PPT/Excel → PDF (Office se), Text/Markdown → Word."}

bp = Blueprint("converter", __name__, url_prefix="/converter")

OUT = Path(__file__).resolve().parent.parent / "Output" / "DocumentConverter"
IS_WIN = sys.platform.startswith("win")
OFFICE_LOCK = threading.Lock()  # one Office conversion at a time

KINDS = {
    "word": {"prog": "Word.Application", "label": "Microsoft Word",
             "exts": {".doc", ".docx", ".docm", ".rtf", ".odt", ".txt"}},
    "ppt": {"prog": "PowerPoint.Application", "label": "Microsoft PowerPoint",
            "exts": {".ppt", ".pptx", ".pptm", ".pps", ".ppsx", ".odp"}},
    "excel": {"prog": "Excel.Application", "label": "Microsoft Excel",
              "exts": {".xls", ".xlsx", ".xlsm", ".xlsb", ".csv", ".ods"}},
}
ZIP_TYPES = {".docx", ".docm", ".pptx", ".pptm", ".xlsx", ".xlsm"}  # modern Office files are zips


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


def ok(paths, note="", warnings=()):
    files = [{"name": Path(p).relative_to(OUT).as_posix(), "size": human(Path(p).stat().st_size)} for p in paths]
    return jsonify(ok=True, files=files, note=note, warnings=list(warnings))


def finish(outs, fails, what):
    if not outs:
        raise Err(" | ".join(fails) or "Kuch convert nahi hua.")
    return ok(outs, f"{len(outs)} {what} ban gayi.", warnings=fails)


def save_uploads(key, allowed, tmpdir):
    files = [f for f in request.files.getlist(key) if f.filename]
    if not files:
        raise Err("Pehle file chuno.")
    items = []
    for f in files:
        ext = Path(f.filename).suffix.lower()
        if ext not in allowed:
            raise Err(f"'{f.filename}' is tab mein nahi chalti. Chalne wali types: {', '.join(sorted(allowed))}")
        p = Path(tmpdir) / f"in_{uuid.uuid4().hex[:8]}{ext}"
        f.save(p)
        items.append((f.filename, p))
    return items


def check_not_encrypted(name, path):
    """Password-protected modern Office files are not zips. Opening them in Office would hang on a dialog."""
    if path.suffix.lower() in ZIP_TYPES:
        with open(path, "rb") as fh:
            head = fh.read(4)
        if head != b"PK\x03\x04":
            raise Err("password-protected ya kharab lagti hai. Pehle Office mein password hata do.")


# ---------------------------------------------------------------- page, files
@bp.route("/")
def page():
    return render_template("converter.html", folder=str(OUT), is_win=IS_WIN)


@bp.route("/file/<path:rel>")
def serve(rel):
    return send_from_directory(OUT, rel)


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


# ---------------------------------------------------------------- Word / PowerPoint / Excel -> PDF (Office COM)
def _word_pdf(app, src, dst):
    # PasswordDocument="x" stops Word from showing a password dialog (wrong password = error, not a hang)
    doc = app.Documents.Open(FileName=str(src), ReadOnly=True, AddToRecentFiles=False,
                             ConfirmConversions=False, PasswordDocument="x")
    try:
        doc.ExportAsFixedFormat(OutputFileName=str(dst), ExportFormat=17)  # 17 = PDF
    finally:
        doc.Close(False)


def _ppt_pdf(app, src, dst):
    pres = app.Presentations.Open(FileName=str(src), ReadOnly=True, Untitled=False, WithWindow=False)
    try:
        pres.SaveAs(str(dst), 32)  # 32 = ppSaveAsPDF
    finally:
        pres.Close()


def _excel_pdf(app, src, dst):
    wb = app.Workbooks.Open(Filename=str(src), UpdateLinks=0, ReadOnly=True, Password="x",
                            IgnoreReadOnlyRecommended=True)
    try:
        wb.ExportAsFixedFormat(Type=0, Filename=str(dst))  # 0 = PDF
    finally:
        wb.Close(False)


CONVERT = {"word": _word_pdf, "ppt": _ppt_pdf, "excel": _excel_pdf}


def clean_err(e):
    hr = e.args[0] if getattr(e, "args", None) else None
    if hr == -2147221005:  # "Invalid class string": program not installed
        return "ye Office program is laptop par installed nahi mila."
    exc = getattr(e, "excepinfo", None)
    if exc and len(exc) > 2 and exc[2]:
        return str(exc[2]).strip()
    return str(e)


def office_to_pdf(kind, items):
    cfg = KINDS[kind]
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        raise Err("pywin32 install nahi hua. CMD mein chalao: .venv\\Scripts\\pip install -r requirements.txt")
    outs, fails = [], []
    pythoncom.CoInitialize()
    app = None
    try:
        try:
            app = win32com.client.DispatchEx(cfg["prog"])
        except Exception as e:
            raise Err(f"{cfg['label']} khul nahi saka: {clean_err(e)}")
        for attr, val in (("Visible", False), ("DisplayAlerts", 0 if kind == "word" else False)):
            try:
                setattr(app, attr, val)
            except Exception:
                pass  # PowerPoint refuses Visible=False; that is fine
        for name, src in items:
            dst = unique_path(f"{stem_of(name)}.pdf")
            try:
                check_not_encrypted(name, src)
                CONVERT[kind](app, src, dst)
                if not dst.exists():
                    raise Err("PDF nahi bani.")
                outs.append(dst)
            except Err as e:
                fails.append(f"'{name}': {e}")
            except Exception as e:
                fails.append(f"'{name}': {clean_err(e)}")
    finally:
        if app is not None:
            try:
                app.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()
    return outs, fails


@bp.route("/office/<kind>", methods=["POST"])
@api
def office(kind):
    cfg = KINDS.get(kind)
    if not cfg:
        raise Err("Ghalat type.")
    if not IS_WIN:
        raise Err("Ye hissa sirf Windows par chalta hai, jahan MS Office installed ho.")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        items = save_uploads("files", cfg["exts"], td)
        with OFFICE_LOCK:
            outs, fails = office_to_pdf(kind, items)
    return finish(outs, fails, "PDF")


# ---------------------------------------------------------------- PDF -> Word
def check_pdf_text(path, pw):
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    doc = fitz.open(str(path))
    if doc.needs_pass and not doc.authenticate(pw or ""):
        raise Err("password-protected hai, sahi password daalo.")
    if not any(pg.get_text().strip() for pg in doc):
        raise Err("is PDF mein text nahi (scanned / tasveer wali lagti hai). Iske liye OCR chahiye (Tool 7).")


@bp.route("/pdf-to-word", methods=["POST"])
@api
def pdf_to_word():
    try:
        from pdf2docx import Converter
    except ImportError:
        raise Err("pdf2docx install nahi hua. CMD mein chalao: .venv\\Scripts\\pip install -r requirements.txt")
    pw = request.form.get("password") or None
    outs, fails = [], []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        items = save_uploads("files", {".pdf"}, td)
        for name, src in items:
            try:
                check_pdf_text(src, pw)
                dst = unique_path(f"{stem_of(name)}.docx")
                cv = Converter(str(src), password=pw)
                try:
                    cv.convert(str(dst))
                finally:
                    cv.close()
                outs.append(dst)
            except Err as e:
                fails.append(f"'{name}': {e}")
            except Exception as e:
                fails.append(f"'{name}': conversion nahi hui ({e})")
    return finish(outs, fails, "Word file")


# ---------------------------------------------------------------- Text / Markdown -> Word
TOKEN = re.compile(r"(\*\*[^*]+\*\*|\*[^*\s][^*]*\*)")


def _rtl_run(run):
    rpr = run._r.get_or_add_rPr()
    rpr.get_or_add_rFonts().set("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}cs", "Arial")
    rpr.get_or_add_rtl()


def _rtl_par(par):
    from docx.oxml import OxmlElement
    ppr = par._p.get_or_add_pPr()
    ppr.insert_element_before(
        OxmlElement("w:bidi"),
        "w:adjustRightInd", "w:snapToGrid", "w:spacing", "w:ind", "w:contextualSpacing", "w:mirrorIndents",
        "w:suppressOverlap", "w:jc", "w:textDirection", "w:textAlignment", "w:textboxTightWrap",
        "w:outlineLvl", "w:divId", "w:cnfStyle", "w:rPr", "w:sectPr", "w:pPrChange")


def add_runs(par, text, rtl):
    for part in TOKEN.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            run = par.add_run(part[2:-2])
            run.bold = True
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            run = par.add_run(part[1:-1])
            run.italic = True
        else:
            run = par.add_run(part)
        if rtl:
            _rtl_run(run)


def build_docx(title, body, size, rtl, dst):
    from docx import Document
    from docx.shared import Pt
    doc = Document()
    doc.styles["Normal"].font.size = Pt(size)

    def new_par(style=None):
        par = doc.add_paragraph(style=style)
        if rtl:
            _rtl_par(par)
        return par

    if title:
        h = doc.add_heading(level=0)
        add_runs(h, title, rtl)
        if rtl:
            _rtl_par(h)
    for raw in body.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        m = re.match(r"^(#{1,3})\s+(.*)", line)
        if m:
            h = doc.add_heading(level=len(m.group(1)))
            add_runs(h, m.group(2), rtl)
            if rtl:
                _rtl_par(h)
            continue
        b = re.match(r"^\s*[-*]\s+(.*)", line)
        if b:
            add_runs(new_par("List Bullet"), b.group(1), rtl)
            continue
        n = re.match(r"^\s*\d+[.)]\s+(.*)", line)
        if n:
            add_runs(new_par("List Number"), n.group(1), rtl)
            continue
        add_runs(new_par(), line.strip(), rtl)
    doc.save(str(dst))


@bp.route("/text-to-word", methods=["POST"])
@api
def text_to_word():
    try:
        import docx  # noqa: F401
    except ImportError:
        raise Err("python-docx install nahi hua. CMD mein chalao: .venv\\Scripts\\pip install -r requirements.txt")
    title = request.form.get("title", "").strip()
    body = request.form.get("body", "")
    name = title
    up = request.files.get("file")
    if not body.strip() and up and up.filename:
        if Path(up.filename).suffix.lower() not in (".txt", ".md", ".markdown"):
            raise Err("Sirf .txt ya .md file chalti hai.")
        raw = up.read()
        for enc in ("utf-8-sig", "cp1252"):
            try:
                body = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise Err("File ka text padha nahi ja saka (UTF-8 mein save karo).")
        name = name or Path(up.filename).stem
    if not title and not body.strip():
        raise Err("Title ya text likho, ya .txt/.md file chuno.")
    try:
        size = max(8, min(int(request.form.get("size") or 12), 28))
    except ValueError:
        size = 12
    rtl = request.form.get("rtl") == "on"
    dst = unique_path(f"{stem_of(name) if name else 'document'}.docx")
    build_docx(title, body, size, rtl, dst)
    return ok([dst], "Word file ban gayi.")
