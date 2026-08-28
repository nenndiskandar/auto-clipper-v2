#!/usr/bin/env python3
"""Regenerate highlights untuk sesi yang sudah ada.
Usage: refind_highlights.py <session_id> <num_clips|0> <result_file>
Result JSON: {ok, count?, error?} — highlights ditulis ulang ke session_data.json sesi tsb."""
import sys
import json
import traceback
from pathlib import Path

SID = sys.argv[1]
NUM_CLIPS = int(sys.argv[2])
RESULT_FILE = sys.argv[3]

sys.path.insert(0, str(Path(__file__).resolve().parent))
from clip_common import APP_DIR, build_core, write_json


def main():
    core, cfg = build_core()
    num = NUM_CLIPS if NUM_CLIPS > 0 else int(cfg.get("num_clips", 5))
    sess_dir = Path(APP_DIR) / "output" / "sessions" / SID
    sdf = sess_dir / "session_data.json"
    data = json.loads(sdf.read_text(encoding="utf-8"))
    url = data.get("url")
    if not url:
        raise RuntimeError("Session tidak punya URL sumber.")

    print(f"[refind] URL={url} num_clips={num}", flush=True)
    sd = core.find_highlights_only(url, num)
    highlights = sd.get("highlights") or []
    if not highlights:
        raise RuntimeError("AI tidak menemukan highlight dari video ini.")
    highlights.sort(key=lambda h: h.get("virality_score", 0) or 0, reverse=True)

    # ponytail: folder sesi baru buatan find_highlights_only dibiarkan (berisi srt segar);
    # kalau jadi sampah, tambahkan cleanup di sini.
    data["highlights"] = highlights
    data["status"] = "highlights_ready"
    data["video_info"] = sd.get("video_info") or data.get("video_info") or data.get("video_info")
    sdf.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[refind] {len(highlights)} highlight tersimpan.", flush=True)
    write_json(RESULT_FILE, {"ok": True, "count": len(highlights)})


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        try:
            write_json(RESULT_FILE, {"ok": False, "error": traceback.format_exc()[-1500:]})
        except Exception:
            pass
        sys.exit(1)
