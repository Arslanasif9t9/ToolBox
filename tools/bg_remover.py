"""Tool 6: Background Remover. Offline after the one-time model download. No API key.

Runs the U2-Net / ISNet ONNX models with OpenCV's DNN module (no onnxruntime / rembg needed, so it also
works on very new Python versions). Models are downloaded once into <ToolBox>/models/ from the rembg
GitHub release and are not committed to git.
"""
import io
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from contextlib import suppress
from functools import wraps
from pathlib import Path

import cv2
import numpy as np
from flask import Blueprint, jsonify, render_template, request, send_from_directory
from PIL import Image, ImageOps

META = {"order": 6, "name": "Background Remover", "slug": "bgremove", "icon": "✂️",
        "desc": "Photo ka background hatao: transparent, rang ya doosri photo. Offline."}

bp = Blueprint("bgremove", __name__, url_prefix="/bgremove")

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "Output" / "BackgroundRemover"
MODELS_DIR = ROOT / "models"
IS_WIN = sys.platform.startswith("win")
MODEL_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/{}.onnx"
MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)
EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
MAX_SIDE = 5000

MODELS = {
    "u2netp": {"label": "Tez aur chhota", "size": 4574861, "human": "4.6 MB", "input": 320, "mean": MEAN, "std": STD,
               "note": "Jaldi test ke liye. Kinare thode kharab ho sakte hain."},
    "u2net_human_seg": {"label": "Insaan / portrait", "size": 175997641, "human": "176 MB", "input": 320,
                        "mean": MEAN, "std": STD, "note": "Passport photo aur selfie ke liye behtar."},
    "u2net": {"label": "Har cheez (general)", "size": 175997641, "human": "176 MB", "input": 320, "mean": MEAN,
              "std": STD, "note": "Product, janwar, insaan, sab."},
    "isnet-general-use": {"label": "Sabse behtar kinare", "size": 178648008, "human": "178 MB", "input": 1024,
                          "mean": np.array([0.5, 0.5, 0.5], np.float32), "std": np.array([1, 1, 1], np.float32),
                          "note": "Sabse saaf kinare (jootay, products). Thora sust."},
}

NETS = {}
NET_LOCK = threading.Lock()
DL = {}  # model name -> {"status","done","total","message"}
DL_LOCK = threading.Lock()


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


def model_path(name):
    return MODELS_DIR / f"{name}.onnx"


def model_ready(name):
    p = model_path(name)
    return p.exists() and p.stat().st_size == MODELS[name]["size"]


def hex_rgb(h, default=(255, 255, 255)):
    h = (h or "").strip().lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{6}", h):
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    return default


# ---------------------------------------------------------------- model download (resumable)
def _download(name):
    info = MODELS[name]
    dst, part = model_path(name), model_path(name).with_suffix(".onnx.part")
    st = DL[name]
    try:
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        last_err = ""
        for attempt in range(1, 7):
            try:
                have = part.stat().st_size if part.exists() else 0
                if have > info["size"]:
                    part.unlink()
                    have = 0
                if have < info["size"]:
                    headers = {"User-Agent": "ArslanToolbox/1.0"}
                    if have:
                        headers["Range"] = f"bytes={have}-"
                    with urllib.request.urlopen(urllib.request.Request(MODEL_URL.format(name), headers=headers),
                                                timeout=30) as r:
                        resume = bool(have) and r.status == 206
                        got = have if resume else 0
                        with open(part, "ab" if resume else "wb") as fh:
                            while True:
                                chunk = r.read(1 << 16)
                                if not chunk:
                                    break
                                fh.write(chunk)
                                got += len(chunk)
                                st.update(done=got, message="Download ho raha hai...")
                if part.stat().st_size == info["size"]:
                    break
                last_err = f"size match nahi hua ({part.stat().st_size} / {info['size']})"
            except Exception as e:  # noqa
                last_err = str(e)
            st["message"] = f"Rabta toota, dobara koshish {attempt}/6... ({last_err[:80]})"
            time.sleep(2)
        else:
            raise Err(f"Download poora nahi hua: {last_err}")
        os.replace(part, dst)
        cv2.dnn.readNetFromONNX(str(dst))  # make sure the file is a loadable model
        st.update(status="done", done=info["size"], message="Taiyar!")
    except Exception as e:  # noqa
        with suppress(Exception):
            if dst.exists() and not model_ready(name):
                dst.unlink()
        st.update(status="error", message=str(e)[:300])


def start_download(name):
    with DL_LOCK:
        cur = DL.get(name)
        if cur and cur["status"] == "running":
            return
        DL[name] = {"status": "running", "done": 0, "total": MODELS[name]["size"], "message": "Shuru ho raha hai..."}
    threading.Thread(target=_download, args=(name,), daemon=True).start()


def models_state():
    out = []
    for name, m in MODELS.items():
        st = DL.get(name, {})
        out.append({"name": name, "label": m["label"], "human": m["human"], "note": m["note"],
                    "ready": model_ready(name), "status": st.get("status", ""), "message": st.get("message", ""),
                    "percent": round(st.get("done", 0) * 100 / m["size"], 1) if st.get("status") == "running" else 0})
    return out


# ---------------------------------------------------------------- inference
def get_net(name):
    if name not in NETS:
        NETS.clear()  # keep one model in memory at a time
        NETS[name] = cv2.dnn.readNetFromONNX(str(model_path(name)))
    return NETS[name]


def predict_mask(name, rgb):
    info = MODELS[name]
    s = info["input"]
    x = cv2.resize(rgb, (s, s), interpolation=cv2.INTER_AREA).astype(np.float32)
    x /= max(float(x.max()), 1e-6)
    x = (x - info["mean"]) / info["std"]
    blob = np.ascontiguousarray(x.transpose(2, 0, 1)[None])
    with NET_LOCK:
        net = get_net(name)
        net.setInput(blob)
        out = net.forward()
    m = out.squeeze().astype(np.float32)
    return (m - m.min()) / max(float(m.max() - m.min()), 1e-6)


def make_alpha(m, size, edge):
    a = np.clip(cv2.resize(m, size, interpolation=cv2.INTER_CUBIC), 0, 1)
    if edge == "hard":
        a = cv2.GaussianBlur((a > 0.5).astype(np.float32), (0, 0), 1.0)
    elif edge == "soft":
        a = cv2.GaussianBlur(a, (0, 0), 2.0)
    return a


def load_rgb(name, data):
    if Path(name).suffix.lower() not in EXTS:
        raise Err(f"ye type nahi chalti ({Path(name).suffix or 'koi nahi'}). Chalti hain: JPG, PNG, WebP, BMP, TIFF.")
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        raise Err("image khul nahi saki (kharab hai ya supported nahi).")
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info:
        im = im.convert("RGBA")
        flat = Image.new("RGB", im.size, "white")
        flat.paste(im, mask=im.split()[3])
        im = flat
    return im.convert("RGB")


# ---------------------------------------------------------------- routes
@bp.route("/")
def page():
    return render_template("bg_remover.html", folder=str(OUT), models_dir=str(MODELS_DIR))


@bp.route("/models")
def models():
    return jsonify(models=models_state())


@bp.route("/models/download", methods=["POST"])
@api
def models_download():
    name = (request.get_json(silent=True) or {}).get("name", "")
    if name not in MODELS:
        raise Err("Ghalat model.")
    if not model_ready(name):
        start_download(name)
    return jsonify(models=models_state())


@bp.route("/remove", methods=["POST"])
@api
def remove():
    name = request.form.get("model", "")
    if name not in MODELS:
        raise Err("Model chuno.")
    if not model_ready(name):
        raise Err("Ye model abhi download nahi hua. Upar iske saamne 'Download' dabao.")
    files = [f for f in request.files.getlist("files") if f.filename]
    if not files:
        raise Err("Pehle image(en) chuno.")
    edge = request.form.get("edge", "natural")
    bg = request.form.get("bg", "transparent")
    color = hex_rgb(request.form.get("color"))
    crop = request.form.get("crop") == "on"
    fmt = request.form.get("fmt", "png")
    bg_img = None
    if bg == "image":
        up = request.files.get("bg_file")
        if not up or not up.filename:
            raise Err("Peeche lagane ki photo chuno.")
        try:
            bg_img = ImageOps.exif_transpose(Image.open(up)).convert("RGB")
        except Exception:
            raise Err("Peeche wali photo khul nahi saki.")
    outs, fails, details = [], [], []
    t0 = time.time()
    for f in files:
        label = f.filename
        try:
            rgb_im = load_rgb(label, f.read())
            note = ""
            if max(rgb_im.size) > MAX_SIDE:
                rgb_im.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
                note = f" (bohat badi thi, {rgb_im.width}×{rgb_im.height} px par chhoti ki)"
            rgb = np.array(rgb_im)
            alpha = make_alpha(predict_mask(name, rgb), rgb_im.size, edge)
            cut = Image.fromarray(np.dstack([rgb, (alpha * 255).astype(np.uint8)]), "RGBA")
            if crop:
                box = cut.getchannel("A").point(lambda v: 255 if v > 20 else 0).getbbox()
                if box:
                    pad = round(0.02 * max(cut.size))
                    cut = cut.crop((max(0, box[0] - pad), max(0, box[1] - pad),
                                    min(cut.width, box[2] + pad), min(cut.height, box[3] + pad)))
            stem = stem_of(label)
            if bg == "transparent":
                ext = "webp" if fmt == "webp" else "png"
                dst = unique_path(f"{stem}_nobg.{ext}")
                cut.save(dst, "WEBP", quality=95) if ext == "webp" else cut.save(dst, "PNG", optimize=True)
            else:
                base = Image.new("RGB", cut.size, color) if bg == "color" else ImageOps.fit(
                    bg_img, cut.size, Image.Resampling.LANCZOS)
                base.paste(cut, mask=cut.getchannel("A"))
                ext = {"jpg": "jpg", "webp": "webp"}.get(fmt, "png")
                dst = unique_path(f"{stem}_bg.{ext}")
                if ext == "jpg":
                    base.save(dst, "JPEG", quality=95)
                elif ext == "webp":
                    base.save(dst, "WEBP", quality=95)
                else:
                    base.save(dst, "PNG", optimize=True)
            outs.append((dst, label))
            details.append(f"{label} → {dst.name}{note}")
        except Err as e:
            fails.append(f"'{label}': {e}")
        except Exception as e:  # noqa
            fails.append(f"'{label}': {e}")
    if not outs:
        raise Err(" | ".join(fails) or "Kuch nahi bana.")
    return jsonify(ok=True, note=f"{len(outs)} image(en) tayyar ({time.time() - t0:.1f} second).",
                   files=[{"name": p.name, "size": human(p.stat().st_size), "source": s} for p, s in outs],
                   warnings=fails, details=details)


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
