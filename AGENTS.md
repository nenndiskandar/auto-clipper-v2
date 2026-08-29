# 🤖 AGENTS.md - AI Developer Guide for Auto Clipper

## 📌 Project Overview
**Auto Clipper** automates short-form clips (TikTok, Reels, Shorts) from long YouTube
videos using AI (GPT-4 / Whisper) for highlight detection & captioning, and Computer
Vision (OpenCV / MediaPipe) for 9:16 smart cropping.

**Fokus repositori ini: Web App + Telegram Bot.** GUI desktop (CustomTkinter) sudah
dihapus. Engine pemrosesan (`clipper_core.py` + `core/`) dipakai bersama oleh kedua
antarmuka.

## 🏗️ Architecture & Tech Stack
- **Language**: Python 3.10+ (engine + bot), Node.js >= 16 (web server)
- **Web**: `webjs/server.js` (HTTP, zero-dep) + `webjs/public/` (static PWA). Server
  memanggil helper Python (`webjs/*.py`) via `child_process`, yang memanggil engine.
- **Telegram**: `python-telegram-bot` (Bot API) + `telethon` (userbot/MTProto).
- **Video**: FFmpeg (subprocess) + OpenCV (face detection) + MediaPipe (active speaker).
- **AI/ML**: OpenAI API (GPT-4 / Whisper) lewat `config.ai_providers`.

## 🔄 Core Pipeline (`clipper_core.py` + `core/`)
`clipper_core.AutoClipperCore` menyusun seluruh pipeline dan mewarisi mixin:
- `core/download.py` (DownloadMixin): download video & subtitle, yt-dlp, progress.
- `core/transcribe.py` (TranscribeMixin): Whisper API + faster-whisper lokal.
- `core/highlight.py` (HighlightMixin): deteksi highlight via LLM, parse SRT.
- `core/portrait.py` (PortraitMixin): crop 9:16, face-tracking, stabilisasi.
- `core/caption.py` (CaptionMixin): hook, caption, watermark, `process_clip`.
- `core/subtitle_generator.py`, `core/effects.py`: mixin lama (tetap dipertahankan).

Alur inti:
1. `download_video` / `download_subtitle_only` -> dapat video + `.srt`.
2. `transcribe_*` -> word-level transcript (Whisper).
3. `find_highlights` / `find_highlights_with_transcription` -> timestamp + hook.
4. `process_clip` -> potong -> portrait -> hook -> caption (burn via FFmpeg).

## 📂 Key Directories
| Path | Deskripsi |
|------|-----------|
| `webjs/server.js` | Server web utama |
| `webjs/public/` | UI statis + PWA |
| `webjs/*.py` | Helper Python per-session (panggil engine) |
| `telegram_bot.py` / `telegram_client.py` | Antarmuka Telegram |
| `clipper_core.py` | Orchestrator + komposisi mixin |
| `core/` | Mixin engine (download/transcribe/highlight/portrait/caption) |
| `config/` | `config_manager.py`, profil provider AI |
| `utils/` | `helpers`, `logger`, `gpu_detector`, `dependency_manager`, `font_scanner` |
| `tiktok_uploader.py` / `youtube_uploader.py` | Uploader (standalone, berbagi) |
| `assets/watermarks/` | Aset watermark (dipakai bot) |
| `tests/` | Unit test (`pytest tests`) |

## 🛠️ Coding Standards
- **Logging**: selalu pakai `utils.logger.debug_log` (bukan `print`). `debug_log`
  menerima multi-arg & kwarg print-like.
- **Error handling**: jangan `except:` kosong. Gunakan
  `except Exception as e:` lalu `debug_log(...)` / `log_error(...)`.
- **Tidak ada GUI**: jangan impor `tkinter`/`customtkinter`/`CTk*` lagi.
- **Modular**: tambah fitur ke mixin `core/` yang sesuai, jangan menumpuk di
  `clipper_core.py`.
- **Tests**: jalankan `pytest tests` sebelum commit.

## 🔗 Related
- `README.md`: quick start web & bot.
