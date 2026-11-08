"""Tool 5: Image Tools (Pillow). Offline, no API key, no AI.

Compress (quality or target KB) | Resize | Convert format | Passport photo (+ print sheet)
Metadata (EXIF / GPS location) is not copied to the outputs; photo rotation is applied first.
"""
import io
import json
import os
import re
import subprocess
import sys
from functools import wraps
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request, send_from_directory
from PIL import Image, ImageDraw, ImageOps

META = {"order": 5, "name": "Image Tools", "slug": "images", "icon": "🖼️",
        "desc": "Compress, resize, format badalna, passport photo aur print sheet."}

bp = Blueprint("images", __name__, url_prefix="/images")

OUT = Path(__file__).resolve().parent.parent / "Output" / "ImageTools"
IS_WIN = sys.platform.startswith("win")
EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
EXT_FOR = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "BMP": ".bmp", "GIF": ".gif", "TIFF": ".tiff", "ICO": ".ico"}
LEVELS = {"low": 85, "medium": 70, "high": 50}
LANCZOS = Image.Resampling.LANCZOS


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
    s = re.sub(r"[^\w\-. ]+", "_", Path(name or "image").stem)[:80].strip()
    return s or "image"


def unique_path(name):
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / name
    stem, suf, i = p.stem, p.suffix, 1
    while p.exists():
        p = OUT / f"{stem} ({i}){suf}"
        i += 1
    return p


def ok(paths, note="", warnings=(), details=()):
    files = [{"name": Path(p).relative_to(OUT).as_posix(), "size": human(Path(p).stat().st_size)} for p in paths]
    return jsonify(ok=True, files=files, note=note, warnings=list(warnings), details=list(details))


def finish(outs, fails, note, details=()):
    if not outs:
        raise Err(" | ".join(fails) or "Kuch nahi bana.")
    return ok(outs, note, fails, details)


def uploads(key="files"):
    files = [f for f in request.files.getlist(key) if f.filename]
    if not files:
        raise Err("Pehle image(en) chuno.")
    return files


def has_alpha(im):
    return im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info


def normalize(im):
    if im.mode in ("RGB", "RGBA", "L"):
        return im
    if im.mode == "P":
        return im.convert("RGBA") if has_alpha(im) else im.convert("RGB")
    if im.mode == "LA":
        return im.convert("RGBA")
    if im.mode == "1":
        return im.convert("L")
    return im.convert("RGBA") if "A" in im.mode else im.convert("RGB")


def open_bytes(name, data):
    ext = Path(name).suffix.lower()
    if ext not in EXTS:
        raise Err(f"ye type nahi chalti ({ext or 'koi nahi'}). Chalti hain: JPG, PNG, WebP, BMP, GIF, TIFF.")
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        raise Err("image khul nahi saki (kharab hai ya supported nahi).")
    return normalize(ImageOps.exif_transpose(im))


def flat_rgb(im):
    """Composite transparency over white; keep greyscale as is (good for JPEG)."""
    if im.mode == "RGBA":
        bg = Image.new("RGB", im.size, "white")
        bg.paste(im, mask=im.split()[3])
        return bg
    return im


def same_format(ext, im):
    return {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP"}.get(
        ext.lower(), "PNG" if im.mode == "RGBA" else "JPEG")


def encode(im, fmt, quality=90, quantize=False, dpi=None):
    buf = io.BytesIO()
    kw = {"dpi": (dpi, dpi)} if dpi else {}
    if fmt == "JPEG":
        flat_rgb(im).save(buf, "JPEG", quality=quality, optimize=True, progressive=True, **kw)
    elif fmt == "WEBP":
        (im.convert("RGB") if im.mode == "L" else im).save(buf, "WEBP", quality=quality, method=6)
    elif fmt == "PNG":
        p = im
        if quantize:
            if im.mode == "RGBA":
                p = im.quantize(colors=256, method=Image.Quantize.FASTOCTREE)
            else:
                p = im.convert("RGB").quantize(colors=256)
        p.save(buf, "PNG", optimize=True, **kw)
    elif fmt == "BMP":
        flat_rgb(im).convert("RGB").save(buf, "BMP")
    elif fmt == "GIF":
        flat_rgb(im).convert("RGB").convert("P", palette=Image.Palette.ADAPTIVE, colors=256).save(buf, "GIF")
    elif fmt == "TIFF":
        im.save(buf, "TIFF", compression="tiff_lzw", **kw)
    else:
        raise Err("Ye format nahi chalta.")
    return buf.getvalue()


def fit_target(im, fmt, limit):
    """Highest quality that stays under `limit` bytes; shrinks the image if even quality 20 is too big."""
    cur = im
    for _ in range(10):
        lo, hi, best = 20, 95, None
        while lo <= hi:
            mid = (lo + hi) // 2
            blob = encode(cur, fmt, quality=mid)
            if len(blob) <= limit:
                best, lo = blob, mid + 1
            else:
                hi = mid - 1
        if best:
            return best, (cur.size if cur is not im else None)
        w, h = cur.size
        if max(w, h) < 300:
            break
        cur = cur.resize((int(w * 0.85), int(h * 0.85)), LANCZOS)
    raise Err(f"{limit // 1024} KB tak chhoti nahi ho saki. Target badhao.")


def num(key, default=None, lo=None, hi=None, cast=float):
    raw = (request.form.get(key) or "").strip()
    if not raw:
        return default
    label = LABELS.get(key, key)
    try:
        v = cast(raw)
    except ValueError:
        raise Err(f"{label} mein sahi number daalo.")
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise Err(f"{label} {lo} aur {hi} ke beech hona chahiye.")
    return v


LABELS = {"mm_w": "Width (mm)", "mm_h": "Height (mm)", "dpi": "DPI", "percent": "Percent", "width": "Width",
          "height": "Height", "quality": "Quality", "target_kb": "Target size (KB)", "max_px": "Max pixels"}


# ---------------------------------------------------------------- page, files
@bp.route("/")
def page():
    return render_template("image_tools.html", folder=str(OUT))


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


# ---------------------------------------------------------------- compress
@bp.route("/compress", methods=["POST"])
@api
def compress():
    level = request.form.get("level", "medium")
    q = LEVELS.get(level, 70)
    target_kb = num("target_kb", None, 5, 20000, int)
    out_fmt = request.form.get("fmt", "same")
    max_px = num("max_px", 0, 0, 20000, int)
    outs, fails, details = [], [], []
    tot_old = tot_new = 0
    for f in uploads():
        name = f.filename
        try:
            data = f.read()
            im = open_bytes(name, data)
            ext = Path(name).suffix
            orig_fmt = same_format(ext, im)
            fmt = {"jpg": "JPEG", "webp": "WEBP"}.get(out_fmt, orig_fmt)
            if max_px and max(im.size) > max_px:
                im = im.copy()
                im.thumbnail((max_px, max_px), LANCZOS)
            if target_kb:
                if fmt == "PNG":
                    raise Err("PNG ke liye target size nahi lag sakta. Output 'JPG' ya 'WebP' chuno.")
                blob, shrunk = fit_target(im, fmt, target_kb * 1024)
            else:
                blob, shrunk = encode(im, fmt, quality=q, quantize=(fmt == "PNG" and level == "high")), None
            if len(blob) >= len(data) and fmt == orig_fmt and not max_px and not target_kb:
                fails.append(f"'{name}': pehle se kaafi chhoti hai ({human(len(data))}), tabdeel nahi ki.")
                continue
            dst = unique_path(f"{stem_of(name)}_compressed{EXT_FOR[fmt]}")
            dst.write_bytes(blob)
            outs.append(dst)
            tot_old, tot_new = tot_old + len(data), tot_new + len(blob)
            line = f"{name}: {human(len(data))} → {human(len(blob))} ({100 - len(blob) * 100 / len(data):.0f}% kam)"
            if shrunk:
                line += f" (target ke liye image chhoti karni padi: {shrunk[0]}×{shrunk[1]} px)"
            details.append(line)
        except Err as e:
            fails.append(f"'{name}': {e}")
        except Exception as e:  # noqa
            fails.append(f"'{name}': {e}")
    note = f"{len(outs)} image(en) compress hui: {human(tot_old)} → {human(tot_new)}." if outs else ""
    return finish(outs, fails, note, details)


# ---------------------------------------------------------------- resize
@bp.route("/resize", methods=["POST"])
@api
def resize():
    mode = request.form.get("mode", "percent")
    pct = num("percent", 50, 1, 500)
    w = num("width", None, 1, 20000, int)
    h = num("height", None, 1, 20000, int)
    need = {"width": (w,), "height": (h,), "fit": (w, h), "fill": (w, h)}.get(mode, ())
    if any(v is None for v in need):
        raise Err("Is mode ke liye width/height daalo.")
    outs, fails, details = [], [], []
    for f in uploads():
        name = f.filename
        try:
            im = open_bytes(name, f.read())
            ow, oh = im.size
            if mode == "percent":
                new = im.resize((max(1, round(ow * pct / 100)), max(1, round(oh * pct / 100))), LANCZOS)
            elif mode == "width":
                new = im.resize((w, max(1, round(oh * w / ow))), LANCZOS)
            elif mode == "height":
                new = im.resize((max(1, round(ow * h / oh)), h), LANCZOS)
            elif mode == "fit":
                s = min(w / ow, h / oh)
                new = im.resize((max(1, round(ow * s)), max(1, round(oh * s))), LANCZOS)
            elif mode == "fill":
                new = ImageOps.fit(im, (w, h), LANCZOS)
            else:
                raise Err("Ghalat mode.")
            fmt = same_format(Path(name).suffix, im)
            dst = unique_path(f"{stem_of(name)}_{new.width}x{new.height}{EXT_FOR[fmt]}")
            dst.write_bytes(encode(new, fmt, quality=92))
            outs.append(dst)
            details.append(f"{name}: {ow}×{oh} → {new.width}×{new.height} px")
        except Err as e:
            fails.append(f"'{name}': {e}")
        except Exception as e:  # noqa
            fails.append(f"'{name}': {e}")
    return finish(outs, fails, f"{len(outs)} image(en) resize hui.", details)


# ---------------------------------------------------------------- convert
@bp.route("/convert", methods=["POST"])
@api
def convert():
    fmt = {"jpg": "JPEG", "png": "PNG", "webp": "WEBP", "bmp": "BMP", "gif": "GIF", "tiff": "TIFF", "ico": "ICO"}.get(
        request.form.get("fmt", "jpg"))
    if not fmt:
        raise Err("Format chuno.")
    q = num("quality", 90, 1, 100, int)
    ico_sizes = sorted({int(s) for s in request.form.getlist("ico") if s.isdigit() and 8 <= int(s) <= 256}) or [16, 32, 48, 256]
    outs, fails, details = [], [], []
    for f in uploads():
        name = f.filename
        try:
            im = open_bytes(name, f.read())
            dst = unique_path(f"{stem_of(name)}{EXT_FOR[fmt]}")
            if fmt == "ICO":
                rgba = im.convert("RGBA")
                side = max(rgba.size)
                sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
                sq.paste(rgba, ((side - rgba.width) // 2, (side - rgba.height) // 2))
                sq = sq.resize((256, 256), LANCZOS)
                sq.save(dst, "ICO", sizes=[(s, s) for s in ico_sizes])
            else:
                dst.write_bytes(encode(im, fmt, quality=q))
            outs.append(dst)
            details.append(f"{name} → {dst.name}")
        except Err as e:
            fails.append(f"'{name}': {e}")
        except Exception as e:  # noqa
            fails.append(f"'{name}': {e}")
    return finish(outs, fails, f"{len(outs)} image(en) {fmt} mein badal gayin.", details)


# ---------------------------------------------------------------- passport photo
SHEETS = {"4x6": (101.6, 152.4), "a4": (210.0, 297.0)}


def plan_sheet(sw, sh, pw, ph, margin=5.0, gap=2.0):
    cols = int((sw - 2 * margin + gap) // (pw + gap))
    rows = int((sh - 2 * margin + gap) // (ph + gap))
    return cols, rows


@bp.route("/passport", methods=["POST"])
@api
def passport():
    up = request.files.get("file")
    if not up or not up.filename:
        raise Err("Pehle photo chuno.")
    mm_w = num("mm_w", 35, 10, 100)
    mm_h = num("mm_h", 45, 10, 100)
    dpi = num("dpi", 300, 150, 600, int)
    try:
        c = json.loads(request.form.get("crop", ""))
        cx, cy, cw, ch = (float(c[k]) for k in ("x", "y", "w", "h"))
    except (ValueError, KeyError, TypeError):
        raise Err("Crop ka box nahi mila. Photo chun kar box set karo.")
    im = open_bytes(up.filename, up.read())
    W, H = im.size
    x0, y0 = max(0, round(cx * W)), max(0, round(cy * H))
    x1, y1 = min(W, round((cx + cw) * W)), min(H, round((cy + ch) * H))
    if x1 - x0 < 20 or y1 - y0 < 20:
        raise Err("Crop ka box bohat chhota hai.")
    px = (round(mm_w / 25.4 * dpi), round(mm_h / 25.4 * dpi))
    photo = flat_rgb(im.crop((x0, y0, x1, y1))).convert("RGB").resize(px, LANCZOS)
    base = stem_of(up.filename)
    single = unique_path(f"{base}_passport_{mm_w:g}x{mm_h:g}mm.jpg")
    photo.save(single, "JPEG", quality=95, dpi=(dpi, dpi))
    outs, note = [single], f"Photo {px[0]}×{px[1]} px ({mm_w:g}×{mm_h:g} mm, {dpi} DPI) ban gayi."

    sheet = request.form.get("sheet", "none")
    if sheet in SHEETS:
        bw, bh = SHEETS[sheet]
        best = max(((plan_sheet(bw, bh, mm_w, mm_h), (bw, bh)), (plan_sheet(bh, bw, mm_w, mm_h), (bh, bw))),
                   key=lambda t: t[0][0] * t[0][1])
        (cols, rows), (sw, sh) = best
        if cols * rows < 1:
            raise Err("Itni badi photo is sheet par nahi aati.")
        mm = lambda v: round(v / 25.4 * dpi)  # noqa: E731
        canvas = Image.new("RGB", (mm(sw), mm(sh)), "white")
        grid_w, grid_h = cols * mm_w + (cols - 1) * 2.0, rows * mm_h + (rows - 1) * 2.0
        ox, oy = (sw - grid_w) / 2, (sh - grid_h) / 2
        draw = ImageDraw.Draw(canvas)
        for r in range(rows):
            for cc in range(cols):
                x, y = mm(ox + cc * (mm_w + 2.0)), mm(oy + r * (mm_h + 2.0))
                canvas.paste(photo, (x, y))
                if request.form.get("cut") == "on":
                    draw.rectangle((x, y, x + px[0] - 1, y + px[1] - 1), outline=(190, 190, 190), width=1)
        as_pdf = request.form.get("sheet_fmt") == "pdf"
        sdst = unique_path(f"{base}_passport_sheet_{sheet}{'.pdf' if as_pdf else '.jpg'}")
        if as_pdf:
            canvas.save(sdst, "PDF", resolution=dpi)
        else:
            canvas.save(sdst, "JPEG", quality=95, dpi=(dpi, dpi))
        outs.append(sdst)
        note += f" Print sheet mein {cols * rows} copies hain."
    return ok(outs, note)
