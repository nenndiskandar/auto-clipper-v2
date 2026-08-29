"""
Auto Clipper Core - Processing logic
Refactored to use OpenAI Whisper API instead of local model
"""

import subprocess
import os
import re
import threading
import json
import cv2
import numpy as np
import tempfile
import sys
import time

# MediaPipe Tasks API (used only when face_tracking_mode == "mediapipe").
# Imported lazily-guarded here so startup stays fast when MediaPipe is unused.
try:
    import mediapipe as mp
    from mediapipe.tasks import python
    from mediapipe.tasks.python import vision
except ImportError:
    mp = None
    python = None
    vision = None

from pathlib import Path
from datetime import datetime
from openai import OpenAI, APIError, APIConnectionError, RateLimitError, APIStatusError
from utils.logger import debug_log
from utils.helpers import get_deno_path, get_ffmpeg_path, is_ytdlp_module_available, extract_video_id

# Check if yt-dlp is available as a Python module
try:
    import yt_dlp
    YTDLP_MODULE_AVAILABLE = True
except ImportError:
    yt_dlp = None
    YTDLP_MODULE_AVAILABLE = False

# Faster-Whisper (local transcription with built-in VAD via silero-vad)
try:
    from faster_whisper import WhisperModel
    from utils.dependency_manager import get_faster_whisper_model_dir
    from utils.helpers import get_app_dir
    FASTER_WHISPER_AVAILABLE = True
except ImportError:
    FASTER_WHISPER_AVAILABLE = False
    debug_log("Faster-Whisper not available. Install with: pip install faster-whisper")


# Hide console window on Windows
SUBPROCESS_FLAGS = 0
if sys.platform == "win32":
    SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW




class DownloadMixin:
        def download_video(self, url: str) -> tuple:
            """Download video and subtitle with progress using yt-dlp module or executable"""
            self.log("[1/4] Downloading video & subtitle...")
        
            # Check if using yt-dlp module
            use_module = YTDLP_MODULE_AVAILABLE and self.ytdlp_path == "yt_dlp_module"
        
            if use_module:
                return self._download_video_module(url)
            else:
                return self._download_video_subprocess(url)

        def _download_video_module(self, url: str) -> tuple:
            """Download video using yt-dlp Python module API"""
            self.log(f"  Using yt-dlp module v{yt_dlp.version.__version__}")
        
            video_info = {}
        
            # Get FFmpeg and Deno paths
            ffmpeg_path = get_ffmpeg_path()
            deno_path = get_deno_path()
        
            self.log(f"  FFmpeg path: {ffmpeg_path}")
            self.log(f"  Deno path: {deno_path}")
        
            # Setup environment with Deno in PATH
            if deno_path and Path(deno_path).exists():
                deno_dir = str(Path(deno_path).parent)
                if "PATH" in os.environ:
                    os.environ["PATH"] = f"{deno_dir}{os.pathsep}{os.environ['PATH']}"
                else:
                    os.environ["PATH"] = deno_dir
                self.log(f"  Deno added to PATH: {deno_dir}")
            else:
                self.log(f"  WARNING: Deno not found!")
        
            # Progress hook for yt-dlp
            def progress_hook(d):
                if self.is_cancelled():
                    raise Exception("Cancelled by user")
            
                if d['status'] == 'downloading':
                    percent_str = d.get('_percent_str', '0%').strip()
                    # Extract numeric percent
                    match = re.search(r'(\d+\.?\d*)%', percent_str)
                    if match:
                        percent = float(match.group(1))
                        downloaded = d.get('downloaded_bytes', 0) or 0
                        total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                        speed = d.get('speed', 0) or 0
                        eta = d.get('eta')
                    
                        dl_str = self._human_bytes(downloaded)
                        if total:
                            dl_str += f" / {self._human_bytes(total)}"
                        speed_str = f"{self._human_bytes(speed)}/s" if speed else "?"
                        eta_str = f", ETA {int(eta)}s" if eta else ""
                        self.set_progress(
                            f"Downloading video... {percent:.1f}% ({dl_str}) at {speed_str}{eta_str}",
                            0.05 + percent / 100 * 0.2
                        )
                elif d['status'] == 'finished':
                    self.log("  Download finished, processing...")
                    self.set_progress("Processing downloaded file...", 0.25)
        
            # High-quality format selector
            format_selector = "bestvideo[height>=720][height<=2160]+bestaudio/best[height>=720][height<=2160]/bestvideo+bestaudio/best"
        
            # Base yt-dlp options
            ydl_opts = {
                'format': format_selector,
                'format_sort': ['res', 'br'],
                'merge_output_format': 'mp4',
                'outtmpl': str(self.temp_dir / 'source.%(ext)s'),
                'progress_hooks': [progress_hook],
                'quiet': True,
                'no_warnings': False,
                'extract_flat': False,
            }

            # aria2c Disabled globally for downloader consistency
            # import shutil
            # aria2_path = shutil.which("aria2c")
            # ...
            self.log("  Using native yt-dlp downloader (aria2c disabled).")
        
            # Only request subtitles if a real language is selected (skip for AI transcription mode)
            if self.subtitle_language and self.subtitle_language != "none":
                ydl_opts['writesubtitles'] = True
                ydl_opts['writeautomaticsub'] = True
                ydl_opts['subtitleslangs'] = [self.subtitle_language]
                ydl_opts['subtitlesformat'] = 'srt'
            else:
                self.log("  Skipping subtitle download (AI transcription mode)")
        
            # Add Deno JS runtime if available
            if deno_path and Path(deno_path).exists():
                ydl_opts['js_runtimes'] = {'deno': {'path': deno_path}}
                ydl_opts['remote_components'] = ['ejs:github']
                self.log(f"  JS runtime: deno at {deno_path}")
            else:
                self.log(f"  WARNING: Deno not found - some formats may be missing!")
        
            # Add FFmpeg location if available
            if ffmpeg_path and Path(ffmpeg_path).exists():
                ydl_opts['ffmpeg_location'] = str(Path(ffmpeg_path).parent)
                self.log(f"  FFmpeg location: {ydl_opts['ffmpeg_location']}")
            
                # Only add subtitle converter postprocessor if FFmpeg is available AND subtitles requested
                if self.subtitle_language and self.subtitle_language != "none":
                    ydl_opts['postprocessors'] = [{
                        'key': 'FFmpegSubtitlesConvertor',
                        'format': 'srt',
                    }]
            else:
                self.log(f"  WARNING: FFmpeg not found - subtitle conversion disabled")
        
            # Add cookies (required)
            from utils.helpers import get_app_dir
            app_dir = get_app_dir()
            cookies_locations = [
                Path("cookies.txt"),  # Current directory
                app_dir / "cookies.txt",  # App directory
            ]
        
            cookies_path = None
            for loc in cookies_locations:
                self.log(f"  Checking cookies at: {loc} - exists: {loc.exists()}")
                if loc.exists() and loc.stat().st_size > 0:
                    cookies_path = loc
                    break
        
            if cookies_path and cookies_path.exists() and cookies_path.stat().st_size > 0:
                ydl_opts['cookiefile'] = str(cookies_path)
                self.log(f"  Using cookies from: {cookies_path}")
            else:
                self.log("  No cookies provided, downloading publicly...")
        
            # Single download attempt (no browser cookies fallback)
            last_error = None
            try:
                self.log(f"  Starting download...")
            
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    # First get video info
                    self.log("  Fetching video info...")
                    info = ydl.extract_info(url, download=False)
                
                    if info:
                        video_info = {
                            "title": info.get("title", ""),
                            "description": (info.get("description", "") or "")[:2000],
                            "channel": info.get("channel", ""),
                        }
                        self.log(f"  Title: {video_info['title'][:50]}...")
                
                    # Now download
                    if self.subtitle_language and self.subtitle_language != "none":
                        self.log(f"  Downloading video with {self.subtitle_language} subtitle...")
                    else:
                        self.log(f"  Downloading video (no subtitle, AI transcription mode)...")
                    ydl.download([url])
            
                self.log(self.colorize("  ✓ Download successful!", "download"))
                
            except Exception as e:
                last_error = str(e)
                self.log(f"  ✗ Failed: {last_error[:100]}")
            
                # Provide helpful error message for common issues
                if "403" in last_error or "Forbidden" in last_error:
                    raise Exception(
                        "❌ ERROR: YouTube menolak akses (HTTP 403 Forbidden)\n\n"
                        "PENYEBAB:\n"
                        "• Cookies sudah EXPIRED (biasanya 1-2 minggu)\n"
                        "• Cookies tidak lengkap atau tidak valid\n"
                        "• Browser tidak login ke YouTube saat export cookies\n\n"
                        "SOLUSI:\n"
                        "1. Buka youtube.com di browser\n"
                        "2. PASTIKAN sudah LOGIN ke akun YouTube/Google\n"
                        "3. Export cookies BARU menggunakan extension:\n"
                        "   - Chrome/Edge: 'Get cookies.txt LOCALLY'\n"
                        "   - Firefox: 'cookies.txt'\n"
                        "4. Upload cookies.txt yang baru di halaman Home\n\n"
                        "📖 Lihat COOKIES.md untuk panduan lengkap"
                    )
                elif "downloaded file is empty" in last_error.lower() or "file is empty" in last_error.lower():
                    raise Exception(
                        "❌ ERROR: File video kosong (0 bytes)\n\n"
                        "PENYEBAB:\n"
                        "• YouTube mendeteksi aktivitas BOT\n"
                        "• Cookies tidak cukup kuat untuk akses video content\n"
                        "• Video mungkin memiliki proteksi khusus\n\n"
                        "SOLUSI:\n"
                        "1. Buka browser INCOGNITO/PRIVATE mode\n"
                        "2. Buka youtube.com dan LOGIN ke akun Google\n"
                        "3. Tonton 2-3 video LENGKAP (bukan skip)\n"
                        "4. Buka video yang ingin di-download, tonton sebentar\n"
                        "5. Export cookies BARU dengan extension:\n"
                        "   - Chrome/Edge: 'Get cookies.txt LOCALLY'\n"
                        "   - Firefox: 'cookies.txt'\n"
                        "6. Upload cookies.txt yang baru\n\n"
                        "💡 TIP: Gunakan akun yang aktif menonton YouTube\n"
                        "📖 Lihat COOKIES.md untuk panduan lengkap"
                    )
                elif "Sign in to confirm" in last_error or "bot" in last_error.lower():
                    raise Exception(
                        "❌ ERROR: YouTube meminta verifikasi bot\n\n"
                        "PENYEBAB:\n"
                        "• Cookies sudah tidak valid\n"
                        "• YouTube mendeteksi aktivitas mencurigakan\n\n"
                        "SOLUSI:\n"
                        "1. Buka youtube.com di browser INCOGNITO/PRIVATE\n"
                        "2. Login ke akun YouTube/Google\n"
                        "3. Tonton 1-2 video untuk 'warm up' akun\n"
                        "4. Export cookies baru\n"
                        "5. Upload cookies.txt yang baru\n\n"
                        "📖 Lihat COOKIES.md untuk panduan lengkap"
                    )
                elif "Unexpected response from webpage request" in last_error or "webpage request" in last_error.lower():
                    raise Exception(
                        "❌ ERROR: TikTok memblokir permintaan (WAF challenge)\n\n"
                        "PENYEBAB:\n"
                        "• TikTok memaksa halaman tantangan (challenge) yang tidak bisa diselesaikan yt-dlp\n"
                        "• IP/region lo bisa diblokir oleh TikTok\n"
                        "• Cookies login TikTok (sessionid) belum ada/expired\n\n"
                        "SOLUSI:\n"
                        "1. Gunakan VPN — ganti region/IP sering langsung berhasil\n"
                        "2. Export ulang cookies TikTok saat LOGIN & membuka halaman video di browser,\n"
                        "   pastikan ada `sessionid`/`sid_tt` (bukan cuma `tt_csrf_token`)\n"
                        "3. Upload cookies TikTok yang fresh via `/cookies`\n"
                        "4. Jika masih gagal, coba video TikTok lain atau tunggu update yt-dlp\n\n"
                        "🐛 Bug ini sedang dibahas di yt-dlp issue #15418 (status: aktif/belum fix)."
                    )
                else:
                    raise Exception(f"Download failed!\n\n{last_error}")
        
            video_path = self.temp_dir / "source.mp4"
            srt_path = self.temp_dir / f"source.{self.subtitle_language}.srt"
        
            if not srt_path.exists():
                # Check if any subtitle was downloaded (fallback to other languages)
                available_subs = list(self.temp_dir.glob("source.*.srt"))
                if available_subs:
                    srt_path = available_subs[0]
                    detected_lang = srt_path.stem.split('.')[-1]
                    self.log(f"  ⚠ {self.subtitle_language} subtitle not found, using {detected_lang} instead")
                else:
                    srt_path = None
                    self.log(f"  ✗ No subtitle found for language: {self.subtitle_language}")
        
            if srt_path:
                # Keep the SRT in the session folder (not _temp)
                srt_path = self._relocate_srt(srt_path)
        
            return str(video_path), str(srt_path) if srt_path else None, video_info

        def _download_video_subprocess(self, url: str) -> tuple:
            """Download video using yt-dlp subprocess (fallback)"""
            # Validate yt-dlp is available
            try:
                version_check = subprocess.run(
                    [self.ytdlp_path, "--version"],
                    capture_output=True,
                    text=True,
                    creationflags=SUBPROCESS_FLAGS,
                    timeout=5
                )
                if version_check.returncode != 0:
                    raise Exception(f"yt-dlp not working properly. Path: {self.ytdlp_path}")
                self.log(f"  Using yt-dlp version: {version_check.stdout.strip()}")
            except FileNotFoundError:
                raise Exception(f"yt-dlp not found at: {self.ytdlp_path}\n\nPlease install yt-dlp or check the path in settings.")
            except subprocess.TimeoutExpired:
                raise Exception(f"yt-dlp not responding. Path: {self.ytdlp_path}")
            except Exception as e:
                raise Exception(f"Failed to validate yt-dlp: {str(e)}")
        
            base_args = []
            try:
                help_result = subprocess.run(
                    [self.ytdlp_path, "--help"],
                    capture_output=True,
                    text=True,
                    creationflags=SUBPROCESS_FLAGS,
                    timeout=5
                )
                if help_result.returncode == 0:
                    help_text = help_result.stdout
                    if "--no-impersonate" in help_text:
                        base_args.append("--no-impersonate")
            except Exception:
                pass
        
            # Get video metadata
            self.log("  Fetching video info...")
            meta_cmd = [self.ytdlp_path, "--dump-json", "--no-download", *base_args, url]
        
            result = subprocess.run(
                meta_cmd, 
                capture_output=True, 
                text=True,
                creationflags=SUBPROCESS_FLAGS
            )
            video_info = {}
        
            if result.returncode == 0:
                try:
                    yt_data = json.loads(result.stdout)
                    video_info = {
                        "title": yt_data.get("title", ""),
                        "description": yt_data.get("description", "")[:2000],
                        "channel": yt_data.get("channel", ""),
                    }
                    self.log(f"  Title: {video_info['title'][:50]}...")
                except json.JSONDecodeError:
                    self.log("  Warning: Could not parse metadata")
        
            # Download video + subtitle with progress
            if self.subtitle_language and self.subtitle_language != "none":
                self.log(f"  Downloading video with {self.subtitle_language} subtitle...")
            else:
                self.log(f"  Downloading video (no subtitle, AI transcription mode)...")
        
            # Try multiple download strategies (fallback on failure)
            download_strategies = [
                {
                    "name": "Browser cookies (Chrome)",
                    "extra_args": ["--cookies-from-browser", "chrome"]
                },
                {
                    "name": "Browser cookies (Edge)",
                    "extra_args": ["--cookies-from-browser", "edge"]
                },
                {
                    "name": "Simple format (no auth)",
                    "extra_args": []
                }
            ]
        
            # High-quality format selector (prioritize 720p+ with fallback)
            format_selector = "bestvideo[height>=720][height<=2160]+bestaudio/best[height>=720][height<=2160]/bestvideo+bestaudio/best"
        
            last_error = None
            for strategy in download_strategies:
                if self.is_cancelled():
                    raise Exception("Cancelled by user")
            
                self.log(f"  Trying: {strategy['name']}...")
            
                cmd = [
                    self.ytdlp_path,
                    "-f", format_selector,
                    "--format-sort", "res,br",
                    *base_args,
                    *strategy["extra_args"],
                ]
            
                # Only request subtitles if a real language is selected
                if self.subtitle_language and self.subtitle_language != "none":
                    cmd.extend([
                        "--write-sub", "--write-auto-sub",
                        "--sub-lang", self.subtitle_language,
                        "--convert-subs", "srt",
                    ])
            
                cmd.extend([
                    "--merge-output-format", "mp4",
                    "--newline",
                    "-o", str(self.temp_dir / "source.%(ext)s"),
                    url
                ])
            
                # Run with realtime progress output
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    creationflags=SUBPROCESS_FLAGS
                )
            
                last_progress = ""
                output_lines = []
            
                while True:
                    if self.is_cancelled():
                        process.terminate()
                        process.wait()
                        raise Exception("Cancelled by user")
                
                    line = process.stdout.readline()
                    if not line and process.poll() is not None:
                        break
                
                    line = line.strip()
                    output_lines.append(line)
                
                    if not line:
                        continue
                    
                    # Parse download progress
                    if "[download]" in line and "%" in line:
                        percent, total, speed, eta = self._parse_ytdlp_progress_line(line)
                        if percent is not None:
                            status = self._format_ytdlp_download_status("Downloading video...", percent, total, speed, eta)
                            if status != last_progress:
                                self.set_progress(status, 0.05 + percent / 100 * 0.2)
                                last_progress = status
                    elif "[Merger]" in line or "Merging" in line:
                        self.log("  Merging video & audio...")
                        self.set_progress("Merging video & audio...", 0.25)
            
                # Check if successful
                if process.returncode == 0:
                    self.log(self.colorize(f"  ✓ Download successful using: {strategy['name']}", "download"))
                    break
                else:
                    # Capture error for logging
                    stderr_output = process.stderr.read() if process.stderr else ""
                    error_lines = []
                
                    for line in output_lines + stderr_output.split('\n'):
                        line = line.strip()
                        if line and ('ERROR' in line or 'error' in line):
                            error_lines.append(line)
                
                    last_error = '\n'.join(error_lines[-5:]) if error_lines else f"Return code {process.returncode}"
                    self.log(f"  ✗ Failed: {last_error.split(chr(10))[0][:80]}")  # First line only
                
                    # Continue to next strategy
                    continue
            else:
                # All strategies failed - provide helpful error message
                if last_error and ("403" in last_error or "Forbidden" in last_error):
                    raise Exception(
                        "❌ ERROR: YouTube menolak akses (HTTP 403 Forbidden)\n\n"
                        "PENYEBAB:\n"
                        "• Cookies sudah EXPIRED (biasanya 1-2 minggu)\n"
                        "• Cookies tidak lengkap atau tidak valid\n"
                        "• Browser tidak login ke YouTube saat export cookies\n\n"
                        "SOLUSI:\n"
                        "1. Buka youtube.com di browser\n"
                        "2. PASTIKAN sudah LOGIN ke akun YouTube/Google\n"
                        "3. Export cookies BARU menggunakan extension:\n"
                        "   - Chrome/Edge: 'Get cookies.txt LOCALLY'\n"
                        "   - Firefox: 'cookies.txt'\n"
                        "4. Upload cookies.txt yang baru di halaman Home\n\n"
                        "📖 Lihat COOKIES.md untuk panduan lengkap\n\n"
                        f"Detail error:\n{last_error}"
                    )
                elif last_error and ("downloaded file is empty" in last_error.lower() or "file is empty" in last_error.lower()):
                    raise Exception(
                        "❌ ERROR: File video kosong (0 bytes)\n\n"
                        "PENYEBAB:\n"
                        "• YouTube mendeteksi aktivitas BOT\n"
                        "• Cookies tidak cukup kuat untuk akses video content\n"
                        "• Video mungkin memiliki proteksi khusus\n\n"
                        "SOLUSI:\n"
                        "1. Buka browser INCOGNITO/PRIVATE mode\n"
                        "2. Buka youtube.com dan LOGIN ke akun Google\n"
                        "3. Tonton 2-3 video LENGKAP (bukan skip)\n"
                        "4. Buka video yang ingin di-download, tonton sebentar\n"
                        "5. Export cookies BARU dengan extension:\n"
                        "   - Chrome/Edge: 'Get cookies.txt LOCALLY'\n"
                        "   - Firefox: 'cookies.txt'\n"
                        "6. Upload cookies.txt yang baru\n\n"
                        "💡 TIP: Gunakan akun yang aktif menonton YouTube\n"
                        "📖 Lihat COOKIES.md untuk panduan lengkap\n\n"
                        f"Detail error:\n{last_error}"
                    )
                elif last_error and ("Sign in to confirm" in last_error or "bot" in last_error.lower()):
                    raise Exception(
                        "❌ ERROR: YouTube meminta verifikasi bot\n\n"
                        "PENYEBAB:\n"
                        "• Cookies sudah tidak valid\n"
                        "• YouTube mendeteksi aktivitas mencurigakan\n\n"
                        "SOLUSI:\n"
                        "1. Buka youtube.com di browser INCOGNITO/PRIVATE\n"
                        "2. Login ke akun YouTube/Google\n"
                        "3. Tonton 1-2 video untuk 'warm up' akun\n"
                        "4. Export cookies baru\n"
                        "5. Upload cookies.txt yang baru\n\n"
                        "📖 Lihat COOKIES.md untuk panduan lengkap\n\n"
                        f"Detail error:\n{last_error}"
                    )
                else:
                    raise Exception(f"Download failed after trying all methods!\n\nLast error:\n{last_error}")
        
            video_path = self.temp_dir / "source.mp4"
            srt_path = self.temp_dir / f"source.{self.subtitle_language}.srt"
        
            if not srt_path.exists():
                # Check if any subtitle was downloaded (fallback to other languages)
                available_subs = list(self.temp_dir.glob("source.*.srt"))
                if available_subs:
                    srt_path = available_subs[0]
                    detected_lang = srt_path.stem.split('.')[-1]
                    self.log(f"  ⚠ {self.subtitle_language} subtitle not found, using {detected_lang} instead")
                else:
                    srt_path = None
                    self.log(f"  ✗ No subtitle found for language: {self.subtitle_language}")
        
            if srt_path:
                # Keep the SRT in the session folder (not _temp)
                srt_path = self._relocate_srt(srt_path)
        
            return str(video_path), str(srt_path) if srt_path else None, video_info

        @staticmethod
        def get_available_subtitles(url: str, ytdlp_path: str = "yt-dlp", cookies_path: str = None) -> dict:
            """Get list of available subtitles for a YouTube video
        
            Args:
                url: YouTube video URL
                ytdlp_path: Path to yt-dlp executable or "yt_dlp_module" for module
                cookies_path: Path to cookies.txt file (required)
        
            Returns:
                dict with keys:
                    - 'subtitles': list of manual subtitle languages
                    - 'automatic_captions': list of auto-generated subtitle languages
                    - 'error': error message if failed
            """
            # Language name mapping (common ones)
            lang_names = {
                "en": "English",
                "id": "Indonesian",
                "es": "Spanish",
                "fr": "French",
                "de": "German",
                "pt": "Portuguese",
                "ru": "Russian",
                "ja": "Japanese",
                "ko": "Korean",
                "zh": "Chinese",
                "ar": "Arabic",
                "hi": "Hindi",
                "it": "Italian",
                "nl": "Dutch",
                "pl": "Polish",
                "tr": "Turkish",
                "vi": "Vietnamese",
                "th": "Thai",
            }
        
            # Check if using yt-dlp module
            use_module = YTDLP_MODULE_AVAILABLE and ytdlp_path == "yt_dlp_module"
        
            if use_module:
                return AutoClipperCore._get_subtitles_module(url, cookies_path, lang_names)
            else:
                return AutoClipperCore._get_subtitles_subprocess(url, ytdlp_path, cookies_path, lang_names)

        @staticmethod
        def _get_subtitles_module(url: str, cookies_path: str, lang_names: dict) -> dict:
            """Get subtitles using yt-dlp Python module API"""
            try:
                # Check if cookies.txt exists
                if not cookies_path or not Path(cookies_path).exists():
                    return {
                        "error": "cookies.txt not found. Please upload cookies.txt file.",
                        "subtitles": [],
                        "automatic_captions": []
                    }
            
                # Bypass validation
                found_cookies = ['bypass']
            
                debug_log(f"Using yt-dlp module v{yt_dlp.version.__version__}")
                debug_log(f"Cookies path: {cookies_path} (exists: {Path(cookies_path).exists()})")
            
                # Setup Deno in PATH if available
                deno_path = get_deno_path()
                ffmpeg_path = get_ffmpeg_path()
            
                if deno_path and Path(deno_path).exists():
                    deno_dir = str(Path(deno_path).parent)
                    if "PATH" in os.environ:
                        if deno_dir not in os.environ["PATH"]:
                            os.environ["PATH"] = f"{deno_dir}{os.pathsep}{os.environ['PATH']}"
                    else:
                        os.environ["PATH"] = deno_dir
                    debug_log(f"Deno path added: {deno_dir}")
            
                # yt-dlp options for fetching info only
                # NOTE: Don't use player_client=android with cookies - it bypasses cookie auth
                ydl_opts = {
                    'skip_download': True,
                    'quiet': False,  # Show warnings for debugging
                    'no_warnings': False,
                    'cookiefile': str(cookies_path),  # Ensure string path
                    'nocheckcertificate': False,
                    'prefer_insecure': False,
                    'socket_timeout': 30,
                    'retries': 5,
                    'http_headers': {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
                    },
                }
            
                # aria2c Disabled globally for downloader consistency
                # import shutil
                # aria2_path = shutil.which("aria2c")
                # if aria2_path:
                #     debug_log(f"Detected aria2c at: {aria2_path}. Using for metadata extraction.")
                #     ydl_opts['external_downloader'] = aria2_path
                #     ydl_opts['external_downloader_args'] = {'default': ['-x', '16', '-s', '16', '-k', '1M']}
                debug_log("Using native yt-dlp downloader (aria2c disabled).")
            
                # Add Deno JS runtime if available
                if deno_path and Path(deno_path).exists():
                    ydl_opts['js_runtimes'] = {'deno': {'path': deno_path}}
                    ydl_opts['remote_components'] = ['ejs:github']
                    debug_log(f"JS runtime: deno at {deno_path}")
            
                # Add FFmpeg location if available
                if ffmpeg_path and Path(ffmpeg_path).exists():
                    ydl_opts['ffmpeg_location'] = str(Path(ffmpeg_path).parent)
                    debug_log(f"FFmpeg location: {ydl_opts['ffmpeg_location']}")
            
                debug_log(f"yt-dlp opts: cookiefile={ydl_opts['cookiefile']}")
            
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    video_data = ydl.extract_info(url, download=False)
            
                if not video_data:
                    return {"error": "Failed to fetch video info", "subtitles": [], "automatic_captions": []}
            
                # Extract subtitles (exclude live_chat)
                subtitles = []
                auto_captions = []
            
                # Get manual subtitles
                if "subtitles" in video_data and video_data["subtitles"]:
                    for lang_code in video_data["subtitles"].keys():
                        if "live_chat" in lang_code:
                            continue
                        lang_name = lang_names.get(lang_code, lang_code.upper())
                        subtitles.append({"code": lang_code, "name": lang_name})
            
                # Get automatic captions
                if "automatic_captions" in video_data and video_data["automatic_captions"]:
                    for lang_code in video_data["automatic_captions"].keys():
                        if "live_chat" in lang_code:
                            continue
                        lang_name = lang_names.get(lang_code, lang_code.upper())
                        auto_captions.append({"code": lang_code, "name": lang_name})
            
                return {
                    "subtitles": subtitles,
                    "automatic_captions": auto_captions,
                    "error": None
                }
            
            except Exception as e:
                debug_log(f"yt-dlp module error: {e}")
                return {"error": str(e), "subtitles": [], "automatic_captions": []}

        @staticmethod
        def _get_subtitles_subprocess(url: str, ytdlp_path: str, cookies_path: str, lang_names: dict) -> dict:
            """Get subtitles using yt-dlp subprocess (fallback)"""
            try:
                # Check if cookies.txt exists
                if not cookies_path or not Path(cookies_path).exists():
                    return {
                        "error": "cookies.txt not found. Please upload cookies.txt file.",
                        "subtitles": [],
                        "automatic_captions": []
                    }
            
                # Setup environment with Deno path if available
                env = os.environ.copy()
                deno_path = get_deno_path()
                if deno_path:
                    deno_dir = str(Path(deno_path).parent)
                    if "PATH" in env:
                        env["PATH"] = f"{deno_dir}{os.pathsep}{env['PATH']}"
                    else:
                        env["PATH"] = deno_dir
                    debug_log(f"Deno found at: {deno_path}")
                else:
                    debug_log("Deno not found - remote-components may not work")
            
                # Use --dump-json to get structured data
                # NOTE: Don't use player_client=android with cookies - it bypasses cookie auth
                cmd = [ytdlp_path, "--dump-json", "--skip-download", 
                       "--cookies", cookies_path]
            
                # Check for remote-components support (requires Deno)
                try:
                    help_result = subprocess.run(
                        [ytdlp_path, "--help"],
                        capture_output=True,
                        text=True,
                        creationflags=SUBPROCESS_FLAGS,
                        timeout=5
                    )
                    if help_result.returncode == 0:
                        help_text = help_result.stdout
                    
                        # Add remote-components if supported AND Deno is available
                        if "--remote-components" in help_text and deno_path:
                            cmd.extend(["--remote-components", "ejs:github"])
                            debug_log("Added --remote-components ejs:github")
                    
                        # Add no-impersonate if supported
                        if "--no-impersonate" in help_text:
                            cmd.append("--no-impersonate")
                            debug_log("Added --no-impersonate flag")
                except Exception as e:
                    debug_log(f"Error checking yt-dlp features: {e}")
            
                # Add URL at the end
                cmd.append(url)
            
                # Log command for debugging
                debug_log(f"Running command: {' '.join(cmd)}")
            
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    creationflags=SUBPROCESS_FLAGS,
                    env=env,  # Use modified environment with Deno path
                    timeout=30  # Add timeout to prevent hanging
                )
            
                if result.returncode != 0:
                    # Log stderr for debugging
                    error_msg = result.stderr.strip() if result.stderr else "Unknown error"
                    debug_log(f"yt-dlp stderr: {error_msg}")
                    return {"error": f"Failed to fetch video info: {error_msg[:100]}", "subtitles": [], "automatic_captions": []}
            
                # Parse JSON output
                video_data = json.loads(result.stdout)
            
                # Extract subtitles (exclude live_chat)
                subtitles = []
                auto_captions = []
            
                # Get manual subtitles
                if "subtitles" in video_data and video_data["subtitles"]:
                    for lang_code in video_data["subtitles"].keys():
                        if "live_chat" in lang_code:
                            continue
                        lang_name = lang_names.get(lang_code, lang_code.upper())
                        subtitles.append({"code": lang_code, "name": lang_name})
            
                # Get automatic captions
                if "automatic_captions" in video_data and video_data["automatic_captions"]:
                    for lang_code in video_data["automatic_captions"].keys():
                        if "live_chat" in lang_code:
                            continue
                        lang_name = lang_names.get(lang_code, lang_code.upper())
                        auto_captions.append({"code": lang_code, "name": lang_name})
            
                return {
                    "subtitles": subtitles,
                    "automatic_captions": auto_captions,
                    "error": None
                }
            
            except subprocess.TimeoutExpired:
                return {"error": "Timeout fetching subtitles", "subtitles": [], "automatic_captions": []}
            except json.JSONDecodeError:
                return {"error": "Failed to parse video data", "subtitles": [], "automatic_captions": []}
            except Exception as e:
                return {"error": str(e), "subtitles": [], "automatic_captions": []}

        def _srt_output_dir(self) -> Path:
            """Directory where the session SRT file should live.

            SRT files belong in the session folder itself (not _temp) so they are
            easy to find and survive across retries. Falls back to temp_dir when
            no session directory is known yet.
            """
            if getattr(self, 'last_session_dir', None):
                return Path(self.last_session_dir)
            return self.temp_dir

        def _srt_to_sec(self, srt_time: str) -> float:
            try:
                m=re.match(r'(\d+):(\d+):(\d+)[,\.](\d+)', str(srt_time))
                if m: return int(m.group(1))*3600+int(m.group(2))*60+int(m.group(3))+int(m.group(4))/1000
                m=re.match(r'(\d+):(\d+):(\d+)', str(srt_time))
                if m: return int(m.group(1))*3600+int(m.group(2))*60+int(m.group(3))
            except Exception: pass
            return 0

        def _download_full_video(self, url: str, out_path: str):
            import yt_dlp
            # cari cookies.txt (TikTok/FB sering butuh login cookie sessionid)
            _app = Path(self.output_dir).parent if getattr(self, 'output_dir', None) else Path.cwd()
            _cookies = None
            for _loc in [Path('cookies.txt'), _app / 'cookies.txt']:
                if _loc.exists():
                    _cookies = str(_loc)
                    break
            # TikTok: simple best, no youtube extractor args
            ydl_opts={'outtmpl': out_path, 'format': 'best', 'quiet': False, 'ffmpeg_location': self.ffmpeg_path}
            if 'tiktok.com' in url:
                ydl_opts={'outtmpl': out_path, 'quiet': False, 'ffmpeg_location': self.ffmpeg_path}
            if _cookies:
                ydl_opts['cookiefile'] = _cookies
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            if not Path(out_path).exists():
                # yt-dlp may add extension
                for ext in ['.mp4','.mkv','.webm']:
                    if Path(out_path+ext).exists(): return
                raise Exception('Full video download failed')

        def _relocate_srt(self, srt_path) -> str:
            """Move an SRT file from _temp into the session folder (if different)."""
            try:
                srt_path = Path(srt_path)
                if not srt_path.exists():
                    return str(srt_path) if srt_path.exists() else None
                dest_dir = self._srt_output_dir()
                if srt_path.parent.resolve() == dest_dir.resolve():
                    return str(srt_path)
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / srt_path.name
                if dest.exists() and dest.resolve() != srt_path.resolve():
                    dest.unlink()
                shutil.move(str(srt_path), str(dest))
                self.log(f"  📄 SRT moved to session folder: {dest.name}")
                return str(dest)
            except Exception as e:
                self.log(f"  ⚠ Could not move SRT: {e}")
                return str(srt_path)

        def download_subtitle_only(self, url: str) -> tuple:
            """Download only subtitle (no video) using yt-dlp.
        
            Returns:
                tuple: (srt_path, video_info) where srt_path is str or None
            """
            self.log("[1/2] Downloading subtitle only...")
        
            use_module = YTDLP_MODULE_AVAILABLE and self.ytdlp_path == "yt_dlp_module"
        
            if use_module:
                return self._download_subtitle_only_module(url)
            else:
                return self._download_subtitle_only_subprocess(url)

        def _download_subtitle_only_module(self, url: str) -> tuple:
            """Download subtitle only using yt-dlp Python module API"""
            self.log(f"  Using yt-dlp module v{yt_dlp.version.__version__}")
        
            video_info = {}
        
            # Get Deno path
            deno_path = get_deno_path()
            ffmpeg_path = get_ffmpeg_path()
        
            # Setup environment with Deno in PATH
            if deno_path and Path(deno_path).exists():
                deno_dir = str(Path(deno_path).parent)
                if "PATH" in os.environ:
                    if deno_dir not in os.environ["PATH"]:
                        os.environ["PATH"] = f"{deno_dir}{os.pathsep}{os.environ['PATH']}"
                else:
                    os.environ["PATH"] = deno_dir
        
            # yt-dlp options: skip video download, only get subtitle
            ydl_opts = {
                'skip_download': True,
                'writesubtitles': True,
                'writeautomaticsub': True,
                'subtitleslangs': [self.subtitle_language],
                'subtitlesformat': 'srt',
                'outtmpl': str(self._srt_output_dir() / 'source.%(ext)s'),
                'quiet': True,
                'no_warnings': False,
            }
        
            # aria2c Disabled globally for downloader consistency
            # import shutil
            # aria2_path = shutil.which("aria2c")
            # if aria2_path:
            #     self.log(f"  Detected aria2c at: {aria2_path}. Using for subtitle download.")
            #     ydl_opts['external_downloader'] = aria2_path
            #     ydl_opts['external_downloader_args'] = {'default': ['-x', '16', '-s', '16', '-k', '1M']}
            self.log("  Using native yt-dlp downloader for subtitle (aria2c disabled).")
        
            # Add Deno JS runtime if available
            if deno_path and Path(deno_path).exists():
                ydl_opts['js_runtimes'] = {'deno': {'path': deno_path}}
                ydl_opts['remote_components'] = ['ejs:github']
        
            # With cookies yt-dlp defaults to web_creator which 403s (needs PO
            # token). Use clients that don't require one.
            ydl_opts['extractor_args'] = {
                'youtube': {
                    'player_client': ['web', 'web_embedded', 'tv_embedded', 'android_vr', 'web_safari']
                }
            }
        
            # Add FFmpeg location for subtitle conversion
            if ffmpeg_path and Path(ffmpeg_path).exists():
                ydl_opts['ffmpeg_location'] = str(Path(ffmpeg_path).parent)
        
            # Add cookies
            from utils.helpers import get_app_dir
            app_dir = get_app_dir()
            cookies_path = None
            for loc in [Path("cookies.txt"), app_dir / "cookies.txt"]:
                if loc.exists() and loc.stat().st_size > 0:
                    cookies_path = loc
                    break
        
            if cookies_path and cookies_path.exists() and cookies_path.stat().st_size > 0:
                ydl_opts['cookiefile'] = str(cookies_path)
            else:
                self.log("  No cookies provided, downloading subtitle publicly...")
        
            try:
                self.log(f"  Downloading {self.subtitle_language} subtitle...")
            
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    # Get video info + download subtitle
                    info = ydl.extract_info(url, download=True)
                
                    if info:
                        video_info = {
                            "title": info.get("title", ""),
                            "description": (info.get("description", "") or "")[:2000],
                            "channel": info.get("channel", ""),
                        }
                        self.log(f"  Title: {video_info['title'][:50]}...")
            
                self.log(f"  ✓ Subtitle download complete!")
            
            except Exception as e:
                last_error = str(e)
                self.log(f"  ✗ Failed: {last_error[:100]}")
            
                if "403" in last_error or "Forbidden" in last_error:
                    raise Exception(
                        "❌ ERROR: YouTube menolak akses (HTTP 403 Forbidden)\n\n"
                        "Cookies sudah EXPIRED. Silakan export cookies baru.\n\n"
                        "📖 Lihat COOKIES.md untuk panduan lengkap"
                    )
                else:
                    raise Exception(f"Subtitle download failed!\n\n{last_error}")
        
            # Find downloaded subtitle file
            srt_path = self._srt_output_dir() / f"source.{self.subtitle_language}.srt"
        
            if not srt_path.exists():
                available_subs = list(self._srt_output_dir().glob("source.*.srt"))
                if available_subs:
                    srt_path = available_subs[0]
                    detected_lang = srt_path.stem.split('.')[-1]
                    self.log(f"  ⚠ {self.subtitle_language} subtitle not found, using {detected_lang} instead")
                else:
                    srt_path = None
                    self.log(f"  ✗ No subtitle found for language: {self.subtitle_language}")
        
            return str(srt_path) if srt_path else None, video_info

        def _download_subtitle_only_subprocess(self, url: str) -> tuple:
            """Download subtitle only using yt-dlp subprocess (fallback)"""
            # Validate yt-dlp
            try:
                version_check = subprocess.run(
                    [self.ytdlp_path, "--version"],
                    capture_output=True, text=True,
                    creationflags=SUBPROCESS_FLAGS, timeout=5
                )
                if version_check.returncode != 0:
                    raise Exception(f"yt-dlp not working properly. Path: {self.ytdlp_path}")
                self.log(f"  Using yt-dlp version: {version_check.stdout.strip()}")
            except FileNotFoundError:
                raise Exception(f"yt-dlp not found at: {self.ytdlp_path}")
        
            # Get video metadata first
            self.log("  Fetching video info...")
            meta_cmd = [self.ytdlp_path, "--dump-json", "--no-download", url]
        
            # Add cookies
            from utils.helpers import get_app_dir
            app_dir = get_app_dir()
            cookies_path = None
            for loc in [Path("cookies.txt"), app_dir / "cookies.txt"]:
                if loc.exists() and loc.stat().st_size > 0:
                    cookies_path = str(loc)
                    break
        
            if cookies_path:
                meta_cmd.extend(["--cookies", cookies_path])
        
            result = subprocess.run(
                meta_cmd, capture_output=True, text=True,
                creationflags=SUBPROCESS_FLAGS, timeout=30
            )
        
            video_info = {}
            if result.returncode == 0:
                try:
                    yt_data = json.loads(result.stdout)
                    video_info = {
                        "title": yt_data.get("title", ""),
                        "description": yt_data.get("description", "")[:2000],
                        "channel": yt_data.get("channel", ""),
                    }
                    self.log(f"  Title: {video_info['title'][:50]}...")
                except json.JSONDecodeError:
                    self.log("  Warning: Could not parse metadata")
        
            # Download subtitle only
            self.log(f"  Downloading {self.subtitle_language} subtitle...")
            cmd = [
                self.ytdlp_path,
                "--skip-download",
                "--write-sub", "--write-auto-sub",
                "--sub-lang", self.subtitle_language,
                "--convert-subs", "srt",
                "-o", str(self._srt_output_dir() / "source.%(ext)s"),
            ]
        
            if cookies_path:
                cmd.extend(["--cookies", cookies_path])
        
            # With cookies yt-dlp defaults to web_creator which 403s (needs PO
            # token). Use clients that don't require one.
            cmd.extend([
                "--extractor-args",
                "youtube:player_client=web,web_embedded,tv_embedded,android_vr,web_safari"
            ])
        
            cmd.append(url)
        
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                creationflags=SUBPROCESS_FLAGS, timeout=30
            )
        
            if result.returncode != 0:
                error_msg = result.stderr.strip() if result.stderr else "Unknown error"
                self.log(f"  ✗ Failed: {error_msg[:100]}")
                raise Exception(f"Subtitle download failed!\n\n{error_msg}")
        
            self.log(f"  ✓ Subtitle download complete!")
        
            # Find downloaded subtitle file
            srt_path = self._srt_output_dir() / f"source.{self.subtitle_language}.srt"
        
            if not srt_path.exists():
                available_subs = list(self._srt_output_dir().glob("source.*.srt"))
                if available_subs:
                    srt_path = available_subs[0]
                    detected_lang = srt_path.stem.split('.')[-1]
                    self.log(f"  ⚠ {self.subtitle_language} subtitle not found, using {detected_lang} instead")
                else:
                    srt_path = None
                    self.log(f"  ✗ No subtitle found for language: {self.subtitle_language}")
        
            return str(srt_path) if srt_path else None, video_info

        def download_video_section(self, url: str, start_time: str, end_time: str, output_path: str, resolution: str = "1080p") -> str:
            """Download a specific section of a video using yt-dlp --download-sections.
        
            Args:
                url: YouTube video URL
                start_time: Start timestamp (HH:MM:SS,mmm or HH:MM:SS.mmm)
                end_time: End timestamp (HH:MM:SS,mmm or HH:MM:SS.mmm)
                output_path: Path to save the downloaded section
                resolution: Target resolution (1080p, 720p, 480p, 360p, 144p) or "auto"
                to use the best resolution available on the server.
            
            Returns:
                str: Path to downloaded video file
            """
            # Normalize timestamps (replace comma with dot for yt-dlp)
            start_clean = start_time.replace(",", ".")
            end_clean = end_time.replace(",", ".")

            # Only use yt-dlp --download-sections via subprocess (built-in retry +
            # hang detection). No fallback strategies.
            return self._download_section_subprocess(
                url, start_clean, end_clean, output_path, resolution
            )

        def fetch_video_info(self, url: str) -> dict:
            """Lightweight fetch of video metadata (title, channel, description).

            Used to repair sessions whose session_data.json has video_info: null
            (e.g. sessions created before the metadata was persisted).

            Returns:
                dict with keys: title, description, channel (may be empty strings)
            """
            import yt_dlp as _yt
            from utils.helpers import get_app_dir

            opts = {
                'quiet': True,
                'no_warnings': True,
                'skip_download': True,
                'noplaylist': True,
            }

            # Add Deno JS runtime (needed for some extractors)
            deno_path = get_deno_path()
            if deno_path and Path(deno_path).exists():
                opts['js_runtimes'] = {'deno': {'path': deno_path}}
                opts['remote_components'] = ['ejs:github']

            # Add cookies
            app_dir = get_app_dir()
            for loc in [Path("cookies.txt"), app_dir / "cookies.txt"]:
                if loc.exists() and loc.stat().st_size > 0:
                    opts['cookiefile'] = str(loc)
                    break

            # With cookies yt-dlp defaults to web_creator which 403s (needs PO
            # token). Use clients that don't require one.
            opts['extractor_args'] = {
                'youtube': {
                    'player_client': ['web', 'web_embedded', 'tv_embedded', 'android_vr', 'web_safari']
                }
            }

            with _yt.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)

            return {
                "title": info.get("title", ""),
                "description": (info.get("description", "") or "")[:2000],
                "channel": info.get("channel", ""),
            }

        def _server_available_heights(self, url: str) -> list:
            """Query the server for available video heights (resolutions) without downloading."""
            import yt_dlp as _yt
            from utils.helpers import get_app_dir
        
            opts = {
                'quiet': True,
                'no_warnings': True,
                'skip_download': True,
                'noplaylist': True,
            }
        
            # Add Deno JS runtime (needed for some extractors)
            deno_path = get_deno_path()
            if deno_path and Path(deno_path).exists():
                opts['js_runtimes'] = {'deno': {'path': deno_path}}
                opts['remote_components'] = ['ejs:github']
        
            # Add cookies
            app_dir = get_app_dir()
            for loc in [Path("cookies.txt"), app_dir / "cookies.txt"]:
                if loc.exists() and loc.stat().st_size > 0:
                    opts['cookiefile'] = str(loc)
                    break
        
            # With cookies yt-dlp defaults to web_creator which 403s (needs PO
            # token). Use clients that don't require one.
            opts['extractor_args'] = {
                'youtube': {
                    'player_client': ['web', 'web_embedded', 'tv_embedded', 'android_vr', 'web_safari']
                }
            }
        
            with _yt.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        
            heights = sorted({
                f.get('height') for f in info.get('formats', [])
                if f.get('height') and f.get('vcodec') != 'none'
            })
            return heights

        def _resolve_target_height(self, url: str, resolution: str) -> int:
            """Resolve the requested resolution to an actual target height.
        
            For "auto" (or when the requested height is not on the server),
            returns the best height the server actually offers (capped at 2160).
            """
            res = str(resolution or "").strip().lower()
            res_map = {"2160p": 2160, "1440p": 1440, "1080p": 1080, "720p": 720, "480p": 480, "360p": 360, "240p": 240, "144p": 144}
            is_auto = res in ("auto", "auto (best)", "best", "otomatis")
            target_h = res_map.get(res, 1080)
        
            try:
                heights = self._server_available_heights(url)
            except Exception as e:
                self.log(f"  ⚠ Could not detect server resolutions: {str(e)[:80]}")
                return target_h
        
            if heights:
                self.log(f"  Server offers: {', '.join(f'{h}p' for h in heights)}")
                if is_auto:
                    target_h = min(max(heights), 2160)
                    self.log(f"  Auto resolution → {target_h}p (best available)")
                elif target_h not in heights:
                    fallback = max([h for h in heights if h <= target_h] or [min(heights)])
                    self.log(f"  ⚠ {target_h}p not available, using {fallback}p instead")
                    target_h = fallback
            return target_h

        def _start_download_monitor(self, part_patterns: list, label: str, progress_value: float):
            """Start a background thread that polls the growing .part file size and
            reports downloaded MB + speed to the UI.

            Some yt-dlp downloads (short clips via download_ranges) never emit
            'downloading' progress events — only 'finished' — so polling the file
            size is the reliable way to show live progress.

            Returns a threading.Event used to stop the monitor.
            """
            import glob as _glob
            import threading as _threading
            import time as _time
        
            stop = _threading.Event()
        
            def monitor():
                last_size = 0
                last_t = _time.time()
                while not stop.is_set():
                    _time.sleep(1.0)
                    size = 0
                    for pat in part_patterns:
                        try:
                            for f in _glob.glob(pat):
                                try:
                                    size += os.path.getsize(f)
                                except OSError:
                                    pass
                        except Exception:
                            pass
                    now = _time.time()
                    dt = now - last_t
                    speed = (size - last_size) / dt if dt > 0 and size >= last_size else 0
                    last_size, last_t = size, now
                    self.set_progress(
                        f"{label} {self._human_bytes(size)} at {self._human_bytes(speed)}/s",
                        progress_value
                    )
        
            t = _threading.Thread(target=monitor, daemon=True)
            t.start()
            return stop

        def _download_section_module(self, url: str, start_time: str, end_time: str, output_path: str, resolution: str = "1080p") -> str:
            self.log(f"  Downloading section {start_time} → {end_time} ({resolution})...")
        
            # Get paths
            ffmpeg_path = get_ffmpeg_path()
            deno_path = get_deno_path()
            # Setup Deno in PATH
            if deno_path and Path(deno_path).exists():
                deno_dir = str(Path(deno_path).parent)
                if "PATH" in os.environ:
                    if deno_dir not in os.environ["PATH"]:
                        os.environ["PATH"] = f"{deno_dir}{os.pathsep}{os.environ['PATH']}"
                else:
                    os.environ["PATH"] = deno_dir
        
            # Progress hook
            def progress_hook(d):
                if self.is_cancelled():
                    raise Exception("Cancelled by user")
            
                if d['status'] == 'downloading':
                    speed = d.get('speed', 0) or 0
                    speed_str = f"{self._human_bytes(speed)}/s" if speed else "?"
                    downloaded = d.get('downloaded_bytes') or 0
                    total = (d.get('total_bytes') or d.get('total_bytes_estimate') or 0)
                    if total:
                        pct = min(downloaded / total, 1.0)
                    else:
                        pct = 0
                    dl_str = self._human_bytes(downloaded)
                    if total:
                        dl_str += f" / {self._human_bytes(total)}"
                    eta = d.get('eta')
                    eta_str = f", ETA {int(eta)}s" if eta else ""
                    self.log(f"  yt-dlp {dl_str} ({pct*100:.0f}%) {speed_str}{eta_str}")
                    self.set_progress(
                        f"Downloading clip... {pct*100:.0f}% ({dl_str}) at {speed_str}{eta_str}",
                        pct
                    )
                elif d['status'] == 'finished':
                    self.log("  Section download finished, processing...")
        
            # Format selector based on chosen resolution (auto = best on server)
            target_h = self._resolve_target_height(url, resolution)
            format_selector = (
                f"bestvideo[height<={target_h}]+bestaudio/"
                f"best[height<={target_h}]/bestvideo+bestaudio/best"
            )
        
            ydl_opts = {
                'format': format_selector,
                'format_sort': ['res', 'br'],
                'max_filesize': None,
                'merge_output_format': 'mp4',
                'outtmpl': output_path,
                'progress_hooks': [progress_hook],
                'quiet': True,
                'no_warnings': False,
                'download_ranges': yt_dlp.utils.download_range_func(None, [(
                    self.parse_timestamp(start_time),
                    self.parse_timestamp(end_time)
                )]),
                'force_keyframes_at_cuts': False,
                # With cookies yt-dlp defaults to web_creator which 403s (needs PO
                # token). Use clients that don't require one.
                'extractor_args': {
                    'youtube': {
                        'player_client': ['web', 'web_embedded', 'tv_embedded', 'android_vr', 'web_safari']
                    }
                },
            }

            # Inject aria2c as external downloader - DISABLED
            # aria2_path = shutil.which("aria2c")
            # if aria2_path:
            #     self.log(f"  Detected aria2c at: {aria2_path}. Using for range download.")
            #     ydl_opts['external_downloader'] = aria2_path
            #     ydl_opts['external_downloader_args'] = {'default': ['-x', '16', '-s', '16', '-k', '1M']}
        
            # Add Deno JS runtime
            if deno_path and Path(deno_path).exists():
                ydl_opts['js_runtimes'] = {'deno': {'path': deno_path}}
                ydl_opts['remote_components'] = ['ejs:github']
        
            # Add FFmpeg location
            if ffmpeg_path and Path(ffmpeg_path).exists():
                ydl_opts['ffmpeg_location'] = str(Path(ffmpeg_path).parent)
        
            # Add cookies
            from utils.helpers import get_app_dir
            app_dir = get_app_dir()
            cookies_path = None
            for loc in [Path("cookies.txt"), app_dir / "cookies.txt"]:
                if loc.exists() and loc.stat().st_size > 0:
                    cookies_path = loc
                    break
        
            if not cookies_path:
                raise Exception("cookies.txt not found!")
        
            ydl_opts['cookiefile'] = str(cookies_path)
        
            stop_monitor = None
            try:
                if ydl_opts.get('external_downloader'):
                    del ydl_opts['external_downloader']
                    del ydl_opts['external_downloader_args']

                stop_monitor = self._start_download_monitor(
                    [output_path + "*.part*", output_path + ".part"],
                    "Downloading clip...", 0
                )
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
            
                self.log(f"  ✓ Section downloaded!")
            
            except Exception as e:
                last_error = str(e)
                self.log(f"  ✗ Section download failed: {last_error[:100]}")
                # Fallback: if download-sections approach fails, download full video and trim with ffmpeg
                self.log("  Falling back to full download + ffmpeg trim...")
                try:
                    return self._download_and_trim_fallback(url, start_clean, end_clean, output_path, resolution, ffmpeg_path)
                except Exception as fallback_err:
                    self.log(f"  ✗ Fallback also failed: {fallback_err}")
                    raise Exception(
                        f"Failed to download video section!\n\n"
                        f"Primary error: {last_error}\n\n"
                        f"Fallback error: {fallback_err}"
                    )
            finally:
                if stop_monitor is not None:
                    stop_monitor.set()
        
            # Find the actual output file (yt-dlp may add extension)
            output_dir = Path(output_path).parent
            output_stem = Path(output_path).stem
        
            # Check for exact match first
            if Path(output_path).exists():
                return output_path
        
            # Check for .mp4 variant
            mp4_path = output_dir / f"{output_stem}.mp4"
            if mp4_path.exists():
                return str(mp4_path)
        
            # Search for any file with the stem
            candidates = list(output_dir.glob(f"{output_stem}.*"))
            video_candidates = [c for c in candidates if c.suffix in ('.mp4', '.mkv', '.webm')]
            if video_candidates:
                return str(video_candidates[0])
        
            raise Exception(f"Downloaded section file not found at: {output_path}")

        def _ytdlp_command_prefix(self) -> list:
            """Return command prefix for running yt-dlp.

            Supports both the standalone binary and the Python module mode
            (ytdlp_path == "yt_dlp_module"), which runs `python -m yt_dlp`.
            """
            if self.ytdlp_path and self.ytdlp_path != "yt_dlp_module":
                return [self.ytdlp_path]
            return [sys.executable, "-m", "yt_dlp"]

        def _download_section_subprocess(self, url: str, start_time: str, end_time: str, output_path: str, resolution: str = "1080p") -> str:
            """Download video section using yt-dlp subprocess (fallback).

            Includes retry + hang detection: if the .part file stops growing for
            several seconds (or a hard timeout is exceeded) while yt-dlp is still
            running, the process is killed and the download is retried.
            """
            self.log(f"  Downloading section {start_time} → {end_time} ({resolution})...")

            target_h = self._resolve_target_height(url, resolution)
            # Force a sensible minimum (clips/shorts look bad below 720p) while
            # still respecting the requested max resolution.
            min_h = 720 if target_h >= 720 else target_h
            format_selector = (
                f"bestvideo[height>={min_h}][height<={target_h}]+bestaudio/"
                f"best[height>={min_h}][height<={target_h}]/"
                f"bestvideo+bestaudio/best"
            )

            section_str = f"*{start_time}-{end_time}"
            output_path = Path(output_path)
            output_dir = output_path.parent
            output_stem = output_path.stem

            MAX_ATTEMPTS = 3
            total_section_secs = max(
                (self.parse_timestamp(end_time) - self.parse_timestamp(start_time)) or 1.0,
                1.0
            )

            # Strategy list mirroring the full-video downloader (cookies fallbacks)
            from utils.helpers import get_app_dir
            app_dir = get_app_dir()
            cookies_path = None
            for loc in [Path("cookies.txt"), app_dir / "cookies.txt"]:
                if loc.exists() and loc.stat().st_size > 0:
                    cookies_path = str(loc)
                    break

            last_error = None
            for attempt in range(1, MAX_ATTEMPTS + 1):
                if self.is_cancelled():
                    raise Exception("Cancelled by user")

                # Remove stale .part files from a previous attempt
                for stale in list(output_dir.glob(f"{output_stem}.*")):
                    try:
                        if stale.exists():
                            stale.unlink()
                    except OSError:
                        pass

                cmd = self._ytdlp_command_prefix() + [
                    "-f", format_selector,
                    "--format-sort", "res,br",
                    "--newline",
                    "--download-sections", section_str,
                    "--merge-output-format", "mp4",
                    "-o", str(output_path),
                ]
                ffmpeg_path = get_ffmpeg_path()
                if ffmpeg_path and Path(ffmpeg_path).exists():
                    cmd.extend(["--ffmpeg-location", str(Path(ffmpeg_path).parent)])
                if cookies_path:
                    cmd.extend(["--cookies", cookies_path])
                # With cookies yt-dlp defaults to web_creator which 403s (needs PO
                # token). Use clients that don't require one.
                cmd.extend([
                    "--extractor-args",
                    "youtube:player_client=web,web_embedded,tv_embedded,android_vr,web_safari"
                ])
                cmd.append(url)

                self.log(f"  Running (attempt {attempt}/{MAX_ATTEMPTS}):")
                self.log("    " + " ".join(cmd))

                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    creationflags=SUBPROCESS_FLAGS,
                )

                # Stream both streams so progress reaches the bot. yt-dlp writes
                # '[download] xx%' on stdout, but stream-copy section cuts output
                # ffmpeg's 'time=...' progress on stderr. Handle both.
                # Also collect all stderr lines into a buffer for error reporting
                # (drain threads consume the pipe, so process.stderr.read() is empty after).
                _stderr_buf = []

                def _drain_stream(stream, is_stderr: bool = False):
                    try:
                        for raw in stream:
                            line = raw.strip()
                            if is_stderr:
                                _stderr_buf.append(line)
                            if not line:
                                continue
                            if "[download]" in line and "%" in line:
                                percent, total, speed, eta = self._parse_ytdlp_progress_line(line)
                                if percent is not None:
                                    self.set_progress(
                                        self._format_ytdlp_download_status("Downloading clip...", percent, total, speed, eta),
                                        0.05 + (percent / 100) * 0.9
                                    )
                            elif is_stderr and "time=" in line:
                                m = re.search(r'time=(\d+):(\d+):(\d+(?:\.\d+)?)', line)
                                if m:
                                    elapsed = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                                    pct = min(elapsed / total_section_secs, 1.0)
                                    speed_m = re.search(r'speed=([0-9.]+)x', line)
                                    speed_str = f"{speed_m.group(1)}x" if speed_m else ""
                                    self.set_progress(
                                        f"Downloading clip... {pct*100:.0f}% ({speed_str})",
                                        0.05 + pct * 0.9
                                    )
                    except Exception:
                        pass

                threads = [
                    threading.Thread(target=_drain_stream, args=(process.stdout, False), daemon=True),
                    threading.Thread(target=_drain_stream, args=(process.stderr, True), daemon=True),
                ]
                for t in threads:
                    t.start()

                # Wait loop with cancellation + a hard timeout so a stalled
                # yt-dlp (e.g. info-extraction hang on some YouTube videos)
                # can't block the whole pipeline forever.
                start_time = time.time()
                download_timeout = 300  # seconds; stall is usually immediate, but allow real downloads
                while process.poll() is None:
                    if self.is_cancelled():
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except Exception:
                            process.kill()
                        raise Exception("Cancelled by user")
                    if time.time() - start_time > download_timeout:
                        self.log(f"  ⚠ Download timed out after {download_timeout}s, terminating stalled yt-dlp...")
                        try:
                            process.terminate()
                            try:
                                process.wait(timeout=5)
                            except Exception:
                                process.kill()
                        except Exception:
                            pass
                        raise Exception(f"Download timed out after {download_timeout}s (yt-dlp appears stalled)")
                    time.sleep(0.5)

                for t in threads:
                    t.join(timeout=5)

                if process.returncode == 0:
                    # Check we actually have a file before accepting
                    final = self._locate_section_output(output_path, output_dir, output_stem)
                    if final:
                        self.log(f"  ✓ Section downloaded!")
                        return final

                # Use the buffer (drain thread already consumed the pipe)
                last_error = "\n".join(_stderr_buf[-30:]).strip() or f"Return code {process.returncode}"
                self.log(f"  ✗ Section download attempt {attempt} failed: {last_error[:300]}")
                # brief pause before retry
                if attempt < MAX_ATTEMPTS:
                    time.sleep(2)
            else:
                raise Exception(f"Section download failed after {MAX_ATTEMPTS} attempts!\n\n{last_error[:300]}")
            raise Exception(f"Section download failed: {str(last_error)[:300]}")

        def _download_and_trim_fallback(self, url, start_time, end_time, output_path, resolution, ffmpeg_path=None):
            """Fallback that downloads the full video and trims with ffmpeg."""
            import tempfile as _tempfile

            target_h = self._resolve_target_height(url, resolution)
            format_selector = (
                f"bestvideo[height<={target_h}]+bestaudio/"
                f"best[height<={target_h}]/bestvideo+bestaudio/best"
            )

            temp_dir = self.temp_dir
            full_name = f"full_{Path(output_path).stem}.mp4"
            full_path = Path(temp_dir) / full_name

            # Download full video using yt-dlp module (most reliable)
            import yt_dlp as _yt
            from utils.helpers import get_app_dir
            app_dir = get_app_dir()
            cookies_path = None
            for loc in [Path("cookies.txt"), app_dir / "cookies.txt"]:
                if loc.exists() and loc.stat().st_size > 0:
                    cookies_path = str(loc)
                    break

            self.log("  Downloading full video for trimming...")
            dl_opts = {
                'format': format_selector,
                'format_sort': ['res', 'br'],
                'merge_output_format': 'mp4',
                'outtmpl': str(full_path),
                'quiet': True,
                'no_warnings': True,
                # With cookies yt-dlp defaults to web_creator which now 403s
                # (needs PO token). Use clients that don't require one.
                'extractor_args': {
                    'youtube': {
                        'player_client': ['web', 'web_embedded', 'tv_embedded', 'android_vr', 'web_safari']
                    }
                },
            }
            if cookies_path:
                dl_opts['cookiefile'] = cookies_path
            if ffmpeg_path and Path(ffmpeg_path).exists():
                dl_opts['ffmpeg_location'] = str(Path(ffmpeg_path).parent)

            # Retry: 403s are often intermittent (rate-limiting / bot detection)
            last_err = None
            for attempt in range(1, 4):
                try:
                    with _yt.YoutubeDL(dl_opts) as ydl:
                        ydl.download([url])
                    if full_path.exists():
                        break
                    last_err = Exception("Full video download produced no file.")
                except Exception as e:
                    last_err = e
                    self.log(f"  ⚠ Full video download attempt {attempt}/3 failed: {str(e)[:150]}")
                    if attempt < 3:
                        time.sleep(5 * attempt)
            else:
                raise Exception(f"Full video download failed: {last_err}")

            if not full_path.exists():
                raise Exception("Full video download failed.")

            self.log("  Full video downloaded. Trimming section with ffmpeg...")

            # Use ffmpeg to trim the section precisely
            actual_ffmpeg = ffmpeg_path or self.ffmpeg_path
            out_final = Path(output_path)
            if not str(out_final).endswith(('.mp4', '.mkv', '.webm')):
                out_final = Path(str(out_final) + '.mp4')

            cmd = [
                actual_ffmpeg,
                "-y",
                "-ss", str(start_time),
                "-to", str(end_time),
                "-i", str(full_path),
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                str(out_final),
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS)
            if result.returncode != 0:
                self.log(f"  ⚠ ffmpeg trim warning: {result.stderr[:300]}")
                # Retry with re-encode if copy fails (some formats can't seek-copy)
                self.log("  Retrying with re-encode...")
                cmd = [
                    actual_ffmpeg, "-y",
                    "-ss", str(start_time),
                    "-to", str(end_time),
                    "-i", str(full_path),
                    "-c:v", "libx264", "-preset", "fast",
                    "-c:a", "aac",
                    "-avoid_negative_ts", "make_zero",
                    str(out_final),
                ]
                result = subprocess.run(cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS)
                if result.returncode != 0:
                    raise Exception(f"ffmpeg trim failed: {result.stderr[:500]}")

            self.log(f"  ✓ Section trimmed: {out_final}")

            # Clean up full video
            try:
                full_path.unlink()
            except Exception:
                pass

            return str(out_final)

        def _build_yt_dlp_cookie_headers(self, info: dict) -> str:
            """Collect the HTTP headers yt-dlp would send (cookies, UA, referer…)
            and format them for ffmpeg's -headers option.

            Without these, ffmpeg gets HTTP 403 from googlevideo.com because it
            lacks the cookies / Origin / User-Agent that YouTube requires.
            """
            headers: dict = {}
            # Global defaults first
            if isinstance(info.get('http_headers'), dict):
                headers.update(info['http_headers'])
            # Merge from the chosen format if present
            for f in (info.get('requested_formats') or []) + info.get('formats', []):
                if isinstance(f, dict) and isinstance(f.get('http_headers'), dict):
                    headers.update(f['http_headers'])
            if not headers:
                return ""
            # ffmpeg expects "Key: Value\r\n" joined lines
            return "\r\n".join(f"{k}: {v}" for k, v in headers.items()) + "\r\n"

        def _download_section_ffmpeg_range(self, url, start_time, end_time, output_path, resolution, ffmpeg_path=None):
            """Download a video section directly with ffmpeg (fast, crash-free).

            Avoids yt-dlp's native --download-sections / download_ranges path which
            shells out to ffmpeg's experimental section downloader and can crash
            (e.g. 'ffmpeg exited with code 3436169992' on some builds / streams).

            Instead we resolve a single progressive (video+audio) stream URL with
            yt-dlp, then let ffmpeg pull only the [start, end] range via -ss/-to.
            YouTube honors HTTP range requests, so ffmpeg seeks without downloading
            the whole file. yt-dlp's cookies/headers are forwarded to ffmpeg so we
            don't get HTTP 403 from googlevideo.com.
            """
            import yt_dlp as _yt
            from utils.helpers import get_app_dir

            target_h = self._resolve_target_height(url, resolution)
            ffmpeg_path = ffmpeg_path or get_ffmpeg_path()
            if not ffmpeg_path or not Path(ffmpeg_path).exists():
                raise Exception("ffmpeg not found for direct range download.")

            app_dir = get_app_dir()
            cookies_path = None
            for loc in [Path("cookies.txt"), app_dir / "cookies.txt"]:
                if loc.exists() and loc.stat().st_size > 0:
                    cookies_path = str(loc)
                    break

            info_opts = {
                'quiet': True,
                'no_warnings': True,
                'skip_download': True,
                'noplaylist': True,
                'format': (
                    f"best[height<={target_h}][acodec!=none][vcodec!=none]/"
                    f"best[acodec!=none][vcodec!=none]/best"
                ),
                # Try several player clients to dodge 403 / bot detection.
                # NOTE: with cookies, yt-dlp defaults to the `web_creator` client
                # which now REQUIRES a PO Token and 403s. Use clients that don't
                # need one. tv/android/ios/web_safari are safe choices.
                'extractor_args': {
                    'youtube': {
                        'player_client': ['web', 'web_embedded', 'tv_embedded', 'android_vr', 'web_safari']
                    }
                },
            }
            if cookies_path:
                info_opts['cookiefile'] = cookies_path
            deno_path = get_deno_path()
            if deno_path and Path(deno_path).exists():
                info_opts['js_runtimes'] = {'deno': {'path': deno_path}}
                info_opts['remote_components'] = ['ejs:github']

            with _yt.YoutubeDL(info_opts) as ydl:
                info = ydl.extract_info(url, download=False)

            # Find a combined (progressive) stream URL we can download in one go.
            stream_url = None
            chosen_format = None
            requested = info.get('requested_formats') or []
            for f in requested:
                if f.get('vcodec') not in (None, 'none') and \
                   f.get('acodec') not in (None, 'none') and f.get('url'):
                    stream_url = f['url']
                    chosen_format = f
                    break
            if not stream_url:
                for f in info.get('formats', []):
                    if f.get('acodec') not in (None, 'none') and \
                       f.get('vcodec') not in (None, 'none') and f.get('url'):
                        stream_url = f['url']
                        chosen_format = f
                        break
            if not stream_url and info.get('url') and \
               info.get('acodec') not in (None, 'none') and \
               info.get('vcodec') not in (None, 'none'):
                stream_url = info['url']
                chosen_format = info

            if not stream_url:
                raise Exception(
                    "No combined (video+audio) stream available for direct range "
                    "download; will fall back to full download + trim."
                )

            # Build headers (cookies!) for ffmpeg so we don't hit 403.
            headers_blob = self._build_yt_dlp_cookie_headers(info)
            if chosen_format and isinstance(chosen_format.get('http_headers'), dict):
                h2 = "\r\n".join(f"{k}: {v}" for k, v in chosen_format['http_headers'].items())
                if h2:
                    headers_blob = (headers_blob + h2 + "\r\n") if headers_blob else (h2 + "\r\n")

            out_final = Path(output_path)
            if not str(out_final).endswith(('.mp4', '.mkv', '.webm')):
                out_final = Path(str(out_final) + '.mp4')

            base_cmd = [
                ffmpeg_path, "-y",
                "-ss", start_time,
                "-to", end_time,
                "-i", stream_url,
                "-avoid_negative_ts", "make_zero",
            ]
            if headers_blob:
                base_cmd = [ffmpeg_path, "-y", "-headers", headers_blob,
                            "-ss", start_time, "-to", end_time, "-i", stream_url,
                            "-avoid_negative_ts", "make_zero"]

            # Attempt 1: stream copy (fast). Attempt 2: re-encode (more compatible).
            attempts = [
                base_cmd + ["-c", "copy", str(out_final)],
                base_cmd + ["-c:v", "libx264", "-preset", "veryfast",
                            "-c:a", "aac", str(out_final)],
            ]
            last_err = ""
            for idx, cmd in enumerate(attempts, 1):
                self.log(f"  Downloading section directly with ffmpeg (range seek, attempt {idx})...")
                result = subprocess.run(cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS)
                if result.returncode == 0 and out_final.exists():
                    self.log(f"  ✓ Section downloaded (direct): {out_final}")
                    return str(out_final)
                last_err = result.stderr[:500]
                self.log(f"  ⚠ ffmpeg range attempt {idx} failed, {'retrying...' if idx == 1 else 'giving up.'}")

            raise Exception(f"ffmpeg range download failed: {last_err}")

        @staticmethod
        def _locate_section_output(output_path: Path, output_dir: Path, output_stem: str):
            """Find the downloaded section file (handles yt-dlp extension naming)."""
            if output_path.exists():
                return str(output_path)
            mp4_path = output_dir / f"{output_stem}.mp4"
            if mp4_path.exists():
                return str(mp4_path)
            candidates = list(output_dir.glob(f"{output_stem}.*"))
            video_candidates = [c for c in candidates if not c.name.endswith(".part") and c.suffix in ('.mp4', '.mkv', '.webm')]
            if video_candidates:
                return str(video_candidates[0])
            return None

        @staticmethod
        def _human_bytes(num) -> str:
            """Format a byte count into a human readable string (B/KB/MB/GB)."""
            if num is None:
                return "?"
            num = float(num)
            for unit in ['B', 'KB', 'MB', 'GB']:
                if abs(num) < 1024.0:
                    return f"{num:.1f} {unit}"
                num /= 1024.0
            return f"{num:.1f} TB"

        def _parse_ytdlp_progress_line(self, line: str):
            """Parse a yt-dlp '[download]' progress line.

            Returns (percent, total_bytes, speed_bytes_per_sec, eta_seconds);
            each field may be None when not present in the line.
            """
            percent = None
            match = re.search(r'(\d+\.?\d*)%', line)
            if match:
                percent = float(match.group(1))
        
            total = None
            match = re.search(r'of\s+([0-9.]+)\s*([KMGT]i?B)', line)
            if match:
                total = float(match.group(1)) * self._unit_multiplier(match.group(2))
        
            speed = None
            match = re.search(r'at\s+([0-9.]+)\s*([KMGT]i?B/s)', line)
            if match:
                speed = float(match.group(1)) * self._unit_multiplier(match.group(2))
        
            eta = None
            match = re.search(r'ETA\s+(\d+:\d+(?::\d+)?)', line)
            if match:
                parts = match.group(1).split(":")
                eta = sum(int(x) * (60 ** (len(parts) - 1 - i)) for i, x in enumerate(parts))
        
            return percent, total, speed, eta

        def _format_ytdlp_download_status(self, prefix: str, percent, total, speed, eta, speed_only: bool = False) -> str:
            """Build a human readable download status string from parsed values.
            When speed_only=True, only the transfer speed is shown (used for
            section downloads where full progress detail is not needed)."""
            speed_str = f"{self._human_bytes(speed)}/s" if speed else "?"
            if speed_only:
                return f"{prefix} {speed_str}"
            dl_str = "?"
            if total:
                downloaded = (percent / 100.0 * total) if percent is not None else total
                dl_str = f"{self._human_bytes(downloaded)} / {self._human_bytes(total)}"
            eta_str = f", ETA {int(eta)}s" if eta else ""
            pct_str = f"{percent:.1f}%" if percent is not None else "?"
            return f"{prefix} {pct_str} ({dl_str}) at {speed_str}{eta_str}"
