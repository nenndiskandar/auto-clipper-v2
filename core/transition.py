"""
core/transition.py — Transition Asset Downloader & Manager.

Downloads and caches transition overlay videos (film burn, light leak,
film grain, film leader) from YouTube sources (Think Make Push channel),
then extracts a usable portion as a cropped/scaled segment ready for
overlay/concatenation.

Adapted from opensource-clipping ``clipping/studio/transitions.py``.
"""

import os
import random
import subprocess

from utils.logger import debug_log
from utils.helpers import get_ffmpeg_path, get_app_dir


# Transitions pool — YouTube sources with skip offsets (branding intro).
# Each entry: url / skip / duration / type / orientation / label.
TMP_TRANSITION_POOL = [
    {
        "url": "https://www.youtube.com/watch?v=yfKv03nLaBE",
        "skip": 5, "duration": None, "type": "film_burn",
        "orientation": "landscape", "label": "GRUNGY Film Burn Transitions",
    },
    {
        "url": "https://www.youtube.com/watch?v=uYBcUpLxtEM",
        "skip": 5, "duration": None, "type": "film_burn",
        "orientation": "landscape", "label": "GRUNGE Film Overlay with Sound",
    },
    {
        "url": "https://www.youtube.com/watch?v=YFzGx0JuUUQ",
        "skip": 5, "duration": None, "type": "film_overlay",
        "orientation": "landscape", "label": "35mm Film Overlay",
    },
    {
        "url": "https://www.youtube.com/watch?v=iGvnBXS3pyM",
        "skip": 5, "duration": None, "type": "film_leader",
        "orientation": "landscape", "label": "Dirty Grainy Film Leader",
    },
    {
        "url": "https://www.youtube.com/watch?v=BsKj9iiimTE",
        "skip": 5, "duration": None, "type": "film_leader",
        "orientation": "landscape", "label": "Classic Film Leader Overlays",
    },
    {
        "url": "https://www.youtube.com/watch?v=OaK3jjBfOi0",
        "skip": 5, "duration": None, "type": "film_grain",
        "orientation": "landscape", "label": "Film Grain Overlay with Sound Effect",
    },
    {
        "url": "https://www.youtube.com/watch?v=k0BvSreLx5E",
        "skip": 5, "duration": None, "type": "film_burn",
        "orientation": "vertical", "label": "Vertical Vibrant Film Burn Overlay",
    },
    {
        "url": "https://www.youtube.com/watch?v=eiditSLUA3I",
        "skip": 5, "duration": None, "type": "film_burn",
        "orientation": "vertical", "label": "Vertical Rich and Vibrant Colors",
    },
]


def _get_cache_dir(base_dir: str = None) -> str:
    """Return (and create) the transition raw-file cache directory."""
    base = base_dir or str(get_app_dir())
    cache_dir = os.path.abspath(os.path.join(base, "transitions_cache"))
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def _raw_filename(entry: dict) -> str:
    """Derive a deterministic raw filename from a pool entry's URL."""
    video_id = entry["url"].split("v=")[-1].split("&")[0]
    return f"tmp_raw_{video_id}.mp4"


def _resolve_ytdlp():
    """Return a usable yt-dlp ``YoutubeDL`` class or None."""
    try:
        from yt_dlp import YoutubeDL
        return YoutubeDL
    except ImportError:
        try:
            # Fallback to subprocess yt-dlp if module unavailable
            import shutil
            if shutil.which("yt-dlp"):
                return None
        except Exception:
            pass
        debug_log("[Transition] yt-dlp tidak tersedia.")
        return None


def _download_with_subprocess(url, outtmpl):
    """Fallback download via yt-dlp CLI."""
    import shutil
    exe = shutil.which("yt-dlp") or "yt-dlp"
    cmd = [exe, "-f", "best[ext=mp4]", "-o", outtmpl, "--no-playlist", url]
    subprocess.run(cmd, check=True, capture_output=True)


def download_transition_raw(entry: dict, base_dir: str = None) -> str | None:
    """
    Download a single transition video from YouTube if not already cached.

    Returns:
        Absolute path to the downloaded raw MP4, or None on failure.
    """
    cache_dir = _get_cache_dir(base_dir)
    raw_path = os.path.join(cache_dir, _raw_filename(entry))

    if os.path.exists(raw_path) and os.path.getsize(raw_path) > 10_000:
        return raw_path

    debug_log(f"[Transition] Downloading: {entry['label']}...")
    YoutubeDL = _resolve_ytdlp()
    try:
        if YoutubeDL is None:
            _download_with_subprocess(entry["url"], raw_path)
        else:
            YoutubeDL(
                {
                    "format": "best[ext=mp4]",
                    "outtmpl": raw_path,
                    "quiet": True,
                    "no_warnings": True,
                    "extractor_args": {"youtube": ["player_client=android,web"]},
                }
            ).download([entry["url"]])
    except Exception as e:
        debug_log(f"[Transition] Download gagal ({entry['label']}): {e}")
        return None

    if os.path.exists(raw_path) and os.path.getsize(raw_path) > 10_000:
        debug_log(f"[Transition] Tersimpan: {raw_path}")
        return raw_path

    debug_log(f"[Transition] File terlalu kecil / gagal: {raw_path}")
    return None


def download_all_transitions(base_dir: str = None, types: list | None = None) -> list[dict]:
    """
    Download all (or filtered) transition assets from the pool.

    Returns:
        List of dicts — pool entries augmented with ``"raw_path"``.
    """
    pool = list(TMP_TRANSITION_POOL)
    if types:
        pool = [e for e in pool if e["type"] in types]

    results = []
    for entry in pool:
        path = download_transition_raw(entry, base_dir)
        if path:
            enriched = dict(entry)
            enriched["raw_path"] = path
            results.append(enriched)

    debug_log(f"[Transition] {len(results)}/{len(pool)} asset berhasil diunduh.")
    return results


def get_random_transition(
    base_dir: str = None,
    transition_type: str | None = None,
    orientation: str | None = None,
) -> dict | None:
    """
    Pick a random transition from the pool, downloading if necessary.

    Args:
        base_dir: Base directory for cache (defaults to app dir).
        transition_type: Filter by type (``film_burn``, ``film_leader``, ...).
        orientation: ``"landscape"`` or ``"vertical"``.

    Returns:
        A pool entry dict with ``"raw_path"`` set, or None if nothing available.
    """
    candidates = list(TMP_TRANSITION_POOL)
    if transition_type:
        candidates = [c for c in candidates if c["type"] == transition_type]
    if orientation:
        candidates = [c for c in candidates if c["orientation"] == orientation]
    if not candidates:
        return None

    random.shuffle(candidates)
    for entry in candidates:
        path = download_transition_raw(entry, base_dir)
        if path:
            result = dict(entry)
            result["raw_path"] = path
            return result
    return None


def prepare_transition_clip(
    entry: dict,
    out_w: int,
    out_h: int,
    clip_duration: float | None = None,
    ffmpeg_path: str = None,
    scale_algo: str = "lanczos",
) -> str | None:
    """
    Extract the usable portion of a downloaded transition asset, crop/scale it
    to the target dimensions, and encode it as a short MP4.

    Args:
        entry: Pool entry dict (must have ``"raw_path"``).
        out_w, out_h: Target output dimensions.
        clip_duration: How many seconds to extract. None → entry duration or 3s.
        ffmpeg_path: FFmpeg executable path.
        scale_algo: ffmpeg scaling algorithm.

    Returns:
        Path to the prepared MP4 file, or None on failure.
    """
    raw_path = entry.get("raw_path")
    if not raw_path or not os.path.exists(raw_path):
        return None

    ffmpeg = ffmpeg_path or get_ffmpeg_path()
    skip = entry.get("skip", 5)
    dur = clip_duration or entry.get("duration") or 3.0
    video_id = entry["url"].split("v=")[-1].split("&")[0]
    out_path = os.path.join(
        os.path.dirname(raw_path) or ".",
        f"transition_{video_id}_{out_w}x{out_h}.mp4",
    )

    if os.path.exists(out_path):
        return out_path

    vf = f"scale={out_w}:{out_h}:flags={scale_algo},setsar=1"
    cmd = [
        ffmpeg, "-y",
        "-ss", str(skip),
        "-t", str(dur),
        "-i", raw_path,
        "-vf", vf,
        "-an",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-pix_fmt", "yuv420p",
        out_path,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=180)
        return out_path
    except subprocess.CalledProcessError as e:
        debug_log(f"[Transition] FFmpeg gagal untuk {entry['label']}: {e}")
        return None