#!/usr/bin/env python3
"""Phase 2 workflow (seperti bot): download section + render highlight terpilih.
Usage: process_session.py <session_dir>
Env: SELECTED="0,2,3" ADD_HOOK=0/1 ADD_CAPS=0/1
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
        subtitle_language=cfg.get("subtitle_language", "id"),
        subtitle_sync_offset=cfg.get("subtitle_sync_offset", -0.3),
    )
    if (cfg.get("gpu_acceleration") or {}).get("enabled"):
        core.enable_gpu_acceleration(True)

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
    print("PHASE2_OK")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
