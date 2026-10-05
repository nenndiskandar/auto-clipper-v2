# Workflow — Auto Clipper V2 (implemented 2026-10-05, pending campaign test)

## 0. Preflight
- cek brief campaign lengkap? (source_links, brief, sound_id)
- cek disk free + estimasi size file (guard kuota metered — GDrive >500MB / TikTok limit minta confirm)

## 1. Cari Campaign (TernakKlip)
→ pilih video_id(s) + sound_id

## 2. Klasifikasi per source
- **YouTube** → GET highlight sections dari TernakKlip API
- **Non-YouTube** → skip highlight
  - GDrive FILE → siap rclone
  - GDrive FOLDER → gdown expand → picker file video → rclone per-file

## 3. Sound (jika campaign butuh backsound)
- sound_id TikTok → cek cache `output/bgm/<id>.mp3`
- miss → `yt-dlp --cookies cookies.txt --impersonate chrome -x --audio-format mp3 <canonical_url>` → normalize loudness → cache (direct mp3 native, tanpa konversi ffmpeg — lihat § Detail teknis TikTok mp3)
- reuse, jangan download ulang

## 4. Download
- YouTube → `yt-dlp --download-sections` per slice highlight (hemat, nggak full)
- Non-YouTube → `rclone copyurl` full download (file / per-file dari folder)
- simpan ke `output/sessions/<id>/raw/` + `downloaded.json`
- kalau udah ada (cache), skip download

## 5. Cari Highlight (HANYA non-YouTube)
- `faster-whisper` → `transcript.json`
- LLM sesuai brief campaign → `highlights.json`
- scoring/filter: buang overlap, <15s / >90s, limit top-N
- YouTube = SKIP (udah ada dari langkah 2)

## 6. Render portrait 9:16 — AUTO RESOLUSI = max source (tanpa upscale)
- probe `ffprobe` dapet orig_w x orig_h
- `crop_w/crop_h` hitung dari orig + ratio 9:16 (bukan hard 720x1280 lagi)
- `out_w/out_h` = ukuran crop itu sendiri (bukan fixed 720x1280)
  - contoh: source 1920x1080 → crop 607x1080 → out 607x1080
  - contoh: source 1280x720 → crop 405x720 → out 405x720
  - contoh: source 1080x1920 → passthrough/scale tanpa crop
- `amix` BGM kalau ada (12–25%, loop/trim ngikut durasi clip)
- simpan ke `clips/<clipDir>/` + `render.json` (checkpoint)

## 7. QC + Manifest (TANPA hapus — keep-all)
- verifikasi durasi, audio, resolusi
- tulis `manifest.json` (clip_id, source, start-end, sound_id, resolusi)
- semua `_full_*.mp4` + transcript + raw KEEP
- re-render tinggal run step 6 lagi (nggak ulang download)
- cleanup cuma manual via tombol UI + warning disk di `/api/disk`

## Detail teknis: TikTok mp3 download (hasil test 2026-10-05)

> Validated — jangan ubah tanpa re-test.

### Tested URLs
- Short URL (input): `https://vt.tiktok.com/ZSbxmPj3c/`
- Canonical (resolved): `https://www.tiktok.com/@hewan_melatah/video/7691665540892380422`

### Hasil
| Skenario | Command | Result |
|---|---|---|
| Short URL `vt.tiktok.com` tanpa cookies/impersonate | `yt-dlp -x --audio-format mp3 https://vt.tiktok.com/ZSbxmPj3c/` | **GAGAL 403** — TikTok block / login required |
| Canonical URL + cookies + impersonate | `yt-dlp --cookies /root/auto-clipper-v2/cookies.txt --impersonate chrome -x --audio-format mp3 https://www.tiktok.com/@hewan_melatah/video/7691665540892380422` | **SUKSES** — direct mp3 native |

### Output sukses (canonical + cookies + impersonate)
- File: `7691665540892380422.mp3` (atau `<id>.mp3` sesuai sound_id)
- Size: **697 KB**
- Durasi: **44s**
- Format: **mp3 native 44100 Hz stereo, 129 kb/s** — `ffprobe` confirm, **tanpa konversi ffmpeg** (yt-dlp extract langsung, bukan transcode)
- Cookies: `/root/auto-clipper-v2/cookies.txt` (Netscape format, wajib)
- Impersonate: `--impersonate chrome` (bypass fingerprint/challenge)

### Aturan implementasi
1. **Jangan pakai short URL** `vt.tiktok.com` — selalu resolve ke canonical `tiktok.com/@user/video/<id>` dulu (follow redirect).
2. **Wajib** `--cookies /root/auto-clipper-v2/cookies.txt --impersonate chrome` untuk semua download TikTok sound.
3. Output mp3 native — **jangan force re-encode** via ffmpeg; cukup `normalize loudness` setelahnya jika diperlukan.
4. Cache di `output/bgm/<sound_id>.mp3` — reuse, jangan download ulang.

### Command final (copy-paste)
```bash
yt-dlp --cookies /root/auto-clipper-v2/cookies.txt --impersonate chrome -x --audio-format mp3 -o "output/bgm/%(id)s.%(ext)s" https://www.tiktok.com/@hewan_melatah/video/7691665540892380422
```

## Catatan pending
- Workflow **implemented 2026-10-05**, pending test 1 campaign nyata (per user: "Nanti test campaign").
- Patch 2026-10-05: `core/portrait.py` auto max-source `_get_ratio_dimensions(orig_w,orig_h)`, wiring `resolution: auto` di `clipper_core.py` + 4 entry (telegram/webjs), `core/download.py` TikTok sound cache `output/bgm/<id>.mp3` (resolve canonical + cookies+impersonate), `webjs/server.js` backsound cache+HIT copy, `core/highlight.py` scoring overlap/<15s/>90s/top-N + skip YouTube, `core/caption.py` manifest.json QC keep-all.
- Sisa sebelum test: restart `clipper-bot` agar wiring baru ke-load (webjs hot-reload), lalu test campaign TernakKlip (YouTube vs GDrive folder+sound TikTok).

## Tooling per step
- **Preflight**: `/api/disk`, size-guard di `core/download.py` (>500MB confirm)
- **YouTube download**: `yt-dlp --download-sections "*start-end"`
- **GDrive**: `gdown --json` (folder expand) → `rclone copyurl` (file)
- **Sound**: `yt-dlp --cookies /root/auto-clipper-v2/cookies.txt --impersonate chrome -x --audio-format mp3 <canonical_tiktok_url>` → `output/bgm/` (direct mp3 native, tanpa konversi ffmpeg)
- **Transcribe**: `faster-whisper` (`faster_whisper_models/*`)
- **Render**: `core/portrait.py` → ffmpeg `crop` + `scale` + `amix` (bgm)
