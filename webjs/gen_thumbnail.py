"""webjs/gen_thumbnail.py — generate thumbnail dari klip via core.thumbnail.buat_thumbnail.

Usage: python gen_thumbnail.py <session> <clipDir> <outPath> [frame_ms] [alpha]
Mencari video final di folder klip, menulis judul (meta.title) ke frame.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.thumbnail import buat_thumbnail

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSIONS = os.path.join(ROOT, "output", "sessions")


def find_video(clip_dir):
    """Pilih video final (bukan hook/landscape) paling baru di folder klip."""
    if not os.path.isdir(clip_dir):
        return None
    cands = [f for f in os.listdir(clip_dir)
             if f.lower().endswith((".mp4", ".mov", ".webm"))
             and f not in ("hook.mp4", "landscape.mp4")]
    if not cands:
        return None
    cands.sort(key=lambda f: os.path.getmtime(os.path.join(clip_dir, f)), reverse=True)
    return os.path.join(clip_dir, cands[0])


def main():
    if len(sys.argv) < 4:
        print("usage: gen_thumbnail.py <session> <clipDir> <outPath> [frame_ms] [alpha]", file=sys.stderr)
        sys.exit(2)
    session, clip_dir_name, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    frame_ms = int(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4] else 5000
    alpha = int(sys.argv[5]) if len(sys.argv) > 5 and sys.argv[5] else 128

    clip_dir = os.path.join(SESSIONS, session, "clips", clip_dir_name)
    video = find_video(clip_dir)
    if not video:
        print(f"tidak ada video di {clip_dir}", file=sys.stderr)
        sys.exit(1)

    # title dari data.json
    title = clip_dir_name
    meta_path = os.path.join(clip_dir, "data.json")
    try:
        if os.path.exists(meta_path):
            meta = json.load(open(meta_path, encoding="utf-8"))
            title = meta.get("title") or title
    except Exception:
        pass

    res = buat_thumbnail(video, out_path, title, frame_ms=frame_ms, overlay_alpha=alpha)
    if not res or not os.path.exists(out_path):
        print("gagal generate thumbnail", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"ok": True, "path": out_path, "title": title, "video": video}))


if __name__ == "__main__":
    main()
