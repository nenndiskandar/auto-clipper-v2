#!/usr/bin/env python3
"""Re-render satu klip dari section landscape yang sudah ada di clip_dir.
Usage: render_clip.py <session_dir> <clip_dir>
Env RENDER_OPTS (JSON): {hook,captions,watermark,credit,aspect_ratio,subtitle_style,
sync_offset,portrait_mode,face_tracking_mode,smooth_follow,pan_speed_limit,
center_weight,switch_threshold,min_shot_duration,lip_activity,gpu}
Default mengikuti config.json (satu sumber dengan bot /config).
"""
from pathlib import Path
import sys, os, json, traceback

SESSION_DIR = sys.argv[1]
CLIP_DIR = sys.argv[2]

APP_DIR = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, APP_DIR)
os.chdir(APP_DIR)

from openai import OpenAI
from config.config_manager import ConfigManager
from utils.helpers import get_ffmpeg_path, get_ytdlp_path
from clipper_core import AutoClipperCore


def main():
    try:
        opts = json.loads(os.environ.get("RENDER_OPTS", "{}"))
    except Exception:
        opts = {}

    app_dir = Path(APP_DIR)
    cfg_mgr = ConfigManager(app_dir / "config.json", app_dir / "output")
    cfg = cfg_mgr.config
    prov = cfg.get("ai_providers") or {}
    hf = prov.get("highlight_finder") or {}
    client = OpenAI(
        api_key=(hf.get("api_key") or cfg.get("api_key", "")),
        base_url=(hf.get("base_url") or cfg.get("base_url", "https://api.openai.com/v1")),
    )

    # --- overlay opts ke config (default = config.json) ---
    def b(key, default):
        return bool(opts[key]) if key in opts else default

    wm = dict(cfg.get("watermark") or {})
    wm["enabled"] = b("watermark", wm.get("enabled", False))
    cw = dict(cfg.get("credit_watermark") or {})
    cw["enabled"] = b("credit", cw.get("enabled", False))

    mp = dict(cfg.get("mediapipe_settings") or {
        "lip_activity_threshold": 0.15, "switch_threshold": 0.3,
        "min_shot_duration": 90, "center_weight": 0.3,
        "smooth_follow": True, "pan_speed_limit": 2.5,
    })
    if "smooth_follow" in opts: mp["smooth_follow"] = bool(opts["smooth_follow"])
    if "pan_speed_limit" in opts: mp["pan_speed_limit"] = float(opts["pan_speed_limit"])
    if "center_weight" in opts: mp["center_weight"] = float(opts["center_weight"])
    if "switch_threshold" in opts: mp["switch_threshold"] = float(opts["switch_threshold"])
    if "min_shot_duration" in opts: mp["min_shot_duration"] = int(opts["min_shot_duration"])
    if "lip_activity" in opts: mp["lip_activity_threshold"] = float(opts["lip_activity"])

    core = AutoClipperCore(
        client=client,
        ffmpeg_path=get_ffmpeg_path(),
        ytdlp_path=get_ytdlp_path(),
        output_dir=str(app_dir / "output"),
        model=cfg.get("model", "gpt-4.1"),
        tts_model=cfg.get("tts_model", "tts-1"),
        temperature=cfg.get("temperature", 1.0),
        system_prompt=cfg.get("system_prompt"),
        watermark_settings=wm,
        credit_watermark_settings=cw,
        hook_style_settings=cfg.get("hook_style"),
        face_tracking_mode=str(opts.get("face_tracking_mode", cfg.get("face_tracking_mode", "opencv"))),
        portrait_mode=str(opts.get("portrait_mode", cfg.get("portrait_mode", "crop"))),
        subtitle_style=str(opts.get("subtitle_style", cfg.get("subtitle_style", "pop"))),
        aspect_ratio=str(opts.get("aspect_ratio", cfg.get("aspect_ratio", "9:16"))),
        mediapipe_settings=mp,
        ai_providers=prov or None,
        subtitle_language=cfg.get("subtitle_language", "id"),
        subtitle_sync_offset=float(opts.get("sync_offset", cfg.get("subtitle_sync_offset", -0.3))),
    )
    if "gpu" in opts:
        core.enable_gpu_acceleration(bool(opts["gpu"]))
    el# GPU selalu aktif
    core.enable_gpu_acceleration(True)

    sd_path = Path(SESSION_DIR) / "session_data.json"
    sd = json.loads(sd_path.read_text(encoding="utf-8"))
    info = sd.get("video_info") or {}
    core.channel_name = info.get("channel") or info.get("uploader") or ""

    meta_path = Path(CLIP_DIR) / "data.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    landscape = Path(CLIP_DIR) / "landscape.mp4"
    if not landscape.exists():
        raise FileNotFoundError(f"source tidak ada: {landscape}")

    highlight = {
        "title": meta["title"],
        "start_time": meta["start_time"],
        "end_time": meta["end_time"],
        "duration_seconds": meta.get("duration_seconds", 0),
        "hook_text": meta.get("hook_text", meta["title"]),
    }
    core.process_clip(
        str(landscape), highlight, index=1, total_clips=1,
        add_captions=b("captions", cfg.get("subtitle_enabled", True)),
        add_hook=b("hook", cfg.get("hook_enabled", True)),
        pre_cut=True, clip_dir=str(Path(CLIP_DIR)),
    )
    print("RENDER_OK")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
