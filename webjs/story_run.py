#!/usr/bin/env python3
"""Story Clip pipeline (multi-source narrative) as a background job.
Usage: story_run.py <result_file>
Options dibaca dari env STORY_OPTS (JSON):
  sources_json     : path ke sources.json
  recipe_json      : path ke story_recipe.json
  outputs_dir      : base output (default ROOT/output)
  whisper_model    : ukuran local faster-whisper (default medium)
  skip_download    : lewati download (pakai cache/local)
  download_height  : resolusi download (default max)
  ratio            : rasio target (default 9:16)
  ffmpeg_path      : ffmpeg executable
"""
import sys, os, json, traceback
from pathlib import Path

RESULT_FILE = sys.argv[1]

APP_DIR = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, APP_DIR)
os.chdir(APP_DIR)

from utils.logger import debug_log
from core.story import run_story_pipeline
from utils.helpers import get_ffmpeg_path


def write_result(d):
    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)


def main():
    opts = json.loads(os.environ.get("STORY_OPTS") or "{}")
    sources_json = opts.get("sources_json") or ""
    recipe_json = opts.get("recipe_json") or ""
    outputs_dir = opts.get("outputs_dir") or str(Path(APP_DIR) / "output")
    whisper_model = opts.get("whisper_model") or "medium"
    skip_download = bool(opts.get("skip_download"))
    download_height = opts.get("download_height") or "max"
    ratio = opts.get("ratio") or "9:16"
    ffmpeg_path = opts.get("ffmpeg_path") or get_ffmpeg_path()

    if not sources_json or not os.path.exists(sources_json):
        raise FileNotFoundError(f"sources.json tidak ditemukan: {sources_json}")
    if not recipe_json or not os.path.exists(recipe_json):
        raise FileNotFoundError(f"story_recipe.json tidak ditemukan: {recipe_json}")

    os.makedirs(outputs_dir, exist_ok=True)
    debug_log(f"[progress] Story pipeline start (overall: 0.0%)", flush=True)
    manifest = run_story_pipeline(
        sources_json_path=sources_json,
        story_recipe_path=recipe_json,
        outputs_dir=outputs_dir,
        whisper_model=whisper_model,
        skip_download=skip_download,
        download_source_height=download_height,
        ratio=ratio,
        ffmpeg_path=ffmpeg_path,
    )
    debug_log(f"[progress] Story complete {len(manifest)} clip (overall: 100.0%)", flush=True)
    ok = manifest and len(manifest) > 0
    write_result({"ok": bool(ok), "count": len(manifest), "manifest": manifest})
    sys.exit(0 if ok else 1)


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