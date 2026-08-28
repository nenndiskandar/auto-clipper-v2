"""Shared bootstrap untuk skrip web (phase1/refind): bangun AutoClipperCore dari config.json."""
import json
import os
from pathlib import Path
import sys

APP_DIR = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, APP_DIR)


def write_json(fp, d):
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)


def build_core():
    from openai import OpenAI
    from config.config_manager import ConfigManager
    from utils.helpers import get_ffmpeg_path, get_ytdlp_path
    from clipper_core import AutoClipperCore

    app_dir = Path(APP_DIR)
    os.chdir(app_dir)
    cfg_mgr = ConfigManager(app_dir / "config.json", app_dir / "output")
    cfg = cfg_mgr.config
    prov = cfg.get("ai_providers") or {}
    hf = prov.get("highlight_finder") or {}
    client = OpenAI(
        api_key=(hf.get("api_key") or cfg.get("api_key", "")),
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
    # GPU selalu aktif
    core.enable_gpu_acceleration(True)
    return core, cfg
