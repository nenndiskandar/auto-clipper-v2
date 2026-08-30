"""
Auto Clipper Core - Processing logic
Refactored to use OpenAI Whisper API instead of local model
"""

import subprocess
import os
from core.subtitle_generator import SubtitleGeneratorMixin
from core.effects import EffectsMixin
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


class SubtitleNotFoundError(Exception):
    """Raised when no subtitle is available for the video.
    
    Carries context needed to offer Whisper transcription fallback.
    """
    def __init__(self, message: str, video_path: str = None, video_info: dict = None, session_dir: str = None):
        super().__init__(message)
        self.video_path = video_path
        self.video_info = video_info or {}
        self.session_dir = session_dir


def _hex_to_rgb(hex_color: str):
    """Convert a #RRGGBB or #RGB string to an (R, G, B) tuple. Falls back to white on bad input."""
    if not isinstance(hex_color, str):
        return (255, 255, 255)
    s = hex_color.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        return (255, 255, 255)
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError:
        return (255, 255, 255)


from core.download import DownloadMixin
from core.transcribe import TranscribeMixin
from core.highlight import HighlightMixin
from core.portrait import PortraitMixin
from core.caption import CaptionMixin


class AutoClipperCore(SubtitleGeneratorMixin, EffectsMixin, DownloadMixin, TranscribeMixin, HighlightMixin, PortraitMixin, CaptionMixin):
    """Core processing logic for Auto Clipper"""
    
    def __init__(
        self,
        client: OpenAI,
        ffmpeg_path: str = "ffmpeg",
        ytdlp_path: str = "yt-dlp",
        output_dir: str = "output",
        model: str = "gpt-4.1",
        tts_model: str = "tts-1",
        temperature: float = 1.0,
        system_prompt: str = None,
        watermark_settings: dict = None,
        credit_watermark_settings: dict = None,
        hook_style_settings: dict = None,
        face_tracking_mode: str = "opencv",
        portrait_mode: str = "crop",
        subtitle_style: str = "pop",
        aspect_ratio: str = "9:16",
        mediapipe_settings: dict = None,
        ai_providers: dict = None,
        subtitle_language: str = "id",
        subtitle_sync_offset: float = -0.3,
        log_callback=None,
        progress_callback=None,
        token_callback=None,
        cancel_check=None
    ):
        # Multi-provider support
        self.ai_providers = ai_providers or {}
        
        # Create separate clients for each provider
        if self.ai_providers:
            # Highlight Finder client
            hf_config = self.ai_providers.get("highlight_finder", {})
            self.highlight_client = OpenAI(
                api_key=hf_config.get("api_key", ""),
                base_url=hf_config.get("base_url", "https://api.openai.com/v1")
            )
            self.model = hf_config.get("model", model)
            
            # Caption Maker client (Whisper) — use longer timeout for large audio uploads
            cm_config = self.ai_providers.get("caption_maker", {})
            cm_api_key = cm_config.get("api_key", "")
            if not cm_api_key:
                # Caption Maker belum diisi -> gunakan provider utama (highlight_finder)
                # supaya transkripsi Whisper tetap jalan tanpa setup terpisah.
                cm_api_key = hf_config.get("api_key", "")
                cm_base_url = hf_config.get("base_url", "https://api.openai.com/v1")
            else:
                cm_base_url = cm_config.get("base_url", "") or "https://api.openai.com/v1"
            self.caption_client = OpenAI(
                api_key=cm_api_key,
                base_url=cm_base_url,
                timeout=600.0  # 10 minutes for large audio files
            )
            self.whisper_model = cm_config.get("model", "whisper-1")
            
            # Hook Maker client (TTS)
            hm_config = self.ai_providers.get("hook_maker", {})
            hm_api_key = hm_config.get("api_key", "") or hf_config.get("api_key", "")
            hm_base_url = hm_config.get("base_url", "") or hf_config.get("base_url", "https://api.openai.com/v1")
            self.tts_client = OpenAI(
                api_key=hm_api_key,
                base_url=hm_base_url,
                timeout=120.0,  # fail fast instead of hanging forever on a dead TTS endpoint
                max_retries=1
            )
            self.tts_model = hm_config.get("model", tts_model)
        else:
            # Fallback to single client (backward compatibility)
            self.highlight_client = client
            self.caption_client = client
            self.tts_client = client
            self.model = model
            self.tts_model = tts_model
            self.whisper_model = "whisper-1"
        
        # Keep original client for backward compatibility
        self.client = client
        
        self.ffmpeg_path = ffmpeg_path
        self.ytdlp_path = ytdlp_path
        self.output_dir = Path(output_dir)
        self.temperature = temperature
        self.system_prompt = system_prompt or self.get_default_prompt()
        self.watermark_settings = watermark_settings or {"enabled": False}
        self.credit_watermark_settings = credit_watermark_settings or {"enabled": False}
        self.hook_style_settings = hook_style_settings or {}
        self.channel_name = ""  # Will be set after download
        self.face_tracking_mode = face_tracking_mode
        self.portrait_mode = portrait_mode
        self.subtitle_style = subtitle_style
        self.aspect_ratio = aspect_ratio
        self.mediapipe_settings = mediapipe_settings or {
            "lip_activity_threshold": 0.08,
            "switch_threshold": 0.18,
            "min_shot_duration": 45,
            "center_weight": 0.15,
            "smooth_follow": False,
            "pan_speed_limit": 1.8
        }
        
        # Professional video editing features (loaded from config)
        self.pro_settings = {
            "stabilize": False,
            "color_grade": "none",
            "motion_blur": 0,
            "vignette": 0,
            "speed_ramp_start": 0,
            "speed_ramp_end": 0,
            "speed_factor": 0.5,
            "ducking_level_db": -15,
            "music_path": ""
        }
        self.subtitle_language = subtitle_language
        # Whisper word timestamps tend to run LATE by ~0.2-0.4s.
        # Negative = show subtitles earlier to compensate.
        self.subtitle_sync_offset = float(subtitle_sync_offset)
        # Set True when caption transcription/burning fails so metadata is honest
        self._caption_failed = False
        self.log = log_callback or (lambda m: debug_log(re.sub(r"\x1b\[[0-9;]*m", "", m), flush=True) if True else None)
        # fix Windows cp1252 crash on emoji ✓✗
        _orig_log = self.log
        def _safe_log(m):
            try:
                _orig_log(m)
            except UnicodeEncodeError:
                try:
                    debug_log(re.sub(r"\x1b\[[0-9;]*m", "", m).encode('cp1252','replace').decode('cp1252'), flush=True)
                except Exception:
                    pass
        self.log = _safe_log if log_callback is None else log_callback
        self.set_progress = progress_callback or (lambda s, p: None)
        self.report_tokens = token_callback or (lambda gi, go, w, t: None)
        self.is_cancelled = cancel_check or (lambda: False)
        
        # GPU acceleration settings
        self.gpu_enabled = False
        self.gpu_encoder_args = []
        
        # MediaPipe Face Landmarker (lazy loaded, Tasks API)
        self.mp_face_landmarker = None
        
        # Faster-Whisper model (lazy loaded)
        self.faster_whisper_model = None
        self.faster_whisper_model_size = None
        self.faster_whisper_compute_type = "int8" # Default to int8 for CPU
        
        # Create temp directory
        self.temp_dir = self.output_dir / "_temp"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
    
    
    # ------------------------------------------------------------------
    # GPU encoder safety net
    # ------------------------------------------------------------------
    # Some GPU encoders (h264_qsv, h264_nvenc, h264_amf) reject specific
    # preset/option combinations depending on the FFmpeg build, driver
    # version, or GPU model. When that happens, the FFmpeg call fails very
    # early with messages like:
    #   "Unable to parse "preset" option value ..."
    #   "Error setting option preset to value ..."
    #   "Error applying encoder options"
    # We detect these signatures, swap the GPU encoder args inside the
    # command for plain libx264 (CPU), and retry once. Subsequent calls in
    # the same session also fall back to CPU automatically.
    _CPU_FALLBACK_ARGS = ['-c:v', 'libx264', '-preset', 'superfast', '-crf', '18']

    _GPU_ENCODER_NAMES = (
        'h264_nvenc', 'hevc_nvenc',
        'h264_qsv', 'hevc_qsv',
        'h264_amf', 'hevc_amf',
        'h264_videotoolbox', 'hevc_videotoolbox',
        'h264_mf', 'hevc_mf',
    )




    def _run_ffmpeg_subprocess(self, cmd: list, timeout: float = 240, **kwargs):
        """Run an FFmpeg command with automatic CPU fallback on GPU encoder
        errors or stalls.

        Wraps ``subprocess.run`` with an optional ``timeout`` so a hung GPU
        encoder (e.g. QSV stalling without returning an error) can't block the
        pipeline forever. If the command fails with a signature that looks like
        a GPU encoder problem, or it times out, the command is rewritten to use
        libx264 and retried once. Returns the final ``CompletedProcess``.
        """
        kwargs.setdefault('capture_output', True)
        kwargs.setdefault('text', True)
        kwargs.setdefault('creationflags', SUBPROCESS_FLAGS)

        try:
            result = subprocess.run(cmd, timeout=timeout, **kwargs)
        except subprocess.TimeoutExpired:
            self.log("  ⚠ FFmpeg timed out (possible GPU encoder stall), retrying on CPU...")
            fallback_cmd = self._swap_cmd_to_cpu_encoder(cmd)
            if fallback_cmd == list(cmd):
                # Nothing to swap to; re-raise so the caller sees the timeout.
                raise
            self._disable_gpu_acceleration_runtime("FFmpeg timeout")
            return subprocess.run(fallback_cmd, timeout=timeout, **kwargs)

        if result.returncode == 0:
            return result

        stderr = result.stderr or ''
        if not self._is_gpu_encoder_error(stderr):
            return result

        # Looks like a GPU encoder issue: swap to CPU and retry once.
        fallback_cmd = self._swap_cmd_to_cpu_encoder(cmd)
        if fallback_cmd == list(cmd):
            # No GPU encoder found in cmd to swap; return original failure.
            return result

        self.log("  ⚠ FFmpeg failed with GPU encoder error, retrying on CPU...")
        # Pull a short reason line from stderr for the log
        reason_line = next(
            (ln.strip() for ln in stderr.splitlines()
             if 'error' in ln.lower() or 'unable' in ln.lower()),
            ''
        )
        self._disable_gpu_acceleration_runtime(reason_line[:120])

        retry = subprocess.run(fallback_cmd, timeout=timeout, **kwargs)
        return retry

    # ANSI color codes for per-step colored logging (parsed by LogPanel in the GUI)
    STEP_COLORS = {
        "download": "\x1b[96m",
        "cut": "\x1b[36m",
        "portrait": "\x1b[35m",
        "hook": "\x1b[33m",
        "caption": "\x1b[32m",
        "watermark": "\x1b[34m",
        "credit": "\x1b[91m",
        "default": "\x1b[37m",
    }
    ANSI_RESET = "\x1b[0m"

    def colorize(cls, msg: str, step: str) -> str:
        """Wrap a message in ANSI color codes for the given pipeline step."""
        code = cls.STEP_COLORS.get(step, cls.STEP_COLORS["default"])
        return f"{code}{msg}{cls.ANSI_RESET}"

    def log_ffmpeg_command(self, cmd: list, description: str = "FFmpeg", step: str = None):
        """Log FFmpeg command for debugging. `step` selects the ANSI color."""
        # Format command nicely
        cmd_str = ' '.join(f"\"{arg}\"" if " " in str(arg) else str(arg) for arg in cmd)
        header = f"  🎬 {description} Command:"
        body = f"     {cmd_str}"
        if step:
            header = self.colorize(header, step)
            body = self.colorize(body, step)
        self.log(header)
        self.log(body)
    
    def _find_system_font_bold(self) -> str:
        """Find a bold system font across platforms"""
        if sys.platform == "win32":
            candidates = [
                "C:/Windows/Fonts/arialbd.ttf",
                "C:/Windows/Fonts/arial.ttf",
                "C:/Windows/Fonts/segoeui.ttf",
            ]
        elif sys.platform == "darwin":
            candidates = [
                "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                "/System/Library/Fonts/Supplemental/Arial.ttf",
                "/Library/Fonts/Arial Bold.ttf",
                "/Library/Fonts/Arial.ttf",
                "/System/Library/Fonts/Helvetica.ttc",
                "/System/Library/Fonts/SFNS.ttf",
            ]
        else:
            candidates = [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
            ]
        
        for font in candidates:
            if os.path.exists(font):
                return font
        return None
    
    def _get_ffmpeg_font_path(self) -> str:
        """Get fontfile argument for FFmpeg drawtext filter, platform-aware"""
        font = self._find_system_font_bold()
        if font:
            if sys.platform == "win32":
                # Escape colon for FFmpeg filter on Windows
                escaped = font.replace("\\", "/").replace(":", "\\:")
                return f"fontfile='{escaped}':"
            else:
                return f"fontfile='{font}':"
        # Fallback: let FFmpeg use fontconfig default
        return "font='Arial':"
    
    
    def process(self, url: str, num_clips = "auto", add_captions: bool = True, add_hook: bool = True):
        """Main processing pipeline"""
        
        # Step 1: Download video
        self.set_progress("Downloading video...", 0.1)
        video_path, srt_path, video_info = self.download_video(url)
        
        # Store channel name for credit watermark
        self.channel_name = video_info.get("channel", "") if video_info else ""
        
        if self.is_cancelled():
            return
        
        if not srt_path:
            raise SubtitleNotFoundError(
                f"No subtitle available for language: {self.subtitle_language.upper()}",
                video_path=video_path,
                video_info=video_info
            )
        
        # Step 2: Find highlights
        self.set_progress("Finding highlights...", 0.3)
        transcript = self.parse_srt(srt_path)
        highlights = self.find_highlights(transcript, video_info, num_clips)
        
        # Snap AI timestamps to real subtitle cue boundaries
        for h in highlights:
            self._snap_highlight_to_subtitles(srt_path, h)
        
        if self.is_cancelled():
            return
        
        if not highlights:
            raise Exception("No valid highlights found!")
        
        # Step 3: Process each clip
        total_clips = len(highlights)
        for i, highlight in enumerate(highlights, 1):
            if self.is_cancelled():
                return
            self.process_clip(video_path, highlight, i, total_clips, add_captions=add_captions, add_hook=add_hook)
        
        # Skip cleanup - temp files are preserved for inspection
        self.set_progress("Complete!", 1.0)
        self.log(f"\n✅ Created {total_clips} clips in: {self.output_dir}")
    
    
    
    
    
    
    
    
    
    
    
    
    



    # ============================================================
    # PROFESSIONAL VIDEO EDITING FEATURES
    # ============================================================


    def _get_duration(self, video_path: str) -> float:
        """Get video duration in seconds using ffprobe."""
        try:
            cmd = [
                self.ffmpeg_path.replace("ffmpeg", "ffprobe"),
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return float(result.stdout.strip())
        except Exception:
            return 30.0  # fallback


    RATIO_DIMENSIONS = {
        "9:16": (1080, 1920),
        "1:1": (1080, 1080),
        "4:5": (1080, 1350),
        "16:9": (1920, 1080),
    }
    
    
    
    
    
    def parse_timestamp(self, ts: str) -> float:
        """Convert timestamp to seconds. Handles:
        - HH:MM:SS / HH:MM:SS.mmm / HH:MM:SS,mmm
        - MM:SS.mmm and collapsed MM:SSmmm (e.g. "01:02:160" = 1m 2.160s,
          where the AI drops the decimal point)
        """
        ts = ts.strip().replace(",", ".")
        parts = ts.split(":")
        if not ts:
            return 0.0

        def _is_collapsed_ms(last: str, m: str) -> bool:
            # Last segment is 3+ digits without a decimal point and the middle
            # segment is 1-2 digits -> actually MM:SS.mmm with the dot dropped.
            return (
                "." not in last and last.isdigit() and len(last) >= 3
                and len(m) <= 2 and m.isdigit()
            )

        if len(parts) == 3:
            h, m, last = parts
            if _is_collapsed_ms(last, m):
                sec = int(m) + int(last[:3]) / 1000.0
                return int(h) * 60 + sec
            return int(h) * 3600 + int(m) * 60 + float(last)
        elif len(parts) == 2:
            m, last = parts
            if _is_collapsed_ms(last, m):
                return int(m) + int(last[:3]) / 1000.0
            return int(m) * 60 + float(last)
        return float(ts)
    
    def cleanup(self):
        """Clean up temp files (no-op - temp files are preserved)"""
        pass
    
    def run_ffmpeg_with_progress(self, cmd: list, duration: float, progress_callback):
        """Run ffmpeg command and parse progress.

        Runs ffmpeg as a subprocess and, while it is encoding, polls the
        output file's current size so the caller can surface real-time
        file-size progress (e.g. to the Telegram status message).
        """
        # Locate the output file: the last cmd element that is not an option
        # and not an option value. Heuristic: last positional arg (output path).
        output_path = None
        for arg in reversed(cmd):
            if arg.startswith("-") or arg in ("-y", "-i"):
                continue
            if output_path is None:
                output_path = arg
            break

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=SUBPROCESS_FLAGS,
        )

        # Drain the child's stdout/stderr pipes in the background. If we leave
        # them unread, FFmpeg blocks once the OS pipe buffer (~64KB) fills up,
        # which makes quick `-c copy` runs appear to hang. Progress lines from
        # `-progress pipe:1` are discarded; stderr is collected for error info.
        stderr_lines = []

        def _drain(pipe, store):
            try:
                for line in pipe:
                    if store is not None:
                        store.append(line)
            except Exception:
                pass

        t_out = threading.Thread(target=_drain, args=(process.stdout, None), daemon=True)
        t_err = threading.Thread(target=_drain, args=(process.stderr, stderr_lines), daemon=True)
        t_out.start()
        t_err.start()

        last_size = -1
        last_report = 0.0
        start_time = time.time()
        last_change = start_time
        STALL_LIMIT = 60       # seconds without output growth => likely GPU stall
        WALL_LIMIT = 240       # hard backstop for commands without an output file
        while True:
            if self.is_cancelled():
                process.terminate()
                process.wait()
                raise Exception("Cancelled by user")

            poll = process.poll()
            now = time.time()
            if output_path and os.path.exists(output_path):
                try:
                    cur_size = os.path.getsize(output_path)
                except OSError:
                    cur_size = 0
                if cur_size != last_size:
                    last_size = cur_size
                    last_change = now
                    if (now - last_report) >= 1.0:
                        last_report = now
                        size_str = self._human_bytes(cur_size)
                        self.set_progress(
                            f"Encoding... {size_str}",
                            min(cur_size / 200_000_000, 0.95) if cur_size else 0.05
                        )
                # No output growth for a while => assume a GPU encoder stall.
                if last_size >= 0 and (now - last_change) > STALL_LIMIT:
                    self.log(f"  ⚠ FFmpeg stalled (no output for {STALL_LIMIT:.0f}s), falling back to CPU encoder...")
                    try:
                        process.terminate()
                        process.wait(timeout=10)
                    except Exception:
                        try:
                            process.kill()
                        except Exception:
                            pass
                    fallback_cmd = self._swap_cmd_to_cpu_encoder(cmd)
                    if fallback_cmd != list(cmd):
                        self._disable_gpu_acceleration_runtime("FFmpeg stall")
                        progress_callback(1.0)
                        return self._run_ffmpeg_subprocess(fallback_cmd)
                    raise Exception("FFmpeg stalled and no CPU fallback available")
            else:
                # No output file to watch: use a wall-clock backstop.
                if (now - start_time) > WALL_LIMIT:
                    self.log("  ⚠ FFmpeg exceeded wall-clock limit, falling back to CPU encoder...")
                    try:
                        process.terminate()
                        process.wait(timeout=10)
                    except Exception:
                        try:
                            process.kill()
                        except Exception:
                            pass
                    fallback_cmd = self._swap_cmd_to_cpu_encoder(cmd)
                    if fallback_cmd != list(cmd):
                        self._disable_gpu_acceleration_runtime("FFmpeg stall")
                        progress_callback(1.0)
                        return self._run_ffmpeg_subprocess(fallback_cmd)
                    raise Exception("FFmpeg exceeded time limit and no CPU fallback available")

            if poll is not None:
                break
            time.sleep(0.5)

        t_out.join(timeout=2)
        t_err.join(timeout=2)
        stderr = "".join(stderr_lines)
        result = subprocess.CompletedProcess(
            cmd, process.returncode, stdout="", stderr=stderr
        )

        # Report final output file size
        if output_path and os.path.exists(output_path):
            try:
                self.set_progress(
                    f"Encoding done: {self._human_bytes(os.path.getsize(output_path))}",
                    0.99
                )
            except OSError:
                pass

        # _run_ffmpeg_subprocess auto-falls-back to libx264 if a GPU encoder
        # error is detected (e.g. invalid preset on h264_qsv). Reuse its logic
        # when the first run failed with a GPU encoder error.
        if result.returncode != 0 and self._is_gpu_encoder_error(stderr):
            fallback_cmd = self._swap_cmd_to_cpu_encoder(cmd)
            if fallback_cmd != list(cmd):
                self.log("  ⚠ FFmpeg failed with GPU encoder error, retrying on CPU...")
                self._disable_gpu_acceleration_runtime("GPU encoder fallback")
                result = self._run_ffmpeg_subprocess(fallback_cmd)

        # Set to 100% when done
        progress_callback(1.0)
        
        if result.returncode != 0:
            error_msg = result.stderr if result.stderr else "Unknown FFmpeg error"
            
            # Extract the actual error (usually at the end)
            error_lines = error_msg.split('\n')
            relevant_errors = [line for line in error_lines if any(keyword in line.lower() for keyword in 
                ['error', 'invalid', 'failed', 'cannot', 'unable', 'not found', 'does not exist'])]
            
            # Get last 10 lines which usually contain the actual error
            last_lines = '\n'.join(error_lines[-10:])
            
            self.log(f"FFmpeg command failed: {' '.join(cmd)}")
            self.log(f"FFmpeg full error output:\n{error_msg}")
            
            # Show relevant error or last lines
            if relevant_errors:
                error_summary = '\n'.join(relevant_errors[-5:])
            else:
                error_summary = last_lines
            
            raise Exception(f"FFmpeg process failed:\n{error_summary}")
    

        # .ass file is kept in the clip folder for inspection

