"""Tool 4: CV / Resume Maker. Offline, no API key, no AI.

PDF  : HTML -> PDF with PyMuPDF (3 templates: classic, modern, compact)
Word : python-docx (clean single-column, ATS friendly)
Data is also saved as JSON in Output/CVMaker so a CV can be loaded and edited later.
"""
import base64
import html as htmllib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from functools import wraps
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:
    import fitz
from flask import Blueprint, jsonify, render_template, request, send_from_directory
from PIL import Image, ImageOps

META = {"order": 4, "name": "CV / Resume Maker", "slug": "cv", "icon": "📝",
        "desc": "Form bharo, template chuno, PDF aur Word CV banao. Data save, baad mein edit."}

bp = Blueprint("cv", __name__, url_prefix="/cv")

OUT = Path(__file__).resolve().parent.parent / "Output" / "CVMaker"
IS_WIN = sys.platform.startswith("win")
TEMPLATES = ("classic", "modern", "compact")
DEFAULT_ACCENT = {"classic": "#1f2937", "modern": "#2563eb", "compact": "#0f766e"}
MARGIN = {"classic": 50, "modern": 40, "compact": 32}


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


def slug(name):
    s = re.sub(r"[^\w\-]+", "_", name or "CV", flags=re.ASCII).strip("_")[:50]
    return s or "CV"


def unique_path(name):
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / name
    stem, suf, i = p.stem, p.suffix, 1
    while p.exists():
        p = OUT / f"{stem} ({i}){suf}"
        i += 1
    return p


def S(v):
    return str(v).strip() if isinstance(v, (str, int, float)) else ""


def split_lines(text):
    return [re.sub(r"^\s*[-•*·]\s*", "", ln).strip() for ln in S(text).splitlines() if ln.strip()]


def rows(items, keys):
    out = []
    for it in (items if isinstance(items, list) else [])[:40]:
        if isinstance(it, dict):
            r = {k: S(it.get(k)) for k in keys}
            if any(r.values()):
                out.append(r)
    return out


def norm(d):
    if not isinstance(d, dict):
        raise Err("CV ka data samajh nahi aaya.")
    tpl = d.get("template") if d.get("template") in TEMPLATES else "classic"
    acc = S(d.get("accent"))
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", acc):
        acc = DEFAULT_ACCENT[tpl]
    try:
        fs = max(8.0, min(float(d.get("font_size") or 10), 13.0))
    except (TypeError, ValueError):
        fs = 10.0
    cv = {
        "name": S(d.get("name")), "title": S(d.get("title")), "email": S(d.get("email")),
        "phone": S(d.get("phone")), "location": S(d.get("location")), "summary": S(d.get("summary")),
        "languages": S(d.get("languages")), "achievements": split_lines(d.get("achievements")),
        "links": rows(d.get("links"), ("label", "url")),
        "experience": rows(d.get("experience"), ("role", "company", "location", "start", "end", "bullets")),
        "education": rows(d.get("education"), ("degree", "school", "start", "end", "grade", "details")),
        "skills": rows(d.get("skills"), ("group", "items")),
        "projects": rows(d.get("projects"), ("name", "link", "desc")),
        "certs": rows(d.get("certs"), ("name", "issuer", "year")),
        "template": tpl, "accent": acc, "font_size": fs, "photo_file": S(d.get("photo_file")),
    }
    if not cv["name"]:
        raise Err("Apna naam likho.")
    return cv


def span(start, end, present=False):
    if start and end:
        return f"{start} – {end}"
    if start:
        return f"{start} – Present" if present else start
    return end


def contact_items(cv):
    items = [cv["email"], cv["phone"], cv["location"]]
    for l in cv["links"]:
        items.append(f"{l['label']}: {l['url']}" if l["label"] and l["url"] else (l["url"] or l["label"]))
    return [x for x in items if x]


# ---------------------------------------------------------------- photo
def get_photo(cv):
    """Returns JPEG bytes (3:4 crop) from a new upload, else the saved photo, else None."""
    up = request.files.get("photo")
    if up and up.filename:
        try:
            im = ImageOps.exif_transpose(Image.open(up)).convert("RGB")
        except Exception:
            raise Err("Photo khul nahi saki. JPG ya PNG chuno.")
        w, h = im.size
        target = 3 / 4
        if w / h > target:
            nw = int(h * target)
            left = (w - nw) // 2
            im = im.crop((left, 0, left + nw, h))
        else:
            nh = int(w / target)
            top = max(0, (h - nh) // 4)
            im = im.crop((0, top, w, top + nh))
        im.thumbnail((450, 600))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88)
        return buf.getvalue()
    if request.form.get("remove_photo") == "on":
        return None
    f = cv["photo_file"]
    if f and re.fullmatch(r"[\w\-]+_photo\.jpg", f) and (OUT / f).exists():
        return (OUT / f).read_bytes()
    return None


# ---------------------------------------------------------------- PDF (HTML -> PDF)
CSS = """
body { font-family: @FONT@; font-size: @FS@pt; color: #1f2937; line-height: 1.3; }
h1 { font-size: @H1@pt; margin: 0; color: @H1COLOR@; }
h2 { font-size: @H2@pt; margin: @H2TOP@pt 0 3pt 0; padding-bottom: 2pt; color: @ACC@; border-bottom: 1px solid @ACC@; }
p { margin: 0 0 2pt 0; }
ul { margin: 1pt 0 3pt 0; }
li { margin: 0 0 1.5pt 0; }
td { vertical-align: top; padding: 0; }
.sub { font-size: @SUB@pt; color: @SUBCOLOR@; margin: 2pt 0 2pt 0; }
.contact { font-size: @SMALL@pt; color: @SUBCOLOR@; }
.muted { color: #6b7280; }
.dates { text-align: right; color: #4b5563; }
.gap { margin-top: 3pt; }
"""


def esc(s):
    return htmllib.escape(s, quote=True)


def bullets_html(lines):
    return "<ul>" + "".join(f"<li>{esc(x)}</li>" for x in lines) + "</ul>" if lines else ""


def entry_head(left_html, right):
    return (f'<table style="width:100%; margin-top:3pt"><tr><td>{left_html}</td>'
            f'<td class="dates">{esc(right)}</td></tr></table>')


def build_html(cv, has_photo):
    t, acc, fs = cv["template"], cv["accent"], cv["font_size"]
    modern, compact = t == "modern", t == "compact"
    css = CSS
    for k, v in {
        "@FONT@": "serif" if t == "classic" else "sans-serif", "@FS@": f"{fs:g}",
        "@H1@": f"{fs * (2.0 if compact else 2.4):.1f}", "@H1COLOR@": "#ffffff" if modern else acc,
        "@H2@": f"{fs + 1:g}", "@H2TOP@": "6" if compact else "10", "@ACC@": acc,
        "@SUB@": f"{fs + 1.5:g}", "@SUBCOLOR@": "#e5e7eb" if modern else "#4b5563", "@SMALL@": f"{fs - 0.5:g}",
    }.items():
        css = css.replace(k, v)

    # header
    contact = " &nbsp;|&nbsp; ".join(esc(x).replace(" ", "&nbsp;") for x in contact_items(cv))
    left = f"<h1>{esc(cv['name'])}</h1>"
    if cv["title"]:
        left += f'<p class="sub">{esc(cv["title"])}</p>'
    if contact:
        left += f'<p class="contact">{contact}</p>'
    align = "left" if compact or modern else "center"
    cell = f"text-align:{align};"
    if modern:
        cell += f"background-color:{acc}; padding:12pt; color:#ffffff;"
    right = ""
    if has_photo:
        pstyle = f"width:80pt; text-align:right;" + (f"background-color:{acc}; padding:12pt;" if modern else "")
        right = f'<td style="{pstyle}"><img src="photo.jpg" width="62" height="83"></td>'
    body = f'<table style="width:100%"><tr><td style="{cell}">{left}</td>{right}</tr></table>'

    def sec(title, inner):
        return f"<h2>{esc(title.upper())}</h2>{inner}" if inner else ""

    if cv["summary"]:
        body += sec("Summary", f"<p>{esc(cv['summary'])}</p>")

    exp = ""
    for e in cv["experience"]:
        lhs = f"<b>{esc(e['role'])}</b>" if e["role"] else ""
        org = ", ".join(x for x in (e["company"], e["location"]) if x)
        if org:
            lhs += (" &nbsp;-&nbsp; " if lhs else "") + esc(org)
        exp += entry_head(lhs, span(e["start"], e["end"], present=True)) + bullets_html(split_lines(e["bullets"]))
    body += sec("Experience", exp)

    edu = ""
    for e in cv["education"]:
        lhs = f"<b>{esc(e['degree'])}</b>" if e["degree"] else ""
        if e["school"]:
            lhs += (", " if lhs else "") + esc(e["school"])
        edu += entry_head(lhs, span(e["start"], e["end"]))
        extra = " &nbsp;|&nbsp; ".join(esc(x) for x in (e["grade"], e["details"]) if x)
        if extra:
            edu += f'<p class="muted">{extra}</p>'
    body += sec("Education", edu)

    sk = ""
    for s in cv["skills"]:
        if s["group"] and s["items"]:
            sk += f"<p><b>{esc(s['group'])}:</b> {esc(s['items'])}</p>"
        else:
            sk += f"<p>{esc(s['items'] or s['group'])}</p>"
    body += sec("Skills", sk)

    pr = ""
    for p in cv["projects"]:
        head = f"<b>{esc(p['name'])}</b>" if p["name"] else ""
        if p["link"]:
            head += (" &nbsp;-&nbsp; " if head else "") + f'<span class="muted">{esc(p["link"])}</span>'
        pr += f'<p class="gap">{head}</p>'
        d = split_lines(p["desc"])
        pr += bullets_html(d) if len(d) > 1 else (f"<p>{esc(d[0])}</p>" if d else "")
    body += sec("Projects", pr)

    certs = [" - ".join(x for x in (c["name"], c["issuer"]) if x) + (f" ({c['year']})" if c["year"] else "")
             for c in cv["certs"]]
    body += sec("Certifications", bullets_html(certs))
    body += sec("Achievements", bullets_html(cv["achievements"]))
    if cv["languages"]:
        body += sec("Languages", f"<p>{esc(cv['languages'])}</p>")
    return f"<style>{css}</style>{body}"


def render_pdf(cv, photo):
    html = build_html(cv, bool(photo))
    m = MARGIN[cv["template"]]
    media = fitz.paper_rect("a4")
    where = media + (m, m, -m, -m)
    buf = io.BytesIO()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        if photo:
            (Path(td) / "photo.jpg").write_bytes(photo)
        story = fitz.Story(html=html, archive=fitz.Archive(td))
        writer = fitz.DocumentWriter(buf)
        more, pages = 1, 0
        while more and pages < 8:
            dev = writer.begin_page(media)
            more, _ = story.place(where)
            story.draw(dev)
            writer.end_page()
            pages += 1
        writer.close()
    return buf.getvalue(), pages


# ---------------------------------------------------------------- Word (python-docx)
def _bottom_border(par, color_hex):
    from docx.oxml import OxmlElement
    ppr = par._p.get_or_add_pPr()
    bdr = OxmlElement("w:pBdr")
    b = OxmlElement("w:bottom")
    for k, v in (("w:val", "single"), ("w:sz", "6"), ("w:space", "1"), ("w:color", color_hex)):
        b.set(f"{{http://schemas.openxmlformats.org/wordprocessingml/2006/main}}{k[2:]}", v)
    bdr.append(b)
    ppr.insert_element_before(
        bdr, "w:shd", "w:tabs", "w:suppressAutoHyphens", "w:kinsoku", "w:wordWrap", "w:overflowPunct",
        "w:topLinePunct", "w:autoSpaceDE", "w:autoSpaceDN", "w:bidi", "w:adjustRightInd", "w:snapToGrid",
        "w:spacing", "w:ind", "w:contextualSpacing", "w:mirrorIndents", "w:suppressOverlap", "w:jc",
        "w:textDirection", "w:textAlignment", "w:textboxTightWrap", "w:outlineLvl", "w:divId", "w:cnfStyle",
        "w:rPr", "w:sectPr", "w:pPrChange")


def build_docx(cv, photo, dst):
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
    from docx.shared import Mm, Pt, RGBColor

    fs = cv["font_size"]
    acc_hex = cv["accent"].lstrip("#").upper()
    acc = RGBColor.from_string(acc_hex)
    doc = Document()
    sec0 = doc.sections[0]
    sec0.page_width, sec0.page_height = Mm(210), Mm(297)
    sec0.left_margin = sec0.right_margin = Mm(18)
    sec0.top_margin = sec0.bottom_margin = Mm(15)
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(fs)
    normal.paragraph_format.space_after = Pt(2)
    width = Mm(174)

    def para(box, text="", bold=False, italic=False, size=None, color=None, align=None, after=None, style=None):
        p = box.add_paragraph(style=style)
        if text:
            r = p.add_run(text)
            r.bold, r.italic = bold, italic
            if size:
                r.font.size = Pt(size)
            if color is not None:
                r.font.color.rgb = color
        if align is not None:
            p.alignment = align
        if after is not None:
            p.paragraph_format.space_after = Pt(after)
        return p

    # header
    contact = "  |  ".join(contact_items(cv))
    if photo:
        tbl = doc.add_table(rows=1, cols=2)
        tbl.autofit = False
        c0, c1 = tbl.rows[0].cells
        c0.width, c1.width = Mm(150), Mm(24)
        box = c0
        pic = c1.paragraphs[0]
        pic.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        pic.add_run().add_picture(io.BytesIO(photo), width=Mm(22))
    else:
        box = doc
    para(box, cv["name"], bold=True, size=fs * 2.2, color=acc, after=0)
    if cv["title"]:
        para(box, cv["title"], size=fs + 1.5, color=RGBColor(0x4B, 0x55, 0x63), after=1)
    if contact:
        para(box, contact, size=fs - 0.5, color=RGBColor(0x4B, 0x55, 0x63), after=2)
    if photo:
        c0._tc.remove(c0.paragraphs[0]._p)  # drop the empty first paragraph

    def heading(text):
        p = para(doc, text.upper(), bold=True, size=fs + 1, color=acc, after=3)
        p.paragraph_format.space_before = Pt(9)
        p.paragraph_format.keep_with_next = True
        _bottom_border(p, acc_hex)

    def line(left_bold, left_rest, right):
        p = doc.add_paragraph()
        p.paragraph_format.tab_stops.add_tab_stop(width, WD_TAB_ALIGNMENT.RIGHT)
        p.paragraph_format.space_before = Pt(3)
        p.paragraph_format.space_after = Pt(1)
        p.paragraph_format.keep_with_next = True
        if left_bold:
            p.add_run(left_bold).bold = True
        if left_rest:
            p.add_run((", " if left_bold else "") + left_rest)
        if right:
            r = p.add_run("\t" + right)
            r.italic = True
        return p

    def bullets(lines):
        for x in lines:
            p = doc.add_paragraph(x, style="List Bullet")
            p.paragraph_format.space_after = Pt(1)

    if cv["summary"]:
        heading("Summary")
        para(doc, cv["summary"])
    if cv["experience"]:
        heading("Experience")
        for e in cv["experience"]:
            line(e["role"], ", ".join(x for x in (e["company"], e["location"]) if x),
                 span(e["start"], e["end"], present=True))
            bullets(split_lines(e["bullets"]))
    if cv["education"]:
        heading("Education")
        for e in cv["education"]:
            line(e["degree"], e["school"], span(e["start"], e["end"]))
            extra = "  |  ".join(x for x in (e["grade"], e["details"]) if x)
            if extra:
                para(doc, extra, color=RGBColor(0x6B, 0x72, 0x80))
    if cv["skills"]:
        heading("Skills")
        for s in cv["skills"]:
            p = doc.add_paragraph()
            if s["group"] and s["items"]:
                p.add_run(s["group"] + ": ").bold = True
                p.add_run(s["items"])
            else:
                p.add_run(s["items"] or s["group"])
    if cv["projects"]:
        heading("Projects")
        for pr in cv["projects"]:
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(3)
            p.paragraph_format.keep_with_next = True
            if pr["name"]:
                p.add_run(pr["name"]).bold = True
            if pr["link"]:
                p.add_run((" - " if pr["name"] else "") + pr["link"]).font.color.rgb = RGBColor(0x6B, 0x72, 0x80)
            d = split_lines(pr["desc"])
            if len(d) > 1:
                bullets(d)
            elif d:
                para(doc, d[0])
    if cv["certs"]:
        heading("Certifications")
        bullets([" - ".join(x for x in (c["name"], c["issuer"]) if x) + (f" ({c['year']})" if c["year"] else "")
                 for c in cv["certs"]])
    if cv["achievements"]:
        heading("Achievements")
        bullets(cv["achievements"])
    if cv["languages"]:
        heading("Languages")
        para(doc, cv["languages"])
    doc.save(str(dst))


# ---------------------------------------------------------------- routes
def read_request():
    try:
        data = json.loads(request.form.get("data", "{}"))
    except ValueError:
        raise Err("Form ka data samajh nahi aaya.")
    cv = norm(data)
    return cv, get_photo(cv)


@bp.route("/")
def page():
    return render_template("cv_maker.html", folder=str(OUT))


@bp.route("/preview", methods=["POST"])
@api
def preview():
    cv, photo = read_request()
    pdf, pages = render_pdf(cv, photo)
    doc = fitz.open("pdf", pdf)
    imgs = ["data:image/png;base64," + base64.b64encode(pg.get_pixmap(dpi=85, alpha=False).tobytes("png")).decode()
            for pg in doc]
    return jsonify(ok=True, pages=imgs, n=pages)


@bp.route("/generate", methods=["POST"])
@api
def generate():
    cv, photo = read_request()
    fmt = request.form.get("fmt", "pdf")
    base = slug(cv["name"])
    OUT.mkdir(parents=True, exist_ok=True)
    saved = dict(cv)
    if photo:
        (OUT / f"{base}_photo.jpg").write_bytes(photo)
        saved["photo_file"] = f"{base}_photo.jpg"
    else:
        saved["photo_file"] = ""
    (OUT / f"{base}.json").write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
    if fmt == "docx":
        dst = unique_path(f"{base}_CV.docx")
        build_docx(cv, photo, dst)
        note = "Word CV ban gayi."
    else:
        dst = unique_path(f"{base}_CV_{cv['template']}.pdf")
        pdf, pages = render_pdf(cv, photo)
        dst.write_bytes(pdf)
        note = f"PDF CV ban gayi ({pages} page{'s' if pages != 1 else ''})."
    return jsonify(ok=True, note=note, files=[{"name": dst.name, "size": human(dst.stat().st_size)}])


@bp.route("/saved")
def saved_list():
    OUT.mkdir(parents=True, exist_ok=True)
    items = sorted(OUT.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return jsonify(items=[p.name for p in items])


@bp.route("/load/<name>")
@api
def load(name):
    if not re.fullmatch(r"[\w\-]+\.json", name) or not (OUT / name).exists():
        raise Err("Saved CV nahi mili.")
    return jsonify(ok=True, data=json.loads((OUT / name).read_text(encoding="utf-8")))


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
