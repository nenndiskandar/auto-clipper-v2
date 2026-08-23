"""
YT Short Clipper - CLI Bridge for OpenClaw
Memanggil clipper_core.py tanpa GUI
"""

import argparse
import json
import os
import sys
import datetime
from pathlib import Path

# Tambahkan path project ke sys.path
sys.path.insert(0, str(Path(__file__).parent))

from openai import OpenAI
from clipper_core import AutoClipperCore, SubtitleNotFoundError
from config.config_manager import ConfigManager
from utils.helpers import get_app_dir, get_ffmpeg_path, get_ytdlp_path

APP_DIR = Path(__file__).parent
CONFIG_FILE = APP_DIR / "config.json"
OUTPUT_DIR = APP_DIR / "output"


def log_output(message):
    timestamp = datetime.datetime.now().strftime("[%H:%M:%S]")
    # Strip ANSI codes for clean output
    import re
    clean = re.sub(r"\x1b\[[0-9;]*m", "", str(message))
    print(f"{timestamp} {clean}", flush=True)


def run_clipper(url, resolution="1080p", num_clips=5,
                add_captions=True, add_hook=True,
                face_tracking=None, subtitle_style=None):
    """Main clipper function - returns dict with result info"""

    config_manager = ConfigManager(CONFIG_FILE, OUTPUT_DIR)
    cfg = config_manager.config

    # Use config defaults if not specified
    face_tracking = face_tracking or cfg.get("face_tracking_mode", "mediapipe")
    subtitle_style = subtitle_style or cfg.get("subtitle_style", "pop_bounce")

    # Build OpenAI client from config
    hf = cfg.get("ai_providers", {}).get("highlight_finder", {})
    client = OpenAI(
        api_key=hf.get("api_key", ""),
        base_url=hf.get("base_url", "https://api.openai.com/v1")
    )

    clipper = AutoClipperCore(
        client=client,
        ffmpeg_path=str(get_ffmpeg_path()),
        ytdlp_path=str(get_ytdlp_path()),
        output_dir=cfg.get("output_dir", str(OUTPUT_DIR)),
        model=cfg.get("model", "AUTO"),
        tts_model=cfg.get("tts_model", "tts-1"),
        temperature=cfg.get("temperature", 1.0),
        system_prompt=cfg.get("system_prompt"),
        watermark_settings=cfg.get("watermark", {}),
        credit_watermark_settings=cfg.get("credit_watermark", {}),
        hook_style_settings=cfg.get("hook_style", {}),
        face_tracking_mode=face_tracking,
        portrait_mode=cfg.get("portrait_mode", "crop"),
        subtitle_style=subtitle_style,
        aspect_ratio=cfg.get("aspect_ratio", "9:16"),
        mediapipe_settings=cfg.get("mediapipe_settings", {}),
        ai_providers=cfg.get("ai_providers", {}),
        subtitle_language="id",
        subtitle_sync_offset=cfg.get("subtitle_sync_offset", -0.3),
        log_callback=log_output,
        progress_callback=lambda s, p: log_output(f"[{p*100:.0f}%] {s}")
    )

    # GPU acceleration
    if cfg.get("gpu_acceleration", {}).get("enabled", False):
        clipper.enable_gpu_acceleration(True)

    result = {"status": "error", "clips_dir": None, "clips_count": 0, "error": None}

    try:
        # Phase 1: Find highlights
        log_output(f"=== PHASE 1: Finding highlights for {url} ===")
        session_data = clipper.find_highlights_only(url=url, num_clips=num_clips)

        if not session_data or not session_data.get("highlights"):
            result["error"] = "No highlights found"
            return result

        highlights = session_data["highlights"]
        session_dir = Path(session_data["session_dir"])
        log_output(f"Found {len(highlights)} highlights")

        # Phase 2: Process all highlights
        log_output(f"=== PHASE 2: Processing clips at {resolution} ===")
        clipper.process_selected_highlights(
            url=url,
            selected_highlights=highlights,
            session_dir=session_dir,
            add_captions=add_captions,
            add_hook=add_hook,
            resolution=resolution
        )

        clips_dir = session_dir / "clips"
        clip_files = list(clips_dir.rglob("*.mp4")) if clips_dir.exists() else []

        result["status"] = "success"
        result["clips_dir"] = str(clips_dir)
        result["session_dir"] = str(session_dir)
        result["clips_count"] = len(clip_files)
        result["clip_files"] = [str(f) for f in clip_files]
        result["video_title"] = session_data.get("video_info", {}).get("title", "Unknown")

        log_output(f"=== DONE: {len(clip_files)} clips in {clips_dir} ===")

    except SubtitleNotFoundError as e:
        result["error"] = f"Subtitle not found: {e}"
        log_output(f"ERROR: {e}")
    except Exception as e:
        result["error"] = str(e)
        log_output(f"ERROR: {e}")

    return result


def main():
    parser = argparse.ArgumentParser(description="YT Short Clipper CLI Bridge")
    parser.add_argument("url", help="YouTube video URL")
    parser.add_argument("--resolution", default="1080p",
                        choices=["1080p", "720p", "480p", "360p"])
    parser.add_argument("--num-clips", type=int, default=5)
    parser.add_argument("--no-captions", action="store_true")
    parser.add_argument("--no-hook", action="store_true")
    parser.add_argument("--face-tracking", default=None,
                        choices=["mediapipe", "opencv", "none"])
    parser.add_argument("--subtitle-style", default=None,
                        choices=["pop_bounce", "pop", "karaoke", "bounce", "animated", "capcut"])
    parser.add_argument("--json", action="store_true", help="Output result as JSON")

    args = parser.parse_args()

    result = run_clipper(
        url=args.url,
        resolution=args.resolution,
        num_clips=args.num_clips,
        add_captions=not args.no_captions,
        add_hook=not args.no_hook,
        face_tracking=args.face_tracking,
        subtitle_style=args.subtitle_style
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if result["status"] == "success":
            print(f"\n✅ {result['clips_count']} clips created!")
            print(f"📁 {result['clips_dir']}")
            for f in result.get("clip_files", []):
                print(f"  🎬 {f}")
        else:
            print(f"\n❌ Error: {result['error']}")

    sys.exit(0 if result["status"] == "success" else 1)


if __name__ == "__main__":
    main()
