"""Tool 1: Video / Audio Downloader (yt-dlp + ffmpeg). No API key, no AI."""
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

META = {"order": 1, "name": "Video/Audio Downloader", "slug": "downloader", "icon": "⬇️",
        "desc": "URL paste karo, video ya audio download karo. Video+audio khud merge."}

bp = Blueprint("downloader", __name__, url_prefix="/downloader")

# <ToolBox folder>/Output/VideoDownloader  (e.g. D:\ToolBox\Output\VideoDownloader)
DOWNLOAD_DIR = Path(__file__).resolve().parent.parent / "Output" / "VideoDownloader"
ANSI = re.compile(r"\x1b\[[0-9;]*m|\[[0-9;]*m")
JOBS = {}  # job_id -> dict


def ffmpeg_path():
    """System ffmpeg if present, else the one bundled in imageio-ffmpeg."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def build_opts(mode, quality, job, cookies=""):
    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes", 0)
            job["percent"] = round(done * 100 / total, 1) if total else job.get("percent", 0)
            job["speed"] = d.get("_speed_str", "").strip()
            job["eta"] = d.get("_eta_str", "").strip()
            job["message"] = "Download ho raha hai..."
        elif d["status"] == "finished":
            job["message"] = "Merge / convert ho raha hai..."
            job["percent"] = 99

    opts = {
        "outtmpl": str(DOWNLOAD_DIR / "%(title).150B [%(id)s].%(ext)s"),
        "progress_hooks": [hook],
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": False,
        "windowsfilenames": True,
        "color": {"stdout": "no_color", "stderr": "no_color"},
    }
    if cookies in ("firefox", "chrome", "edge"):
        opts["cookiesfrombrowser"] = (cookies,)
    ff = ffmpeg_path()
    if ff:
        opts["ffmpeg_location"] = ff

    if mode == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        h = {"best": None, "1080": 1080, "720": 720, "480": 480, "360": 360}.get(quality)
        if h:
            opts["format"] = f"bv*[height<={h}]+ba/b[height<={h}]/b"
        else:
            opts["format"] = "bv*+ba/b"
        opts["merge_output_format"] = "mp4"
    return opts


def run_job(job_id, url, mode, quality, playlist, cookies=""):
    job = JOBS[job_id]
    try:
        import yt_dlp
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        opts = build_opts(mode, quality, job, cookies)
        if playlist:
            opts["noplaylist"] = False
            opts["outtmpl"] = str(DOWNLOAD_DIR / "%(playlist_title).80B" / "%(playlist_index)s - %(title).120B.%(ext)s")
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            job["title"] = (info or {}).get("title", "")
        job.update(status="done", percent=100, message="Ho gaya! File Output/VideoDownloader folder mein hai.")
    except Exception as e:  # show a readable error to the user
        msg = ANSI.sub("", str(e)).replace("ERROR: ", "")
        job.update(status="error", message=msg[:400])


@bp.route("/")
def page():
    return render_template("downloader.html", ffmpeg_ok=bool(ffmpeg_path()), folder=str(DOWNLOAD_DIR))


@bp.route("/start", methods=["POST"])
def start():
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify(error="Sahi URL daalo (http/https se shuru)."), 400
    mode = data.get("mode", "video")
    quality = data.get("quality", "best")
    playlist = bool(data.get("playlist"))
    cookies = data.get("cookies", "")
    job_id = uuid.uuid4().hex[:8]
    JOBS[job_id] = {"status": "running", "percent": 0, "message": "Shuru ho raha hai...", "speed": "", "eta": ""}
    threading.Thread(target=run_job, args=(job_id, url, mode, quality, playlist, cookies), daemon=True).start()
    return jsonify(job_id=job_id)


@bp.route("/status/<job_id>")
def status(job_id):
    return jsonify(JOBS.get(job_id, {"status": "error", "message": "Job nahi mila."}))


@bp.route("/open-folder", methods=["POST"])
def open_folder():
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("win"):
        os.startfile(DOWNLOAD_DIR)  # noqa
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(DOWNLOAD_DIR)])
    else:
        subprocess.Popen(["xdg-open", str(DOWNLOAD_DIR)])
    return jsonify(ok=True)


@bp.route("/update-ytdlp", methods=["POST"])
def update_ytdlp():
    """Sites change often; this upgrades yt-dlp in place."""
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp"],
                       capture_output=True, text=True)
    return jsonify(ok=r.returncode == 0, output=(r.stdout + r.stderr)[-400:])
