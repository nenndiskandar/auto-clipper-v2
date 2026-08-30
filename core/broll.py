"""
core/broll.py — Automated B-roll support (Pexels video search & download).

Adapted from opensource-clipping ``clipping/studio/broll.py``.

Dependencies:
    - ``pexels_api_key``: Pexels API key passed explicitly when calling
      :func:`download_pexels_broll` (config key: ``pexels_api_key``).

The AI highlight detector can produce a ``broll_list`` of desired B-roll
scenes (``["cuplikan gedung", "suasana kota malam", ...]``); each entry is
downloaded with a distinct query and later blended over specific time
ranges by the renderer.
"""

import json
import os
import random
import shutil
import urllib.parse
import urllib.request

import cv2

from utils.logger import debug_log

FIREFOX_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0"

# Pool of Pexels video IDs already used during this process,
# so repeated queries in one session don't return the same clip twice.
USED_PEXELS_IDS: set = set()


def _is_vertical_ratio(rasio: str) -> bool:
    """True bila rasio string adalah portrait/vertical (9:16, 4:5, 3:4)."""
    r = str(rasio or "9:16").lower()
    if "x" in r:
        w, h = r.split("x")
        try:
            return float(w) < float(h)
        except (ValueError, TypeError):
            return True
    try:
        w, h = r.split(":")
        return float(w) < float(h)
    except (ValueError, TypeError):
        return True


def resize_frame(img, size):
    """Resize an OpenCV BGR frame to ``(w, h)`` (interpolation-aware)."""
    w, h = size
    interpolation = cv2.INTER_AREA if (img.shape[1] > w or img.shape[0] > h) else cv2.INTER_LANCZOS4
    return cv2.resize(img, (w, h), interpolation=interpolation)


def download_pexels_broll(
    query: str,
    rasio: str,
    output_filename: str,
    pexels_api_key: str,
) -> bool:
    """
    Search and download one Pexels B-roll video clip matching the query
    and target aspect ratio.

    Args:
        query: Search query term (e.g. 'nature', 'technology').
        rasio: Target aspect ratio string ('9:16' portrait or '16:9' landscape).
        output_filename: Local file path where the MP4 will be saved.
        pexels_api_key: Valid Pexels API key for authorization.

    Returns:
        True if the video was downloaded and saved, otherwise False.
    """
    global USED_PEXELS_IDS

    if not pexels_api_key:
        debug_log("[B-roll] PEXELS_API_KEY tidak ditemukan. B-roll dilewati.")
        return False

    orientation = "portrait" if _is_vertical_ratio(rasio) else "landscape"

    params = urllib.parse.urlencode(
        {
            "query": query,
            "orientation": orientation,
            "per_page": 30,
            "size": "large",
            "resolution_name": "1080p",
        }
    )
    search_url = f"https://api.pexels.com/videos/search?{params}"

    req = urllib.request.Request(
        search_url,
        headers={"Authorization": pexels_api_key, "User-Agent": "Mozilla/5.0"},
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.load(response)
    except Exception as e:
        debug_log(f"[B-roll] Error API Pexels saat mencari '{query}': {e}")
        return False

    if not data.get("videos"):
        debug_log(f"[B-roll] Pexels tidak menemukan video untuk '{query}'.")
        return False

    available_videos = [v for v in data["videos"] if v["id"] not in USED_PEXELS_IDS]
    if not available_videos:
        debug_log(f"[B-roll] Pool untuk '{query}' habis, me-reset.")
        available_videos = data["videos"]

    video_data = random.choice(available_videos)
    USED_PEXELS_IDS.add(video_data["id"])

    video_files = [
        vf
        for vf in video_data.get("video_files", [])
        if vf.get("file_type") == "video/mp4"
    ]
    if not video_files:
        debug_log(f"[B-roll] Tidak ada file MP4 di dalam data video '{query}'.")
        return False

    video_files.sort(
        key=lambda vf: (
            vf.get("quality") != "hd",
            -(vf.get("width") or 0),
            -(vf.get("height") or 0),
        )
    )

    download_url = video_files[0]["link"]
    download_req = urllib.request.Request(download_url, headers={"User-Agent": FIREFOX_UA})

    try:
        temp_path = output_filename + ".part"
        with urllib.request.urlopen(download_req, timeout=120) as response, \
                open(temp_path, "wb") as f:
            shutil.copyfileobj(response, f)
        os.replace(temp_path, output_filename)
        debug_log(f"[B-roll] Berhasil: {output_filename}")
        return True
    except Exception as e:
        debug_log(f"[B-roll] Error saat mengunduh B-roll '{query}': {e}")
        return False


def crop_center_broll(img, target_w: int, target_h: int):
    """
    Center-crop an image frame to the exact target aspect ratio, then resize it.

    Args:
        img: Input image frame array (from OpenCV).
        target_w: Desired output width in pixels.
        target_h: Desired output height in pixels.

    Returns:
        The cropped and resized frame.
    """
    h, w = img.shape[:2]
    target_ratio = target_w / target_h
    img_ratio = w / h

    if img_ratio > target_ratio:
        new_w = int(h * target_ratio)
        x = (w - new_w) // 2
        img = img[:, x : x + new_w]
    elif img_ratio < target_ratio:
        new_h = int(w / target_ratio)
        y = (h - new_h) // 2
        img = img[y : y + new_h, :]

    return resize_frame(img, (target_w, target_h))


def download_broll_list(
    broll_list: list,
    rasio: str,
    output_dir: str,
    pexels_api_key: str,
) -> list[dict]:
    """
    Download B-roll clips for a list of queries.

    Args:
        broll_list: List of query strings (or dicts with ``query``).
        rasio: Target aspect ratio string.
        output_dir: Directory to save downloaded B-roll clips.
        pexels_api_key: Valid Pexels API key.

    Returns:
        List of dicts: ``{"query": ..., "filepath": ...}`` for successes only.
    """
    os.makedirs(output_dir, exist_ok=True)
    results = []
    for idx, item in enumerate(broll_list or []):
        query = item.get("query") if isinstance(item, dict) else item
        if not query:
            continue
        out_path = os.path.join(output_dir, f"broll_{idx:02d}.mp4")
        ok = download_pexels_broll(query, rasio, out_path, pexels_api_key)
        if ok and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            results.append({"query": query, "filepath": out_path})
    return results


def overlay_broll(
    input_video: str,
    broll_path: str,
    out_video: str,
    start: float = 0,
    end: float = 0,
    ffmpeg_path: str = "ffmpeg",
    opacity: float = 1.0,
    fade: float = 0.25,
) -> bool:
    """
    Overlay a single B-roll clip on top of the base video across ``[start, end]``.

    The B-roll is scaled + center-cropped to fill the base frame, faded in/out,
    and its audio (with adjustable mix volume) is blended under the base audio.

    Args:
        input_video: Base video file.
        broll_path: B-roll MP4 file.
        out_video: Output path.
        start/end: Overlay window (seconds) on the base-video timeline.
        ffmpeg_path: FFmpeg executable.
        opacity: Video overlay opacity 0-1 (1 = fully cover).
        fade: Fade duration in/out (seconds).

    Returns:
        True on success.
    """
    import subprocess as _sp
    import re as _re

    if not os.path.exists(broll_path) or not os.path.exists(input_video):
        debug_log("[B-roll] Input/broll tidak ditemukan untuk overlay.")
        return False

    end = max(end, start + 0.5)
    # probe base video dimensions
    out_w, out_h = 1080, 1920
    try:
        probe = _sp.run([ffmpeg_path, "-i", input_video, "-f", "null", "-"],
                        capture_output=True, text=True, timeout=60)
        m = _re.search(r"(\d{3,4})x(\d{3,4})", probe.stderr)
        if m:
            out_w, out_h = int(m.group(1)), int(m.group(2))
    except Exception as e:
        debug_log(f"[B-roll] probe gagal ({e}), pakai 1080x1920.")

    ovl = min(float(opacity) if opacity else 1.0, 1.0)
    fade_expr = ""
    if fade > 0:
        fade_expr = (
            f"*min(min(t/{fade},1),max(0,min(({end}-t)/{fade:.3f},1)))"
        )
    alpha = f"min({ovl}{fade_expr},1)" if fade > 0 else f"min({ovl},1)"

    fc = (
        f"[1:v]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
        f"crop={out_w}:{out_h},setsar=1,fade=t=in:st={start}:d={fade:.3f}:alpha=1,"
        f"fade=t=out:st={max(start, end - fade)}:d={fade:.3f}:alpha=1[b];"
        f"[0:v][b]overlay=0:0:enable='between(t,{start},{end})':format=auto[v]"
    )
    cmd = [
        ffmpeg_path, "-y",
        "-i", input_video,
        "-i", broll_path,
        "-filter_complex", fc,
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
    ]
    if start > 0:
        cmd += ["-ss", str(start), "-t", str(end - start)]
    cmd.append(out_video)
    try:
        _sp.run(cmd, check=True, capture_output=True, timeout=600)
        return os.path.exists(out_video) and os.path.getsize(out_video) > 10_000
    except _sp.CalledProcessError as e:
        debug_log(f"[B-roll] overlay gagal: {(e.stderr or b'')[-300:]}")
        return False


def apply_transition(
    input_video: str,
    transition_video: str,
    out_video: str,
    ffmpeg_path: str = "ffmpeg",
) -> bool:
    """
    Blend a fade-through transition over the first ~2s of the input video
    (functional hook for the transition library; keeps it simple & safe).

    Args:
        input_video: Base clip.
        transition_video: Prepared transition MP4 (or any short clip).
        out_video: Output path.
        ffmpeg_path: FFmpeg executable.

    Returns:
        True on success.
    """
    import subprocess as _sp
    import re as _re

    if not os.path.exists(input_video):
        return False
    if not os.path.exists(transition_video):
        # no asset -> passthrough
        import shutil
        shutil.copy(input_video, out_video)
        return True

    out_w, out_h = 1080, 1920
    try:
        probe = _sp.run([ffmpeg_path, "-i", input_video, "-f", "null", "-"],
                        capture_output=True, text=True, timeout=60)
        m = _re.search(r"(\d{3,4})x(\d{3,4})", probe.stderr)
        if m:
            out_w, out_h = int(m.group(1)), int(m.group(2))
    except Exception:
        pass

    # simple: crossfade the transition on top during the first 2 seconds
    fc = (
        f"[1:v]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
        f"crop={out_w}:{out_h},setsar=1,format=yuva420p[t];"
        f"[0:v][t]overlay=0:0:enable='between(t,0,2)':format=auto[v]"
    )
    cmd = [
        ffmpeg_path, "-y",
        "-i", input_video,
        "-i", transition_video,
        "-filter_complex", fc,
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
    ]
    cmd.append(out_video)
    try:
        _sp.run(cmd, check=True, capture_output=True, timeout=600)
        return os.path.exists(out_video) and os.path.getsize(out_video) > 10_000
    except _sp.CalledProcessError as e:
        debug_log(f"[Transition] aplikasi gagal: {(e.stderr or b'')[-300:]}")
        return False