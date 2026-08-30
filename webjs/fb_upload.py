#!/usr/bin/env python3
"""Facebook Reels uploader as a background job.
Usage: fb_upload.py <result_file>
Options dibaca dari env FB_OPTS (JSON):
  manifest      : path ke render_manifest.json (default ROOT/output/render_manifest.json)
  result        : path result upload (default ROOT/output/fb_upload_results.json)
  updated       : path manifest yang sudah di-update status-nya (default *_fb_uploaded.json)
"""
import sys, os, json, traceback
from pathlib import Path

RESULT_FILE = sys.argv[1]

APP_DIR = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, APP_DIR)
os.chdir(APP_DIR)

from utils.logger import debug_log
from facebook_uploader import upload_manifest_to_facebook, validate_page_token, load_fb_config


def write_result(d):
    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)


def main():
    opts = json.loads(os.environ.get("FB_OPTS") or "{}")
    out_dir = str(Path(APP_DIR) / "output")
    manifest_file = opts.get("manifest") or os.path.join(out_dir, "render_manifest.json")
    result_file = opts.get("result") or os.path.join(out_dir, "fb_upload_results.json")
    updated_manifest_file = opts.get("updated") or manifest_file + "_fb_uploaded.json"
    test_mode = bool(opts.get("test_mode"))

    fb_config = load_fb_config()
    if test_mode:
        debug_log("[FB] test_mode aktif — hanya validasi token, tanpa upload.")
        try:
            info = validate_page_token(fb_config)
        except Exception as e:
            write_result({"ok": False, "error": str(e)})
            sys.exit(1)
        write_result({"ok": True, "test": True, "page": info.get("name"), "page_id": info.get("id")})
        sys.exit(0)

    if not os.path.exists(manifest_file):
        raise FileNotFoundError(f"render_manifest.json tidak ditemukan: {manifest_file}")

    debug_log(f"[progress] Facebook upload start (overall: 0.0%)", flush=True)
    results = upload_manifest_to_facebook(
        manifest_file=str(manifest_file),
        result_file=str(result_file),
        updated_manifest_file=str(updated_manifest_file),
        fb_config=fb_config,
    )
    debug_log(f"[progress] Facebook upload complete {len(results)} (overall: 100.0%)", flush=True)
    write_result({"ok": True, "uploaded": len(results), "results": results, "updated_manifest": str(updated_manifest_file)})


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