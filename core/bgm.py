"""BGM helper for auto-clipper - simple amix at 12% volume.
Provides build_bgm_filter and get_local_bgm_file used by core/caption.py.
If core/bgm was missing, auto BGM was silently skipped (copy).
This implementation enables backsound mixing for brief AI flow.
"""
import os
from pathlib import Path

def build_bgm_filter(mode: str = "ducking", base_volume: float = 0.25, duck_level_db: float = -15):
    """
    Return (filter_complex, audio_map) for ffmpeg.
    Simple: lower BGM to base_volume then amix with original audio.
    mode = ducking or background - both map to amix here.
    base_volume 0.12 ~ 12% as requested for backsound.
    duck_level_db unused in simple mode but kept for compat.
    """
    # clamp volume
    try:
        v = float(base_volume)
    except:
        v = 0.25
    v = max(0.02, min(1.0, v))
    # filter: BGM volume -> a1, then amix with original 0:a
    # amix normalize=0 keeps original loud, BGM quiet
    # dropout_transition 0 prevents click, duration shortest
    ffc = f"[1:a]volume={v:.3f},apad[a1];[0:a][a1]amix=inputs=2:duration=shortest:dropout_transition=0:normalize=0[aout]"
    return ffc, "[aout]"

def get_local_bgm_file(mood: str, bgm_dir: str = "") -> str:
    """
    Look up local BGM file by mood. Returns path string or None.
    Search in bgm_dir then ./assets/bgm and ./bgm
    """
    if not mood:
        return None
    mood = str(mood).strip().lower()
    candidates = []
    if bgm_dir:
        candidates.append(Path(bgm_dir) / f"{mood}.mp3")
        candidates.append(Path(bgm_dir) / f"{mood}.m4a")
        candidates.append(Path(bgm_dir) / f"{mood}.wav")
    # default dirs
    for base in ["./assets/bgm", "./bgm", "./output/bgm"]:
        candidates.append(Path(base) / f"{mood}.mp3")
        candidates.append(Path(base) / f"{mood}.m4a")
    for p in candidates:
        try:
            if p.exists() and p.stat().st_size > 1000:
                return str(p.resolve())
        except:
            continue
    return None
