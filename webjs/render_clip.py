#!/usr/bin/env python3
"""Re-render satu klip (dari landscape.mp4) dengan konfigurasi aktif.

Env RENDER_OPTS (JSON): {hook,captions,watermark,credit,aspect_ratio,subtitle_style,
portrait_mode,face_tracking_mode,sync_offset,gpu,face_detector_model,yolo_size,
bgm_mood,bgm_path,broll_query,pexels_api_key}
"""
import sys, os, json, traceback
from pathlib import Path

APP_DIR = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, APP_DIR)
os.chdir(APP_DIR)

from openai import OpenAI
from config.config_manager import ConfigManager
from utils.logger import debug_log
from utils.helpers import get_ffmpeg_path, get_ytdlp_path
from clipper_core import AutoClipperCore

SESSION_DIR = sys.argv[1]
CLIP_DIR = sys.argv[2]


def b(key, opts, default):
    return bool(opts[key]) if key in opts else default


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
    wm = dict(cfg.get("watermark") or {})
    wm["enabled"] = b("watermark", opts, wm.get("enabled", False))
    cw = dict(cfg.get("credit_watermark") or {})
    cw["enabled"] = b("credit", opts, cw.get("enabled", False))

    mp = dict(cfg.get("mediapipe_settings") or {
        "lip_activity_threshold": 0.08, "switch_threshold": 0.18,
        "min_shot_duration": 45, "center_weight": 0.15,
        "smooth_follow": False, "pan_speed_limit": 1.8,
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
        pro_settings=cfg.get("pro_settings"),
        auto_bgm_settings=dict(cfg.get("auto_bgm") or {}),
        auto_camera_switch_settings=cfg.get("auto_camera_switch"),
        thumbnail_settings=cfg.get("thumbnail"),
        metadata_settings=cfg.get("metadata_settings"),
        auto_broll_settings=dict(cfg.get("auto_broll") or {}),
        transition_library_settings=cfg.get("transition_library"),
        subtitle_language=cfg.get("subtitle_language", "id"),
        subtitle_sync_offset=float(opts.get("sync_offset", cfg.get("subtitle_sync_offset", -0.3))),
    )
    # Per-clip override: BGM mood + B-roll query (dari UI re-render)
    if str(opts.get("bgm_mood") or "").strip():
        core.auto_bgm_settings["mood"] = str(opts["bgm_mood"]).strip()
    if "bgm_path" in opts and str(opts.get("bgm_path") or "").strip():
        core.auto_bgm_settings["path"] = str(opts["bgm_path"]).strip()
    if str(opts.get("broll_query") or "").strip():
        core.auto_broll_settings["query"] = str(opts["broll_query"]).strip()
    if "pexels_api_key" in opts and str(opts.get("pexels_api_key") or "").strip():
        core.auto_broll_settings["pexels_api_key"] = str(opts["pexels_api_key"]).strip()
    if cfg.get("face_detector_model"):
        core.face_detector_model = cfg.get("face_detector_model")
    if cfg.get("yolo_size"):
        core.yolo_size = cfg.get("yolo_size")
    if "gpu" in opts:
        core.enable_gpu_acceleration(bool(opts["gpu"]))
    else:  # GPU selalu aktif
        core.enable_gpu_acceleration(True)

    sd_path = Path(SESSION_DIR) / "session_data.json"
    sd = json.loads(sd_path.read_text(encoding="utf-8"))
    info = sd.get("video_info") or {}
    core.channel_name = info.get("channel") or info.get("uploader") or ""

    debug_log(f"[progress] Render start (overall: 0.0%)", flush=True)
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
        add_captions=b("captions", opts, cfg.get("subtitle_enabled", True)),
        add_hook=b("hook", opts, cfg.get("hook_enabled", True)),
        pre_cut=True, clip_dir=str(Path(CLIP_DIR)),
    )
    debug_log("[progress] Render complete (overall: 100.0%)", flush=True)
    debug_log("RENDER_OK")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)