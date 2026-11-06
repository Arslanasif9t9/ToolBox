"""Tool 2: PDF Toolkit (PyMuPDF + Pillow). Offline, no API key, no AI."""
import html as htmllib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from functools import wraps
from pathlib import Path

try:
    import pymupdf as fitz  # PyMuPDF (new name)
except ImportError:  # older versions
    import fitz
from flask import Blueprint, Response, jsonify, render_template, request, send_from_directory
from PIL import Image, ImageOps

META = {"order": 2, "name": "PDF Toolkit", "slug": "pdf", "icon": "📄",
        "desc": "Merge, split, compress, rotate, watermark, password, edit, naya PDF - sab free."}

bp = Blueprint("pdf", __name__, url_prefix="/pdf")

OUT = Path(__file__).resolve().parent.parent / "Output" / "PDFToolkit"
TMP = Path(tempfile.gettempdir()) / "toolbox_pdf"


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


def ok(paths=(), note="", preview=""):
    files = []
    for p in paths:
        p = Path(p)
        size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else p.stat().st_size
        rel = p.relative_to(OUT).as_posix()
        files.append({"name": rel, "size": human(size), "is_dir": p.is_dir()})
    return jsonify(ok=True, files=files, note=note, preview=preview)


def get_file(key="file"):
    f = request.files.get(key)
    if not f or not f.filename:
        raise Err("Pehle PDF file chuno.")
    return f


def open_pdf(f, password=None):
    data = f.read()
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        raise Err(f"'{f.filename}' khul nahi saki. Sahi PDF hai?")
    if doc.needs_pass:
        if not password or not doc.authenticate(password):
            raise Err(f"'{f.filename}' password-protected hai. Sahi password daalo.")
    if doc.page_count == 0:
        raise Err("Is PDF mein koi page nahi.")
    return doc


def parse_spec(spec, n):
    """'1-3,5,8-' -> 0-based page list. Empty/'all' -> every page."""
    spec = (spec or "").strip().lower()
    if spec in ("", "all", "sab"):
        return list(range(n))
    out = []
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        m = re.fullmatch(r"(\d*)(-?)(\d*)", part)
        if not m or not (m.group(1) or m.group(3)):
            raise Err(f"'{part}' sahi page range nahi. Misal: 1-3,5,8-")
        a, dash, b = m.groups()
        if not dash:
            s = e = int(a)
        else:
            s = int(a) if a else 1
            e = int(b) if b else n
        if s < 1 or e > n or s > e:
            raise Err(f"Page range '{part}' galat hai (is PDF mein {n} pages hain).")
        out.extend(range(s - 1, e))
    if not out:
        raise Err("Koi page nahi chuna.")
    return out


def hex_rgb(h, default=(0, 0, 0)):
    try:
        h = h.lstrip("#")
        return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except Exception:
        return default


def need_latin(text):
    try:
        text.encode("cp1252")
    except UnicodeEncodeError:
        raise Err("Is hissay mein abhi sirf English/Latin letters chalte hain (Urdu text supported nahi).")


def save_pdf(doc, path, **kw):
    doc.save(str(path), garbage=3, deflate=True, **kw)


# ---------------------------------------------------------------- page + file serving
@bp.route("/")
def page():
    return render_template("pdf_toolkit.html", folder=str(OUT))


@bp.route("/file/<path:rel>")
def serve(rel):
    return send_from_directory(OUT, rel)


@bp.route("/open-folder", methods=["POST"])
def open_folder():
    OUT.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("win"):
        os.startfile(OUT)  # noqa
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(OUT)])
    else:
        subprocess.Popen(["xdg-open", str(OUT)])
    return jsonify(ok=True)


# ---------------------------------------------------------------- 1. merge
@bp.route("/merge", methods=["POST"])
@api
def merge():
    files = [f for f in request.files.getlist("files") if f.filename]
    if len(files) < 2:
        raise Err("Merge ke liye kam az kam 2 PDF chuno.")
    out = fitz.open()
    for f in files:
        out.insert_pdf(open_pdf(f, None))
    p = unique_path("merged.pdf")
    save_pdf(out, p)
    return ok([p], f"{len(files)} PDFs jodi gayin, kul {out.page_count} pages.")


# ---------------------------------------------------------------- 2. split
@bp.route("/split", methods=["POST"])
@api
def split():
    f = get_file()
    doc = open_pdf(f, request.form.get("password"))
    n, stem = doc.page_count, stem_of(f.filename)
    mode = request.form.get("mode", "ranges")
    groups = []  # (label, [pages])
    if mode == "each":
        groups = [(f"page{i + 1}", [i]) for i in range(n)]
    elif mode == "every":
        try:
            k = int(request.form.get("every") or 0)
        except ValueError:
            k = 0
        if k < 1:
            raise Err("'Har kitne pages' mein 1 ya zyada number daalo.")
        groups = [(f"pages{i + 1}-{min(i + k, n)}", list(range(i, min(i + k, n)))) for i in range(0, n, k)]
    else:
        spec = request.form.get("spec", "")
        if not spec.strip():
            raise Err("Page ranges likho, misal: 1-3,5,7-9")
        for part in spec.split(","):
            if part.strip():
                groups.append((f"pages{part.strip().replace(' ', '')}", parse_spec(part, n)))
    d = unique_path(f"{stem}_split")
    d.mkdir(parents=True, exist_ok=True)
    for label, pages in groups:
        new = fitz.open()
        for pg in pages:
            new.insert_pdf(doc, from_page=pg, to_page=pg)
        save_pdf(new, d / f"{stem}_{label}.pdf")
    return ok([d], f"{len(groups)} files banin.")


# ---------------------------------------------------------------- 3. keep / delete pages
@bp.route("/pages", methods=["POST"])
@api
def pages():
    f = get_file()
    doc = open_pdf(f, request.form.get("password"))
    n = doc.page_count
    chosen = parse_spec(request.form.get("spec", ""), n)
    if not request.form.get("spec", "").strip():
        raise Err("Page numbers likho, misal: 2,4-6")
    if request.form.get("mode") == "delete":
        keep = [i for i in range(n) if i not in set(chosen)]
        if not keep:
            raise Err("Saare pages delete nahi kar sakte.")
        tag = "removed"
    else:
        keep, tag = chosen, "selected"
    doc.select(keep)
    p = unique_path(f"{stem_of(f.filename)}_{tag}.pdf")
    save_pdf(doc, p)
    return ok([p], f"Ab {len(keep)} pages hain.")


# ---------------------------------------------------------------- 4. rotate
@bp.route("/rotate", methods=["POST"])
@api
def rotate():
    f = get_file()
    doc = open_pdf(f, request.form.get("password"))
    angle = int(request.form.get("angle", 90))
    for i in parse_spec(request.form.get("spec", ""), doc.page_count):
        pg = doc[i]
        pg.set_rotation((pg.rotation + angle) % 360)
    p = unique_path(f"{stem_of(f.filename)}_rotated.pdf")
    save_pdf(doc, p)
    return ok([p], f"{angle}° ghuma diya.")


# ---------------------------------------------------------------- 5. compress
LEVELS = {"low": (2000, 80), "medium": (1400, 65), "high": (1000, 50)}


def compress_images(doc, max_px, quality):
    seen = set()
    for pg in doc:
        for img in pg.get_images(full=True):
            xref = img[0]
            if xref in seen:
                continue
            seen.add(xref)
            try:
                info = doc.extract_image(xref)
                if not info or info.get("smask"):
                    continue  # skip transparent images
                im = Image.open(io.BytesIO(info["image"]))
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                scale = max_px / max(im.size)
                if scale < 1:
                    im = im.resize((int(im.width * scale), int(im.height * scale)), Image.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=quality, optimize=True)
                if buf.tell() < len(info["image"]):
                    pg.replace_image(xref, stream=buf.getvalue())
            except Exception:
                continue


@bp.route("/compress", methods=["POST"])
@api
def compress():
    f = get_file()
    raw = f.read()
    f.seek(0)
    doc = open_pdf(f, request.form.get("password"))
    max_px, q = LEVELS.get(request.form.get("level", "medium"), LEVELS["medium"])
    compress_images(doc, max_px, q)
    data = doc.tobytes(garbage=4, deflate=True, clean=True)
    if len(data) >= len(raw):
        return ok([], f"Is PDF mein aur compress ki gunjaish nahi (pehle se chhoti hai: {human(len(raw))}).")
    p = unique_path(f"{stem_of(f.filename)}_compressed.pdf")
    p.write_bytes(data)
    saved = 100 - len(data) * 100 / len(raw)
    return ok([p], f"{human(len(raw))} → {human(len(data))} ({saved:.0f}% kam).")


# ---------------------------------------------------------------- 6. images -> pdf
@bp.route("/images-to-pdf", methods=["POST"])
@api
def images_to_pdf():
    files = [f for f in request.files.getlist("images") if f.filename]
    if not files:
        raise Err("Kam az kam 1 image chuno.")
    a4 = request.form.get("size") == "a4"
    doc = fitz.open()
    for f in files:
        try:
            im = ImageOps.exif_transpose(Image.open(f))
        except Exception:
            raise Err(f"'{f.filename}' image nahi khul saki.")
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, "white")
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=92)
        w, h = im.size
        if a4:
            pw, ph = (842, 595) if w > h else (595, 842)
            page = doc.new_page(width=pw, height=ph)
            m = 20
            s = min((pw - 2 * m) / w, (ph - 2 * m) / h)
            rw, rh = w * s, h * s
            rect = fitz.Rect((pw - rw) / 2, (ph - rh) / 2, (pw + rw) / 2, (ph + rh) / 2)
        else:
            page = doc.new_page(width=w * 0.75, height=h * 0.75)
            rect = page.rect
        page.insert_image(rect, stream=buf.getvalue())
    p = unique_path("images.pdf")
    save_pdf(doc, p)
    return ok([p], f"{len(files)} images se PDF bani.")


# ---------------------------------------------------------------- 7. pdf -> images
@bp.route("/pdf-to-images", methods=["POST"])
@api
def pdf_to_images():
    f = get_file()
    doc = open_pdf(f, request.form.get("password"))
    fmt = "jpg" if request.form.get("fmt") == "jpg" else "png"
    dpi = max(50, min(int(request.form.get("dpi") or 150), 400))
    idx = parse_spec(request.form.get("spec", ""), doc.page_count)
    d = unique_path(f"{stem_of(f.filename)}_images")
    d.mkdir(parents=True, exist_ok=True)
    for i in idx:
        pix = doc[i].get_pixmap(dpi=dpi, alpha=False)
        pix.save(str(d / f"page-{i + 1:03d}.{fmt}"))
    return ok([d], f"{len(idx)} pages ki images ban gayin.")


# ---------------------------------------------------------------- 8. password
@bp.route("/password", methods=["POST"])
@api
def password():
    f = get_file()
    pw = request.form.get("pw", "")
    if not pw:
        raise Err("Password daalo.")
    if request.form.get("action") == "unlock":
        doc = open_pdf(f, pw)
        p = unique_path(f"{stem_of(f.filename)}_unlocked.pdf")
        doc.save(str(p), encryption=fitz.PDF_ENCRYPT_NONE, garbage=3, deflate=True)
        return ok([p], "Password hata diya.")
    doc = open_pdf(f, None)
    p = unique_path(f"{stem_of(f.filename)}_locked.pdf")
    doc.save(str(p), encryption=fitz.PDF_ENCRYPT_AES_256, user_pw=pw, owner_pw=pw, garbage=3, deflate=True)
    return ok([p], "Password laga diya. Yaad rakhna, bhool gaye to wapas nahi nikalega.")


# ---------------------------------------------------------------- 9. watermark + page numbers
@bp.route("/watermark", methods=["POST"])
@api
def watermark():
    f = get_file()
    doc = open_pdf(f, request.form.get("password"))
    text = request.form.get("text", "").strip()
    numbers = request.form.get("numbers") == "on"
    if not text and not numbers:
        raise Err("Watermark text likho ya page numbers ka option tick karo.")
    if text:
        need_latin(text)
    opacity = float(request.form.get("opacity") or 0.2)
    total = doc.page_count
    for i, pg in enumerate(doc):
        r = pg.rect
        if text:
            fs = min(r.width, r.height) / max(len(text), 6) * 1.6
            tl = fitz.get_text_length(text, fontname="helv", fontsize=fs)
            c = fitz.Point(r.width / 2, r.height / 2)
            pg.insert_text(fitz.Point(c.x - tl / 2, c.y + fs / 3), text, fontsize=fs, fontname="helv",
                           color=(0.5, 0.5, 0.5), fill_opacity=opacity, morph=(c, fitz.Matrix(45)))
        if numbers:
            label = f"{i + 1} / {total}"
            tl = fitz.get_text_length(label, fontname="helv", fontsize=10)
            pg.insert_text(fitz.Point((r.width - tl) / 2, r.height - 20), label, fontsize=10, fontname="helv",
                           color=(0.2, 0.2, 0.2))
    p = unique_path(f"{stem_of(f.filename)}_marked.pdf")
    save_pdf(doc, p)
    return ok([p], "Ho gaya.")


# ---------------------------------------------------------------- 10. extract text
@bp.route("/text", methods=["POST"])
@api
def extract_text():
    f = get_file()
    doc = open_pdf(f, request.form.get("password"))
    parts = [f"--- Page {i + 1} ---\n{pg.get_text().strip()}" for i, pg in enumerate(doc)]
    full = "\n\n".join(parts)
    if not re.sub(r"--- Page \d+ ---|\s", "", full):
        return ok([], "Is PDF mein text nahi mila. Ye scanned (tasveer wali) PDF lagti hai, iske liye OCR tool chahiye (Tool 7).")
    p = unique_path(f"{stem_of(f.filename)}.txt")
    p.write_text(full, encoding="utf-8")
    return ok([p], f"{doc.page_count} pages se text nikala.", preview=full[:3000])


# ---------------------------------------------------------------- 11. create new PDF
def md_to_html(body):
    out, in_ul = [], False
    for raw in body.splitlines():
        line = htmllib.escape(raw.rstrip())
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        bullet = re.match(r"^\s*[-*]\s+(.*)", line)
        if bullet:
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{bullet.group(1)}</li>")
            continue
        if in_ul:
            out.append("</ul>")
            in_ul = False
        h = re.match(r"^(#{1,3})\s+(.*)", line)
        if h:
            n = len(h.group(1))
            out.append(f"<h{n}>{h.group(2)}</h{n}>")
        elif line.strip():
            out.append(f"<p>{line}</p>")
    if in_ul:
        out.append("</ul>")
    return "\n".join(out)


@bp.route("/create", methods=["POST"])
@api
def create():
    title = request.form.get("title", "").strip()
    body = request.form.get("body", "")
    if not title and not body.strip():
        raise Err("Title ya text likho.")
    need_latin(title + body)
    fs = max(8, min(int(request.form.get("size") or 11), 24))
    css = (f"body{{font-family:sans-serif;font-size:{fs}pt;line-height:1.45}}"
           "h1{font-size:22pt;margin:0 0 8pt}h2{font-size:16pt;margin:12pt 0 4pt}h3{font-size:13pt;margin:10pt 0 3pt}"
           "p{margin:0 0 7pt}ul{margin:0 0 7pt}")
    html = f"<style>{css}</style>"
    if title:
        html += f"<h1>{htmllib.escape(title)}</h1>"
    html += md_to_html(body)
    p = unique_path(f"{stem_of(title) if title else 'document'}.pdf")
    story = fitz.Story(html=html)
    media = fitz.paper_rect("a4")
    where = media + (54, 54, -54, -54)
    writer = fitz.DocumentWriter(str(p))
    more = 1
    while more:
        dev = writer.begin_page(media)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
    return ok([p], "PDF ban gayi.")


# ---------------------------------------------------------------- 12. visual editor
def token_path(token):
    if not re.fullmatch(r"[0-9a-f]{12}", token or ""):
        raise Err("Edit session nahi mili, PDF dobara kholo.")
    p = TMP / f"{token}.pdf"
    if not p.exists():
        raise Err("Edit session khatam ho gaya, PDF dobara kholo.")
    return p


@bp.route("/edit/open", methods=["POST"])
@api
def edit_open():
    f = get_file()
    doc = open_pdf(f, request.form.get("password"))
    TMP.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:12]
    doc.save(str(TMP / f"{token}.pdf"), encryption=fitz.PDF_ENCRYPT_NONE)
    sizes = [[round(pg.rect.width, 2), round(pg.rect.height, 2)] for pg in doc]
    return jsonify(ok=True, token=token, sizes=sizes, name=f.filename)


@bp.route("/edit/preview/<token>/<int:n>")
@api
def edit_preview(token, n):
    doc = fitz.open(str(token_path(token)))
    if n < 0 or n >= doc.page_count:
        raise Err("Page nahi mila.")
    pix = doc[n].get_pixmap(dpi=110, alpha=False)
    return Response(pix.tobytes("png"), mimetype="image/png")


@bp.route("/edit/apply", methods=["POST"])
@api
def edit_apply():
    doc = fitz.open(str(token_path(request.form.get("token"))))
    try:
        ops = json.loads(request.form.get("ops", "[]"))
    except ValueError:
        raise Err("Edits samajh nahi aaye.")
    if not ops:
        raise Err("Koi edit nahi kiya. Pehle page par text / cover / image lagao.")
    for op in ops:
        pg = doc[int(op["page"])]
        dm, rot = pg.derotation_matrix, pg.rotation
        kind = op["type"]
        if kind == "text":
            text = op.get("text", "")
            need_latin(text)
            size = float(op["size"])
            pt = fitz.Point(float(op["x"]), float(op["y"]) + size * 0.85) * dm
            pg.insert_text(pt, text, fontsize=size, fontname="helv", color=hex_rgb(op.get("color", "#000000")),
                           rotate=rot)
        elif kind in ("rect", "image"):
            r = fitz.Rect(float(op["x0"]), float(op["y0"]), float(op["x1"]), float(op["y1"])) * dm
            r.normalize()
            if kind == "rect":
                pg.draw_rect(r, color=None, fill=hex_rgb(op.get("color", "#ffffff"), (1, 1, 1)), width=0)
            else:
                up = request.files.get(op.get("img", ""))
                if not up:
                    raise Err("Image file nahi mili.")
                pg.insert_image(r, stream=up.read(), keep_proportion=True, rotate=rot)
    name = stem_of(request.form.get("name", "edited"))
    p = unique_path(f"{name}_edited.pdf")
    save_pdf(doc, p)
    return ok([p], f"{len(ops)} edits lag gayin.")
