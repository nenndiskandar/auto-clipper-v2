# Auto Clipper

Auto Clipper membuat klip pendek (TikTok, Reels, Shorts) dari video YouTube panjang
secara otomatis memakai AI (GPT-4 / Whisper) untuk deteksi highlight & caption, dan
Computer Vision (OpenCV / MediaPipe) untuk smart cropping ke 9:16.

Proyek ini berfokus pada **dua antarmuka**:

1. **Web App** (`webjs/`) — server Node.js (zero-dependency) + UI statis (PWA) yang
   memanggil engine Python lewat subprocess.
2. **Telegram Bot** (`telegram_bot.py` + `telegram_client.py`) — Bot API & userbot
   yang memicu pipeline yang sama.

Engine pemrosesan berada di `clipper_core.py` + `core/` dan dipakai bersama oleh
kedua antarmuka. (GUI desktop CustomTkinter sudah dihilangkan dari repositori ini.)

## Quick Start

### Prasyarat
- Python 3.10+
- Node.js >= 16 (untuk web)
- `ffmpeg` & `yt-dlp` di PATH (atau biarkan `utils/dependency_manager` mengunduhnya)
- `config.json` dengan setidaknya `telegram_bot_token` (untuk auth web & bot)

```bash
pip install -r requirements.txt
```

### Jalankan Web App
```bash
node webjs/server.js
# buka http://localhost:3000
```

Server membaca `config.json` dan menjalankan helper Python di `webjs/*.py`
(`phase1_create.py`, `process_session.py`, `refind_highlights.py`, `render_clip.py`)
yang pada gilirannya memanggil `clipper_core.AutoClipperCore`.

### Jalankan Telegram Bot
```bash
python telegram_bot.py        # Bot API
python telegram_client.py     # Userbot (MTProto, tanpa bot token)
```
Atau pakai launcher: `0.start.bot.Tele.bat` / `1.start.client.Tele.bat`.

## Struktur Proyek

| Path | Deskripsi |
|------|-----------|
| `webjs/server.js` | HTTP server Node (auth HMAC, serve `public/`, proxy ke Python) |
| `webjs/public/` | UI statis + PWA (manifest, sw.js, pwa.js) |
| `webjs/*.py` | Helper Python yang dipanggil server (pipeline per-session) |
| `telegram_bot.py` | Interface Telegram Bot API |
| `telegram_client.py` | Interface Telegram userbot (Telethon) |
| `clipper_core.py` | Orchestrator engine + komposisi mixin |
| `core/` | Mixin fokus: `download`, `transcribe`, `highlight`, `portrait`, `caption` |
| `config/` | `config_manager.py` + profil provider AI |
| `utils/` | `helpers`, `logger`, `gpu_detector`, `dependency_manager`, `font_scanner` |
| `tiktok_uploader.py` / `youtube_uploader.py` | Uploader (berbagi, standalone) |
| `assets/watermarks/` | Aset watermark (dipakai bot) |
| `tests/` | Unit test (`pytest tests`) |

## Testing
```bash
pytest tests
```

## Catatan
- Semua log lewat `utils.logger.debug_log` (bukan `print`).
- Jangan pakai `except:` kosong; selalu `except Exception as e:` + `debug_log`.
- `clipper_core` sudah dipecah jadi mixin di `core/` — tambah fitur di mixin yang
  sesuai, jangan menumpuk di `clipper_core.py`.
