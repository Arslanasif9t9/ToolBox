"""Tool 9: Audio Tools (ffmpeg). Extract audio from video, convert, cut, join, plus small effects. Offline.

Uses the system ffmpeg if present, else the one bundled by imageio-ffmpeg (same as Tool 1).
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from functools import wraps
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request, send_from_directory

META = {"order": 9, "name": "Audio Tools", "slug": "audio", "icon": "🎧",
        "desc": "Video se audio nikalo, MP3 wagaira convert, cut aur join, awaaz theek karo. Offline."}

bp = Blueprint("audio", __name__, url_prefix="/audio")

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "Output" / "AudioTools"
IS_WIN = sys.platform.startswith("win")
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MAX_JOIN = 50
FORMATS = {
    "mp3": {"ext": "mp3", "codec": ["-c:a", "libmp3lame"], "lossy": True},
    "m4a": {"ext": "m4a", "codec": ["-c:a", "aac"], "lossy": True},
    "ogg": {"ext": "ogg", "codec": ["-c:a", "libvorbis"], "lossy": True},
    "wav": {"ext": "wav", "codec": ["-c:a", "pcm_s16le"], "lossy": False},
    "flac": {"ext": "flac", "codec": ["-c:a", "flac"], "lossy": False},
}
BITRATES = {"128", "192", "256", "320"}


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


def ffmpeg_path():
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa
        return ""


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def stem_of(name):
    s = re.sub(r"[^\w\-. ]+", "_", Path(name or "audio").stem)[:80].strip()
    return s or "audio"


def unique_path(name):
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / name
    stem, suf, i = p.stem, p.suffix, 1
    while p.exists():
        p = OUT / f"{stem} ({i}){suf}"
        i += 1
    return p


def fmt_time(sec):
    sec = int(round(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def parse_time(s, label):
    s = (s or "").strip().replace(",", ".")
    if not s:
        return None
    try:
        parts = [float(x) for x in s.split(":")]
    except ValueError:
        raise Err(f"{label} ka time samajh nahi aaya ('{s}'). Aise likho: 90 ya 1:30 ya 0:01:30.5")
    if len(parts) > 3 or any(p < 0 for p in parts):
        raise Err(f"{label} ka time ghalat hai ('{s}').")
    t = 0.0
    for p in parts:
        t = t * 60 + p
    return t


def run_ff(args, timeout=7200):
    ff = ffmpeg_path()
    if not ff:
        raise Err("ffmpeg nahi mila. `.venv\\Scripts\\pip install -r requirements.txt` chalao.")
    try:
        r = subprocess.run([ff, "-hide_banner", "-nostdin", "-y", *args], capture_output=True, timeout=timeout,
                           creationflags=NOWIN)
    except subprocess.TimeoutExpired:
        raise Err("Bohat der lag gayi, rok diya. Chhoti file try karo.")
    err = r.stderr.decode("utf-8", "replace")
    if r.returncode != 0:
        low = err.lower()
        if "does not contain any stream" in low or "matches no streams" in low or "stream specifier" in low and "matches no" in low:
            raise Err("Is file mein audio nahi hai.")
        if "unknown encoder" in low or "encoder not found" in low:
            raise Err("Is format ka encoder ffmpeg mein nahi hai. MP3 ya WAV chuno.")
        if "invalid data found" in low or "moov atom not found" in low:
            raise Err("File kharab hai ya ye type nahi chalti.")
        tail = " ".join(err.strip().splitlines()[-2:])[:300]
        raise Err(f"ffmpeg error: {tail}")
    return err


def probe_duration(path):
    ff = ffmpeg_path()
    r = subprocess.run([ff, "-hide_banner", "-nostdin", "-i", str(path)], capture_output=True, creationflags=NOWIN, timeout=60)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr.decode("utf-8", "replace"))
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else None


def has_audio(path):
    r = subprocess.run([ffmpeg_path(), "-hide_banner", "-nostdin", "-i", str(path)], capture_output=True,
                       creationflags=NOWIN, timeout=60)
    return "Audio:" in r.stderr.decode("utf-8", "replace")


def save_uploads(files, tmp):
    paths = []
    for i, f in enumerate(files):
        ext = Path(f.filename).suffix.lower()[:8] or ".bin"
        p = Path(tmp) / f"in{i}{re.sub(r'[^.\w]', '', ext)}"
        f.save(p)
        paths.append((p, f.filename))
    return paths


def out_args(form):
    """Codec + bitrate + channel/rate args from the form."""
    fmt = form.get("fmt", "mp3")
    if fmt not in FORMATS:
        fmt = "mp3"
    a = list(FORMATS[fmt]["codec"])
    if FORMATS[fmt]["lossy"]:
        br = form.get("bitrate", "192")
        a += ["-b:a", f"{br if br in BITRATES else '192'}k"]
    ch = form.get("channels", "")
    if ch in ("1", "2"):
        a += ["-ac", ch]
    sr = form.get("rate", "")
    if sr in ("22050", "44100", "48000"):
        a += ["-ar", sr]
    if fmt == "mp3":
        a += ["-id3v2_version", "3"]
    return fmt, a


def tempo_chain(speed):
    parts = []
    while speed > 2.0:
        parts.append("atempo=2.0")
        speed /= 2.0
    while speed < 0.5:
        parts.append("atempo=0.5")
        speed /= 0.5
    parts.append(f"atempo={speed:.4f}")
    return parts


def fx_filters(form):
    """Effect filters (without fade-out, which needs the final length). Returns (filters, fade_in, fade_out, notes)."""
    f = []
    notes = []
    if form.get("silence") == "on":
        # trim long pauses + start; the reverse pass trims the tail exactly (plain stop_periods leaves ~1 s of it)
        f.append("silenceremove=start_periods=1:start_threshold=-45dB:stop_periods=-1:stop_duration=1:stop_threshold=-45dB"
                 ":stop_silence=0.3,areverse,silenceremove=start_periods=1:start_threshold=-45dB,areverse")
        notes.append("silence hatai")
    try:
        speed = float(form.get("speed", "1") or 1)
    except ValueError:
        speed = 1.0
    if not 0.25 <= speed <= 4:
        raise Err("Speed 0.25 se 4 ke beech rakho.")
    if abs(speed - 1) > 0.001:
        f += tempo_chain(speed)
        notes.append(f"speed {speed:g}x")
    try:
        gain = float(form.get("gain", "0") or 0)
    except ValueError:
        gain = 0.0
    if not -30 <= gain <= 30:
        raise Err("Awaaz (dB) -30 se +30 ke beech rakho.")
    if abs(gain) > 0.01:
        f.append(f"volume={gain:g}dB")
        notes.append(f"{gain:+g} dB")
    if form.get("normalize") == "on":
        f.append("loudnorm=I=-16:TP=-1.5:LRA=11,aresample=44100")  # loudnorm upsamples to 192 kHz otherwise
        notes.append("normalize")
    def secs(name):
        try:
            v = float(form.get(name, "0") or 0)
        except ValueError:
            v = 0.0
        return min(max(v, 0.0), 60.0)
    fi, fo = secs("fade_in"), secs("fade_out")
    if fi:
        f.append(f"afade=t=in:d={fi:g}")
        notes.append(f"fade-in {fi:g}s")
    if fo:
        notes.append(f"fade-out {fo:g}s")
    return f, fo, notes


def encode(src, dst, fmt_args, filters=None, fade_out=0.0, pre=None, complex_graph=None, map_out=None):
    """Encode src -> dst. Handles the 2-step fade-out (needs final length)."""
    base = ["-vn", "-map_metadata", "0"]
    filt = list(filters or [])
    if fade_out > 0:
        with tempfile.TemporaryDirectory(prefix="aud_") as t2:
            mid = Path(t2) / "mid.wav"
            run_ff([*(pre or []), "-i", str(src), *base, *(["-af", ",".join(filt)] if filt else []), "-c:a", "pcm_s16le", str(mid)]
                   if not complex_graph else [*(pre or []), "-i", str(src), "-filter_complex", complex_graph, "-map", map_out, "-c:a", "pcm_s16le", str(mid)])
            d = probe_duration(mid) or 0
            st = max(d - fade_out, 0)
            run_ff(["-i", str(mid), *base, "-af", f"afade=t=out:st={st:.3f}:d={min(fade_out, d):.3f}", *fmt_args, str(dst)])
        return
    if complex_graph:
        run_ff([*(pre or []), "-i", str(src), "-filter_complex", complex_graph, "-map", map_out, *fmt_args, str(dst)])
    else:
        run_ff([*(pre or []), "-i", str(src), *base, *(["-af", ",".join(filt)] if filt else []), *fmt_args, str(dst)])


def result(files, notes=None, warnings=None, t0=None):
    return jsonify(ok=True, note=f"{len(files)} file(en) tayyar" + (f" ({time.time() - t0:.1f} second)." if t0 else "."),
                   files=[{"name": p.name, "size": human(p.stat().st_size), "dur": fmt_time(probe_duration(p) or 0)} for p in files],
                   details=notes or [], warnings=warnings or [])


# ---------------------------------------------------------------- routes
@bp.route("/")
def page():
    return render_template("audio_tools.html", folder=str(OUT), ffmpeg_ok=bool(ffmpeg_path()))


@bp.route("/convert", methods=["POST"])
@api
def convert():
    files = [f for f in request.files.getlist("files") if f.filename]
    if not files:
        raise Err("Pehle video ya audio file chuno.")
    fmt, fargs = out_args(request.form)
    filters, fade_out, notes = fx_filters(request.form)
    outs, fails, details = [], [], []
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="aud_") as tmp:
        for p, label in save_uploads(files, tmp):
            try:
                if not has_audio(p):
                    raise Err("Is file mein audio nahi hai." if probe_duration(p) is not None else "File khul nahi saki ya ye type nahi chalti.")
                dst = unique_path(f"{stem_of(label)}.{FORMATS[fmt]['ext']}")
                encode(p, dst, fargs, filters, fade_out)
                outs.append(dst)
                details.append(f"{label} → {dst.name}" + (f" ({', '.join(notes)})" if notes else ""))
            except Err as e:
                fails.append(f"'{label}': {e}")
            except Exception as e:  # noqa
                fails.append(f"'{label}': {e}")
    if not outs:
        raise Err(" | ".join(fails) or "Kuch nahi bana.")
    return result(outs, details, fails, t0)


@bp.route("/cut", methods=["POST"])
@api
def cut():
    f = request.files.get("file")
    if not f or not f.filename:
        raise Err("Pehle file chuno.")
    start = parse_time(request.form.get("start"), "Start")
    end = parse_time(request.form.get("end"), "End")
    if start is None and end is None:
        raise Err("Start ya End mein se kam az kam ek time likho.")
    start = start or 0.0
    if end is not None and end <= start:
        raise Err("End time Start se bara hona chahiye.")
    mode = request.form.get("mode", "keep")
    fmt, fargs = out_args(request.form)
    filters, fade_out, notes = fx_filters(request.form)
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="aud_") as tmp:
        (p, label), = save_uploads([f], tmp)
        if not has_audio(p):
            raise Err("Is file mein audio nahi hai." if probe_duration(p) is not None else "File khul nahi saki ya ye type nahi chalti.")
        dur = probe_duration(p)
        if dur and start >= dur:
            raise Err(f"Start time ({fmt_time(start)}) file ki length ({fmt_time(dur)}) se zyada hai.")
        warn = []
        if dur and end and end > dur:
            warn.append(f"End time file ki length ({fmt_time(dur)}) se zyada tha, aakhir tak kaata.")
            end = None
        dst = unique_path(f"{stem_of(label)}_{'cut' if mode == 'keep' else 'removed'}.{FORMATS[fmt]['ext']}")
        if mode == "keep":
            pre = []
            sel = [f"atrim=start={start:.3f}" + (f":end={end:.3f}" if end else ""), "asetpts=PTS-STARTPTS"]
            encode(p, dst, fargs, sel + filters, fade_out, pre)
            note = f"{fmt_time(start)} se {fmt_time(end) if end else 'aakhir'} tak ka hissa rakha."
        else:
            if start <= 0 and not end:
                raise Err("Poori file hatani hai? Start ya End sahi likho.")
            if start <= 0:
                graph = f"[0:a]atrim=start={end:.3f},asetpts=PTS-STARTPTS" + (("," + ",".join(filters)) if filters else "") + "[out]"
            elif not end:
                graph = f"[0:a]atrim=end={start:.3f},asetpts=PTS-STARTPTS" + (("," + ",".join(filters)) if filters else "") + "[out]"
            else:
                graph = (f"[0:a]atrim=end={start:.3f},asetpts=PTS-STARTPTS[a];[0:a]atrim=start={end:.3f},asetpts=PTS-STARTPTS[b];"
                         f"[a][b]concat=n=2:v=0:a=1" + (("," + ",".join(filters)) if filters else "") + "[out]")
            encode(p, dst, fargs, None, fade_out, None, complex_graph=graph, map_out="[out]")
            note = f"{fmt_time(start)} se {fmt_time(end) if end else 'aakhir'} tak ka hissa hata diya."
    return result([dst], [note], warn, t0)


@bp.route("/join", methods=["POST"])
@api
def join():
    files = [f for f in request.files.getlist("files") if f.filename]
    if len(files) < 2:
        raise Err("Jodne ke liye kam az kam 2 files chuno.")
    if len(files) > MAX_JOIN:
        raise Err(f"Ek baar mein {MAX_JOIN} files tak jod sakte ho.")
    fmt, fargs = out_args(request.form)
    filters, fade_out, notes = fx_filters(request.form)
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="aud_") as tmp:
        ups = save_uploads(files, tmp)
        for p, label in ups:
            if not has_audio(p):
                raise Err(f"'{label}' mein audio nahi hai ya file khul nahi saki.")
        n = len(ups)
        norm = "".join(f"[{i}:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}];" for i in range(n))
        graph = norm + "".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1" + (("," + ",".join(filters)) if filters else "") + "[out]"
        dst = unique_path(f"{stem_of(ups[0][1])}_joined.{FORMATS[fmt]['ext']}")
        inputs = []
        for p, _ in ups:
            inputs += ["-i", str(p)]
        # encode() takes one -i; build the multi-input call directly
        tail = ["-filter_complex", graph, "-map", "[out]", "-map_metadata", "-1"]
        if fade_out > 0:
            mid = Path(tmp) / "mid.wav"
            run_ff([*inputs, *tail, "-c:a", "pcm_s16le", str(mid)])
            d = probe_duration(mid) or 0
            run_ff(["-i", str(mid), "-af", f"afade=t=out:st={max(d - fade_out, 0):.3f}:d={min(fade_out, d):.3f}", *fargs, str(dst)])
        else:
            run_ff([*inputs, *tail, *fargs, str(dst)])
    return result([dst], [f"{n} files jodi gayin: " + " + ".join(l for _, l in ups)[:200]] + ([", ".join(notes)] if notes else []), None, t0)


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
