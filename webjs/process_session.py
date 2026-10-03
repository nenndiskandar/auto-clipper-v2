#!/usr/bin/env python3
"""Phase 2 workflow (seperti bot): download section + render highlight terpilih.
Usage: process_session.py <session_dir>
Env: SELECTED="0,2,3" ADD_HOOK=0/1 ADD_CAPS=0/1 BGM_MOOD=... BROLL_QUERY=...
"""
from pathlib import Path
import sys, os, json, traceback

if len(sys.argv) < 2:
    print(f"Error: argumen kurang. Usage: {sys.argv[0]} <session_dir>", file=sys.stderr)
    sys.exit(1)

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
    _preset_name = os.environ.get("PRESET", "").strip() or os.environ.get("preset", "").strip()

    app_dir = Path(APP_DIR)
    cfg_mgr = ConfigManager(app_dir / "config.json", app_dir / "output")
    cfg = cfg_mgr.config
    # Apply preset overrides from UNIFIED_TEMPLATES (auto-render 1b) so 9:16/pop/crop tetap konsisten
    if _preset_name:
        try:
            from pathlib import Path as _P
            # preset cfg map mirrored from webjs/public/templates.js UNIFIED_TEMPLATES
            _PRESETS = {
                "tiktok_viral": {"aspect_ratio": "9:16", "portrait_mode": "crop", "subtitle_style": "pop", "face_tracking_mode": "mediapipe", "pan_speed_limit": 1.5, "center_weight": 0.6, "switch_threshold": 0.35, "min_shot_duration": 30},
                "gaming_action": {"aspect_ratio": "9:16", "portrait_mode": "crop", "subtitle_style": "pop", "face_tracking_mode": "mediapipe", "pan_speed_limit": 3.0, "center_weight": 0.15, "switch_threshold": 0.15, "min_shot_duration": 20},
                "education_clean": {"aspect_ratio": "9:16", "portrait_mode": "crop", "subtitle_style": "pop", "face_tracking_mode": "mediapipe", "pan_speed_limit": 1.0, "center_weight": 0.20, "switch_threshold": 0.30, "min_shot_duration": 60},
                "news_formal": {"aspect_ratio": "9:16", "portrait_mode": "crop", "subtitle_style": "karaoke", "face_tracking_mode": "mediapipe", "pan_speed_limit": 1.3, "center_weight": 0.15, "switch_threshold": 0.30, "min_shot_duration": 60},
                "vlog_dynamic": {"aspect_ratio": "9:16", "portrait_mode": "crop", "subtitle_style": "pop", "face_tracking_mode": "mediapipe", "pan_speed_limit": 1.8, "center_weight": 0.4, "switch_threshold": 0.35, "min_shot_duration": 45},
                "square_feed": {"aspect_ratio": "1:1", "portrait_mode": "crop", "subtitle_style": "karaoke"},
                "reels_34": {"aspect_ratio": "3:4", "portrait_mode": "crop", "subtitle_style": "pop"},
                "story_time": {"aspect_ratio": "9:16", "portrait_mode": "crop", "subtitle_style": "pop"},
            }
            _pcfg = _PRESETS.get(_preset_name)
            if _pcfg:
                for _k, _v in _pcfg.items():
                    cfg[_k] = _v
                # mediapipe_settings sync
                if "mediapipe_settings" not in cfg or not isinstance(cfg["mediapipe_settings"], dict):
                    cfg["mediapipe_settings"] = {}
                for _mk in ("pan_speed_limit", "center_weight", "switch_threshold", "min_shot_duration"):
                    if _mk in _pcfg:
                        cfg["mediapipe_settings"][_mk] = _pcfg[_mk]
                debug_log(f"[preset] Applied { _preset_name } -> aspect {cfg.get('aspect_ratio')} style {cfg.get('subtitle_style')}")
            else:
                debug_log(f"[preset] Unknown preset { _preset_name }, using config defaults")
        except Exception as _e:
            debug_log(f"[preset] Failed to apply { _preset_name }: { _e}")
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
        thumbnail_settings=cfg.get("thumbnail"),
        metadata_settings=cfg.get("metadata_settings"),
        auto_broll_settings=dict(cfg.get("auto_broll") or {}),
                subtitle_language=cfg.get("subtitle_language", "id"),
        subtitle_sync_offset=cfg.get("subtitle_sync_offset", -0.3),
    )
    # Per-clip override: BGM mood + B-roll query (dari UI) + BGM_PATH per-session (brief AI backsound)
    mod = os.environ.get("BGM_MOOD", "").strip()
    bq = os.environ.get("BROLL_QUERY", "").strip()
    bgm_path = os.environ.get("BGM_PATH", "").strip()
    if mod:
        core.auto_bgm_settings["mood"] = mod
    if bq:
        core.auto_broll_settings["query"] = bq
    if bgm_path and Path(bgm_path).exists():
        core.auto_bgm_settings["enabled"] = True
        core.auto_bgm_settings["path"] = str(Path(bgm_path).resolve())
        core.auto_bgm_settings["mode"] = "ducking"
        core.auto_bgm_settings["base_volume"] = 0.12
        debug_log(f"[bgm] Using per-session BGM {bgm_path} vol 0.12")
    # iGPU Ivy Bridge gagal VAAPI  -  matiin biar langsung CPU kualitas terbaik medium crf 18
    core.enable_gpu_acceleration(False)
    if cfg.get("face_detector_model"):
        core.face_detector_model = cfg.get("face_detector_model")

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