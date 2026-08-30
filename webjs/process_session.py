#!/usr/bin/env python3
"""Phase 2 workflow (seperti bot): download section + render highlight terpilih.
Usage: process_session.py <session_dir>
Env: SELECTED="0,2,3" ADD_HOOK=0/1 ADD_CAPS=0/1 BGM_MOOD=... BROLL_QUERY=...
"""
from pathlib import Path
import sys, os, json, traceback

SESSION_DIR = sys.argv[1]

APP_DIR = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, APP_DIR)
os.chdir(APP_DIR)

from openai import OpenAI
from config.config_manager import ConfigManager
from utils.helpers import get_ffmpeg_path, get_ytdlp_path
from clipper_core import AutoClipperCore
from utils.logger import debug_log


def main():
    sel_idx = [int(x) for x in os.environ.get("SELECTED", "").split(",") if x.strip()]
    add_hook = os.environ.get("ADD_HOOK", "1") == "1"
    add_caps = os.environ.get("ADD_CAPS", "1") == "1"

    app_dir = Path(APP_DIR)
    cfg_mgr = ConfigManager(app_dir / "config.json", app_dir / "output")
    cfg = cfg_mgr.config
    prov = cfg.get("ai_providers") or {}
    hf = prov.get("highlight_finder") or {}
    client = OpenAI(
        api_key=((hf.get("api_key") or cfg.get("api_key", ""))),
        base_url=(hf.get("base_url") or cfg.get("base_url", "https://api.openai.com/v1")),
    )
    core = AutoClipperCore(
        client=client,
        ffmpeg_path=get_ffmpeg_path(),
        ytdlp_path=get_ytdlp_path(),
        output_dir=str(app_dir / "output"),
        model=cfg.get("model", "gpt-4.1"),
        tts_model=cfg.get("tts_model", "tts-1"),
        temperature=cfg.get("temperature", 1.0),
        system_prompt=cfg.get("system_prompt"),
        watermark_settings=cfg.get("watermark"),
        credit_watermark_settings=cfg.get("credit_watermark"),
        hook_style_settings=cfg.get("hook_style"),
        face_tracking_mode=cfg.get("face_tracking_mode", "opencv"),
        portrait_mode=cfg.get("portrait_mode", "crop"),
        subtitle_style=cfg.get("subtitle_style", "pop"),
        aspect_ratio=cfg.get("aspect_ratio", "9:16"),
        mediapipe_settings=cfg.get("mediapipe_settings"),
        ai_providers=prov or None,
        pro_settings=cfg.get("pro_settings"),
        auto_bgm_settings=dict(cfg.get("auto_bgm") or {}),
        auto_camera_switch_settings=cfg.get("auto_camera_switch"),
        thumbnail_settings=cfg.get("thumbnail"),
        metadata_settings=cfg.get("metadata_settings"),
        auto_broll_settings=dict(cfg.get("auto_broll") or {}),
        transition_library_settings=cfg.get("transition_library"),
        subtitle_language=cfg.get("subtitle_language", "id"),
        subtitle_sync_offset=cfg.get("subtitle_sync_offset", -0.3),
    )
    # Per-clip override: BGM mood + B-roll query (dari UI)
    mod = os.environ.get("BGM_MOOD", "").strip()
    bq = os.environ.get("BROLL_QUERY", "").strip()
    if mod:
        core.auto_bgm_settings["mood"] = mod
    if bq:
        core.auto_broll_settings["query"] = bq
    # GPU selalu aktif
    core.enable_gpu_acceleration(True)
    if cfg.get("face_detector_model"):
        core.face_detector_model = cfg.get("face_detector_model")
    if cfg.get("yolo_size"):
        core.yolo_size = cfg.get("yolo_size")

    debug_log(f"[progress] Process start {len(sel_idx)} clips (overall: 10%)", flush=True)
    sd_path = Path(SESSION_DIR) / "session_data.json"
    sd = json.loads(sd_path.read_text(encoding="utf-8"))
    hs = sd.get("highlights") or []
    selected = [hs[i] for i in sel_idx if 0 <= i < len(hs)]
    if not selected:
        raise RuntimeError(f"Tidak ada highlight valid dari: {os.environ.get('SELECTED','')}")

    core.process_selected_highlights(
        sd.get("url"), selected, Path(SESSION_DIR),
        add_captions=add_caps, add_hook=add_hook,
        resolution=str(cfg.get("resolution", "1080p")),
    )
    debug_log("[progress] Process complete (overall: 100.0%)", flush=True)
    debug_log("PHASE2_OK")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()