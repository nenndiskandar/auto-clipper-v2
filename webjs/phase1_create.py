#!/usr/bin/env python3
"""Phase 1 workflow (seperti bot): download subtitle + AI cari highlights.
Usage: phase1_create.py <url> <num_clips> <result_file>
Tulis result JSON: {ok, session_dir, error?}
"""
import sys, os, json, traceback
from pathlib import Path

URL = sys.argv[1]
NUM_CLIPS = int(sys.argv[2])
RESULT_FILE = sys.argv[3]

APP_DIR = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, APP_DIR)
os.chdir(APP_DIR)

from openai import OpenAI
from config.config_manager import ConfigManager
from utils.helpers import get_ffmpeg_path, get_ytdlp_path
from utils.logger import debug_log
from clipper_core import AutoClipperCore


def _progress_cb(message, fraction):
    try:
        pct = max(0.0, min(1.0, float(fraction))) * 100.0
        debug_log(f"[progress] {message} (overall: {pct:.1f}%)", flush=True)
    except Exception:
        pass


def write_result(d):
    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)


def main():
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
        auto_bgm_settings=cfg.get("auto_bgm"),
        auto_camera_switch_settings=cfg.get("auto_camera_switch"),
        thumbnail_settings=cfg.get("thumbnail"),
        metadata_settings=cfg.get("metadata_settings"),
        auto_broll_settings=cfg.get("auto_broll"),
        transition_library_settings=cfg.get("transition_library"),
        subtitle_language=cfg.get("subtitle_language", "id"),
        subtitle_sync_offset=cfg.get("subtitle_sync_offset", -0.3),
    )
    # GPU selalu aktif, gagal -> fallback CPU (clipper_core)
    core.enable_gpu_acceleration(True)
    if cfg.get("face_detector_model"):
        core.face_detector_model = cfg.get("face_detector_model")
    if cfg.get("yolo_size"):
        core.yolo_size = cfg.get("yolo_size")

    num_clips = "auto"
    debug_log(f"[progress] Starting Phase1 analyze (overall: 0.0%)", flush=True)
    sd = core.find_highlights_only(URL, num_clips, progress_callback=_progress_cb)
    highlights = sd.get("highlights") or []
    if not highlights:
        write_result({"ok": False, "error": "AI tidak menemukan highlight dari video ini."})
        sys.exit(1)

    highlights.sort(key=lambda h: h.get("virality_score", 0) or 0, reverse=True)
    sess_dir = Path(sd["session_dir"])
    sdf = sess_dir / "session_data.json"
    data = {}
    if sdf.exists():
        try:
            data = json.loads(sdf.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data.update({
        "session_dir": str(sess_dir),
        "url": URL,
        "video_info": sd.get("video_info") or data.get("video_info") or {"title": "Unknown", "channel": "Unknown"},
        "highlights": highlights,
        "status": "highlights_ready",
        "created_at": data.get("created_at") or __import__("datetime").datetime.now().isoformat(),
    })
    with open(sdf, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    debug_log(f"[progress] Phase1 complete {len(highlights)} highlights (overall: 100.0%)", flush=True)
    write_result({"ok": True, "session_dir": str(sess_dir), "session_id": sess_dir.name, "count": len(highlights)})


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        try:
            write_result({"ok": False, "error": traceback.format_exc()[-1500:]})
        except Exception:
            pass
        sys.exit(1)
