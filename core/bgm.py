"""
core/bgm.py — Auto BGM (Background Music) support.

Reads MP3 files from ``assets/bgm/<mood>/`` directories, selects one at random,
and builds FFmpeg filter_complex strings for mixing it under the vocal track
(either constant-volume background mix or sidechain duping).

Adapted from opensource-clipping ``clipping/studio/audio_bgm.py``.
"""

import os
import random
from pathlib import Path

from utils.logger import debug_log

# Moods understood by the AI highlight detector (bgm_mood field).
SUPPORTED_MOODS = ("chill", "epic", "sad", "upbeat", "suspense")

# Default base volume for the BGM layer (fraction of original).
DEFAULT_BGM_BASE_VOLUME = 0.25

# Moods per BGM mode.
DEFAULT_BGM_MODE = "ducking"  # "ducking" | "background"


def get_bgm_dir(app_dir=None) -> str:
    """Return (and create) the base directory holding BGM assets by mood."""
    base = Path(app_dir) if app_dir else Path(__file__).resolve().parent.parent
    bgm_dir = base / "assets" / "bgm"
    bgm_dir.mkdir(parents=True, exist_ok=True)
    return str(bgm_dir)


def get_local_bgm_file(mood: str, bgm_dir: str = None) -> str | None:
    """
    Get a random BGM MP3 file from the local assets directory based on mood.

    Args:
        mood: One of ``SUPPORTED_MOODS`` (e.g. 'chill', 'epic', 'sad').
        bgm_dir: Base directory for BGM assets. Defaults to ``assets/bgm``.

    Returns:
        Absolute path to the selected MP3 file, or None if not found/empty.
    """
    mood = (mood or "upbeat").lower().strip()
    if mood not in SUPPORTED_MOODS:
        debug_log(f"[BGM] Mood '{mood}' tidak dikenal, fallback ke upbeat.")
        mood = "upbeat"

    mood_dir = os.path.join(bgm_dir or get_bgm_dir(), mood)
    if not os.path.exists(mood_dir) or not os.path.isdir(mood_dir):
        debug_log(f"[BGM] Folder asset belum ada: {mood_dir}")
        return None

    audio_files = [
        f
        for f in os.listdir(mood_dir)
        if f.lower().endswith((".mp3", ".m4a", ".wav", ".aac", ".ogg"))
    ]
    if not audio_files:
        debug_log(f"[BGM] Tidak ada file musik di {mood_dir}")
        return None

    selected = random.choice(audio_files)
    path = os.path.abspath(os.path.join(mood_dir, selected))
    debug_log(f"[BGM] Dipilih: {path}")
    return path


def build_bgm_filter(
    bgm_mode: str = DEFAULT_BGM_MODE,
    bgm_base_volume: float = DEFAULT_BGM_BASE_VOLUME,
    audio_input_voc: str = "[1:a]",
    audio_input_bgm: str = "[2:a]",
) -> str:
    """
    Build the FFmpeg filter_complex string for BGM mixing.

    Args:
        bgm_mode: 'ducking' for sidechain compress, 'background' for constant mix.
        bgm_base_volume: Base volume level for BGM (e.g. 0.25).
        audio_input_voc: FFmpeg stream label for vocal audio input (input #1).
        audio_input_bgm: FFmpeg stream label for BGM audio input (input #2).

    Returns:
        The filter_complex string for FFmpeg.
    """
    voc_format = (
        f"{audio_input_voc}aformat=sample_fmts=fltp:sample_rates=48000:"
        f"channel_layouts=stereo,volume=1.2[voc]"
    )
    bgm_format = (
        f"{audio_input_bgm}aformat=sample_fmts=fltp:sample_rates=48000:"
        f"channel_layouts=stereo,volume={bgm_base_volume}[bgm]"
    )

    if bgm_mode == "background":
        # Simple constant-volume mix — no sidechain, BGM stays at base volume.
        return (
            f"{voc_format}; {bgm_format}; "
            f"[voc][bgm]amix=inputs=2:duration=first[a_out]"
        )
    else:
        # Ducking mode (default) — sidechain compress makes BGM duck under vocals.
        return (
            f"{voc_format}; {bgm_format}; "
            f"[voc]asplit=2[voc_sc][voc_mix]; "
            f"[bgm][voc_sc]sidechaincompress=threshold=0.08:ratio=5.0:"
            f"attack=100:release=1000[bgm_ducked]; "
            f"[voc_mix][bgm_ducked]amix=inputs=2:duration=first[a_out]"
        )