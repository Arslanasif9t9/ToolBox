"""Tool 8: QR / Barcode. Make styled QR codes and barcodes, read them from a photo or the live camera. Offline.

QR drawing is our own renderer on top of the `qrcode` matrix (styles, gradients, eyes, logo, frame) and produces
PNG, SVG and PDF. Reading uses OpenCV (QR + common barcodes).
"""
import base64
import csv
import io
import math
import os
import re
import subprocess
import sys
import time
import zipfile
from functools import wraps
from pathlib import Path
from urllib.parse import quote

import cv2
import numpy as np
import qrcode
from flask import Blueprint, jsonify, render_template, request, send_from_directory
from PIL import Image, ImageDraw, ImageFont, ImageOps

import barcode as pybarcode
from barcode.writer import ImageWriter, SVGWriter

try:  # best reader (same family as phone scanners); OpenCV is the fallback
    import zxingcpp
except Exception:  # noqa
    zxingcpp = None

META = {"order": 8, "name": "QR / Barcode", "slug": "qr", "icon": "🔳",
        "desc": "Sundar QR banao (rang, logo, frame), barcode banao, aur camera/photo se QR parho. Offline."}

bp = Blueprint("qr", __name__, url_prefix="/qr")

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "Output" / "QRBarcode"
IS_WIN = sys.platform.startswith("win")

EC = {"L": qrcode.constants.ERROR_CORRECT_L, "M": qrcode.constants.ERROR_CORRECT_M,
      "Q": qrcode.constants.ERROR_CORRECT_Q, "H": qrcode.constants.ERROR_CORRECT_H}
MOD_STYLES = ("square", "rounded", "dots", "liquid")
EYE_STYLES = ("square", "rounded", "circle")
GRADS = ("none", "h", "v", "diag", "radial")
BARCODES = {"code128": "Code 128 (letters, digits, symbols)", "code39": "Code 39 (CAPITAL letters, digits)",
            "ean13": "EAN-13 (12 ya 13 digits)", "ean8": "EAN-8 (7 ya 8 digits)",
            "upca": "UPC-A (11 ya 12 digits)", "itf": "ITF (digits, jorey mein)"}
MAX_BULK = 300


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


def safe_name(s, default="code"):
    s = re.sub(r"[^\w\-. ]+", "_", str(s or ""))[:40].strip(" ._")
    return s or default


def unique_path(name, folder=None):
    folder = folder or OUT
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    stem, suf, i = p.stem, p.suffix, 1
    while p.exists():
        p = folder / f"{stem} ({i}){suf}"
        i += 1
    return p


def hex_rgb(h, default):
    h = (h or "").strip().lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{6}", h):
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    return default


def lum(c):
    def f(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2])


def contrast(a, b):
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def data_url(img, fmt="PNG"):
    buf = io.BytesIO()
    img.save(buf, fmt)
    return f"data:image/{fmt.lower()};base64," + base64.b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------- QR payloads
def esc_wifi(s):
    return re.sub(r'([\\;,:"])', r"\\\1", s)


def esc_vcard(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def build_payload(form):
    kind = form.get("kind", "text")
    g = lambda n: (form.get(n) or "").strip()  # noqa
    notes = []
    if kind == "text":
        t = (form.get("text") or "").strip()
        if not t:
            raise Err("QR mein kya likhna hai? Text ya link daalo.")
        if re.fullmatch(r"www\.\S+", t):
            t = "https://" + t
        return t, notes
    if kind == "wifi":
        ssid, enc = g("wifi_ssid"), g("wifi_enc") or "WPA"
        if not ssid:
            raise Err("WiFi ka naam (SSID) likho.")
        if enc not in ("WPA", "WEP", "nopass"):
            enc = "WPA"
        pw = "" if enc == "nopass" else form.get("wifi_pass", "")
        if enc != "nopass" and not pw:
            raise Err("WiFi ka password likho (ya 'Password nahi' chuno).")
        hidden = "true" if form.get("wifi_hidden") == "on" else "false"
        return f"WIFI:T:{enc};S:{esc_wifi(ssid)};" + (f"P:{esc_wifi(pw)};" if pw else "") + f"H:{hidden};;", notes
    if kind in ("whatsapp", "phone", "sms"):
        raw = g("num")
        digits = re.sub(r"\D", "", raw)
        if not digits:
            raise Err("Phone number likho.")
        if re.fullmatch(r"0[3]\d{9}", digits):  # Pakistani mobile 03xx... -> 923xx...
            digits = "92" + digits[1:]
            notes.append(f"Number {digits} bana diya (03... ki jagah 92...).")
        msg = form.get("msg", "").strip()
        if kind == "whatsapp":
            return f"https://wa.me/{digits}" + (f"?text={quote(msg)}" if msg else ""), notes
        if kind == "phone":
            return f"tel:+{digits}", notes
        return f"SMSTO:+{digits}:{msg}", notes
    if kind == "email":
        to = g("mail_to")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", to):
            raise Err("Sahi email address likho.")
        parts = []
        if g("mail_sub"):
            parts.append("subject=" + quote(g("mail_sub")))
        if form.get("mail_body", "").strip():
            parts.append("body=" + quote(form.get("mail_body").strip()))
        return f"mailto:{to}" + ("?" + "&".join(parts) if parts else ""), notes
    if kind == "vcard":
        name = g("vc_name")
        if not name:
            raise Err("Contact ka naam likho.")
        lines = ["BEGIN:VCARD", "VERSION:3.0", f"FN:{esc_vcard(name)}", f"N:{esc_vcard(name)};;;;"]
        for key, tag in (("vc_org", "ORG"), ("vc_title", "TITLE"), ("vc_url", "URL")):
            if g(key):
                lines.append(f"{tag}:{esc_vcard(g(key))}")
        if g("vc_phone"):
            lines.append(f"TEL;TYPE=CELL:{re.sub(r'[^0-9+]', '', g('vc_phone'))}")
        if g("vc_email"):
            lines.append(f"EMAIL:{g('vc_email')}")
        if g("vc_addr"):
            lines.append(f"ADR:;;{esc_vcard(g('vc_addr'))};;;;")
        lines.append("END:VCARD")
        return "\n".join(lines), notes
    if kind == "geo":
        try:
            lat, lng = float(g("lat")), float(g("lng"))
        except ValueError:
            raise Err("Latitude aur longitude numbers mein likho (jaise 32.1 aur 74.2).")
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise Err("Latitude -90..90 aur longitude -180..180 ke beech honi chahiye.")
        return f"geo:{lat},{lng}", notes
    raise Err("Ghalat qism.")


# ---------------------------------------------------------------- QR options
def pick(v, allowed, default):
    return v if v in allowed else default


def parse_opts(form, logo_file=None):
    c1 = hex_rgb(form.get("c1"), (0, 0, 0))
    grad = pick(form.get("grad"), GRADS, "none")
    c2 = hex_rgb(form.get("c2"), c1) if grad != "none" else c1
    try:
        size = min(max(int(form.get("size", "1000")), 300), 2000)
    except ValueError:
        size = 1000
    try:
        logo_frac = min(max(float(form.get("logo_pct", "22")) / 100, 0.10), 0.30)
    except ValueError:
        logo_frac = 0.22
    logo = None
    if logo_file is not None and logo_file.filename:
        try:
            logo = Image.open(io.BytesIO(logo_file.read()))
            logo = ImageOps.exif_transpose(logo).convert("RGBA")
        except Exception:
            raise Err("Logo ki image khul nahi saki.")
    return {
        "style": pick(form.get("style"), MOD_STYLES, "square"),
        "eye_outer": pick(form.get("eye_outer"), EYE_STYLES, "square"),
        "eye_inner": pick(form.get("eye_inner"), EYE_STYLES, "square"),
        "c1": c1, "c2": c2, "grad": grad,
        "eye_color": hex_rgb(form.get("eye_color"), c1) if form.get("eye_custom") == "on" else None,
        "bg": None if form.get("bg_transparent") == "on" else hex_rgb(form.get("bg"), (255, 255, 255)),
        "ec": pick(form.get("ec"), EC, "M"),
        "size": size, "logo": logo, "logo_frac": logo_frac,
        "frame": "label" if form.get("frame") == "on" else "none",
        "caption": (form.get("caption") or "").strip()[:24],
    }


def make_matrix(data, ec):
    qr = qrcode.QRCode(error_correction=EC[ec], border=0, box_size=1)
    qr.add_data(data)
    try:
        qr.make(fit=True)
    except Exception:
        raise Err("Itna zyada data QR mein nahi aata. Text chhota karo.")
    return qr.get_matrix(), qr.version


# ---------------------------------------------------------------- QR geometry (units = modules)
def rr(x, y, w, h, r, corners=(1, 1, 1, 1)):
    return ("rr", x, y, w, h, r, tuple(bool(c) for c in corners))


def circ(cx, cy, r):
    return ("c", cx, cy, r)


def build_scene(mat, o):
    n = len(mat)
    framed = o["frame"] != "none"
    q = 3 if framed else 4
    U = n + 2 * q
    pad = U * 0.07 if framed else 0.0
    band = U * 0.15 if framed and o["caption"] else 0.0
    sc = {"n": n, "q": q, "U": U, "pad": pad, "band": band, "ox": pad, "oy": pad,
          "TW": U + 2 * pad, "TH": U + 2 * pad + band, "box": None}
    ox, oy = sc["ox"], sc["oy"]
    if o["logo"] is not None:
        L = o["logo_frac"] * n
        cx, cy = ox + U / 2, oy + U / 2
        sc["box"] = (cx - L / 2, cy - L / 2, cx + L / 2, cy + L / 2)
    mods = []
    eye_cells = lambda i, j: (i < 7 and j < 7) or (i < 7 and j >= n - 7) or (i >= n - 7 and j < 7)  # noqa
    for i in range(n):
        for j in range(n):
            if not mat[i][j] or eye_cells(i, j):
                continue
            x, y = ox + q + j, oy + q + i
            if sc["box"] and not (x + 1 < sc["box"][0] - 0.4 or x > sc["box"][2] + 0.4
                                  or y + 1 < sc["box"][1] - 0.4 or y > sc["box"][3] + 0.4):
                continue
            s = o["style"]
            if s == "square":
                mods.append(rr(x, y, 1, 1, 0))
            elif s == "rounded":
                mods.append(rr(x + .04, y + .04, .92, .92, .3))
            elif s == "dots":
                mods.append(circ(x + .5, y + .5, .45))
            else:  # liquid
                up = i > 0 and mat[i - 1][j]
                dn = i < n - 1 and mat[i + 1][j]
                lf = j > 0 and mat[i][j - 1]
                rt = j < n - 1 and mat[i][j + 1]
                mods.append(rr(x, y, 1, 1, .5, (not up and not lf, not up and not rt, not dn and not rt, not dn and not lf)))
    eyes = []
    for (ci, ri) in ((0, 0), (n - 7, 0), (0, n - 7)):
        x, y = ox + q + ci, oy + q + ri
        eo, ei = o["eye_outer"], o["eye_inner"]
        outer = {"square": rr(x, y, 7, 7, 0), "rounded": rr(x, y, 7, 7, 1.9), "circle": circ(x + 3.5, y + 3.5, 3.5)}[eo]
        hole = {"square": rr(x + 1, y + 1, 5, 5, 0), "rounded": rr(x + 1, y + 1, 5, 5, 1.2),
                "circle": circ(x + 3.5, y + 3.5, 2.5)}[eo]
        pupil = {"square": rr(x + 2, y + 2, 3, 3, 0), "rounded": rr(x + 2, y + 2, 3, 3, .9),
                 "circle": circ(x + 3.5, y + 3.5, 1.5)}[ei]
        eyes.append((outer, hole, pupil))
    sc["mods"], sc["eyes"] = mods, eyes
    if framed:
        b = U * 0.022
        rad = pad * 1.3
        sc["frame_outer"] = rr(0, 0, sc["TW"], sc["TH"], rad)
        sc["frame_hole"] = rr(b, b, sc["TW"] - 2 * b, sc["TH"] - 2 * b, max(rad - b, 0))
        if band:
            sc["pill"] = rr(pad, oy + U + band * 0.1, U, band * 0.8, band * 0.4)
    return sc


# ---------------------------------------------------------------- QR raster renderer
def draw_prim(d, p, S, fill):
    if p[0] == "rr":
        _, x, y, w, h, r, cs = p
        box = [round(x * S), round(y * S), round((x + w) * S) - 1, round((y + h) * S) - 1]
        if r <= 0:
            d.rectangle(box, fill=fill)
        else:
            rad = min(r * S, (min(box[2] - box[0], box[3] - box[1]) + 1) / 2 - 2)  # Pillow breaks near half-size
            if rad < 1:
                d.rectangle(box, fill=fill)
            else:
                d.rounded_rectangle(box, radius=rad, fill=fill, corners=cs)
    else:
        _, cx, cy, r = p
        d.ellipse([(cx - r) * S, (cy - r) * S, (cx + r) * S, (cy + r) * S], fill=fill)


def gradient_img(W, H, c1, c2, kind, box):
    if kind == "none" or c1 == c2:
        return Image.new("RGB", (W, H), c1)
    x0, y0, x1, y1 = box
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    u, v = (xx - x0) / max(x1 - x0, 1), (yy - y0) / max(y1 - y0, 1)
    t = {"h": u, "v": v, "diag": (u + v) / 2}.get(kind)
    if t is None:
        t = np.hypot(u - .5, v - .5) / 0.7071
    t = np.clip(t, 0, 1)[..., None]
    arr = np.array(c1, np.float32) * (1 - t) + np.array(c2, np.float32) * t
    return Image.fromarray(arr.astype(np.uint8), "RGB")


FONT_CANDIDATES = ["C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf",
                   "/Library/Fonts/Arial Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]


def load_font(px):
    for f in FONT_CANDIDATES:
        if os.path.exists(f):
            try:
                return ImageFont.truetype(f, px)
            except Exception:  # noqa
                pass
    try:
        return ImageFont.load_default(size=px)
    except TypeError:
        return ImageFont.load_default()


def masked_layer(color_img, mask):
    layer = color_img.convert("RGBA")
    layer.putalpha(mask)
    return layer


def render_raster(sc, o, size=None):
    W = size or o["size"]
    px = W / sc["TW"]
    H = round(sc["TH"] * px)
    SS = 3 if W <= 1200 else 2
    S = px * SS
    big = (round(sc["TW"] * S), round(sc["TH"] * S))

    def shrink(m):
        return m.resize((W, H), Image.Resampling.LANCZOS)

    m_mod, m_eye = Image.new("L", big, 0), Image.new("L", big, 0)
    d1, d2 = ImageDraw.Draw(m_mod), ImageDraw.Draw(m_eye)
    for p in sc["mods"]:
        draw_prim(d1, p, S, 255)
    for outer, hole, pupil in sc["eyes"]:
        draw_prim(d2, outer, S, 255)
        draw_prim(d2, hole, S, 0)
        draw_prim(d2, pupil, S, 255)
    qa = (sc["ox"] * px, sc["oy"] * px, (sc["ox"] + sc["U"]) * px, (sc["oy"] + sc["U"]) * px)
    base = Image.new("RGBA", (W, H), (o["bg"] + (255,)) if o["bg"] else (0, 0, 0, 0))
    fg = gradient_img(W, H, o["c1"], o["c2"], o["grad"], qa)
    base = Image.alpha_composite(base, masked_layer(fg, shrink(m_mod)))
    eye_fill = Image.new("RGB", (W, H), o["eye_color"]) if o["eye_color"] else fg
    base = Image.alpha_composite(base, masked_layer(eye_fill, shrink(m_eye)))
    if o["frame"] != "none":
        m_fr = Image.new("L", big, 0)
        d3 = ImageDraw.Draw(m_fr)
        draw_prim(d3, sc["frame_outer"], S, 255)
        draw_prim(d3, sc["frame_hole"], S, 0)
        if sc.get("pill"):
            draw_prim(d3, sc["pill"], S, 255)
        base = Image.alpha_composite(base, masked_layer(Image.new("RGB", (W, H), o["c1"]), shrink(m_fr)))
        if sc.get("pill") and o["caption"]:
            _, x, y, w, h, _, _ = sc["pill"]
            fs = max(int(h * px * 0.55), 8)
            font = load_font(fs)
            d = ImageDraw.Draw(base)
            while fs > 8 and d.textlength(o["caption"], font=font) > w * px * 0.9:
                fs -= 2
                font = load_font(fs)
            txt = (255, 255, 255) if lum(o["c1"]) < 0.4 else (0, 0, 0)
            d.text(((x + w / 2) * px, (y + h / 2) * px), o["caption"], font=font, fill=txt, anchor="mm")
    if o["logo"] is not None and sc["box"]:
        x0, y0, x1, y1 = (v * px for v in sc["box"])
        side = max(int(round(x1 - x0)), 8)
        T = side * 4
        tile = Image.new("RGBA", (T, T), (0, 0, 0, 0))
        ImageDraw.Draw(tile).rounded_rectangle([0, 0, T - 1, T - 1], radius=T * 0.18, fill=(o["bg"] or (255, 255, 255)) + (255,))
        tile = tile.resize((side, side), Image.Resampling.LANCZOS)
        lg = ImageOps.contain(o["logo"], (int(side * 0.8), int(side * 0.8)), Image.Resampling.LANCZOS)
        tile.alpha_composite(lg, ((side - lg.width) // 2, (side - lg.height) // 2))
        ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ov.paste(tile, (round(x0), round(y0)))
        base = Image.alpha_composite(base, ov)
    return base if o["bg"] is None else base.convert("RGB")


# ---------------------------------------------------------------- QR SVG renderer
def f3(v):
    return f"{v:.3f}".rstrip("0").rstrip(".")


def svg_path(p):
    if p[0] == "c":
        _, cx, cy, r = p
        return f"M{f3(cx - r)} {f3(cy)}a{f3(r)} {f3(r)} 0 1 0 {f3(2 * r)} 0a{f3(r)} {f3(r)} 0 1 0 {f3(-2 * r)} 0Z"
    _, x, y, w, h, r, cs = p
    r = min(r, w / 2, h / 2)
    tl, tr, br, bl = (r if c else 0 for c in cs)

    def arc(rad, dx, dy):
        return f"a{f3(rad)} {f3(rad)} 0 0 1 {f3(dx)} {f3(dy)}" if rad > 0 else ""
    return (f"M{f3(x + tl)} {f3(y)}H{f3(x + w - tr)}{arc(tr, tr, tr)}V{f3(y + h - br)}{arc(br, -br, br)}"
            f"H{f3(x + bl)}{arc(bl, -bl, -bl)}V{f3(y + tl)}{arc(tl, tl, -tl)}Z")


def rgb_hex(c):
    return "#%02x%02x%02x" % tuple(c)


def render_svg(sc, o, size=None):
    W = size or o["size"]
    TW, TH = sc["TW"], sc["TH"]
    ox, oy, U = sc["ox"], sc["oy"], sc["U"]
    defs, fill = "", rgb_hex(o["c1"])
    if o["grad"] != "none" and o["c1"] != o["c2"]:
        stops = f'<stop offset="0" stop-color="{rgb_hex(o["c1"])}"/><stop offset="1" stop-color="{rgb_hex(o["c2"])}"/>'
        if o["grad"] == "radial":
            defs = (f'<radialGradient id="g" gradientUnits="userSpaceOnUse" cx="{f3(ox + U / 2)}" cy="{f3(oy + U / 2)}" '
                    f'r="{f3(U * 0.7071)}">{stops}</radialGradient>')
        else:
            x2, y2 = {"h": (ox + U, oy), "v": (ox, oy + U), "diag": (ox + U, oy + U)}[o["grad"]]
            defs = (f'<linearGradient id="g" gradientUnits="userSpaceOnUse" x1="{f3(ox)}" y1="{f3(oy)}" '
                    f'x2="{f3(x2)}" y2="{f3(y2)}">{stops}</linearGradient>')
        fill = "url(#g)"
    eye_fill = rgb_hex(o["eye_color"]) if o["eye_color"] else fill
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="{W}" '
           f'height="{round(W * TH / TW)}" viewBox="0 0 {f3(TW)} {f3(TH)}">']
    if defs:
        out.append(f"<defs>{defs}</defs>")
    if o["bg"]:
        out.append(f'<rect width="{f3(TW)}" height="{f3(TH)}" fill="{rgb_hex(o["bg"])}"/>')
    out.append(f'<path fill="{fill}" d="{"".join(svg_path(p) for p in sc["mods"])}"/>')
    for outer, hole, pupil in sc["eyes"]:
        out.append(f'<path fill="{eye_fill}" fill-rule="evenodd" d="{svg_path(outer)}{svg_path(hole)}"/>'
                   f'<path fill="{eye_fill}" d="{svg_path(pupil)}"/>')
    if o["frame"] != "none":
        c = rgb_hex(o["c1"])
        out.append(f'<path fill="{c}" fill-rule="evenodd" d="{svg_path(sc["frame_outer"])}{svg_path(sc["frame_hole"])}"/>')
        if sc.get("pill") and o["caption"]:
            _, x, y, w, h, _, _ = sc["pill"]
            txt = "#ffffff" if lum(o["c1"]) < 0.4 else "#000000"
            cap = o["caption"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            out.append(f'<path fill="{c}" d="{svg_path(sc["pill"])}"/>'
                       f'<text x="{f3(x + w / 2)}" y="{f3(y + h / 2)}" fill="{txt}" font-size="{f3(h * 0.55)}" '
                       f'font-family="Segoe UI, Arial, sans-serif" font-weight="700" text-anchor="middle" '
                       f'dominant-baseline="central">{cap}</text>')
    if o["logo"] is not None and sc["box"]:
        x0, y0, x1, y1 = sc["box"]
        L = x1 - x0
        bgc = rgb_hex(o["bg"] or (255, 255, 255))
        buf = io.BytesIO()
        o["logo"].save(buf, "PNG")
        out.append(f'<rect x="{f3(x0)}" y="{f3(y0)}" width="{f3(L)}" height="{f3(L)}" rx="{f3(L * 0.18)}" fill="{bgc}"/>'
                   f'<image x="{f3(x0 + L * 0.1)}" y="{f3(y0 + L * 0.1)}" width="{f3(L * 0.8)}" height="{f3(L * 0.8)}" '
                   f'preserveAspectRatio="xMidYMid meet" href="data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}"/>')
    out.append("</svg>")
    return "\n".join(out)


# ---------------------------------------------------------------- reading (OpenCV)
def _barcode_det():
    ctor = getattr(cv2, "barcode_BarcodeDetector", None) or getattr(getattr(cv2, "barcode", None), "BarcodeDetector", None)
    try:
        return ctor() if ctor else None
    except Exception:  # noqa
        return None


def decode_all(bgr):
    found, seen = [], set()

    def add(text, kind):
        if text and (kind, text) not in seen:
            seen.add((kind, text))
            found.append({"kind": kind, "text": text})

    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    variants = [bgr]
    if max(h, w) < 900:
        variants.append(cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC))
    if max(h, w) > 1600:
        s = 1400 / max(h, w)
        variants.append(cv2.resize(gray, None, fx=s, fy=s, interpolation=cv2.INTER_AREA))
    variants += [cv2.bitwise_not(gray), cv2.adaptiveThreshold(cv2.GaussianBlur(gray, (3, 3), 0), 255,
                 cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 41, 10)]
    dets = []
    for name in ("QRCodeDetectorAruco", "QRCodeDetector"):
        if hasattr(cv2, name):
            dets.append(getattr(cv2, name)())
    bd = _barcode_det()
    for v in variants:
        if zxingcpp is not None:
            try:
                for r in zxingcpp.read_barcodes(v):
                    nm = getattr(r.format, "name", str(r.format))
                    add(r.text, "QR" if "QR" in nm.upper() else nm)
            except Exception:  # noqa
                pass
            if found:
                break
        for det in dets:
            try:
                r = det.detectAndDecodeMulti(v)
                for t in (r[1] if r[0] else []):
                    add(t, "QR")
            except Exception:  # noqa
                pass
            if found:
                break
        if bd is not None and not found:
            try:
                r = bd.detectAndDecodeWithType(v)
                if r[0]:
                    for t, ty in zip(r[1], r[2]):
                        add(t, ty or "Barcode")
            except Exception:  # noqa
                pass
        if found:
            break
    return found


def describe(text):
    t = text.strip()
    low = t.lower()
    if low.startswith("wifi:"):
        def field(k):
            m = re.search(r"(?:^|;)" + k + r":((?:\\.|[^;])*)", t[5:])
            return re.sub(r"\\(.)", r"\1", m.group(1)) if m else ""
        return {"type": "WiFi", "info": {"Naam": field("S"), "Password": field("P"), "Security": field("T")}}
    if re.match(r"https?://", low):
        return {"type": "Link", "url": t}
    if low.startswith("mailto:"):
        return {"type": "Email", "url": t}
    if low.startswith("tel:"):
        return {"type": "Phone", "url": t}
    if low.startswith("begin:vcard"):
        info = {}
        for k, lab in (("FN", "Naam"), ("ORG", "Company"), ("TEL", "Phone"), ("EMAIL", "Email"), ("URL", "Website")):
            m = re.search(r"^" + k + r"[^:\n]*:(.+)$", t, re.M | re.I)
            if m:
                info[lab] = m.group(1).strip()
        return {"type": "Contact (vCard)", "info": info}
    if low.startswith("geo:"):
        return {"type": "Location", "url": "https://www.google.com/maps?q=" + t[4:].split("?")[0]}
    return {"type": "Text"}


# ---------------------------------------------------------------- barcode
def make_barcode(kind, text, fmt, scale, fg, bg, show_text):
    if kind not in BARCODES:
        raise Err("Ghalat barcode type.")
    if not text:
        raise Err("Barcode mein kya likhna hai?")
    mw = {"s": 0.25, "m": 0.4, "l": 0.6, "xl": 0.8}.get(scale, 0.4)
    opts = {"module_width": mw, "module_height": 15.0, "quiet_zone": 4.0, "font_size": 10, "text_distance": 5.0,
            "background": rgb_hex(bg), "foreground": rgb_hex(fg), "write_text": show_text}
    writer = SVGWriter() if fmt == "svg" else ImageWriter(format="PNG")
    try:
        bc = pybarcode.get_barcode_class(kind)(text, writer=writer)
        buf = io.BytesIO()
        bc.write(buf, options=opts)
    except pybarcode.errors.IllegalCharacterError:
        raise Err(f"'{text}' mein aise characters hain jo {BARCODES[kind].split(' (')[0]} mein nahi chalte.")
    except pybarcode.errors.NumberOfDigitsError:
        raise Err(f"Digits ki ginti {BARCODES[kind].split(' (')[0]} ke hisaab se sahi nahi. ({BARCODES[kind].split('(')[-1]}")
    except pybarcode.errors.BarcodeError as e:
        raise Err(f"Barcode nahi ban saka: {e}")
    except Exception as e:  # noqa
        raise Err(f"Barcode nahi ban saka: {e}")
    return buf.getvalue()


def barcode_form(form):
    return (form.get("bc_kind", "code128"), (form.get("bc_text") or "").strip(), form.get("bc_scale", "m"),
            hex_rgb(form.get("bc_fg"), (0, 0, 0)), hex_rgb(form.get("bc_bg"), (255, 255, 255)),
            form.get("bc_text_show", "on") == "on")


def png_to_pdf(img, dst):
    flat = Image.new("RGB", img.size, "white")
    flat.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[3])
    flat.save(dst, "PDF", resolution=300)


def selfcheck(img, data):
    """Try to read our own QR back (safety net; a real phone test before printing is still wise)."""
    try:
        rgb = img.convert("RGBA")
        flat = Image.new("RGB", rgb.size, "white")
        flat.paste(rgb, mask=rgb.split()[3])
        if flat.width > 700:
            flat = flat.resize((700, round(flat.height * 700 / flat.width)), Image.Resampling.LANCZOS)
        bgr = cv2.cvtColor(np.array(flat), cv2.COLOR_RGB2BGR)
        return any(r["text"].strip() == data.strip() for r in decode_all(bgr))
    except Exception:  # noqa
        return None


def warnings_for(o):
    w = []
    bg = o["bg"] or (255, 255, 255)
    cols = [o["c1"], o["c2"]] + ([o["eye_color"]] if o["eye_color"] else [])
    if min(contrast(c, bg) for c in cols) < 3:
        w.append("QR ke rang aur peeche ke rang mein contrast kam hai, scan mushkil ho sakta hai. Gehra rang + halka peeche rakho.")
    if all(lum(c) > lum(bg) for c in cols):
        w.append("Halka QR + gehra peeche (ulta) kai scanners nahi parhte. Seedha (gehra QR, halka peeche) rakho.")
    if o["style"] in ("dots",) and o["ec"] == "L":
        w.append("Dots style ke saath 'L' error correction kamzor hai, 'M' ya 'H' rakho.")
    return w


# ---------------------------------------------------------------- routes
@bp.route("/")
def page():
    return render_template("qr_barcode.html", folder=str(OUT), barcodes=BARCODES)


def _qr_from_request(preview=False):
    data, notes = build_payload(request.form)
    o = parse_opts(request.form, request.files.get("logo"))
    ec = "H" if o["logo"] is not None else o["ec"]
    mat, version = make_matrix(data, ec)
    sc = build_scene(mat, o)
    return data, notes, o, sc, version


@bp.route("/preview", methods=["POST"])
@api
def preview():
    data, notes, o, sc, version = _qr_from_request(True)
    img = render_raster(sc, o, 520)
    warns = notes + warnings_for(o)
    if o["logo"] is not None:
        warns.append("Logo ke saath error correction apne aap 'H' ho jata hai.")
    if len(data) > 300:
        warns.append("Data lamba hai, QR ghana ho jayega aur door se scan mushkil hoga.")
    return jsonify(img=data_url(img), version=version, scan_ok=selfcheck(img, data), warnings=warns, data=data)


@bp.route("/generate", methods=["POST"])
@api
def generate():
    data, notes, o, sc, version = _qr_from_request()
    fmts = set(request.form.getlist("fmt")) & {"png", "svg", "pdf"} or {"png"}
    f = request.form
    kind = f.get("kind", "text")
    auto = {"wifi": "wifi_" + (f.get("wifi_ssid") or ""), "vcard": "contact_" + (f.get("vc_name") or ""),
            "whatsapp": "whatsapp_" + re.sub(r"\D", "", f.get("num") or ""), "phone": "phone_" + re.sub(r"\D", "", f.get("num") or ""),
            "sms": "sms_" + re.sub(r"\D", "", f.get("num") or ""), "email": "email_" + (f.get("mail_to") or ""),
            "geo": "location"}.get(kind) or re.sub(r"^https?://", "", data.split("\n")[0])
    stem = safe_name(f.get("fname") or auto, "qr")
    img = render_raster(sc, o)
    files = []
    if "png" in fmts:
        p = unique_path(f"{stem}.png")
        img.save(p, "PNG", optimize=True)
        files.append(p)
    if "svg" in fmts:
        p = unique_path(f"{stem}.svg")
        p.write_text(render_svg(sc, o), encoding="utf-8")
        files.append(p)
    if "pdf" in fmts:
        p = unique_path(f"{stem}.pdf")
        png_to_pdf(img, p)
        files.append(p)
    return jsonify(ok=True, note=f"{len(files)} file(en) save ho gayin.", files=[{"name": p.name, "size": human(p.stat().st_size)} for p in files],
                   scan_ok=selfcheck(img, data), warnings=notes + warnings_for(o))


@bp.route("/barcode/preview", methods=["POST"])
@api
def barcode_preview():
    kind, text, scale, fg, bg, show = barcode_form(request.form)
    png = make_barcode(kind, text, "png", scale, fg, bg, show)
    img = Image.open(io.BytesIO(png))
    img.thumbnail((900, 500))
    try:
        got = decode_all(cv2.cvtColor(np.array(Image.open(io.BytesIO(png)).convert("RGB")), cv2.COLOR_RGB2BGR))
        want = text.lstrip("0")[:11]
        ok = any(r["text"] == text or r["text"].lstrip("0").startswith(want) for r in got)
    except Exception:  # noqa
        ok = None
    return jsonify(img=data_url(img), scan_ok=ok)


@bp.route("/barcode/generate", methods=["POST"])
@api
def barcode_generate():
    kind, text, scale, fg, bg, show = barcode_form(request.form)
    fmts = set(request.form.getlist("bc_fmt")) & {"png", "svg", "pdf"} or {"png"}
    stem = safe_name(f"{kind}_{text}", "barcode")
    files = []
    if fmts & {"png", "pdf"}:
        png = make_barcode(kind, text, "png", scale, fg, bg, show)
        if "png" in fmts:
            p = unique_path(f"{stem}.png")
            p.write_bytes(png)
            files.append(p)
        if "pdf" in fmts:
            p = unique_path(f"{stem}.pdf")
            png_to_pdf(Image.open(io.BytesIO(png)), p)
            files.append(p)
    if "svg" in fmts:
        p = unique_path(f"{stem}.svg")
        p.write_bytes(make_barcode(kind, text, "svg", scale, fg, bg, show))
        files.append(p)
    return jsonify(ok=True, note=f"{len(files)} file(en) save ho gayin.",
                   files=[{"name": p.name, "size": human(p.stat().st_size)} for p in files])


@bp.route("/decode", methods=["POST"])
@api
def decode():
    data = request.files["file"].read() if "file" in request.files else request.get_data()
    if not data:
        raise Err("Image nahi mili.")
    arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if arr is None:
        raise Err("Image khul nahi saki.")
    found = decode_all(arr)
    for f in found:
        f.update(describe(f["text"]))
    return jsonify(found=found)


@bp.route("/bulk", methods=["POST"])
@api
def bulk():
    mode = request.form.get("bulk_mode", "qr")
    raw = request.form.get("bulk_text", "")
    up = request.files.get("bulk_file")
    if up and up.filename:
        raw = up.read().decode("utf-8-sig", errors="replace")
    rows = [r for r in csv.reader(io.StringIO(raw)) if r and any(c.strip() for c in r)] if request.form.get("bulk_named") == "on" \
        else [[ln] for ln in raw.splitlines() if ln.strip()]
    if not rows:
        raise Err("List khaali hai. Har line mein ek value likho.")
    if len(rows) > MAX_BULK:
        raise Err(f"Ek baar mein {MAX_BULK} tak chalte hain ({len(rows)} diye).")
    fmt = "svg" if request.form.get("bulk_fmt") == "svg" else "png"
    stamp = time.strftime("%Y%m%d_%H%M%S")
    folder = OUT / f"Bulk_{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    o = parse_opts(request.form, request.files.get("logo")) if mode == "qr" else None
    bk = barcode_form(request.form)
    done, fails, previews = [], [], []
    width = len(str(len(rows)))
    for i, r in enumerate(rows, 1):
        named = request.form.get("bulk_named") == "on" and len(r) >= 2
        name, value = (r[0].strip(), ",".join(r[1:]).strip()) if named else ("", r[0].strip())
        label = f"{i:0{width}d}_{safe_name(name or value, 'item')}"
        try:
            if not value:
                raise Err("value khaali hai")
            if mode == "qr":
                oo = dict(o)
                ec = "H" if oo["logo"] is not None else oo["ec"]
                mat, _ = make_matrix(value, ec)
                sc = build_scene(mat, oo)
                if fmt == "svg":
                    (folder / f"{label}.svg").write_text(render_svg(sc, oo), encoding="utf-8")
                else:
                    img = render_raster(sc, oo)
                    img.save(folder / f"{label}.png", "PNG", optimize=True)
                    if len(previews) < 6:
                        t = img.copy()
                        t.thumbnail((150, 150))
                        previews.append({"name": label, "img": data_url(t)})
            else:
                data = make_barcode(bk[0], value, fmt, bk[2], bk[3], bk[4], bk[5])
                (folder / f"{label}.{fmt}").write_bytes(data)
                if fmt == "png" and len(previews) < 6:
                    t = Image.open(io.BytesIO(data))
                    t.thumbnail((200, 120))
                    previews.append({"name": label, "img": data_url(t)})
            done.append(label)
        except Err as e:
            fails.append(f"Line {i} ('{value[:30]}'): {e}")
        except Exception as e:  # noqa
            fails.append(f"Line {i} ('{value[:30]}'): {e}")
    if not done:
        raise Err(" | ".join(fails[:5]) or "Kuch nahi bana.")
    zp = unique_path(f"Bulk_{stamp}.zip")
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(folder.iterdir()):
            z.write(f, f.name)
    return jsonify(ok=True, note=f"{len(done)} ban gaye. Folder: Bulk_{stamp}", zip=zp.name, zip_size=human(zp.stat().st_size),
                   previews=previews, warnings=fails[:20])


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
