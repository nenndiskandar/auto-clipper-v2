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


class AutoClipperCore:
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
            cm_api_key = cm_config.get("api_key", "") or hf_config.get("api_key", "")
            cm_base_url = cm_config.get("base_url", "") or hf_config.get("base_url", "https://api.openai.com/v1")
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
                base_url=hm_base_url
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
            "lip_activity_threshold": 0.15,
            "switch_threshold": 0.3,
            "min_shot_duration": 90,
            "center_weight": 0.3,
            "smooth_follow": True,
            "pan_speed_limit": 2.5
        }
        self.subtitle_language = subtitle_language
        # Whisper word timestamps tend to run LATE by ~0.2-0.4s.
        # Negative = show subtitles earlier to compensate.
        self.subtitle_sync_offset = float(subtitle_sync_offset)
        # Set True when caption transcription/burning fails so metadata is honest
        self._caption_failed = False
        self.log = log_callback or (lambda m: print(re.sub(r"\x1b\[[0-9;]*m", "", m)))
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
        self.faster_whisper_compute_type = "int8" # Default to int8 for CPU
        
        # Create temp directory
        self.temp_dir = self.output_dir / "_temp"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
    
    def _init_faster_whisper_model(self, model_size: str):
        """Initialize Faster-Whisper model."""
        if not FASTER_WHISPER_AVAILABLE:
            self.log("Faster-Whisper is not installed. Please install it to use local transcription.")
            return False
        
        app_dir = get_app_dir()
        model_dir = get_faster_whisper_model_dir(app_dir, model_size)
        
        if not model_dir.exists() or not (model_dir / "model.bin").exists():
            self.log(f"Faster-Whisper model '{model_size}' not found locally. Attempting to download...")
            from utils.dependency_manager import setup_faster_whisper_model
            if not setup_faster_whisper_model(app_dir, model_size):
                self.log(f"Failed to download Faster-Whisper model '{model_size}'.")
                return False
        
        # Determine device and compute type via ctranslate2
        try:
            import ctranslate2
            cuda_available = bool(ctranslate2.get_cuda_device_count())
        except Exception:
            cuda_available = False

        if cuda_available:
            device = "cuda"
            self.faster_whisper_compute_type = "float16"
            self.log("  ⚡ Using CUDA (float16) for Faster-Whisper inference.")
        else:
            device = "cpu"
            self.faster_whisper_compute_type = "int8"
            self.log("  Using CPU (int8) for Faster-Whisper inference.")

        self.log(f"  Loading Faster-Whisper model '{model_size}' from {model_dir}...")
        self.faster_whisper_model = WhisperModel(
            str(model_dir),
            device=device,
            compute_type=self.faster_whisper_compute_type,
            local_files_only=True
        )
        self.log(f"  ✓ Faster-Whisper model '{model_size}' loaded.")
        return True
    
    def enable_gpu_acceleration(self, enabled: bool = True):
        """Enable or disable GPU acceleration for video encoding"""
        self.gpu_enabled = enabled
        
        if enabled:
            try:
                from utils.gpu_detector import GPUDetector
                detector = GPUDetector(self.ffmpeg_path)
                self.gpu_encoder_args = detector.get_encoder_args(use_gpu=True)
                self.log(f"  ⚡ GPU Acceleration: ENABLED")
                self.log(f"  Encoder args: {' '.join(self.gpu_encoder_args)}")
            except Exception as e:
                self.log(f"  ⚠ GPU Acceleration failed to initialize: {e}")
                self.log(f"  Falling back to CPU encoding")
                self.gpu_enabled = False
                self.gpu_encoder_args = []
        else:
            self.log(f"  💻 GPU Acceleration: DISABLED (using CPU)")
            self.gpu_encoder_args = []
    
    def get_video_encoder_args(self) -> list:
        """Get video encoder arguments based on GPU settings"""
        if self.gpu_enabled and self.gpu_encoder_args:
            return self.gpu_encoder_args
        else:
            # Default CPU encoding
            return ['-c:v', 'libx264', '-preset', 'superfast', '-crf', '18']

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

    @classmethod
    def _is_gpu_encoder_error(cls, stderr: str) -> bool:
        """Heuristically detect FFmpeg failures caused by GPU encoder options."""
        if not stderr:
            return False
        text = stderr.lower()
        # Mention of any hardware encoder + a known option/init failure phrase
        mentions_hw = any(enc in text for enc in cls._GPU_ENCODER_NAMES)
        failure_phrases = (
            'error applying encoder options',
            'error setting option',
            'unable to parse',
            'no nvenc capable devices found',
            'cannot load nvcuda',
            'cannot load nvencodeapi',
            'failed loading nvenc',
            'device creation failed',
            'no device available',
            'impossible to convert between',
            'function not implemented',
        )
        mentions_failure = any(p in text for p in failure_phrases)
        return mentions_hw and mentions_failure

    @classmethod
    def _swap_cmd_to_cpu_encoder(cls, cmd: list) -> list:
        """Return a copy of cmd with any GPU encoder block replaced by CPU args.

        This walks the command, finds every ``-c:v <hw_encoder>`` and removes
        the encoder + any GPU-specific options that follow it (until the next
        FFmpeg flag or input/output token). It then injects the CPU fallback
        args in the same position. Audio codec args (``-c:a``) are preserved.
        """
        if not cmd:
            return cmd

        # Options that are known to belong to GPU encoders. We strip them
        # together with their value so libx264 doesn't choke on them.
        gpu_only_opts = {
            '-preset', '-rc', '-cq', '-qp', '-qp_i', '-qp_p', '-qp_b',
            '-quality', '-global_quality', '-look_ahead', '-rc_lookahead',
            '-spatial_aq', '-temporal_aq', '-aq-strength', '-tune',
            '-profile:v', '-level', '-b:v', '-maxrate', '-bufsize',
            '-pix_fmt',
        }

        new_cmd = []
        i = 0
        replaced = False
        while i < len(cmd):
            token = cmd[i]
            if token == '-c:v' and i + 1 < len(cmd) and cmd[i + 1] in cls._GPU_ENCODER_NAMES:
                # Inject CPU fallback once
                if not replaced:
                    new_cmd.extend(cls._CPU_FALLBACK_ARGS)
                    replaced = True
                # Skip '-c:v <hw_encoder>'
                i += 2
                # Skip any trailing GPU-specific options
                while i < len(cmd) - 1 and cmd[i] in gpu_only_opts:
                    i += 2
                continue
            new_cmd.append(token)
            i += 1

        # If no GPU encoder was present in cmd but caller still asked for
        # fallback, leave cmd untouched (nothing to swap).
        return new_cmd if replaced else list(cmd)

    def _disable_gpu_acceleration_runtime(self, reason: str = ""):
        """Disable GPU encoding for the rest of this processing session."""
        if not self.gpu_enabled:
            return
        self.gpu_enabled = False
        self.gpu_encoder_args = []
        msg = "  ⚠ GPU encoding disabled for the rest of this session"
        if reason:
            msg += f" ({reason})"
        self.log(msg)
        self.log("  💻 Continuing with CPU encoding (libx264)")

    def _run_ffmpeg_subprocess(self, cmd: list, **kwargs):
        """Run an FFmpeg command with automatic CPU fallback on GPU encoder errors.

        Wraps ``subprocess.run`` and, if the command fails with a signature
        that looks like a GPU encoder problem, rewrites the command to use
        libx264 and retries once. Returns the final ``CompletedProcess``.
        """
        kwargs.setdefault('capture_output', True)
        kwargs.setdefault('text', True)
        kwargs.setdefault('creationflags', SUBPROCESS_FLAGS)

        result = subprocess.run(cmd, **kwargs)
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

        retry = subprocess.run(fallback_cmd, **kwargs)
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

    @classmethod
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
    
    @staticmethod
    def get_default_prompt():
        """Get default system prompt for highlight detection"""
        return """Kamu adalah asisten AI untuk menemukan highlight video. Tugasmu adalah memilih momen terbaik dari transcript dan mereturn tepat {num_clips} klip.

SYARAT:
1. Durasi tiap klip antara 60 hingga 120 detik (hitung dari timestamp).
2. Pilih momen yang menarik, lucu, atau memiliki statement penting.
3. Format waktu: HH:MM:SS,mmm.

OUTPUT HARUS BERUPA JSON ARRAY TANPA TEKS LAIN:
[
  {
    "start_time": "00:01:10,000",
    "end_time": "00:02:15,000",
    "title": "Judul Klip Menarik",
    "description": "Deskripsi singkat klip ini.",
    "virality_score": 8,
    "hook_text": "Kalimat pendek yang menarik"
  }
]

====================
{video_context}

Transcript:
{transcript}"""
    
    def process(self, url: str, num_clips: int = 5, add_captions: bool = True, add_hook: bool = True):
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
            if loc.exists():
                cookies_path = loc
                break
        
        if not cookies_path:
            raise Exception("cookies.txt not found!\n\nPlease upload cookies.txt file from home page.")
        
        ydl_opts['cookiefile'] = str(cookies_path)
        self.log(f"  Using cookies from: {cookies_path}")
        
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
            
            # Validate cookies file has YouTube auth cookies
            # Check both plain cookies (SID, HSID, etc.) and __Secure- prefixed variants
            # Modern browsers/extensions often export only __Secure- versions
            required_cookies = ['SID', 'HSID', 'SSID', 'APISID', 'SAPISID', 'LOGIN_INFO']
            secure_prefixes = ['__Secure-1P', '__Secure-3P']
            found_cookies = []
            try:
                with open(cookies_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                    for cookie in required_cookies:
                        # Check plain cookie name (tab-separated format)
                        if f"\t{cookie}\t" in content or content.endswith(f"\t{cookie}"):
                            found_cookies.append(cookie)
                        else:
                            # Check __Secure- prefixed variants (e.g. __Secure-3PSID)
                            for prefix in secure_prefixes:
                                secure_name = f"{prefix}{cookie}"
                                if f"\t{secure_name}\t" in content or content.endswith(f"\t{secure_name}"):
                                    found_cookies.append(secure_name)
                                    break
                
                if not found_cookies:
                    debug_log(f"Cookies file missing required auth cookies. Found: {found_cookies}")
                    return {
                        "error": "Invalid cookies.txt - missing YouTube authentication cookies.\n\n"
                                 "Please export fresh cookies from your browser while logged into YouTube.\n\n"
                                 "Required cookies: SID, HSID, SSID, APISID, SAPISID, LOGIN_INFO\n\n"
                                 "Use a browser extension like 'Get cookies.txt LOCALLY' to export.",
                        "subtitles": [],
                        "automatic_captions": []
                    }
                debug_log(f"Found auth cookies: {found_cookies}")
            except Exception as e:
                debug_log(f"Error reading cookies file: {e}")
            
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
    
    def parse_srt(self, srt_path: str) -> str:
        """Parse SRT to text with timestamps"""
        with open(srt_path, "r", encoding="utf-8") as f:
            content = f.read()
        
        pattern = r"(\d+)\n(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})\n(.*?)(?=\n\n|\Z)"
        matches = re.findall(pattern, content, re.DOTALL)
        
        lines = []
        for idx, start, end, text in matches:
            clean_text = text.replace("\n", " ").strip()
            lines.append(f"[{start} - {end}] {clean_text}")
        
        return "\n".join(lines)
    
    def extract_transcript_for_highlight(self, srt_path: str, highlight: dict) -> str:
        """Extract subtitle text within a highlight's time range.
        
        Args:
            srt_path: Path to SRT file
            highlight: Dict with start_time and end_time keys
            
        Returns:
            str: Concatenated subtitle text within the time range
        """
        with open(srt_path, "r", encoding="utf-8") as f:
            content = f.read()
        
        pattern = r"(\d+)\n(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})\n(.*?)(?=\n\n|\Z)"
        matches = re.findall(pattern, content, re.DOTALL)
        
        start_sec = self.parse_timestamp(highlight["start_time"])
        end_sec = self.parse_timestamp(highlight["end_time"])
        
        lines = []
        for idx, start, end, text in matches:
            sub_start = self.parse_timestamp(start)
            sub_end = self.parse_timestamp(end)
            
            # Include subtitle if it overlaps with highlight range
            if sub_end >= start_sec and sub_start <= end_sec:
                clean_text = text.replace("\n", " ").strip()
                if clean_text:
                    lines.append(clean_text)
        
        return " ".join(lines)
    
    def _snap_highlight_to_subtitles(self, srt_path: str, highlight: dict) -> None:
        """Snap a highlight's start/end timestamps onto real subtitle cue boundaries.
        
        Auto-generated YouTube captions drift over time and the AI sometimes
        returns guessed timestamps that don't match any actual subtitle line.
        This rewrites start_time/end_time so they align to the nearest real
        cue, keeping the requested duration as close as possible.
        """
        try:
            with open(srt_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            self.log(f"  ⚠ Snap: could not read srt: {e}")
            return

        pattern = r"(\d+)\n(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})\n(.*?)(?=\n\n|\Z)"
        matches = re.findall(pattern, content, re.DOTALL)
        if not matches:
            return

        cues = []
        for _, start, end, text in matches:
            st = self.parse_timestamp(start)
            et = self.parse_timestamp(end)
            clean = text.replace("\n", " ").strip()
            if clean:
                cues.append((st, et))

        start_sec = self.parse_timestamp(highlight.get("start_time", "0"))
        end_sec = self.parse_timestamp(highlight.get("end_time", "0"))
        req_duration = end_sec - start_sec

        new_start = start_sec
        # Snap start to the nearest cue start (prefer the cue containing it)
        containing = [c for c in cues if c[0] <= start_sec < c[1]]
        if containing:
            new_start = containing[0][0]
        elif cues:
            new_start = min(cues, key=lambda c: abs(c[0] - start_sec))[0]

        # Find cue start nearest to the requested end, but approximate requested duration
        target_end = new_start + req_duration
        containing_end = [c for c in cues if c[0] <= target_end < c[1]]
        if containing_end:
            new_end = containing_end[0][1]
        elif cues:
            new_end = min(cues, key=lambda c: abs(c[0] - target_end))[1]

        # Avoid regression: never end before start
        if new_end <= new_start:
            new_end = new_start + 1.0

        norm_start = self._seconds_to_srt_timestamp(new_start)
        norm_end = self._seconds_to_srt_timestamp(new_end)
        if norm_start != highlight.get("start_time") or norm_end != highlight.get("end_time"):
            self.log(
                f"  • Snap '{highlight.get('title', '')}' "
                f"{highlight.get('start_time')}→{norm_start} | "
                f"{highlight.get('end_time')}→{norm_end} "
                f"(dur {req_duration:.0f}s)"
            )
            highlight["start_time"] = norm_start
            highlight["end_time"] = norm_end
            highlight["duration_seconds"] = round(new_end - new_start, 1)
    
    def _srt_output_dir(self) -> Path:
        """Directory where the session SRT file should live.

        SRT files belong in the session folder itself (not _temp) so they are
        easy to find and survive across retries. Falls back to temp_dir when
        no session directory is known yet.
        """
        if getattr(self, 'last_session_dir', None):
            return Path(self.last_session_dir)
        return self.temp_dir

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
            if loc.exists():
                cookies_path = loc
                break
        
        if not cookies_path:
            raise Exception("cookies.txt not found!\n\nPlease upload cookies.txt file from home page.")
        
        ydl_opts['cookiefile'] = str(cookies_path)
        
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
            if loc.exists():
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
            if loc.exists():
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
            if loc.exists():
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
            if loc.exists():
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
            if loc.exists():
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

            # Simple wait loop: no hang detection, just cancellation + completion.
            while process.poll() is None:
                if self.is_cancelled():
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except Exception:
                        process.kill()
                    raise Exception("Cancelled by user")
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
            if loc.exists():
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
            if loc.exists():
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
    
    def transcribe_full_video(self, video_path: str) -> str:
        """Transcribe full video audio using Whisper API (Caption Maker).
        
        Extracts audio from the video, compresses to mp3, splits into chunks
        if needed (Whisper API has ~25MB limit), and returns a transcript
        formatted like parse_srt output so find_highlights can consume it directly.
        
        Returns:
            str: Transcript with timestamps in SRT-like format:
                 [HH:MM:SS,mmm - HH:MM:SS,mmm] text
        """
        self.log("[AI Transcription] Transcribing full video with Whisper API...")
        
        # Check Caption Maker is configured
        cm_config = self.ai_providers.get("caption_maker", {})
        if not cm_config.get("api_key"):
            raise Exception(
                "Caption Maker is not configured!\n\n"
                "Please set up Caption Maker in:\n"
                "Settings → AI API Settings → Caption Maker"
            )
        
        # Extract audio as compressed mp3 to minimize file size
        audio_file = tempfile.NamedTemporaryFile(suffix='.mp3', delete=False).name
        cmd = [
            self.ffmpeg_path, "-y",
            "-i", video_path,
            "-vn",
            "-acodec", "libmp3lame",
            "-ar", "16000",
            "-ac", "1",
            "-b:a", "64k",
            audio_file
        ]
        self.log("  Extracting audio from video...")
        result = subprocess.run(cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS)
        
        if result.returncode != 0:
            if os.path.exists(audio_file):
                os.unlink(audio_file)
            raise Exception(f"Failed to extract audio from video:\n{result.stderr[:200]}")
        
        file_size_mb = os.path.getsize(audio_file) / (1024 * 1024)
        self.log(f"  Audio file size: {file_size_mb:.1f} MB")
        
        # Get total audio duration
        probe_cmd = [self.ffmpeg_path, "-i", audio_file, "-f", "null", "-"]
        probe_result = subprocess.run(probe_cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS)
        duration_match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", probe_result.stderr)
        total_duration = 0
        if duration_match:
            h, m, s = duration_match.groups()
            total_duration = int(h) * 3600 + int(m) * 60 + float(s)
        
        self.log(f"  Audio duration: {total_duration:.0f}s ({total_duration/60:.1f} min)")
        
        # Report Whisper usage
        self.report_tokens(0, 0, total_duration, 0)
        
        # Split into chunks if file is too large (>4MB to avoid proxy timeout)
        MAX_CHUNK_SIZE_MB = 4
        all_segments = []
        
        if file_size_mb <= MAX_CHUNK_SIZE_MB:
            # Single file, transcribe directly
            self.log("  Sending to Whisper API...")
            self.set_progress("Transcribing audio with AI...", 0.3)
            segments = self._whisper_transcribe_file(audio_file, 0)
            all_segments.extend(segments)
        else:
            # Split into chunks by duration
            chunk_count = int(file_size_mb / MAX_CHUNK_SIZE_MB) + 1
            chunk_duration = total_duration / chunk_count
            self.log(f"  File too large, splitting into {chunk_count} chunks (~{chunk_duration:.0f}s each)...")
            
            for i in range(chunk_count):
                if self.is_cancelled():
                    os.unlink(audio_file)
                    return ""
                
                chunk_start = i * chunk_duration
                chunk_file = tempfile.NamedTemporaryFile(suffix='.mp3', delete=False).name
                
                cmd = [
                    self.ffmpeg_path, "-y",
                    "-i", audio_file,
                    "-ss", str(chunk_start),
                    "-t", str(chunk_duration),
                    "-acodec", "libmp3lame",
                    "-ar", "16000",
                    "-ac", "1",
                    "-b:a", "64k",
                    chunk_file
                ]
                subprocess.run(cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS)
                
                chunk_size = os.path.getsize(chunk_file) / (1024 * 1024)
                self.log(f"  Transcribing chunk {i+1}/{chunk_count} ({chunk_size:.1f}MB, ~{chunk_duration:.0f}s)...")
                self.set_progress(f"Transcribing audio chunk {i+1}/{chunk_count}...", 
                                  0.3 + (0.2 * (i + 1) / chunk_count))
                
                segments = self._whisper_transcribe_file(chunk_file, chunk_start)
                all_segments.extend(segments)
                
                try:
                    os.unlink(chunk_file)
                except Exception:
                    pass
        
        # Cleanup main audio file
        try:
            os.unlink(audio_file)
        except Exception:
            pass
        
        if not all_segments:
            raise Exception("Whisper API returned empty transcription. The video may have no speech.")
        
        # Format segments into SRT-like transcript (same format as parse_srt output)
        lines = []
        for seg in all_segments:
            start_ts = self._seconds_to_srt_timestamp(seg["start"])
            end_ts = self._seconds_to_srt_timestamp(seg["end"])
            text = seg["text"].strip()
            if text:
                lines.append(f"[{start_ts} - {end_ts}] {text}")
        
        transcript = "\n".join(lines)
        self.log(f"  ✓ Transcription complete: {len(lines)} segments")
        
        return transcript
    
    def _whisper_transcribe_file(self, audio_path: str, time_offset: float = 0) -> list:
        """Transcribe a single audio file with Whisper API.
        
        Uses raw httpx POST instead of OpenAI SDK for better proxy compatibility.
        
        Args:
            audio_path: Path to audio file
            time_offset: Offset in seconds to add to all timestamps (for chunked files)
        
        Returns:
            list of dicts with 'start', 'end', 'text' keys
        """
        import time as _time
        import requests as _requests
        
        file_size_mb = os.path.getsize(audio_path) / (1024 * 1024)
        base_url = str(self.caption_client.base_url).rstrip("/")
        api_key = self.caption_client.api_key
        
        self.log(f"    Uploading {file_size_mb:.1f}MB to Whisper API ({self.whisper_model})...")
        self.log(f"    Base URL: {base_url}")
        
        # Build multipart form data
        url = f"{base_url}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {api_key}"}
        
        form_data = {
            "model": self.whisper_model,
            "response_format": "verbose_json",
        }
        if self.subtitle_language and self.subtitle_language != "none":
            form_data["language"] = self.subtitle_language
        
        # Run API call in a thread so we can log heartbeat while waiting
        response_data = None
        api_error = None
        
        def _call_api():
            nonlocal response_data, api_error
            try:
                with open(audio_path, "rb") as f:
                    files = {"file": (os.path.basename(audio_path), f, "audio/mpeg")}
                    resp = _requests.post(url, headers=headers, data=form_data, files=files, timeout=600)
                    resp.raise_for_status()
                    response_data = resp.json()
            except Exception as e:
                api_error = e
        
        api_thread = threading.Thread(target=_call_api, daemon=True)
        start_time = _time.time()
        api_thread.start()
        
        # Heartbeat: log every 15s so user knows it's still working
        TIMEOUT_SECONDS = 300  # 5 minutes max per chunk
        while api_thread.is_alive():
            api_thread.join(timeout=15)
            if api_thread.is_alive():
                elapsed = _time.time() - start_time
                
                # Check cancellation
                if self.is_cancelled():
                    self.log(f"    ⚠️ Cancelled by user during Whisper API call")
                    return []
                
                if elapsed > TIMEOUT_SECONDS:
                    self.log(f"    ⏱️ Whisper API timed out after {TIMEOUT_SECONDS}s")
                    raise Exception(
                        f"Whisper API timed out after {TIMEOUT_SECONDS}s.\n\n"
                        "Possible causes:\n"
                        "1. Your AI API provider may not support the Whisper audio endpoint\n"
                        "2. The server may be overloaded or unreachable\n"
                        "3. Network connection issue\n\n"
                        "Try:\n"
                        "- Check if your Caption Maker API supports audio transcription\n"
                        "- Try again later\n"
                        "- Use a different API provider for Caption Maker"
                    )
                self.log(f"    ⏳ Waiting for Whisper API response... ({elapsed:.0f}s elapsed)")
                self.set_progress(f"Transcribing with AI... waiting for response ({elapsed:.0f}s)", 0.35)
        
        elapsed = _time.time() - start_time
        
        if api_error:
            self.log(f"  ❌ Whisper API error after {elapsed:.1f}s: {api_error}")
            raise Exception(f"Whisper transcription failed:\n{str(api_error)}")
        
        if response_data is None:
            self.log(f"  ❌ Whisper API returned no response after {elapsed:.1f}s")
            raise Exception("Whisper API returned no response. The endpoint may not support audio transcription.")
        
        self.log(f"    ✓ Whisper API responded in {elapsed:.1f}s")
        
        segments = []
        if "segments" in response_data and response_data["segments"]:
            for seg in response_data["segments"]:
                segments.append({
                    "start": seg.get("start", 0) + time_offset,
                    "end": seg.get("end", 0) + time_offset,
                    "text": seg.get("text", "")
                })
        
        return segments
    
    def transcribe_words(self, audio_path: str):
        """Transcribe audio with word-level timestamps using local Faster-Whisper.

        Captions now always run fully offline via Faster-Whisper (no Whisper API).
        """
        return self._transcribe_words_faster_whisper(audio_path)
    
    def _transcribe_words_faster_whisper(self, audio_path: str):
        """Transcribe an audio file with word-level timestamps using local Faster-Whisper with VAD.
        
        Returns an object exposing .words and .segments (mirroring the SDK response shape).
        """
        from types import SimpleNamespace
        import time as _time
        
        # Get model size from config
        cm_config = self.ai_providers.get("caption_maker", {})
        fw_settings = cm_config.get("faster_whisper", {})
        model_size = fw_settings.get("model_size", "small")
        
        # Initialize model if needed
        if not self.faster_whisper_model:
            self.log(f"  [Caption] Initializing local Faster-Whisper model '{model_size}'...")
            success = self._init_faster_whisper_model(model_size)
            if not success:
                raise Exception(f"Failed to initialize local Faster-Whisper model '{model_size}'")
        
        self.log(f"  [Caption] Transcribing locally with Faster-Whisper ({model_size})...")
        start_time = _time.time()
        
        lang = getattr(self, "subtitle_language", None)
        if lang == "none":
            lang = None
            
        # Run transcription with VAD and word timestamps
        segments_gen, info = self.faster_whisper_model.transcribe(
            audio_path,
            word_timestamps=True,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
            language=lang
        )
        
        # Consume generator to get all segments and words
        raw_segments = list(segments_gen)
        
        elapsed = _time.time() - start_time
        self.log(f"  [Caption] Faster-Whisper transcription finished in {elapsed:.1f}s. Language: {info.language} (prob: {info.language_probability:.2f})")
        
        words = []
        segments = []
        full_text_parts = []
        
        for seg in raw_segments:
            full_text_parts.append(seg.text)
            segments.append({
                "start": seg.start,
                "end": seg.end,
                "text": seg.text
            })
            if seg.words:
                for w in seg.words:
                    words.append(SimpleNamespace(
                        word=w.word,
                        start=w.start,
                        end=w.end
                    ))
        
        # Fallback: some providers (e.g. Groq proxy) return NO word timestamps.
        # Build pseudo word timestamps by distributing each segment's duration
        # evenly across its words so animated captions still work.
        if not words and segments:
            words = self._build_pseudo_words(segments)
            self.log("  [Caption] No word timestamps from API, estimated word timing from segments")
        
        full_text = " ".join(full_text_parts)
        self.log(f"  [Caption] Got {len(words)} words, {len(segments)} segments")
        
        return SimpleNamespace(words=words, segments=segments, text=full_text)
    
    def _whisper_transcribe_words_api(self, audio_path: str):
        """Transcribe an audio file with word-level timestamps using raw HTTP.

        Compresses the audio to MP3 before uploading (the ytclip proxy drops
        connections for large WAV files >~1MB). Uses ``requests`` instead of
        the OpenAI SDK for proxy compatibility. Tries with
        ``timestamp_granularities[]=word`` first; if the proxy rejects it
        (400), retries without that field (still gets segments).

        Returns an object exposing ``.words`` and ``.segments`` (mirroring the
        SDK response shape consumed by ``create_ass_subtitle_capcut``), or
        raises on failure.
        """
        import requests as _requests
        from types import SimpleNamespace

        base_url = str(self.caption_client.base_url).rstrip("/")
        api_key = self.caption_client.api_key
        url = f"{base_url}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {api_key}"}

        lang = getattr(self, "subtitle_language", None) or "id"

        # Compress WAV → MP3 to reduce upload size (proxy rejects large bodies)
        upload_path = audio_path
        mp3_tmp = None
        if audio_path.lower().endswith(".wav"):
            mp3_tmp = audio_path.rsplit(".", 1)[0] + "_upload.mp3"
            cmd = [
                self.ffmpeg_path, "-y",
                "-i", audio_path,
                "-acodec", "libmp3lame",
                "-b:a", "64k",
                "-ar", "16000",
                "-ac", "1",
                mp3_tmp
            ]
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    creationflags=SUBPROCESS_FLAGS)
            if result.returncode == 0 and os.path.exists(mp3_tmp):
                upload_path = mp3_tmp
                self.log(f"  [Caption] Compressed WAV→MP3: "
                         f"{os.path.getsize(audio_path)/1024:.0f}KB → "
                         f"{os.path.getsize(mp3_tmp)/1024:.0f}KB")
            else:
                self.log("  [Caption] MP3 compression failed, uploading WAV as-is")
                mp3_tmp = None

        file_size_mb = os.path.getsize(upload_path) / (1024 * 1024)
        mime = "audio/mpeg" if upload_path.endswith(".mp3") else "audio/wav"
        self.log(f"  [Caption] Uploading {file_size_mb:.2f}MB to Whisper ({self.whisper_model})...")

        # Attempt 1: with word-level granularity
        form_data = [
            ("model", self.whisper_model),
            ("response_format", "verbose_json"),
            ("timestamp_granularities[]", "word"),
            ("timestamp_granularities[]", "segment"),
        ]
        if lang and lang != "none":
            form_data.append(("language", lang))

        resp = None
        for attempt in range(2):
            with open(upload_path, "rb") as f:
                files = {"file": (os.path.basename(upload_path), f, mime)}
                resp = _requests.post(url, headers=headers, data=form_data,
                                      files=files, timeout=600)

            if resp.status_code == 200:
                break

            # Log the actual error body for debugging
            self.log(f"  [Caption] Attempt {attempt+1} failed: HTTP {resp.status_code}")
            try:
                self.log(f"  [Caption] Response: {resp.text[:300]}")
            except Exception:
                pass

            if attempt == 0:
                # Retry without timestamp_granularities (proxy may not support it)
                self.log("  [Caption] Retrying without timestamp_granularities...")
                form_data = [
                    ("model", self.whisper_model),
                    ("response_format", "verbose_json"),
                ]
                if lang and lang != "none":
                    form_data.append(("language", lang))
            else:
                # Both attempts failed — raise
                raise Exception(
                    f"Whisper API returned HTTP {resp.status_code}: "
                    f"{resp.text[:300]}"
                )

        # Note: the compressed _upload.mp3 is kept next to the wav in the clip folder
        data = resp.json()
        self.log(f"  [Caption] Whisper OK, text length: {len(data.get('text', ''))}")

        words = [
            SimpleNamespace(
                word=w.get("word", ""),
                start=w.get("start", 0.0),
                end=w.get("end", 0.0),
            )
            for w in (data.get("words") or [])
        ]
        segments = data.get("segments") or []
        
        # Some providers (e.g. Groq proxy) return NO word timestamps:
        # estimate word timing by distributing each segment's duration evenly.
        if not words and segments:
            words = self._build_pseudo_words(segments)
            self.log("  [Caption] No word timestamps from API, estimated word timing from segments")
        
        # No segments at all -> nothing to subtitle. Raise so the caller can
        # fall back to local Faster-Whisper instead of silently producing a
        # video without captions.
        if not words and not segments:
            raise Exception(
                "Whisper API returned no words/segments (empty transcription). "
                "Falling back to local transcription..."
            )
        
        self.log(f"  [Caption] Got {len(words)} words, {len(segments)} segments")
        return SimpleNamespace(words=words, segments=segments,
                               text=data.get("text", ""))
    
    def _build_pseudo_words(self, segments):
        """Build word-level timestamps from segment-level data by distributing
        each segment's duration evenly across its words. This keeps animated
        word-by-word captions working even when the provider (e.g. Groq proxy)
        omits word timestamps entirely.
        """
        from types import SimpleNamespace
        
        words = []
        for seg in segments:
            text = seg.get("text", "").strip()
            if not text:
                continue
            seg_start = float(seg.get("start", 0.0))
            seg_end = float(seg.get("end", 0.0))
            if seg_end <= seg_start:
                continue
            
            parts = text.split()
            if not parts:
                continue
            
            dur = (seg_end - seg_start) / len(parts)
            for i, token in enumerate(parts):
                w_start = seg_start + i * dur
                words.append(SimpleNamespace(
                    word=token + " ",
                    start=w_start,
                    end=min(w_start + dur, seg_end),
                ))
        
        return words
    
    @staticmethod
    def _seconds_to_srt_timestamp(seconds: float) -> str:
        """Convert seconds to SRT timestamp format HH:MM:SS,mmm"""
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = seconds % 60
        ms = int((s - int(s)) * 1000)
        return f"{h:02d}:{m:02d}:{int(s):02d},{ms:03d}"
    
    @staticmethod
    def _sanitize_name(name: str, max_len: int = 60) -> str:
        """Sanitize a title into a safe folder/file name (Windows-safe)."""
        import re as re_module
        safe = re_module.sub(r'[<>:"/\\|?*\x00-\x1f]', '', str(name or "")).strip()
        safe = re_module.sub(r'\s+', ' ', safe).strip('. ')
        return safe[:max_len]
    
    def find_highlights_with_transcription(self, video_path: str, video_info: dict, 
                                            num_clips: int, session_dir: str = None) -> dict:
        """Find highlights by first transcribing the video with Whisper API.
        
        This is the fallback path when no subtitle is available.
        Uses Caption Maker (Whisper) to generate transcript, then feeds it
        to Highlight Finder (GPT) as usual.
        
        Returns:
            dict: Same session_data format as find_highlights_only
        """
        from datetime import datetime
        
        # Use existing session_dir or create new one
        if session_dir:
            session_dir = Path(session_dir)
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            video_title = (video_info or {}).get("title", "") or ""
            safe_title = self._sanitize_name(video_title)
            folder_name = f"{safe_title}_{timestamp}" if safe_title else timestamp
            session_dir = self.output_dir / "sessions" / folder_name
            session_dir.mkdir(parents=True, exist_ok=True)
        
        # Update temp_dir to session-specific temp
        self.temp_dir = session_dir / "_temp"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        
        # Session data saved at every milestone (also on failure)
        session_data_file = session_dir / "session_data.json"
        session_data = {
            "session_dir": str(session_dir),
            "video_path": video_path,
            "srt_path": None,
            "highlights": [],
            "video_info": video_info,
            "created_at": datetime.now().isoformat(),
            "status": "analyzing",
            "transcription_method": "whisper_api"
        }
        self._save_session_data(session_data_file, session_data)
        
        try:
            # Step 1: Transcribe with Whisper
            self.set_progress("Transcribing video with AI...", 0.3)
            transcript = self.transcribe_full_video(video_path)
            
            if self.is_cancelled():
                session_data["status"] = "cancelled"
                self._save_session_data(session_data_file, session_data)
                return None
            
            # Step 2: Find highlights using the transcript
            self.set_progress("Finding highlights with AI...", 0.6)
            highlights = self.find_highlights(transcript, video_info, num_clips)
            
            if self.is_cancelled():
                session_data["status"] = "cancelled"
                self._save_session_data(session_data_file, session_data)
                return None
            
            if not highlights:
                raise Exception(
                    "No valid highlights found!\n\n"
                    "Possible causes:\n"
                    "1. AI model failed to generate highlights\n"
                    "2. Video transcript too short or not suitable\n"
                    "3. AI model configuration issue\n\n"
                    "Try:\n"
                    "- Using a different AI model\n"
                    "- Checking AI API settings\n"
                    "- Using a longer video with more content"
                )
            
            self.set_progress("Highlights found!", 1.0)
            self.log(f"\n✅ Found {len(highlights)} highlights (via AI transcription)")
            
            session_data["highlights"] = highlights
            session_data["status"] = "highlights_found"
            self._save_session_data(session_data_file, session_data)
            
            return session_data
        except Exception as e:
            # Persist the failure so the session is traceable in the browser
            session_data["status"] = "error"
            session_data["error"] = str(e)[:300]
            self._save_session_data(session_data_file, session_data)
            raise
    
    @staticmethod
    def _repair_json_text(text: str) -> str:
        """Repair common LLM JSON mistakes so json.loads can succeed:
        - strips markdown code fences
        - removes trailing commas before ] or }
        - escapes unescaped double quotes inside string values
        """
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"```[a-zA-Z]*\n?", "", text)
            text = re.sub(r"```\n?", "", text)
            text = text.strip()
        
        # Remove trailing commas (e.g. {"a": 1,} or [1, 2,])
        text = re.sub(r",\s*([}\]])", r"\1", text)
        
        # Escape unescaped double quotes inside string values.
        # A quote is a string CLOSER only if followed (ignoring whitespace) by
        # a JSON delimiter (, : } ]) or end of text; otherwise it's a stray
        # quote inside the string content and must be escaped.
        out = []
        in_string = False
        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            if ch == "\\" and in_string and i + 1 < n:
                out.append(ch)
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"' and not in_string:
                in_string = True
                out.append(ch)
                i += 1
                continue
            if ch == '"' and in_string:
                j = i + 1
                while j < n and text[j] in " \t\r\n":
                    j += 1
                if j >= n or text[j] in ",:}]":
                    in_string = False
                    out.append(ch)
                else:
                    out.append('\\"')
                i += 1
                continue
            out.append(ch)
            i += 1
        return "".join(out)
    
    def find_highlights(self, transcript: str, video_info: dict, num_clips: int) -> list:
        """Find highlights using AI (OpenAI-compatible API)"""
        self.log(f"[2/4] Finding highlights (using {self.model})...")
        
        request_clips = num_clips + 3
        
        video_context = ""
        if video_info:
            video_context = f"""INFO VIDEO:
- Judul: {video_info.get('title', 'Unknown')}
- Channel: {video_info.get('channel', 'Unknown')}
- Deskripsi: {video_info.get('description', '')[:500]}"""

        # Replace placeholders safely (avoid .format() which breaks on user's curly braces)
        prompt = self.system_prompt.replace("{num_clips}", str(request_clips))
        prompt = prompt.replace("{video_context}", video_context)
        prompt = prompt.replace("{transcript}", transcript)
        
        # Warn if required placeholders are missing
        if "{transcript}" in self.system_prompt and "{transcript}" in prompt:
            self.log("  ⚠ Warning: {transcript} placeholder not replaced - check your system prompt")
        if "{num_clips}" in self.system_prompt and "{num_clips}" in prompt:
            self.log("  ⚠ Warning: {num_clips} placeholder not replaced - check your system prompt")

        # Use OpenAI-compatible API for all providers
        self.log(f"  Using API: {self.highlight_client.base_url} (Model: {self.model})")
        try:
            # ponytail: retry utk router lambat/kosong (AUTO -> model reasoning sering timeout); naikkan AI_HIGHLIGHT_ATTEMPTS kalau stabil
            max_attempts = int(os.environ.get('AI_HIGHLIGHT_ATTEMPTS', '2'))
            response = None
            for attempt in range(1, max_attempts + 1):
                try:
                    self.log(f"  ⏳ Mengirim request ke AI... (percobaan {attempt}/{max_attempts})")
                    response = self.highlight_client.chat.completions.create(
                        model=self.model,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=self.temperature,
                        max_tokens=request_clips * 300 + 2500,  # headroom ekstra: model reasoning buang token di reasoning_content
                        timeout=float(os.environ.get('AI_HIGHLIGHT_TIMEOUT', '600.0'))  # transcript panjang + banyak klip butuh >2 menit
                    )
                    if getattr(response, 'choices', None):
                        break
                    self.log("  ⚠ Respons AI tanpa 'choices' — mengulang...")
                except Exception as attempt_err:
                    if attempt >= max_attempts:
                        raise
                    self.log(f"  ⚠ Percobaan {attempt} gagal: {str(attempt_err)[:150]}")
                    self.log("  ↻ Mengulang request...")
            self.log("  ✓ Respons diterima dari AI!")
            
            # Validate response structure
            if not response:
                raise Exception("API returned empty response")
            
            if not hasattr(response, 'choices') or not response.choices:
                # Log response structure for debugging
                self.log(f"  ⚠ Unexpected API response structure: {type(response)}")
                self.log(f"  Response attributes: {dir(response)}")
                raise Exception(
                    "API response missing 'choices' field.\n\n"
                    "This usually happens with custom API providers that don't follow OpenAI format.\n\n"
                    "Please check:\n"
                    "1. API key is valid and has credits\n"
                    "2. Base URL is correct for your provider\n"
                    "3. Model name is supported by your provider\n"
                    "4. Provider follows OpenAI-compatible API format"
                )
            
            if not response.choices[0].message or not response.choices[0].message.content:
                raise Exception(
                    "API returned empty content.\n\n"
                    "Possible causes:\n"
                    "1. Model refused to generate content (content filter)\n"
                    "2. API quota exceeded\n"
                    "3. Model doesn't support this type of request"
                )
            
            # Report token usage (input and output separately)
            if hasattr(response, 'usage') and response.usage:
                self.report_tokens(response.usage.prompt_tokens, response.usage.completion_tokens, 0, 0)
            
            result = response.choices[0].message.content.strip()
            
        except Exception as e:
            # Check if it's our custom exception
            if "API response missing" in str(e) or "API returned empty" in str(e):
                raise
            
            # Otherwise, wrap with more context
            self.log(f"  ❌ API Error: {e}")
            raise Exception(
                f"Failed to get highlights from AI model.\n\n"
                f"Error: {str(e)}\n\n"
                f"Please check:\n"
                f"1. API key is valid: {self.highlight_client.api_key[:20]}...\n"
                f"2. Base URL is correct: {self.highlight_client.base_url}\n"
                f"3. Model exists: {self.model}\n"
                f"4. You have sufficient credits/quota"
            )
        
        # Log raw response for debugging
        self.log(f"  Raw AI response (first 500 chars):\n{result[:500]}")
        
        # Save raw response to file if session_dir is available
        if hasattr(self, 'last_session_dir') and self.last_session_dir:
            try:
                raw_file = Path(self.last_session_dir) / "ai_raw_response.txt"
                with open(raw_file, "w", encoding="utf-8") as f:
                    f.write(result)
                self.log(f"  ✅ Raw AI response saved to: {raw_file.name}")
            except Exception as e:
                self.log(f"  ⚠️ Could not save raw response: {e}")
        
        # Strip markdown code fences
        if result.startswith("```"):
            result = re.sub(r"```json?\n?", "", result)
            result = re.sub(r"```\n?", "", result)
        
        # Try direct parse first
        try:
            highlights = json.loads(result)
        except json.JSONDecodeError:
            # Try repairing common LLM JSON mistakes (unescaped quotes,
            # trailing commas) before falling back to extraction
            repaired = self._repair_json_text(result)
            try:
                highlights = json.loads(repaired)
                self.log(f"  ✅ Repaired JSON parse succeeded")
            except json.JSONDecodeError:
                highlights = None
                result = repaired
            
            if highlights is None:
                # If direct parse fails, try to extract JSON array or object from the response
                self.log(f"  ⚠ Direct JSON parse failed, attempting to extract JSON from response...")
                
                # Try to find a JSON array [...] or object {...} in the text
                extracted = None
                
                # Strategy 1: Find first '[' to last ']' (for array response)
                first_bracket = result.find('[')
                last_bracket = result.rfind(']')
                if first_bracket != -1 and last_bracket != -1 and last_bracket > first_bracket:
                    candidate = result[first_bracket:last_bracket + 1]
                    try:
                        extracted = json.loads(candidate)
                        self.log(f"  ✅ Extracted JSON array from response (chars {first_bracket}-{last_bracket})")
                    except json.JSONDecodeError:
                        pass
                
                # Strategy 2: Find first '{' to last '}' (for object response)
                if extracted is None:
                    first_brace = result.find('{')
                    last_brace = result.rfind('}')
                    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
                        candidate = result[first_brace:last_brace + 1]
                        try:
                            extracted = json.loads(candidate)
                            self.log(f"  ✅ Extracted JSON object from response (chars {first_brace}-{last_brace})")
                        except json.JSONDecodeError:
                            pass
                
                if extracted is not None:
                    highlights = extracted
                else:
                    # Log full response on error
                    self.log(f"\n❌ JSON Parse Error: All extraction strategies failed")
                    self.log(f"📄 Full GPT Response:\n{result}")
                    raise Exception(
                        f"Failed to parse GPT response as JSON.\n\n"
                        f"Response starts with: {result[:100]}...\n\n"
                        f"The AI model did not return valid JSON.\n"
                        f"Please try again or switch to a different model/provider."
                    )
        
        # Normalize response shape: LLMs sometimes return an object instead of
        # an array, or wrap the array inside a key (e.g. {"highlights": [...]}).
        if isinstance(highlights, dict):
            self.log("  ⚠ AI returned an object instead of an array, extracting list...")
            for key in ("highlights", "clips", "segments", "data", "results"):
                if key in highlights and isinstance(highlights[key], list):
                    highlights = highlights[key]
                    self.log(f"  ✅ Extracted list from key '{key}'")
                    break
            else:
                # Object with no list key: use its values if they look like highlights
                vals = [v for v in highlights.values() if isinstance(v, dict)]
                if vals:
                    highlights = vals
                elif "start_time" in highlights or "end_time" in highlights:
                    # The dict itself looks like a single highlight
                    highlights = [highlights]
                    self.log("  ✅ Wrapped single highlight object in a list")
                else:
                    raise Exception(
                        "AI returned an object with no usable highlight list."
                    )
        elif isinstance(highlights, list):
            # Filter out stray non-dict entries (strings, numbers) the LLM may emit
            highlights = [h for h in highlights if isinstance(h, dict)]
            if not highlights:
                raise Exception("AI returned an empty or malformed highlight list.")
        else:
            raise Exception(
                f"Unexpected AI response type: {type(highlights).__name__}"
            )
        
        # Filter by duration (min 58s, max 120s)
        valid = []
        for h in highlights:
            # Fallback: convert "reason" to "description" if exists
            if "reason" in h and "description" not in h:
                h["description"] = h.pop("reason")
                self.log(f"  ⚠ Converted 'reason' to 'description' for '{h.get('title', 'Unknown')}'")
            
            # Defensively handle missing/invalid timestamps from the AI
            start_raw = h.get("start_time") or h.get("start")
            end_raw = h.get("end_time") or h.get("end")
            if not start_raw or not end_raw:
                self.log(f"  ✗ '{h.get('title', 'Unknown')}' - missing start/end time, skipped")
                continue

            try:
                start_s = self.parse_timestamp(start_raw)
                end_s = self.parse_timestamp(end_raw)
            except (ValueError, TypeError):
                self.log(f"  ✗ '{h.get('title', 'Unknown')}' - unparseable time ({start_raw}→{end_raw}), skipped")
                continue

            h["start_time"] = start_raw
            h["end_time"] = end_raw
            duration = end_s - start_s
            h["duration_seconds"] = round(duration, 1)
            
            # Ensure virality_score exists (default to 5 if missing)
            if "virality_score" not in h:
                h["virality_score"] = 5
                self.log(f"  ⚠ Missing virality_score for '{h.get('title', 'Unknown')}', defaulting to 5")
            
            # Ensure description exists
            if "description" not in h:
                h["description"] = h.get("title", "No description")
                self.log(f"  ⚠ Missing description for '{h.get('title', 'Unknown')}', using title")
            
            if 58 <= duration <= 120:
                valid.append(h)
                virality = h.get("virality_score", 5)
                self.log(f"  ✓ {h['title']} ({duration:.0f}s) [🔥 {virality}/10]")
            elif duration > 120:
                self.log(f"  ✗ {h['title']} ({duration:.0f}s) - Too long, skipped")
            elif duration < 58:
                self.log(f"  ✗ {h['title']} ({duration:.0f}s) - Too short, skipped")
            
            if len(valid) >= num_clips:
                break
        
        # If we don't have enough valid clips, warn user
        if len(valid) < num_clips:
            self.log(f"\n⚠️ WARNING: Only found {len(valid)} valid clips out of {num_clips} requested!")
            self.log(f"   AI returned many segments that were too short (< 58s).")
            self.log(f"   Consider using a better AI model or adjusting the prompt.")
        
        return valid[:num_clips]
    
    def process_clip(self, video_path: str, highlight: dict, index: int, total_clips: int = 1, add_captions: bool = True, add_hook: bool = True, pre_cut: bool = False, clip_dir: str = None):
        """Process a single clip: cut, portrait, hook (optional), captions (optional)
        
        Args:
            video_path: Path to source video (full video or pre-cut section)
            highlight: Highlight dict with metadata
            index: Clip index (1-based)
            total_clips: Total number of clips being processed
            add_captions: Whether to add captions
            add_hook: Whether to add hook
            pre_cut: If True, video_path is already a pre-cut section (skip cutting step)
            clip_dir: Pre-created output folder for this clip (used by the
                section-download flow so the section is written straight into
                the final clip folder without an intermediate copy)
        """
        
        # Set clip title first (used in final output path, even on early return)
        clip_title = self._sanitize_name(highlight.get("title", ""), 80)
        if not clip_title:
            clip_title = f"clip_{index:02d}"

        # Check cancel before starting
        if self.is_cancelled():
            return
        if clip_dir:
            clip_dir = Path(clip_dir)
        else:
            # Create output folder named after the clip title
            clip_dir = self.output_dir / f"{index:02d}_{clip_title}"
            if clip_dir.exists():
                clip_dir = self.output_dir / f"{index:02d}_{clip_title}_{datetime.now().strftime('%H%M%S')}"
            clip_dir.mkdir(parents=True, exist_ok=True)
        
        self.log(f"  Output folder: {clip_dir}")
        
        start = highlight["start_time"].replace(",", ".")
        end = highlight["end_time"].replace(",", ".")
        
        self.log(f"\n[Clip {index}] {highlight['title']}")
        
        # Calculate total steps based on options
        total_steps = 1  # Re-encode/Cut is always 1 step
        total_steps += 1  # Portrait conversion always
        if add_hook:
            total_steps += 1
        if add_captions:
            total_steps += 1
        if self.watermark_settings.get("enabled"):
            total_steps += 1
        if self.credit_watermark_settings.get("enabled"):
            total_steps += 1
        
        # Helper to report sub-progress with percentage
        def clip_progress(step_name: str, step_num: int, sub_progress: float = 0):
            # Calculate overall progress: base (30%) + clip progress (60%)
            clip_base = 0.3 + (0.6 * (index - 1) / total_clips)
            clip_portion = 0.6 / total_clips
            step_progress = clip_portion * ((step_num + sub_progress) / total_steps)
            overall = clip_base + step_progress
            
            # Format with percentage
            percent = int(sub_progress * 100)
            if percent > 0:
                status = f"Clip {index}/{total_clips}: {step_name} ({percent}%)"
            else:
                status = f"Clip {index}/{total_clips}: {step_name}"
            
            print(f"[DEBUG] clip_progress: {status} (overall: {overall*100:.1f}%)")
            self.set_progress(status, overall)
        
        current_step = 0
        
        # Step 1: Cut video (skip if pre-cut section from --download-sections)
        if self.is_cancelled():
            return
        
        landscape_file = clip_dir / "landscape.mp4"
        duration = self.parse_timestamp(end) - self.parse_timestamp(start)
        
        if pre_cut:
            # Video is already cut to the right section. Downstream steps
            # (portrait, captions, hook, watermark, credit) all re-encode anyway,
            # so skip the redundant full re-encode and just stream-copy (-c copy).
            # If the section was downloaded straight into the final clip folder
            # as landscape.mp4, there is nothing to copy at all.
            if os.path.abspath(str(video_path)) == os.path.abspath(str(landscape_file)):
                self.log("  ✓ Section already in clip folder (no copy needed)")
            else:
                clip_progress("Normalizing pre-cut section...", current_step, 0)

                cmd = [
                    self.ffmpeg_path, "-y",
                    "-i", video_path,
                    "-c", "copy",
                    "-progress", "pipe:1",
                    str(landscape_file)
                ]

                self.log_ffmpeg_command(cmd, "Stream-copy Pre-cut Section", step="cut")

                self.run_ffmpeg_with_progress(cmd, duration,
                    lambda p: clip_progress("Normalizing pre-cut section...", current_step, p))

                self.log("  ✓ Pre-cut section stream-copied (skip redundant re-encode)")
        else:
            # Original flow: cut from full video
            clip_progress("Cutting video...", current_step, 0)
            
            encoder_args = self.get_video_encoder_args()
            
            cmd = [
                self.ffmpeg_path, "-y",
                "-i", video_path,
                "-ss", start, "-to", end,
                *encoder_args,
                "-c:a", "aac", "-b:a", "192k",
                "-progress", "pipe:1",
                str(landscape_file)
            ]
            
            self.log_ffmpeg_command(cmd, "Cut Video", step="cut")
            
            self.run_ffmpeg_with_progress(cmd, duration, 
                lambda p: clip_progress("Cutting video...", current_step, p))
            
            self.log(self.colorize("  ✓ Cut video", "cut"))
        
        current_step += 1
        
        # Step 2: Convert to portrait with progress
        if self.is_cancelled():
            return
        clip_progress("Converting to portrait...", current_step, 0)
        portrait_file = clip_dir / "portrait.mp4"
        self.convert_to_portrait_with_progress(str(landscape_file), str(portrait_file), 
            lambda p: clip_progress("Converting to portrait...", current_step, p))
        self.log(self.colorize("  ✓ Portrait conversion", "portrait"))
        current_step += 1
        
        # Track which file is the current output
        current_output = portrait_file
        hook_duration = 0
        
        # Step 3: Add hook (optional)
        if add_hook:
            if self.is_cancelled():
                return
            clip_progress("Adding hook...", current_step, 0)
            hooked_file = clip_dir / "hook.mp4"
            hook_text = highlight.get("hook_text", highlight["title"])
            hook_duration = self.add_hook_with_progress(str(current_output), hook_text, str(hooked_file),
                lambda p: clip_progress("Adding hook...", current_step, p))
            
            # Verify hooked file was created
            if not hooked_file.exists():
                raise Exception(f"Failed to create hooked video: {hooked_file}")
            
            self.log(self.colorize(f"  ✓ Added hook ({hook_duration:.1f}s)", "hook"))
            current_output = hooked_file
            current_step += 1
        else:
            self.log("  ⊘ Skipped hook (disabled)")
        
        # Step 4: Add captions (optional)
        final_file = clip_dir / f"{clip_title}.mp4"
        captioned_file = clip_dir / "captioned.mp4"
        if add_captions:
            if self.is_cancelled():
                return
            clip_progress("Adding captions...", current_step, 0)
            
            # Use portrait_file (without hook) as audio source for transcription
            audio_source = str(portrait_file) if add_hook else None
            
            self.add_captions_api_with_progress(str(current_output), str(captioned_file), audio_source, hook_duration,
                lambda p: clip_progress("Adding captions...", current_step, p))
            
            if not captioned_file.exists():
                raise Exception(f"Failed to create captioned video: {captioned_file}")
            
            current_output = captioned_file
            self.log(self.colorize("  ✓ Added captions", "caption"))
            current_step += 1
        else:
            self.log("  ⊘ Skipped captions (disabled)")
        
        # Step 5: Add watermark (if enabled)
        watermark_file = clip_dir / "watermark.mp4"
        if self.watermark_settings.get("enabled"):
            if self.is_cancelled():
                return
            
            clip_progress("Adding watermark...", current_step, 0)
            
            # Apply watermark to current output
            self.add_watermark_with_progress(str(current_output), str(watermark_file),
                lambda p: clip_progress("Adding watermark...", current_step, p))
            
            if not watermark_file.exists():
                raise Exception(f"Failed to create watermark video: {watermark_file}")
            
            self.log(self.colorize("  ✓ Added watermark", "watermark"))
            current_output = watermark_file
            current_step += 1
        else:
            self.log("  ⊘ Skipped watermark (disabled)")
        
        # Step 6: Add credit watermark (if enabled)
        credit_file = clip_dir / "credit.mp4"
        if self.credit_watermark_settings.get("enabled"):
            if not self.channel_name:
                self.log("  ⚠ Credit watermark enabled but no channel name available, skipping")
            elif self.is_cancelled():
                return
            else:
                clip_progress("Adding credit...", current_step, 0)
                
                self.add_credit_watermark_with_progress(str(current_output), str(credit_file),
                    lambda p: clip_progress("Adding credit...", current_step, p))
                
                if not credit_file.exists():
                    raise Exception(f"Failed to create credit video: {credit_file}")
                
                self.log(self.colorize(f"  ✓ Added credit: Source: {self.channel_name}", "credit"))
                current_output = credit_file
                current_step += 1
        
        # Copy the final stage to the clip-named output file
        if str(current_output) != str(final_file):
            import shutil
            shutil.copy(str(current_output), str(final_file))
        
        # Mark complete
        clip_progress("Done", total_steps, 0)
        
        # Temp files are kept for inspection (landscape, portrait, hooked, etc.)
        
        # Save metadata
        watermark_applied = (
            self.watermark_settings.get("enabled", False)
            and self.watermark_settings.get("image_path")
            and Path(self.watermark_settings["image_path"]).exists()
        )
        credit_applied = (
            self.credit_watermark_settings.get("enabled", False)
            and bool(self.channel_name)
        )
        metadata = {
            "title": highlight["title"],
            "hook_text": highlight.get("hook_text", highlight["title"]),
            "start_time": highlight["start_time"],
            "end_time": highlight["end_time"],
            "duration_seconds": highlight["duration_seconds"],
            "has_hook": add_hook,
            "has_captions": add_captions and not getattr(self, "_caption_failed", False),
            "has_watermark": watermark_applied,
            "has_credit": credit_applied,
            "channel_name": self.channel_name,
            "aspect_ratio": self.aspect_ratio,
        }
        
        # Auto generate social kit metadata if client is available
        if self.client:
            try:
                self.log("  Generating Social Kit metadata...")
                prompt = f"""Kamu adalah expert Social Media Manager untuk konten short-form (TikTok, Instagram Reels, YouTube Shorts).

Berdasarkan informasi clip berikut, buatkan:
1. Title yang catchy dan clickbait (max 100 karakter, include emoji)
2. Description yang engaging dan interaktif untuk memancing komentar penonton (max 300 karakter)
3. Hashtags yang relevan dan viral (minimal 5-8 hashtags dipisahkan spasi)
4. AI Analysis & Hook Results: Penjelasan singkat mengapa hook ini menarik, target audiensnya siapa, dan analisis singkat tentang konten ini.

Info Clip:
- Judul: {highlight['title']}
- Hook: {highlight.get('hook_text', highlight['title'])}

Format response dalam JSON:
{{
    "title": "judul dengan emoji",
    "description": "deskripsi engaging",
    "hashtags": "#shorts #viral #fyp #tag1 #tag2",
    "ai_analysis": "Penjelasan hook, target audiens, dan analisis konten."
}}

PENTING:
- Gunakan bahasa Indonesia
- Return HANYA JSON, tanpa markdown code blocks atau text lain."""

                response = self.client.chat.completions.create(
                    model=self.model if hasattr(self, 'model') else "gpt-4.1",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=self.temperature
                )
                
                result = response.choices[0].message.content.strip()
                if result.startswith("```"):
                    import re
                    result = re.sub(r"```json?\n?", "", result)
                    result = re.sub(r"```\n?", "", result)
                
                social_data = json.loads(result)
                metadata["social_kit"] = social_data
                self.log("  Social Kit metadata generated successfully!")
            except Exception as e:
                self.log(f"  Warning: Failed to auto-generate Social Kit: {e}")
        
        with open(clip_dir / "data.json", "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)
    
    @staticmethod
    def _interpolate_sampled(sampled_values: list, sampled_indices: list, total_frames: int) -> list:
        """Expand sparse per-frame samples to one value per frame (linear interpolation).

        Falls back to the last known value when sampling stops early.
        """
        if not sampled_values:
            return []
        if total_frames <= 0:
            return list(sampled_values)
        if len(sampled_values) == 1:
            return [sampled_values[0]] * total_frames
        x = np.array(sampled_indices, dtype=float)
        y = np.array(sampled_values, dtype=float)
        target = np.arange(total_frames, dtype=float)
        return np.interp(target, x, y).tolist()

    @staticmethod
    def _build_crop_expression(positions: list, min_run: int = 20, quantize: int = 4) -> str:
        """Piecewise-constant ffmpeg crop x expression from per-frame positions.

        Evaluated per frame with ffmpeg's ``n`` variable. Short runs (< min_run
        frames) and sub-`quantize` movements are dropped (invisible jitter),
        keeping the expression small even for long videos.
        """
        if not positions:
            return "0"
        quantize = max(1, int(quantize))
        runs = []  # (start_frame, value)
        prev_val = None
        for i, x in enumerate(positions):
            q = int(round(x / quantize) * quantize)
            if prev_val is None or q != prev_val:
                runs.append([i, q])
                prev_val = q
        filtered = [runs[0]]
        for start, val in runs[1:]:
            if start - filtered[-1][0] < min_run:
                continue  # inherit previous value
            filtered.append([start, val])
        if len(filtered) == 1:
            return str(filtered[0][1])
        expr = str(filtered[-1][1])
        for k in range(len(filtered) - 2, 0, -1):
            val = filtered[k][1]
            next_start = filtered[k + 1][0]
            expr = f"if(lt(n,{next_start}),{val},{expr})"
        return f"if(lt(n,{filtered[1][0]}),{filtered[0][1]},{expr})"

    def _encode_portrait_single_pass(self, input_path: str, output_path: str,
                                     crop_positions: list, crop_w: int, crop_h: int,
                                     out_w: int, out_h: int,
                                     progress_callback=None, duration: float = 0):
        """Crop + scale + encode + audio mux in ONE ffmpeg pass.

        Replaces the old two-step flow (OpenCV VideoWriter temp file, then a
        second full re-encode for audio merge) with a single encode, which is
        roughly twice as fast and no longer depends on OpenCV's H.264 writer.
        """
        expr = self._build_crop_expression(crop_positions)
        fd, script_path = tempfile.mkstemp(suffix=".txt", prefix="portrait_crop_", text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(
                    f"[0:v]crop={crop_w}:{crop_h}:x='{expr}':y=0,"
                    f"scale={out_w}:{out_h}:flags=bicubic,setsar=1,format=yuv420p[v]"
                )
            encoder_args = self.get_video_encoder_args()
            cmd = [
                self.ffmpeg_path, "-y",
                "-i", input_path,
                "-filter_complex_script", script_path,
                "-map", "[v]", "-map", "0:a?",
                *encoder_args,
                "-c:a", "aac", "-b:a", "192k",
                "-shortest",
                output_path,
            ]
            self.log_ffmpeg_command(cmd, "Portrait Crop+Encode (single pass)", step="portrait")
            if progress_callback is not None:
                self.run_ffmpeg_with_progress(cmd, duration, progress_callback)
            else:
                result = self._run_ffmpeg_subprocess(cmd)
                if result.returncode != 0:
                    stderr = (result.stderr or "")[-2000:]
                    raise Exception(f"Portrait encode failed:\n{stderr}")
        finally:
            try:
                os.unlink(script_path)
            except OSError:
                pass

    def convert_to_portrait(self, input_path: str, output_path: str):
        """Convert landscape to 9:16 portrait (router method)"""
        if self.portrait_mode == "blur":
            self.log("  Using Blurred Background (no crop)")
            return self.convert_to_portrait_blur(input_path, output_path)
        try:
            if self.face_tracking_mode == "mediapipe":
                self.log("  Using MediaPipe (Active Speaker Detection)")
                return self.convert_to_portrait_mediapipe(input_path, output_path)
            else:
                self.log("  Using OpenCV (Fast Mode)")
                return self.convert_to_portrait_opencv(input_path, output_path)
        except Exception as e:
            # Fallback to OpenCV if MediaPipe fails
            if self.face_tracking_mode == "opencv":
                self.log(f"  ⚠ MediaPipe failed: {e}")
                self.log("  Falling back to OpenCV mode...")
                return self.convert_to_portrait_opencv(input_path, output_path)
            else:
                raise
    
    def convert_to_portrait_opencv(self, input_path: str, output_path: str):
        """Convert landscape to 9:16 portrait with speaker tracking (OpenCV Haar Cascade)"""
        
        cap = cv2.VideoCapture(input_path)
        orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        
        # Calculate crop dimensions
        crop_w, crop_h = self._get_crop_window(orig_w, orig_h)
        out_w, out_h = self._get_ratio_dimensions()
        
        # Face detector
        face_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
        )
        
        # First pass: analyze frames
        self.log("  Pass 1: Analyzing frames (fast mode: every 5th frame)...")
        crop_positions = []
        current_target = orig_w / 2
        
        ANALYSIS_STEP = 5
        ANALYSIS_MAX_WIDTH = 640
        scale = min(1.0, ANALYSIS_MAX_WIDTH / orig_w)
        
        analyzed_indices = []
        analyzed_positions = []
        frame_idx = 0
        
        while True:
            if frame_idx % ANALYSIS_STEP == 0:
                ret, frame = cap.read()
                if not ret:
                    break
                if scale < 1.0:
                    small = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                else:
                    small = frame
                gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                faces = face_cascade.detectMultiScale(gray, 1.1, 5, minSize=(50, 50))
                
                if len(faces) > 0:
                    # Find largest face (coordinates in downscaled space -> map back)
                    largest = max(faces, key=lambda f: f[2] * f[3])
                    face_center = (largest[0] + largest[2] / 2) / scale
                    current_target = face_center
                
                crop_x = int(current_target - crop_w / 2)
                crop_x = max(0, min(crop_x, orig_w - crop_w))
                analyzed_indices.append(frame_idx)
                analyzed_positions.append(crop_x)
            else:
                ret = cap.grab()
                if not ret:
                    break
            frame_idx += 1
        
        # Interpolate positions for every frame
        crop_positions = self._interpolate_sampled(analyzed_positions, analyzed_indices, frame_idx)
        
        # Stabilize positions
        crop_positions = self.stabilize_positions(crop_positions)
        
        # Second pass: single ffmpeg command (crop + scale + encode + audio)
        self.log("  Pass 2: Encoding portrait video (single ffmpeg pass, crop + audio)...")
        self._encode_portrait_single_pass(
            input_path, output_path, crop_positions, crop_w, crop_h, out_w, out_h,
            duration=frame_idx / fps if fps else 0,
        )
        cap.release()
    
    def stabilize_positions(self, positions: list) -> list:
        """Stabilize crop positions - reduce jitter and sudden movements"""
        if not positions:
            return positions
        
        # Use longer window for smoother movement
        window_size = 60  # ~2 seconds at 30fps - longer window = smoother
        stabilized = []
        
        for i in range(len(positions)):
            # Get window around current position
            start = max(0, i - window_size // 2)
            end = min(len(positions), i + window_size // 2)
            window = positions[start:end]
            
            # Use median for stability (resistant to outliers)
            avg = int(np.median(window))
            stabilized.append(avg)
        
        # Second pass: detect shot changes and lock position per shot
        # A shot change is when position jumps significantly
        # Use very high threshold to minimize scene switches
        final = []
        shot_start = 0
        threshold = 250  # pixels - very high threshold = less scene switches
        min_shot_duration = 90  # minimum frames (~3 seconds) before allowing switch
        
        for i in range(len(stabilized)):
            frames_since_last_switch = i - shot_start
            
            # Only allow switch if:
            # 1. Minimum shot duration has passed
            # 2. Position changed significantly
            # 3. Activity is high enough (speaker is talking)
            if frames_since_last_switch >= min_shot_duration:
                position_diff = abs(stabilized[i] - stabilized[shot_start])
                
                # Switch if position changed significantly
                if position_diff > threshold:
                    # Shot change detected - lock previous shot to median
                    shot_positions = stabilized[shot_start:i]
                    if shot_positions:
                        shot_median = int(np.median(shot_positions))
                        final.extend([shot_median] * len(shot_positions))
                    
                    shot_start = i
                    current_position = stabilized[i]
        
        # Handle last shot
        shot_positions = stabilized[shot_start:]
        if shot_positions:
            shot_median = int(np.median(shot_positions))
            final.extend([shot_median] * len(shot_positions))
        
        return final if final else stabilized
    
    def _init_mediapipe(self):
        """Initialize MediaPipe Face Landmarker (lazy loading)"""
        if self.mp_face_landmarker is None:
            try:
                if vision is None or python is None:
                    raise Exception("MediaPipe not installed. Run: pip install mediapipe")
                from utils.helpers import get_mediapipe_model_path
                model_path = get_mediapipe_model_path()
                
                base_options = python.BaseOptions(model_asset_path=model_path)
                options = vision.FaceLandmarkerOptions(
                    base_options=base_options,
                    output_face_blendshapes=False,
                    output_facial_transformation_matrixes=False,
                    num_faces=3
                )
                self.mp_face_landmarker = vision.FaceLandmarker.create_from_options(options)
                self.log("  MediaPipe Face Landmarker initialized successfully")
            except Exception as e:
                raise Exception(f"Failed to initialize MediaPipe Face Landmarker: {e}")
    
    def convert_to_portrait_mediapipe(self, input_path: str, output_path: str):
        """Convert landscape to 9:16 portrait with active speaker detection (MediaPipe)"""
        
        # Initialize MediaPipe
        self._init_mediapipe()
        
        cap = cv2.VideoCapture(input_path)
        if not cap.isOpened():
            raise Exception(f"Failed to open video: {input_path}")
        
        orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        
        if total_frames == 0 or fps == 0:
            cap.release()
            raise Exception(f"Invalid video properties: {total_frames} frames, {fps} fps")
        
        # Calculate crop dimensions
        crop_w, crop_h = self._get_crop_window(orig_w, orig_h)
        out_w, out_h = self._get_ratio_dimensions()
        
        # MediaPipe Face Mesh settings
        lip_threshold = self.mediapipe_settings.get("lip_activity_threshold", 0.15)
        switch_threshold = self.mediapipe_settings.get("switch_threshold", 0.3)
        min_shot_duration = self.mediapipe_settings.get("min_shot_duration", 90)
        center_weight = self.mediapipe_settings.get("center_weight", 0.3)
        
        # First pass: analyze frames with MediaPipe
        self.log("  Pass 1: Analyzing lip movements (fast mode: every 5th frame)...")
        crop_positions = []
        face_activities = []  # Store activity scores per frame
        
        ANALYSIS_STEP = 5
        ANALYSIS_MAX_WIDTH = 640
        scale = min(1.0, ANALYSIS_MAX_WIDTH / orig_w)
        if scale < 1.0:
            self.log(f"  Fast analysis at {ANALYSIS_MAX_WIDTH}px width (x{1/scale:.0f} speedup)")
        
        analyzed_indices = []
        analyzed_positions = []
        analyzed_activities = []
        frame_idx = 0
        prev_lip_distances = {}  # Track previous lip distances per face
        
        while True:
            if self.is_cancelled():
                cap.release()
                raise Exception("Cancelled by user")
            
            if frame_idx % ANALYSIS_STEP != 0:
                ret = cap.grab()
                if not ret:
                    break
                frame_idx += 1
                continue
            
            ret, frame = cap.read()
            if not ret:
                break
            
            # Downscale for faster inference (coordinates are normalized)
            if scale < 1.0:
                small = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            else:
                small = frame
            
            # Convert to RGB for MediaPipe
            rgb_frame = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)
            results = self.mp_face_landmarker.detect(mp_image)
            
            best_face_x = orig_w / 2  # Default to center
            max_activity = 0
            
            if results.face_landmarks:
                faces_data = []
                
                for face_id, face_landmarks in enumerate(results.face_landmarks):
                    # Calculate lip activity
                    activity = self._calculate_lip_activity(
                        face_landmarks, 
                        orig_w, 
                        orig_h,
                        prev_lip_distances.get(face_id, None)
                    )
                    
                    # Get face center position (landmark 1 is nose tip)
                    face_x = face_landmarks[1].x * orig_w
                    
                    # Combined score (activity + center position)
                    center_score = 1.0 - abs(face_x - orig_w / 2) / (orig_w / 2)
                    combined_score = (activity * (1 - center_weight)) + (center_score * center_weight)
                    
                    faces_data.append({
                        'x': face_x,
                        'activity': activity,
                        'combined_score': combined_score
                    })
                    
                    # Update previous lip distance
                    upper_lip = face_landmarks[13]  # Upper lip center
                    lower_lip = face_landmarks[14]  # Lower lip center
                    lip_distance = abs(upper_lip.y - lower_lip.y)
                    prev_lip_distances[face_id] = lip_distance
                
                # Select face with highest combined score
                if faces_data:
                    best_face = max(faces_data, key=lambda f: f['combined_score'])
                    best_face_x = best_face['x']
                    max_activity = best_face['activity']
            
            # Calculate crop position
            crop_x = int(best_face_x - crop_w / 2)
            crop_x = max(0, min(crop_x, orig_w - crop_w))
            analyzed_indices.append(frame_idx)
            analyzed_positions.append(crop_x)
            analyzed_activities.append(max_activity)
            
            frame_idx += 1
            
            if frame_idx % 150 == 0:
                self.log(f"    Analyzed {frame_idx}/{total_frames} frames...")
        
        self.log(f"  Analyzed {frame_idx} frames (sampled {len(analyzed_positions)} frames)")
        
        # Interpolate to one position/activity per frame
        crop_positions = self._interpolate_sampled(analyzed_positions, analyzed_indices, frame_idx)
        face_activities = self._interpolate_sampled(analyzed_activities, analyzed_indices, frame_idx)
        
        # Stabilize positions with shot-based switching or smooth face follow
        if self.mediapipe_settings.get("smooth_follow", True):
            self.log(f"  Smooth face follow: camera pans continuously after face movement")
            crop_positions = self._smooth_follow_positions(
                crop_positions,
                self.mediapipe_settings.get("pan_speed_limit", 2.5)
            )
        else:
            crop_positions = self._stabilize_positions_with_activity(
                crop_positions, 
                face_activities,
                min_shot_duration,
                switch_threshold
            )
        
        # Second pass: single ffmpeg command (crop + scale + encode + audio)
        self.log("  Pass 2: Encoding portrait video (single ffmpeg pass, crop + audio)...")
        self._encode_portrait_single_pass(
            input_path, output_path, crop_positions, crop_w, crop_h, out_w, out_h,
            duration=frame_idx / fps if fps else 0,
        )
        cap.release()
    
    def _calculate_lip_activity(self, face_landmarks, frame_width, frame_height, prev_lip_distance=None):
        """Calculate lip movement activity score"""
        
        # Key lip landmarks (MediaPipe Face Landmarker indices)
        # Upper lip: 13, Lower lip: 14
        upper_lip = face_landmarks[13]
        lower_lip = face_landmarks[14]
        
        # Mouth corners: 61 (left), 291 (right)
        mouth_left = face_landmarks[61]
        mouth_right = face_landmarks[291]
        
        # Calculate mouth openness (vertical distance)
        mouth_height = abs(upper_lip.y - lower_lip.y)
        
        # Calculate mouth width (horizontal distance)
        mouth_width = abs(mouth_left.x - mouth_right.x)
        
        # Aspect ratio (height/width) - higher when mouth is open
        if mouth_width > 0:
            aspect_ratio = mouth_height / mouth_width
        else:
            aspect_ratio = 0
        
        # Calculate movement delta (change from previous frame)
        delta = 0
        if prev_lip_distance is not None:
            delta = abs(mouth_height - prev_lip_distance)
        
        # Activity score: combination of openness and movement
        # Weight movement more heavily (0.6) than static openness (0.4)
        activity_score = (aspect_ratio * 0.4) + (delta * 0.6)
        
        return activity_score
    
    def _stabilize_positions_with_activity(self, positions, activities, min_shot_duration, switch_threshold):
        """Stabilize crop positions based on activity scores"""
        if not positions:
            return positions
        
        # First pass: smooth positions with moving median
        window_size = 30
        smoothed = []
        
        for i in range(len(positions)):
            start = max(0, i - window_size // 2)
            end = min(len(positions), i + window_size // 2)
            window = positions[start:end]
            smoothed.append(int(np.median(window)))
        
        # Second pass: lock positions per shot based on activity
        final = []
        shot_start = 0
        current_position = smoothed[0] if smoothed else 0
        
        for i in range(len(smoothed)):
            frames_since_switch = i - shot_start
            
            # Only allow switch if:
            # 1. Minimum shot duration has passed
            # 2. Position changed significantly
            # 3. Activity is high enough (speaker is talking)
            if frames_since_switch >= min_shot_duration:
                position_diff = abs(smoothed[i] - current_position)
                activity = activities[i] if i < len(activities) else 0
                
                # Switch if position changed significantly AND there's activity
                if position_diff > switch_threshold and activity > 0:
                    # Shot change detected - lock previous shot to median
                    shot_positions = smoothed[shot_start:i]
                    if shot_positions:
                        shot_median = int(np.median(shot_positions))
                        final.extend([shot_median] * len(shot_positions))
                    
                    shot_start = i
                    current_position = smoothed[i]
        
        # Handle last shot
        shot_positions = smoothed[shot_start:]
        if shot_positions:
            shot_median = int(np.median(shot_positions))
            final.extend([shot_median] * len(shot_positions))
        
        return final if final else smoothed
    
    def _smooth_follow_positions(self, positions: list, pan_speed_limit: float = 2.5):
        """Smooth continuous camera pan that follows the face movement.

        Unlike shot-based stabilization (which locks the crop to a fixed
        position per "shot"), this keeps the crop window gliding after the
        face frame by frame: a moving average removes jitter and a per-frame
        speed limit prevents violent jumps, producing a natural camera pan.
        """
        if not positions:
            return positions
        if len(positions) < 3:
            return positions

        # 1) Moving average to remove single-frame face detection jitter
        window = 15
        smoothed = []
        for i in range(len(positions)):
            start = max(0, i - window // 2)
            end = min(len(positions), i + window // 2)
            smoothed.append(int(np.mean(positions[start:end])))

        # 2) Clamp per-frame speed so the pan never jerks
        final = [smoothed[0]]
        for i in range(1, len(smoothed)):
            prev = final[-1]
            curr = smoothed[i]
            delta = curr - prev
            if abs(delta) > pan_speed_limit:
                delta = pan_speed_limit if delta > 0 else -pan_speed_limit
            final.append(prev + int(delta))

        return final
    
    def add_hook(self, input_path: str, hook_text: str, output_path: str) -> float:
        """Add hook scene at the beginning with multi-line yellow text (Fajar Sadboy style)"""
        
        # Report TTS character usage
        self.report_tokens(0, 0, 0, len(hook_text))
        
        # Generate TTS audio
        try:
            tts_response = self.tts_client.audio.speech.create(
                model=self.tts_model,
                voice="nova",
                input=hook_text,
                speed=1.0
            )
        except APIConnectionError as e:
            self.log(f"  ❌ TTS API Connection Error: Could not connect to {self.tts_client.base_url}")
            raise Exception(f"TTS API connection failed!\n\nCould not connect to: {self.tts_client.base_url}\nError: {e}")
        except RateLimitError as e:
            self.log(f"  ❌ TTS API Rate Limit: {e}")
            raise Exception(f"TTS API rate limit exceeded!\n\nPlease wait a moment and try again.\nDetails: {e}")
        except APIStatusError as e:
            self.log(f"  ❌ TTS API Error (HTTP {e.status_code}): {e.message}")
            self.log(f"     Model: {self.tts_model}, Base URL: {self.tts_client.base_url}")
            raise Exception(
                f"TTS (Hook) API Error!\n\n"
                f"Status: {e.status_code}\n"
                f"Message: {e.message}\n"
                f"Model: {self.tts_model}\n"
                f"Base URL: {self.tts_client.base_url}\n\n"
                f"Check your Hook Maker API settings."
            )
        except Exception as e:
            self.log(f"  ❌ TTS API Unexpected Error: {type(e).__name__}: {e}")
            raise Exception(f"TTS (Hook) generation failed!\n\nError: {type(e).__name__}: {e}\nModel: {self.tts_model}")
        
        tts_file = tempfile.NamedTemporaryFile(suffix='.mp3', delete=False).name
        with open(tts_file, 'wb') as f:
            f.write(tts_response.content)
        
        # Get TTS duration using ffprobe
        probe_cmd = [
            self.ffmpeg_path, "-i", tts_file,
            "-f", "null", "-"
        ]
        result = subprocess.run(probe_cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS)
        duration_match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", result.stderr)
        
        if duration_match:
            h, m, s = duration_match.groups()
            hook_duration = int(h) * 3600 + int(m) * 60 + float(s) + 0.5
        else:
            hook_duration = 3.0
        
        # Format hook text: uppercase, split into lines (max 3 words per line for better visibility)
        hook_upper = hook_text.upper()
        words = hook_upper.split()
        
        # Split into lines (max 3 words per line - Fajar Sadboy style)
        lines = []
        current_line = []
        for word in words:
            current_line.append(word)
            if len(current_line) >= 3:
                lines.append(' '.join(current_line))
                current_line = []
        if current_line:
            lines.append(' '.join(current_line))
        
        # Get input video info
        probe_cmd = [self.ffmpeg_path, "-i", input_path]
        result = subprocess.run(probe_cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS)
        
        # Extract fps
        fps_match = re.search(r'(\d+(?:\.\d+)?)\s*fps', result.stderr)
        fps = float(fps_match.group(1)) if fps_match else 30
        
        # Extract resolution
        res_match = re.search(r'(\d{3,4})x(\d{3,4})', result.stderr)
        if res_match:
            width, height = int(res_match.group(1)), int(res_match.group(2))
        else:
            width, height = 1080, 1920
        
        # Create hook video: freeze first frame + TTS audio + text overlay
        hook_video = tempfile.NamedTemporaryFile(suffix='.mp4', delete=False).name
        
        # Build drawtext filter for each line
        # Style: Yellow/gold text on white background box
        drawtext_filters = []
        line_height = 85  # pixels between lines
        font_size = 58
        total_text_height = len(lines) * line_height
        start_y = (height // 3) - (total_text_height // 2)  # Position at upper third
        
        for i, line in enumerate(lines):
            # Escape special characters for FFmpeg drawtext
            escaped_line = line.replace("'", "'\\''").replace(":", "\\:").replace("\\", "\\\\")
            y_pos = start_y + (i * line_height)
            
            # Yellow/gold text with white box background
            font_path = self._get_ffmpeg_font_path()
            drawtext_filters.append(
                f"drawtext=text='{escaped_line}':"
                f"{font_path}"
                f"fontsize={font_size}:"
                f"fontcolor=#FFD166:"  # Golden yellow
                f"box=1:"
                f"boxcolor=white@0.95:"  # White background
                f"boxborderw=12:"  # Padding around text
                f"x=(w-text_w)/2:"
                f"y={y_pos}"
            )
        
        filter_chain = ",".join(drawtext_filters)
        
        # Get encoder args
        encoder_args = self.get_video_encoder_args()
        
        # Step 1: Create hook video with frozen frame + text + TTS audio
        # Use -t to set exact duration, freeze first frame
        cmd = [
            self.ffmpeg_path, "-y",
            "-i", input_path,
            "-i", tts_file,
            "-filter_complex",
            f"[0:v]trim=0:0.04,loop=loop=-1:size=1:start=0,setpts=N/{fps}/TB,{filter_chain},trim=0:{hook_duration},setpts=PTS-STARTPTS[v];"
            f"[1:a]aresample=44100,apad=whole_dur={hook_duration}[a]",
            "-map", "[v]",
            "-map", "[a]",
            *encoder_args,
            "-r", str(fps),
            "-s", f"{width}x{height}",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", "44100",
            "-ac", "2",
            "-t", str(hook_duration),
            hook_video
        ]
        self.log_ffmpeg_command(cmd, "Create Hook Video", step="hook")
        result = self._run_ffmpeg_subprocess(cmd)
        
        if result.returncode != 0:
            error_lines = result.stderr.split('\n') if result.stderr else []
            actual_errors = [line for line in error_lines if 'error' in line.lower()]
            error_msg = '\n'.join(actual_errors[-3:]) if actual_errors else "Unknown error"
            raise Exception(f"Failed to create hook video: {error_msg}")
        
        # Step 2: Re-encode main video to EXACT same format (critical for concat)
        main_reencoded = tempfile.NamedTemporaryFile(suffix='.mp4', delete=False).name
        cmd = [
            self.ffmpeg_path, "-y",
            "-i", input_path,
            *encoder_args,
            "-r", str(fps),
            "-s", f"{width}x{height}",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", "44100",
            "-ac", "2",
            main_reencoded
        ]
        self.log_ffmpeg_command(cmd, "Re-encode Main Video", step="hook")
        result = self._run_ffmpeg_subprocess(cmd)
        
        if result.returncode != 0:
            error_lines = result.stderr.split('\n') if result.stderr else []
            actual_errors = [line for line in error_lines if 'error' in line.lower()]
            error_msg = '\n'.join(actual_errors[-3:]) if actual_errors else "Unknown error"
            raise Exception(f"Failed to re-encode main video: {error_msg}")
        
        # Step 3: Concatenate using concat demuxer (more reliable than filter_complex)
        concat_list = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False).name
        with open(concat_list, 'w') as f:
            f.write(f"file '{hook_video.replace(chr(92), '/')}'\n")
            f.write(f"file '{main_reencoded.replace(chr(92), '/')}'\n")
        
        cmd = [
            self.ffmpeg_path, "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", concat_list,
            "-c", "copy",
            output_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS)
        
        # If concat demuxer fails, try filter_complex as fallback
        if result.returncode != 0:
            # Extract actual error message (skip ffmpeg version info)
            error_lines = result.stderr.split('\n') if result.stderr else []
            actual_errors = [line for line in error_lines if 'error' in line.lower() or 'invalid' in line.lower() or 'failed' in line.lower()]
            error_summary = '\n'.join(actual_errors[-3:]) if actual_errors else "Unknown concat error"
            
            self.log(f"  Concat demuxer failed: {error_summary[:100]}")
            self.log(f"  Trying filter_complex fallback...")
            
            cmd = [
                self.ffmpeg_path, "-y",
                "-i", hook_video,
                "-i", main_reencoded,
                "-filter_complex",
                "[0:v:0][0:a:0][1:v:0][1:a:0]concat=n=2:v=1:a=1[outv][outa]",
                "-map", "[outv]",
                "-map", "[outa]",
                *encoder_args,
                "-c:a", "aac",
                "-b:a", "192k",
                output_path
            ]
            self.log_ffmpeg_command(cmd, "Concat Hook (filter_complex fallback - old)", step="hook")
            result = self._run_ffmpeg_subprocess(cmd)
            
            if result.returncode != 0:
                # Extract actual error, not version info
                error_lines = result.stderr.split('\n') if result.stderr else []
                actual_errors = [line for line in error_lines if 'error' in line.lower() or 'invalid' in line.lower() or 'failed' in line.lower()]
                error_msg = '\n'.join(actual_errors[-3:]) if actual_errors else result.stderr[-200:] if result.stderr else "Unknown error"
                raise Exception(f"Failed to concatenate hook video: {error_msg}")
        
        # Cleanup
        try:
            os.unlink(tts_file)
        except Exception as e:
            pass  # Ignore cleanup errors
        
        try:
            os.unlink(hook_video)
        except Exception as e:
            pass
        
        try:
            os.unlink(main_reencoded)
        except Exception as e:
            pass
        
        try:
            os.unlink(concat_list)
        except Exception as e:
            pass
        
        # Verify output was created
        if not os.path.exists(output_path):
            raise Exception(f"Failed to create hook video at {output_path}")
        
        return hook_duration
    
    def add_captions_api(self, input_path: str, output_path: str, audio_source: str = None, time_offset: float = 0):
        """Add CapCut-style captions using OpenAI Whisper API
        
        Args:
            input_path: Video to burn captions into (with hook)
            output_path: Output video path
            audio_source: Video to extract audio from for transcription (without hook)
            time_offset: Offset to add to all timestamps (hook duration)
        """
        
        # Use audio_source if provided, otherwise use input_path
        transcribe_source = audio_source if audio_source else input_path
        
        # Extract audio from video - use WAV format for better compatibility
        audio_file = tempfile.NamedTemporaryFile(suffix='.wav', delete=False).name
        cmd = [
            self.ffmpeg_path, "-y",
            "-i", transcribe_source,
            "-vn",
            "-acodec", "pcm_s16le",  # PCM 16-bit WAV
            "-ar", "16000",  # 16kHz sample rate
            "-ac", "1",  # Mono
            audio_file
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS)
        
        if result.returncode != 0:
            self.log(f"  Warning: Audio extraction failed")
            import shutil
            shutil.copy(input_path, output_path)
            return
        
        # Check if audio file exists and has content
        if not os.path.exists(audio_file) or os.path.getsize(audio_file) < 1000:
            self.log(f"  Warning: Audio file too small or missing")
            import shutil
            shutil.copy(input_path, output_path)
            if os.path.exists(audio_file):
                os.unlink(audio_file)
            return
        
        # Get audio duration for token reporting
        probe_cmd = [self.ffmpeg_path, "-i", audio_file, "-f", "null", "-"]
        result = subprocess.run(probe_cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS)
        duration_match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", result.stderr)
        audio_duration = 0
        if duration_match:
            h, m, s = duration_match.groups()
            audio_duration = int(h) * 3600 + int(m) * 60 + float(s)
            self.report_tokens(0, 0, audio_duration, 0)
        
        # Transcribe using Whisper API (raw HTTP for proxy compatibility)
        try:
            transcript = self.transcribe_words(audio_file)
        except Exception as e:
            self.log(f"  ❌ Caption transcription FAILED: {e}")
            self.log("  Captions will be SKIPPED for this clip (video still saved without captions)")
            self._caption_failed = True
            import shutil
            shutil.copy(input_path, output_path)
            os.unlink(audio_file)
            return
        
        os.unlink(audio_file)
        
        # Create ASS subtitle file with time offset for hook
        ass_file = tempfile.NamedTemporaryFile(mode='w', suffix='.ass', delete=False, encoding='utf-8').name
        # Whisper word timestamps are systematically late -> compensate
        sync_offset = getattr(self, "subtitle_sync_offset", 0.0)
        ass_offset = time_offset + sync_offset
        if getattr(self, "subtitle_style", "pop") == "karaoke":
            self.create_ass_subtitle_karaoke(transcript, ass_file, ass_offset)
        elif getattr(self, "subtitle_style", "pop") == "bounce":
            self.create_ass_subtitle_bounce(transcript, ass_file, ass_offset)
        elif getattr(self, "subtitle_style", "pop") == "animated":
            self.create_ass_subtitle_animated(transcript, ass_file, ass_offset)
        elif getattr(self, "subtitle_style", "pop") == "pop_bounce":
            self.create_ass_subtitle_pop_bounce(transcript, ass_file, ass_offset)
        else:
            self.create_ass_subtitle_capcut(transcript, ass_file, ass_offset)
        
        # Burn subtitles into video using GPU/CPU encoder
        # Escape path for FFmpeg on Windows
        ass_path_escaped = ass_file.replace('\\', '/').replace(':', '\\:')
        
        encoder_args = self.get_video_encoder_args()
        cmd = [
            self.ffmpeg_path, "-y",
            "-i", input_path,
            "-vf", f"ass='{ass_path_escaped}'",
            *encoder_args,
            "-c:a", "copy",
            output_path
        ]
        
        self.log_ffmpeg_command(cmd, "Burn Captions", step="caption")
        result = self._run_ffmpeg_subprocess(cmd)
        os.unlink(ass_file)
        
        if result.returncode != 0:
            self.log(f"  ❌ Caption burn failed, copying without captions")
            self._caption_failed = True
            import shutil
            shutil.copy(input_path, output_path)
    
    def create_ass_subtitle_karaoke(self, transcript, output_path: str, time_offset: float = 0):
        """Create ASS subtitle file with KTV-style karaoke: the WHOLE sentence is
        shown while each word lights up in yellow (PrimaryColour) as it is spoken,
        with unspoken words staying gray (SecondaryColour).

        Uses \\kf karaoke timing tags — the word's fill fades in over its spoken
        duration, so the highlight follows the voice exactly like karaoke lyrics.
        Lines are grouped per sentence (segment); very long sentences are split
        into smaller lines so text never overflows the screen.
        """
        ass_content = """[Script Info]
Title: Karaoke captions
ScriptType: v4.00+
WrapStyle: 0
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial Black,62,&H0000FFFF&,&H00808080&,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,50,50,400,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        
        events = []
        words = list(getattr(transcript, 'words', None) or [])
        segments = list(getattr(transcript, 'segments', None) or [])
        
        def make_karaoke_line(chunk):
            """Build a karaoke dialogue event from a word chunk."""
            parts = []
            for w in chunk:
                dur_cs = max(1, int(round((w.end - w.start) * 100)))
                parts.append("{\\kf%d}%s" % (dur_cs, str(w.word).strip().upper()))
            return {
                'start': self.format_time(chunk[0].start + time_offset),
                'end': self.format_time(chunk[-1].end + time_offset),
                'text': " ".join(parts)
            }
        
        if words and segments:
            # Group words by sentence (segment) using time overlap
            for seg in segments:
                seg_words = [w for w in words
                             if w.start >= seg.get('start', 0) - 0.15
                             and w.start <= seg.get('end', 0) + 0.15]
                if not seg_words:
                    continue
                # Split very long sentences into lines of at most 8 words
                for i in range(0, len(seg_words), 8):
                    events.append(make_karaoke_line(seg_words[i:i + 8]))
        elif words:
            # No segments: split by pause gaps (>1.0s) into sentence-like lines
            current = []
            for w in words:
                if current and w.start - current[-1].end > 1.0:
                    events.append(make_karaoke_line(current))
                    current = []
                current.append(w)
            if current:
                events.append(make_karaoke_line(current))
        
        # Fallback: segment-level timestamps without word timing (plain display)
        elif segments:
            for segment in segments:
                start = segment.get('start', 0) + time_offset
                end = segment.get('end', 0) + time_offset
                text = segment.get('text', '').strip().upper()
                if text:
                    events.append({
                        'start': self.format_time(start),
                        'end': self.format_time(end),
                        'text': text
                    })
        
        for event in events:
            ass_content += f"Dialogue: 0,{event['start']},{event['end']},Default,,0,0,0,,{event['text']}\n"
        
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(ass_content)
    
    def create_ass_subtitle_bounce(self, transcript, output_path: str, time_offset: float = 0):
        """Create ASS subtitle file with word-by-word bounce-in animation:
        each word appears one at a time as it is spoken — hidden until its
        turn, then popping in yellow with a bounce (scale 0% -> 115% -> 100%),
        then fades back to white when the next word takes over.
        Long sentences are split into lines of at most 8 words.
        """
        ass_content = """[Script Info]
Title: Bounce captions
ScriptType: v4.00+
WrapStyle: 0
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial Black,62,&H0000FFFF&,&H0000FFFF&,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,50,50,400,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        
        events = []
        words = list(getattr(transcript, 'words', None) or [])
        segments = list(getattr(transcript, 'segments', None) or [])
        
        def make_bounce_line(chunk):
            chunk_start = chunk[0].start
            parts = []
            for w in chunk:
                d = max(1, int(round((w.end - w.start) * 1000)))
                offset_ms = max(0, int(round((w.start - chunk_start) * 1000)))
                p1 = offset_ms + max(1, int(d * 0.15))
                p2 = offset_ms + max(2, int(d * 0.40))
                end_ms = offset_ms + d  # back to white right after this word
                parts.append(
                    "{\\alpha&HFF&"
                    "\\t(%d,%d,1,\\alpha&H00&\\c&H00FFFF&\\fscx115\\fscy115)"
                    "\\t(%d,%d,1,\\fscx100\\fscy100)"
                    "\\t(%d,%d,1,\\c&HFFFFFF&)}%s"
                    % (offset_ms, p1, p1, p2, p2, end_ms,
                       str(w.word).strip().upper())
                )
            return {
                'start': self.format_time(chunk[0].start + time_offset),
                'end': self.format_time(chunk[-1].end + time_offset),
                'text': " ".join(parts)
            }
        
        if words and segments:
            for seg in segments:
                seg_words = [w for w in words
                             if w.start >= seg.get('start', 0) - 0.15
                             and w.start <= seg.get('end', 0) + 0.15]
                if not seg_words:
                    continue
                for i in range(0, len(seg_words), 8):
                    events.append(make_bounce_line(seg_words[i:i + 8]))
        elif words:
            current = []
            for w in words:
                if current and w.start - current[-1].end > 1.0:
                    events.append(make_bounce_line(current))
                    current = []
                current.append(w)
            if current:
                events.append(make_bounce_line(current))
        
        elif segments:
            for segment in segments:
                start = segment.get('start', 0) + time_offset
                end = segment.get('end', 0) + time_offset
                text = segment.get('text', '').strip().upper()
                if text:
                    events.append({
                        'start': self.format_time(start),
                        'end': self.format_time(end),
                        'text': text
                    })
        
        for event in events:
            ass_content += f"Dialogue: 0,{event['start']},{event['end']},Default,,0,0,0,,{event['text']}\n"
        
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(ass_content)
    
    def create_ass_subtitle_pop_bounce(self, transcript, output_path: str, time_offset: float = 0):
        """Create ASS subtitle file with Pop + Bounce captions:
        one line shows 4 words (all visible white), and when each word is
        spoken it turns YELLOW with a bounce overshoot animation
        (100% -> 130% -> 90% -> 105% -> 100%), then fades back to white
        when the next word takes over. CapCut-style word highlight + bounce.
        """
        ass_content = """[Script Info]
Title: Pop bounce captions
ScriptType: v4.00+
WrapStyle: 0
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial Black,65,&H00FFFFFF,&H00808080&,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,50,50,400,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        
        events = []
        words = list(getattr(transcript, 'words', None) or [])
        segments = list(getattr(transcript, 'segments', None) or [])
        CHUNK = 4  # max words per line
        
        def make_line(chunk):
            chunk_start = chunk[0].start
            chunk_end = chunk[-1].end
            parts = []
            for k, w in enumerate(chunk):
                d = max(1, int(round((w.end - w.start) * 1000)))
                offset_ms = max(0, int(round((w.start - chunk_start) * 1000)))
                t1 = offset_ms + max(1, int(d * 0.15))
                t2 = offset_ms + max(2, int(d * 0.30))
                t3 = offset_ms + max(3, int(d * 0.45))
                t4 = offset_ms + max(4, int(d * 0.60))
                t5 = offset_ms + d  # back to white right after this word
                parts.append(
                    "{\\c&HFFFFFF&"
                    "\\t(%d,%d,1,\\c&H00FFFF&\\fscx130\\fscy130)"
                    "\\t(%d,%d,1,\\fscx90\\fscy90)"
                    "\\t(%d,%d,1,\\fscx105\\fscy105)"
                    "\\t(%d,%d,1,\\fscx100\\fscy100)"
                    "\\t(%d,%d,1,\\c&HFFFFFF&)}%s"
                    % (offset_ms, t1, t1, t2, t2, t3, t3, t4, t4, t5,
                       str(w.word).strip().upper())
                )
            return {
                'start': self.format_time(chunk_start + time_offset),
                'end': self.format_time(chunk_end + time_offset),
                'text': " ".join(parts)
            }
        
        if words and segments:
            # Group words by sentence (segment) using time overlap
            for seg in segments:
                seg_words = [w for w in words
                             if w.start >= seg.get('start', 0) - 0.15
                             and w.start <= seg.get('end', 0) + 0.15]
                if not seg_words:
                    continue
                for i in range(0, len(seg_words), CHUNK):
                    events.append(make_line(seg_words[i:i + CHUNK]))
        elif words:
            # No segments: split by pause gaps (>1.0s) into sentence-like lines
            current = []
            for w in words:
                if current and w.start - current[-1].end > 1.0:
                    for i in range(0, len(current), CHUNK):
                        events.append(make_line(current[i:i + CHUNK]))
                    current = []
                current.append(w)
            if current:
                for i in range(0, len(current), CHUNK):
                    events.append(make_line(current[i:i + CHUNK]))
        
        # Fallback: segment-level timestamps without word timing (plain display)
        elif segments:
            for segment in segments:
                start = segment.get('start', 0) + time_offset
                end = segment.get('end', 0) + time_offset
                text = segment.get('text', '').strip().upper()
                if text:
                    events.append({
                        'start': self.format_time(start),
                        'end': self.format_time(end),
                        'text': text
                    })
        
        for event in events:
            ass_content += f"Dialogue: 0,{event['start']},{event['end']},Default,,0,0,0,,{event['text']}\n"
        
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(ass_content)
    
    def create_ass_subtitle_animated(self, transcript, output_path: str, time_offset: float = 0):
        """Create ASS subtitle file with combined Bounce + Animated word-by-word
        captions: words are grouped into short lines, each word appears one at a
        time as it is spoken with a multi-stage BOUNCE animation
        (0% -> 125% -> 95% -> 105% -> 100%). The current word is highlighted
        in yellow, previous words stay white, upcoming words stay gray —
        like CapCut/TikTok animated captions with a bouncy overshoot.
        """
        ass_content = """[Script Info]
Title: Bounce word-by-word captions
ScriptType: v4.00+
WrapStyle: 0
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial Black,65,&H00FFFFFF,&H00808080&,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,50,50,400,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        
        events = []
        words = list(getattr(transcript, 'words', None) or [])
        segments = list(getattr(transcript, 'segments', None) or [])
        
        def make_animated_line(chunk):
            """Build an animated dialogue event from a word chunk."""
            chunk_start = chunk[0].start
            parts = []
            for k, w in enumerate(chunk):
                d = max(1, int(round((w.end - w.start) * 1000)))
                offset_ms = max(0, int(round((w.start - chunk_start) * 1000)))
                t1 = offset_ms + max(1, int(d * 0.15))
                t2 = offset_ms + max(2, int(d * 0.30))
                t3 = offset_ms + max(3, int(d * 0.45))
                t4 = offset_ms + max(4, int(d * 0.60))
                bounce = ("{\\c&H00FFFF&\\fscx0\\fscy0"
                          "\\t(%d,%d,1,\\fscx125\\fscy125)"
                          "\\t(%d,%d,1,\\fscx95\\fscy95)"
                          "\\t(%d,%d,1,\\fscx105\\fscy105)"
                          "\\t(%d,%d,1,\\fscx100\\fscy100)}%s"
                          % (offset_ms, t1, t1, t2, t2, t3, t3, t4,
                             str(w.word).strip().upper()))
                if k == len(chunk) - 1:
                    parts.append(bounce)
                else:
                    parts.append(bounce + "{\\c&HFFFFFF&}")
            return {
                'start': self.format_time(chunk[0].start + time_offset),
                'end': self.format_time(chunk[-1].end + time_offset),
                'text': " ".join(parts)
            }
        
        if words and segments:
            # Group words by sentence (segment) using time overlap
            for seg in segments:
                seg_words = [w for w in words
                             if w.start >= seg.get('start', 0) - 0.15
                             and w.start <= seg.get('end', 0) + 0.15]
                if not seg_words:
                    continue
                # Split very long sentences into lines of at most 8 words
                for i in range(0, len(seg_words), 8):
                    events.append(make_animated_line(seg_words[i:i + 8]))
        elif words:
            # No segments: split by pause gaps (>1.0s) into sentence-like lines
            current = []
            for w in words:
                if current and w.start - current[-1].end > 1.0:
                    events.append(make_animated_line(current))
                    current = []
                current.append(w)
            if current:
                events.append(make_animated_line(current))
        
        # Fallback: segment-level timestamps without word timing (plain display)
        elif segments:
            for segment in segments:
                start = segment.get('start', 0) + time_offset
                end = segment.get('end', 0) + time_offset
                text = segment.get('text', '').strip().upper()
                if text:
                    events.append({
                        'start': self.format_time(start),
                        'end': self.format_time(end),
                        'text': text
                    })
        
        for event in events:
            ass_content += f"Dialogue: 0,{event['start']},{event['end']},Default,,0,0,0,,{event['text']}\n"
        
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(ass_content)
    
    def create_ass_subtitle_capcut(self, transcript, output_path: str, time_offset: float = 0):
        """Create ASS subtitle file with CapCut-style word-by-word highlighting"""
        
        # ASS header - CapCut style: white text, yellow highlight, black outline
        ass_content = """[Script Info]
Title: Auto-generated captions
ScriptType: v4.00+
WrapStyle: 0
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial Black,65,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,50,50,400,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        
        events = []
        
        # Check if we have word-level timestamps
        if hasattr(transcript, 'words') and transcript.words:
            words = transcript.words
            
            # Group words into chunks (3-4 words per line for readability)
            chunk_size = 4
            
            for i in range(0, len(words), chunk_size):
                chunk = words[i:i + chunk_size]
                if not chunk:
                    continue
                
                # For each word in the chunk, create a subtitle event with that word highlighted
                for j, current_word in enumerate(chunk):
                    # Add time_offset to account for hook duration
                    word_start = current_word.start + time_offset
                    word_end = current_word.end + time_offset
                    
                    # Build text with current word highlighted in yellow
                    text_parts = []
                    for k, w in enumerate(chunk):
                        word_text = w.word.strip().upper()
                        if k == j:
                            # Highlight current word (yellow: &H00FFFF in BGR)
                            text_parts.append(f"{{\\c&H00FFFF&}}{word_text}{{\\c&HFFFFFF&}}")
                        else:
                            text_parts.append(word_text)
                    
                    text = " ".join(text_parts)
                    
                    events.append({
                        'start': self.format_time(word_start),
                        'end': self.format_time(word_end),
                        'text': text
                    })
        
        # Fallback: use segment-level timestamps if no word timestamps
        elif hasattr(transcript, 'segments') and transcript.segments:
            for segment in transcript.segments:
                start = segment.get('start', 0) + time_offset
                end = segment.get('end', 0) + time_offset
                text = segment.get('text', '').strip().upper()
                
                if text:
                    events.append({
                        'start': self.format_time(start),
                        'end': self.format_time(end),
                        'text': text
                    })
        
        # Write events to ASS file
        for event in events:
            ass_content += f"Dialogue: 0,{event['start']},{event['end']},Default,,0,0,0,,{event['text']}\n"
        
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(ass_content)
    
    def format_time(self, seconds: float) -> str:
        """Convert seconds to ASS time format"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        centisecs = int((seconds % 1) * 100)
        return f"{hours}:{minutes:02d}:{secs:02d}.{centisecs:02d}"
    
    RATIO_DIMENSIONS = {
        "9:16": (1080, 1920),
        "1:1": (1080, 1080),
        "4:5": (1080, 1350),
        "16:9": (1920, 1080),
    }
    
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
    
    def _get_ratio_dimensions(self):
        """Get (out_w, out_h) for the configured aspect ratio."""
        return self.RATIO_DIMENSIONS.get(self.aspect_ratio, (1080, 1920))
    
    def _get_crop_window(self, orig_w: int, orig_h: int):
        """Compute (crop_w, crop_h) for the configured aspect ratio, clamped to the
        source video dimensions so the crop never exceeds the frame."""
        out_w, out_h = self._get_ratio_dimensions()
        target_ratio = out_w / out_h
        crop_w = int(orig_h * target_ratio)
        crop_h = orig_h
        if crop_w > orig_w:
            crop_w = orig_w
            crop_h = int(crop_w / target_ratio)
        return crop_w, crop_h
    
    @staticmethod
    def _unit_multiplier(unit: str) -> float:
        """Byte multiplier for a unit string like 'MiB', 'KB', 'GiB/s'."""
        unit = unit.replace("/s", "").upper()
        if "I" in unit:
            base = 1024.0
        else:
            base = 1000.0
        if unit.startswith("K"):
            return base
        if unit.startswith("M"):
            return base ** 2
        if unit.startswith("G"):
            return base ** 3
        if unit.startswith("T"):
            return base ** 4
        return 1.0
    
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
        while True:
            if self.is_cancelled():
                process.terminate()
                process.wait()
                raise Exception("Cancelled by user")

            poll = process.poll()
            if output_path and os.path.exists(output_path):
                try:
                    cur_size = os.path.getsize(output_path)
                except OSError:
                    cur_size = 0
                now = time.time()
                if cur_size != last_size and (now - last_report) >= 1.0:
                    last_size = cur_size
                    last_report = now
                    size_str = self._human_bytes(cur_size)
                    self.set_progress(
                        f"Encoding... {size_str}",
                        min(cur_size / 200_000_000, 0.95) if cur_size else 0.05
                    )
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
    
    def convert_to_portrait_blur(self, input_path: str, output_path: str):
        """Convert landscape to 9:16 portrait WITHOUT cropping: the whole video is
        kept visible (fit to height, centered), and a blurred zoomed copy fills
        the empty sides as background."""
        return self.convert_to_portrait_blur_with_progress(input_path, output_path, None)

    def convert_to_portrait_blur_with_progress(self, input_path: str, output_path: str, progress_callback):
        """Blurred-background conversion (no cropping) with progress."""
        out_w, out_h = self._get_ratio_dimensions()
        fd, script_path = tempfile.mkstemp(suffix=".txt", prefix="portrait_blur_", text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(
                    f"[0:v]split=2[bg][fg];"
                    f"[bg]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
                    f"crop={out_w}:{out_h},gblur=sigma=24,eq=brightness=-0.1:saturation=1.15[bgb];"
                    f"[fg]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,setsar=1[fgs];"
                    f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]"
                )
            encoder_args = self.get_video_encoder_args()
            cmd = [
                self.ffmpeg_path, "-y",
                "-i", input_path,
                "-filter_complex_script", script_path,
                "-map", "[v]", "-map", "0:a?",
                *encoder_args,
                "-c:a", "aac", "-b:a", "192k",
                "-shortest",
                output_path,
            ]
            self.log_ffmpeg_command(cmd, "Portrait Blur (no crop)", step="portrait")
            if progress_callback is not None:
                self.run_ffmpeg_with_progress(cmd, 0, progress_callback)
            else:
                result = self._run_ffmpeg_subprocess(cmd)
                if result.returncode != 0:
                    stderr = (result.stderr or "")[-2000:]
                    raise Exception(f"Portrait blur encode failed:\n{stderr}")
        finally:
            try:
                os.unlink(script_path)
            except OSError:
                pass
        self.log("  Blurred background conversion complete")

    def convert_to_portrait_with_progress(self, input_path: str, output_path: str, progress_callback):
        """Convert landscape to 9:16 portrait with speaker tracking and progress (router method)"""
        if self.portrait_mode == "blur":
            self.log("  Using Blurred Background (no crop)")
            return self.convert_to_portrait_blur_with_progress(input_path, output_path, progress_callback)
        try:
            if self.face_tracking_mode == "mediapipe":
                self.log("  Using MediaPipe (Active Speaker Detection)")
                return self.convert_to_portrait_mediapipe_with_progress(input_path, output_path, progress_callback)
            else:
                self.log("  Using OpenCV (Fast Mode)")
                return self.convert_to_portrait_opencv_with_progress(input_path, output_path, progress_callback)
        except Exception as e:
            # Fallback to OpenCV if MediaPipe fails
            if self.face_tracking_mode == "mediapipe":
                self.log(f"  ⚠ MediaPipe failed: {e}")
                self.log("  Falling back to OpenCV mode...")
                return self.convert_to_portrait_opencv_with_progress(input_path, output_path, progress_callback)
            else:
                raise
    
    def convert_to_portrait_opencv_with_progress(self, input_path: str, output_path: str, progress_callback):
        """Convert landscape to 9:16 portrait with speaker tracking and progress (OpenCV)"""
        
        self.log("[DEBUG] Starting portrait conversion...")
        print("[DEBUG] Starting portrait conversion...")
        print(f"[DEBUG] Input: {input_path}")
        print(f"[DEBUG] Output: {output_path}")
        sys.stdout.flush()
        
        cap = cv2.VideoCapture(input_path)
        if not cap.isOpened():
            raise Exception(f"Failed to open video: {input_path}")
        
        orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        
        self.log(f"[DEBUG] Video: {orig_w}x{orig_h}, {fps}fps, {total_frames} frames")
        print(f"[DEBUG] Video: {orig_w}x{orig_h}, {fps}fps, {total_frames} frames")
        sys.stdout.flush()
        
        if total_frames == 0 or fps == 0:
            cap.release()
            raise Exception(f"Invalid video properties: {total_frames} frames, {fps} fps")
        
        # Calculate crop dimensions
        crop_w, crop_h = self._get_crop_window(orig_w, orig_h)
        out_w, out_h = self._get_ratio_dimensions()
        
        # Face detector
        face_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
        )
        
        # First pass: analyze frames (0-40%)
        print("[DEBUG] Pass 1: Analyzing frames... (fast mode: every 5th frame)")
        sys.stdout.flush()
        
        crop_positions = []
        current_target = orig_w / 2
        frame_count = 0
        last_log_time = 0
        import time
        
        ANALYSIS_STEP = 5
        ANALYSIS_MAX_WIDTH = 640
        scale = min(1.0, ANALYSIS_MAX_WIDTH / orig_w)
        
        analyzed_indices = []
        analyzed_positions = []
        frames_read = 0
        
        while True:
            # Check for cancellation
            if self.is_cancelled():
                cap.release()
                raise Exception("Cancelled by user")
            
            if frames_read % ANALYSIS_STEP != 0:
                ret = cap.grab()
                if not ret:
                    break
                frames_read += 1
                continue
            
            ret, frame = cap.read()
            if not ret:
                break
            
            if scale < 1.0:
                small = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            else:
                small = frame
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            faces = face_cascade.detectMultiScale(gray, 1.1, 5, minSize=(50, 50))
            
            if len(faces) > 0:
                # Find largest face
                largest = max(faces, key=lambda f: f[2] * f[3])
                current_target = (largest[0] + largest[2] / 2) / scale
            
            crop_x = int(current_target - crop_w / 2)
            crop_x = max(0, min(crop_x, orig_w - crop_w))
            analyzed_indices.append(frames_read)
            analyzed_positions.append(crop_x)
            
            frame_count += 1
            frames_read += 1
            
            # Update progress more frequently with time-based logging
            current_time = time.time()
            if frames_read % 150 == 0 or (current_time - last_log_time) > 2:  # Every 150 frames or 2 seconds
                progress = (frames_read / total_frames) * 0.4  # 0-40%
                print(f"[DEBUG] Pass 1 progress: {progress*100:.1f}% ({frames_read}/{total_frames} frames)")
                sys.stdout.flush()
                progress_callback(progress)
                last_log_time = current_time
        
        print(f"[DEBUG] Analyzed {frame_count} frames (sampled)")
        sys.stdout.flush()
        
        # Interpolate positions for every frame
        crop_positions = self._interpolate_sampled(analyzed_positions, analyzed_indices, frames_read)
        
        # Stabilize positions
        crop_positions = self.stabilize_positions(crop_positions)
        progress_callback(0.45)
        
        # Second pass: single ffmpeg command (45-85%)
        print("[DEBUG] Pass 2: Encoding portrait video (single ffmpeg pass, crop + audio)...")
        sys.stdout.flush()
        
        self._encode_portrait_single_pass(
            input_path, output_path, crop_positions, crop_w, crop_h, out_w, out_h,
            progress_callback=lambda p: progress_callback(0.45 + p * 0.4),
            duration=frames_read / fps if fps else 0,
        )
        cap.release()
        
        print("[DEBUG] Portrait encode complete")
        sys.stdout.flush()
        
        progress_callback(0.85)
        
        print("[DEBUG] Portrait conversion complete")
        sys.stdout.flush()
    
    def convert_to_portrait_mediapipe_with_progress(self, input_path: str, output_path: str, progress_callback):
        """Convert landscape to 9:16 portrait with active speaker detection and progress (MediaPipe)"""
        
        # Initialize MediaPipe
        self._init_mediapipe()
        
        self.log("[DEBUG] Starting MediaPipe portrait conversion...")
        print("[DEBUG] Starting MediaPipe portrait conversion...")
        sys.stdout.flush()
        
        cap = cv2.VideoCapture(input_path)
        if not cap.isOpened():
            raise Exception(f"Failed to open video: {input_path}")
        
        orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        
        self.log(f"[DEBUG] Video: {orig_w}x{orig_h}, {fps}fps, {total_frames} frames")
        print(f"[DEBUG] Video: {orig_w}x{orig_h}, {fps}fps, {total_frames} frames")
        sys.stdout.flush()
        
        if total_frames == 0 or fps == 0:
            cap.release()
            raise Exception(f"Invalid video properties: {total_frames} frames, {fps} fps")
        
        # Calculate crop dimensions
        crop_w, crop_h = self._get_crop_window(orig_w, orig_h)
        out_w, out_h = self._get_ratio_dimensions()
        
        # MediaPipe settings
        lip_threshold = self.mediapipe_settings.get("lip_activity_threshold", 0.15)
        switch_threshold = self.mediapipe_settings.get("switch_threshold", 0.3)
        min_shot_duration = self.mediapipe_settings.get("min_shot_duration", 90)
        center_weight = self.mediapipe_settings.get("center_weight", 0.3)
        
        # First pass: analyze frames with MediaPipe (0-40%)
        print("[DEBUG] Pass 1: Analyzing lip movements with MediaPipe... (fast mode: every 5th frame)")
        sys.stdout.flush()
        
        crop_positions = []
        face_activities = []
        frame_count = 0
        last_log_time = 0
        import time
        
        ANALYSIS_STEP = 5
        ANALYSIS_MAX_WIDTH = 640
        scale = min(1.0, ANALYSIS_MAX_WIDTH / orig_w)
        
        analyzed_indices = []
        analyzed_positions = []
        analyzed_activities = []
        frames_read = 0
        prev_lip_distances = {}
        
        while True:
            if self.is_cancelled():
                cap.release()
                raise Exception("Cancelled by user")
            
            if frames_read % ANALYSIS_STEP != 0:
                ret = cap.grab()
                if not ret:
                    break
                frames_read += 1
                continue
            
            ret, frame = cap.read()
            if not ret:
                break
            
            # Downscale for faster inference (coordinates are normalized)
            if scale < 1.0:
                small = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            else:
                small = frame
            
            # Convert to RGB for MediaPipe
            rgb_frame = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)
            results = self.mp_face_landmarker.detect(mp_image)
            
            best_face_x = orig_w / 2
            max_activity = 0
            
            if results.face_landmarks:
                faces_data = []
                
                for face_id, face_landmarks in enumerate(results.face_landmarks):
                    # Calculate lip activity
                    activity = self._calculate_lip_activity(
                        face_landmarks,
                        orig_w,
                        orig_h,
                        prev_lip_distances.get(face_id, None)
                    )
                    
                    # Get face center position (landmark 1 is nose tip)
                    face_x = face_landmarks[1].x * orig_w
                    
                    # Combined score
                    center_score = 1.0 - abs(face_x - orig_w / 2) / (orig_w / 2)
                    combined_score = (activity * (1 - center_weight)) + (center_score * center_weight)
                    
                    faces_data.append({
                        'x': face_x,
                        'activity': activity,
                        'combined_score': combined_score
                    })
                    
                    # Update previous lip distance
                    upper_lip = face_landmarks[13]
                    lower_lip = face_landmarks[14]
                    lip_distance = abs(upper_lip.y - lower_lip.y)
                    prev_lip_distances[face_id] = lip_distance
                
                if faces_data:
                    best_face = max(faces_data, key=lambda f: f['combined_score'])
                    best_face_x = best_face['x']
                    max_activity = best_face['activity']
            
            crop_x = int(best_face_x - crop_w / 2)
            crop_x = max(0, min(crop_x, orig_w - crop_w))
            analyzed_indices.append(frames_read)
            analyzed_positions.append(crop_x)
            analyzed_activities.append(max_activity)
            
            frame_count += 1
            frames_read += 1
            
            current_time = time.time()
            if frames_read % 150 == 0 or (current_time - last_log_time) > 2:
                progress = (frames_read / total_frames) * 0.4
                print(f"[DEBUG] Pass 1 progress: {progress*100:.1f}% ({frames_read}/{total_frames} frames)")
                sys.stdout.flush()
                progress_callback(progress)
                last_log_time = current_time
        
        print(f"[DEBUG] Analyzed {frame_count} frames with MediaPipe (sampled)")
        sys.stdout.flush()
        
        # Interpolate to one position/activity per frame
        crop_positions = self._interpolate_sampled(analyzed_positions, analyzed_indices, frames_read)
        face_activities = self._interpolate_sampled(analyzed_activities, analyzed_indices, frames_read)
        
        # Stabilize positions (40-45%)
        progress_callback(0.4)
        if self.mediapipe_settings.get("smooth_follow", True):
            print("[DEBUG] Smooth face follow enabled: camera pans continuously")
            sys.stdout.flush()
            crop_positions = self._smooth_follow_positions(
                crop_positions,
                self.mediapipe_settings.get("pan_speed_limit", 2.5)
            )
        else:
            crop_positions = self._stabilize_positions_with_activity(
                crop_positions,
                face_activities,
                min_shot_duration,
                switch_threshold
            )
        progress_callback(0.45)
        
        # Second pass: single ffmpeg command (45-85%)
        print("[DEBUG] Pass 2: Encoding portrait video (single ffmpeg pass, crop + audio)...")
        sys.stdout.flush()
        
        self._encode_portrait_single_pass(
            input_path, output_path, crop_positions, crop_w, crop_h, out_w, out_h,
            progress_callback=lambda p: progress_callback(0.45 + p * 0.4),
            duration=frames_read / fps if fps else 0,
        )
        cap.release()
        
        print("[DEBUG] Portrait encode complete")
        sys.stdout.flush()
        
        progress_callback(0.85)
        
        print("[DEBUG] MediaPipe portrait conversion complete")
        sys.stdout.flush()
    
    def add_hook_with_progress(self, input_path: str, hook_text: str, output_path: str, progress_callback) -> float:
        """Add hook scene at the beginning with progress tracking"""
        
        # Report TTS character usage
        self.report_tokens(0, 0, 0, len(hook_text))
        
        # Generate TTS audio (10% progress)
        progress_callback(0.1)
        try:
            tts_response = self.tts_client.audio.speech.create(
                model=self.tts_model,
                voice="nova",
                input=hook_text,
                speed=1.0
            )
        except APIConnectionError as e:
            self.log(f"  ❌ TTS API Connection Error: Could not connect to {self.tts_client.base_url}")
            raise Exception(f"TTS API connection failed!\n\nCould not connect to: {self.tts_client.base_url}\nError: {e}")
        except RateLimitError as e:
            self.log(f"  ❌ TTS API Rate Limit: {e}")
            raise Exception(f"TTS API rate limit exceeded!\n\nPlease wait a moment and try again.\nDetails: {e}")
        except APIStatusError as e:
            self.log(f"  ❌ TTS API Error (HTTP {e.status_code}): {e.message}")
            self.log(f"     Model: {self.tts_model}, Base URL: {self.tts_client.base_url}")
            raise Exception(
                f"TTS (Hook) API Error!\n\n"
                f"Status: {e.status_code}\n"
                f"Message: {e.message}\n"
                f"Model: {self.tts_model}\n"
                f"Base URL: {self.tts_client.base_url}\n\n"
                f"Check your Hook Maker API settings."
            )
        except Exception as e:
            self.log(f"  ❌ TTS API Unexpected Error: {type(e).__name__}: {e}")
            raise Exception(f"TTS (Hook) generation failed!\n\nError: {type(e).__name__}: {e}\nModel: {self.tts_model}")
        
        tts_file = str(Path(output_path).parent / "hook_tts.mp3")
        with open(tts_file, 'wb') as f:
            f.write(tts_response.content)
        
        progress_callback(0.2)
        
        # Get TTS duration using ffprobe
        probe_cmd = [
            self.ffmpeg_path, "-i", tts_file,
            "-f", "null", "-"
        ]
        result = subprocess.run(probe_cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS)
        duration_match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", result.stderr)
        
        if duration_match:
            h, m, s = duration_match.groups()
            hook_duration = int(h) * 3600 + int(m) * 60 + float(s) + 0.5
        else:
            hook_duration = 3.0
        
        # Format hook text
        hook_upper = hook_text.upper()
        words = hook_upper.split()
        
        lines = []
        current_line = []
        for word in words:
            current_line.append(word)
            if len(current_line) >= 3:
                lines.append(' '.join(current_line))
                current_line = []
        if current_line:
            lines.append(' '.join(current_line))
        
        # Get input video info
        probe_cmd = [self.ffmpeg_path, "-i", input_path]
        result = subprocess.run(probe_cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS)
        
        fps_match = re.search(r'(\d+(?:\.\d+)?)\s*fps', result.stderr)
        fps = float(fps_match.group(1)) if fps_match else 30
        
        res_match = re.search(r'(\d{3,4})x(\d{3,4})', result.stderr)
        if res_match:
            width, height = int(res_match.group(1)), int(res_match.group(2))
        else:
            width, height = 1080, 1920
        
        progress_callback(0.3)
        
        # Create hook video in our temp directory
        hook_video = str(self.temp_dir / f"hook_{int(time.time() * 1000)}.mp4")
        
        # Use a simpler approach: create static image with text, then combine with audio
        # This avoids complex FFmpeg filter escaping issues
        
        # First, create a simple background video from the first frame using GPU/CPU encoder.
        # Robust strategy: extract one frame as PNG, then loop it as a static image video.
        # (The old trim+loop filter chain is flaky on some inputs/encoders.)
        bg_video = str(self.temp_dir / f"hook_bg_{int(time.time() * 1000)}.mp4")
        frame_png = str(self.temp_dir / f"hook_frame_{int(time.time() * 1000)}.png")
        
        encoder_args = self.get_video_encoder_args()
        
        def _bg_ok() -> bool:
            return os.path.exists(bg_video) and os.path.getsize(bg_video) >= 1000
        
        # Step 1: extract the first frame to a PNG (letterboxed to target size)
        frame_cmd = [
            self.ffmpeg_path, "-y",
            "-i", input_path,
            "-frames:v", "1",
            "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                   f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black",
            "-q:v", "2",
            frame_png
        ]
        self.log_ffmpeg_command(frame_cmd, "Extract Hook Background Frame", step="hook")
        self._run_ffmpeg_subprocess(frame_cmd)
        
        # Step 2: loop the frame PNG into a video of hook_duration
        bg_cmd = [
            self.ffmpeg_path, "-y",
            "-loop", "1",
            "-i", frame_png,
            "-t", str(hook_duration),
            *encoder_args,
            "-r", str(fps),
            "-s", f"{width}x{height}",
            "-pix_fmt", "yuv420p",
            "-an",
            bg_video
        ]
        self.log_ffmpeg_command(bg_cmd, "Create Hook Background", step="hook")
        result = self._run_ffmpeg_subprocess(bg_cmd)
        if result.returncode != 0:
            self.log(f"Failed to create background video: {result.stderr}")
        
        if not _bg_ok():
            # Fallback: solid dark background (never let the hook hard-fail here)
            self.log("  ⚠ Background frame loop failed, using solid dark background")
            bg_cmd = [
                self.ffmpeg_path, "-y",
                "-f", "lavfi",
                "-i", f"color=c=0x141414:s={width}x{height}:r={fps}",
                "-t", str(hook_duration),
                *encoder_args,
                "-pix_fmt", "yuv420p",
                "-an",
                bg_video
            ]
            self.log_ffmpeg_command(bg_cmd, "Create Hook Background (fallback)", step="hook")
            result = self._run_ffmpeg_subprocess(bg_cmd)
        
        # Verify background video was created successfully
        if not _bg_ok():
            raise Exception("Background video was not created properly")
        
        # === Render hook overlay using PIL (supports user-customized font, colors, corners) ===
        from PIL import Image, ImageDraw, ImageFont

        style = self.hook_style_settings or {}
        font_size_frac = float(style.get("font_size", 0.054))
        font_color_hex = style.get("font_color", "#FFD166")
        bg_color_hex = style.get("bg_color", "#FFFFFF")
        corner_radius = int(style.get("corner_radius", 0))
        pos_x = float(style.get("position_x", 0.5))
        pos_y = float(style.get("position_y", 0.333))
        user_font_path = style.get("font_path") or ""

        # Resolve font path with sensible fallbacks
        font_candidates = [user_font_path, self._find_system_font_bold()]
        pil_font = None
        font_px = max(20, int(font_size_frac * width))
        for candidate in font_candidates:
            if not candidate or not os.path.exists(candidate):
                continue
            try:
                pil_font = ImageFont.truetype(candidate, font_px)
                self.log(f"  Hook font: {candidate} @ {font_px}px")
                break
            except Exception as e:
                self.log(f"  ⚠ Failed to load font {candidate}: {e}")
        if pil_font is None:
            self.log("  ⚠ No usable TTF font found, using PIL default (will look basic)")
            pil_font = ImageFont.load_default()

        font_color_rgb = _hex_to_rgb(font_color_hex)
        bg_color_rgb = _hex_to_rgb(bg_color_hex)

        # Per-line geometry
        padding = max(10, int(font_px * 0.22))
        line_spacing = max(6, int(font_px * 0.25))

        line_metrics = []
        for line in lines:
            try:
                bbox = pil_font.getbbox(line)
            except AttributeError:
                w, h = pil_font.getsize(line)
                bbox = (0, 0, w, h)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
            line_metrics.append({
                "text": line,
                "bbox": bbox,
                "box_w": text_w + padding * 2,
                "box_h": text_h + padding * 2,
            })

        total_h = sum(m["box_h"] for m in line_metrics)
        if len(line_metrics) > 1:
            total_h += line_spacing * (len(line_metrics) - 1)

        center_x = int(pos_x * width)
        center_y = int(pos_y * height)
        block_top = center_y - total_h // 2

        # Compose the static overlay (transparent everywhere except the hook boxes)
        overlay_img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay_img)

        cur_y = block_top
        for m in line_metrics:
            box_w = m["box_w"]
            box_h = m["box_h"]
            box_x1 = center_x - box_w // 2
            box_y1 = cur_y
            box_x2 = box_x1 + box_w
            box_y2 = box_y1 + box_h

            if corner_radius > 0 and hasattr(draw, "rounded_rectangle"):
                # Clamp radius so it never exceeds half the smaller dimension
                r = min(corner_radius, box_w // 2, box_h // 2)
                draw.rounded_rectangle(
                    [box_x1, box_y1, box_x2, box_y2],
                    radius=r,
                    fill=(*bg_color_rgb, 255),
                )
            else:
                draw.rectangle(
                    [box_x1, box_y1, box_x2, box_y2],
                    fill=(*bg_color_rgb, 255),
                )

            # PIL draws text at the top-left of the glyph bounding box;
            # subtract bbox[0]/[1] so the glyphs sit cleanly inside the padding.
            text_x = box_x1 + padding - m["bbox"][0]
            text_y = box_y1 + padding - m["bbox"][1]
            draw.text(
                (text_x, text_y),
                m["text"],
                font=pil_font,
                fill=(*font_color_rgb, 255),
            )

            cur_y = box_y2 + line_spacing

        overlay_png = str(self.temp_dir / f"hook_overlay_{int(time.time() * 1000)}.png")
        overlay_img.save(overlay_png, "PNG")
        progress_callback(0.4)

        # Composite overlay on the (frozen) background video in one FFmpeg pass
        overlay_video = str(self.temp_dir / f"hook_overlay_video_{int(time.time() * 1000)}.mp4")
        encoder_args = self.get_video_encoder_args()
        overlay_cmd = [
            self.ffmpeg_path, "-y",
            "-i", bg_video,
            "-i", overlay_png,
            "-filter_complex", "[0:v][1:v]overlay=0:0[v]",
            "-map", "[v]",
            *encoder_args,
            "-pix_fmt", "yuv420p",
            "-an",
            overlay_video,
        ]
        self.log_ffmpeg_command(overlay_cmd, "Composite Hook Overlay (PIL)", step="hook")
        result = self._run_ffmpeg_subprocess(overlay_cmd)
        if result.returncode != 0:
            self.log(f"Failed to composite hook overlay: {result.stderr}")
            raise Exception("Failed to composite hook overlay video")

        if not os.path.exists(overlay_video) or os.path.getsize(overlay_video) < 1000:
            raise Exception("Hook overlay video was not created properly")

        progress_callback(0.55)

        # Both names point at the same file so the rest of the pipeline (audio mux,
        # cleanup) keeps working without further changes.
        current_video = overlay_video
        reencoded_video = overlay_video

        
        # Finally, add audio to re-encoded video
        cmd = [
            self.ffmpeg_path, "-y",
            "-i", reencoded_video,
            "-i", tts_file,
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", "44100",
            "-ac", "2",
            "-shortest",
            hook_video
        ]
        
        # Hook creation is 30-60%
        self.run_ffmpeg_with_progress(cmd, hook_duration, 
            lambda p: progress_callback(0.3 + p * 0.3))
        
        # Re-encode main video (60-80%) using GPU/CPU encoder
        progress_callback(0.6)
        main_reencoded = tempfile.NamedTemporaryFile(suffix='.mp4', delete=False).name
        
        # Get main video duration
        probe_cmd = [self.ffmpeg_path, "-i", input_path, "-f", "null", "-"]
        result = subprocess.run(probe_cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS)
        duration_match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", result.stderr)
        main_duration = 60
        if duration_match:
            h, m, s = duration_match.groups()
            main_duration = int(h) * 3600 + int(m) * 60 + float(s)
        
        encoder_args = self.get_video_encoder_args()
        cmd = [
            self.ffmpeg_path, "-y",
            "-i", input_path,
            *encoder_args,
            "-r", str(fps),
            "-s", f"{width}x{height}",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", "44100",
            "-ac", "2",
            "-progress", "pipe:1",
            main_reencoded
        ]
        
        self.log_ffmpeg_command(cmd, "Re-encode Main Video for Hook Concat", step="hook")
        self.run_ffmpeg_with_progress(cmd, main_duration,
            lambda p: progress_callback(0.6 + p * 0.2))
        
        # Concatenate (80-100%)
        progress_callback(0.8)
        concat_list = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False).name
        with open(concat_list, 'w') as f:
            f.write(f"file '{hook_video.replace(chr(92), '/')}'\n")
            f.write(f"file '{main_reencoded.replace(chr(92), '/')}'\n")
        
        cmd = [
            self.ffmpeg_path, "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", concat_list,
            "-c", "copy",
            output_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS)
        
        if result.returncode != 0:
            # Fallback to filter_complex using GPU/CPU encoder
            encoder_args = self.get_video_encoder_args()
            cmd = [
                self.ffmpeg_path, "-y",
                "-i", hook_video,
                "-i", main_reencoded,
                "-filter_complex",
                "[0:v:0][0:a:0][1:v:0][1:a:0]concat=n=2:v=1:a=1[outv][outa]",
                "-map", "[outv]",
                "-map", "[outa]",
                *encoder_args,
                "-c:a", "aac",
                "-b:a", "192k",
                "-progress", "pipe:1",
                output_path
            ]
            self.log_ffmpeg_command(cmd, "Concat Hook (filter_complex fallback - old)", step="hook")
            total_duration = hook_duration + main_duration
            self.run_ffmpeg_with_progress(cmd, total_duration,
                lambda p: progress_callback(0.8 + p * 0.2))
        else:
            progress_callback(1.0)
        
        # Cleanup (hook_tts.mp3 is kept in the clip folder for inspection)
        for path in (hook_video, main_reencoded, concat_list,
                     bg_video, overlay_video, overlay_png, frame_png):
            try:
                if path and os.path.exists(path):
                    os.unlink(path)
            except Exception:
                pass
        
        return hook_duration
    
    def add_captions_api_with_progress(self, input_path: str, output_path: str, audio_source: str = None, time_offset: float = 0, progress_callback=None):
        """Add CapCut-style captions using OpenAI Whisper API with progress"""
        
        if progress_callback:
            progress_callback(0.1)
        
        # Use audio_source if provided, otherwise use input_path
        transcribe_source = audio_source if audio_source else input_path
        
        # Extract audio from video - kept in the clip folder (no deletion)
        clip_folder = Path(output_path).parent
        audio_file = str(clip_folder / "captions_audio.wav")
        cmd = [
            self.ffmpeg_path, "-y",
            "-i", transcribe_source,
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", "16000",
            "-ac", "1",
            audio_file
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS)
        
        if result.returncode != 0:
            self.log(f"  Warning: Audio extraction failed")
            import shutil
            shutil.copy(input_path, output_path)
            return
        
        if progress_callback:
            progress_callback(0.2)
        
        # Check if audio file exists
        if not os.path.exists(audio_file) or os.path.getsize(audio_file) < 1000:
            self.log(f"  Warning: Audio file too small or missing")
            import shutil
            shutil.copy(input_path, output_path)
            if os.path.exists(audio_file):
                os.unlink(audio_file)
            return
        
        # Get audio duration for token reporting
        probe_cmd = [self.ffmpeg_path, "-i", audio_file, "-f", "null", "-"]
        result = subprocess.run(probe_cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS)
        duration_match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", result.stderr)
        audio_duration = 0
        if duration_match:
            h, m, s = duration_match.groups()
            audio_duration = int(h) * 3600 + int(m) * 60 + float(s)
            self.report_tokens(0, 0, audio_duration, 0)
        
        if progress_callback:
            progress_callback(0.3)
        
        # Transcribe using Whisper API (raw HTTP for proxy compatibility)
        try:
            transcript = self.transcribe_words(audio_file)
        except Exception as e:
            self.log(f"  ❌ Caption transcription FAILED: {e}")
            self.log("  Captions will be SKIPPED for this clip (video still saved without captions)")
            self._caption_failed = True
            import shutil
            shutil.copy(input_path, output_path)
            return
        
        if progress_callback:
            progress_callback(0.5)
        
        # Create ASS subtitle file - kept in the clip folder (no deletion)
        ass_file = str(clip_folder / "captions.ass")
        # Whisper word timestamps are systematically late -> compensate
        sync_offset = getattr(self, "subtitle_sync_offset", 0.0)
        ass_offset = time_offset + sync_offset
        if getattr(self, "subtitle_style", "pop") == "karaoke":
            self.create_ass_subtitle_karaoke(transcript, ass_file, ass_offset)
        elif getattr(self, "subtitle_style", "pop") == "bounce":
            self.create_ass_subtitle_bounce(transcript, ass_file, ass_offset)
        elif getattr(self, "subtitle_style", "pop") == "animated":
            self.create_ass_subtitle_animated(transcript, ass_file, ass_offset)
        elif getattr(self, "subtitle_style", "pop") == "pop_bounce":
            self.create_ass_subtitle_pop_bounce(transcript, ass_file, ass_offset)
        else:
            self.create_ass_subtitle_capcut(transcript, ass_file, ass_offset)
        
        if progress_callback:
            progress_callback(0.6)
        
        # Burn subtitles into video using GPU/CPU encoder
        ass_path_escaped = ass_file.replace('\\', '/').replace(':', '\\:')
        
        # Get video duration for progress
        probe_cmd = [self.ffmpeg_path, "-i", input_path, "-f", "null", "-"]
        result = subprocess.run(probe_cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS)
        duration_match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", result.stderr)
        video_duration = 60
        if duration_match:
            h, m, s = duration_match.groups()
            video_duration = int(h) * 3600 + int(m) * 60 + float(s)
        
        encoder_args = self.get_video_encoder_args()
        cmd = [
            self.ffmpeg_path, "-y",
            "-i", input_path,
            "-vf", f"ass='{ass_path_escaped}'",
            *encoder_args,
            "-c:a", "copy",
            "-progress", "pipe:1",
            output_path
        ]
        
        self.log_ffmpeg_command(cmd, "Burn Captions", step="caption")
        
        # Caption burn is 60-100%
        self.run_ffmpeg_with_progress(cmd, video_duration,
            lambda p: progress_callback(0.6 + p * 0.4) if progress_callback else None)
        # .ass file is kept in the clip folder for inspection

    def add_watermark_with_progress(self, input_path: str, output_path: str, progress_callback):
        """Add watermark overlay to video with progress tracking"""
        
        watermark_path = self.watermark_settings.get("image_path", "")
        if not watermark_path or not Path(watermark_path).exists():
            self.log("  Warning: Watermark image not found, skipping")
            import shutil
            shutil.copy(input_path, output_path)
            return
        
        progress_callback(0.1)
        
        # Get video dimensions
        probe_cmd = [self.ffmpeg_path, "-i", input_path]
        result = subprocess.run(probe_cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS)
        
        res_match = re.search(r'(\d{3,4})x(\d{3,4})', result.stderr)
        if res_match:
            video_width, video_height = int(res_match.group(1)), int(res_match.group(2))
        else:
            video_width, video_height = 1080, 1920
        
        progress_callback(0.2)
        
        # Calculate watermark size and position
        scale = self.watermark_settings.get("scale", 0.15)
        pos_x = self.watermark_settings.get("position_x", 0.85)
        pos_y = self.watermark_settings.get("position_y", 0.05)
        opacity = self.watermark_settings.get("opacity", 0.8)
        
        # Calculate watermark width in pixels
        watermark_width = int(video_width * scale)
        
        # Calculate position in pixels
        x_pixels = int(pos_x * video_width)
        y_pixels = int(pos_y * video_height)
        
        # Escape watermark path for FFmpeg (Windows paths)
        watermark_escaped = watermark_path.replace('\\', '/').replace(':', '\\:')
        
        # Build FFmpeg overlay filter with proper opacity control
        # Scale watermark, apply opacity via colorchannelmixer, then overlay
        filter_complex = (
            f"[1:v]scale={watermark_width}:-1,format=rgba,"
            f"colorchannelmixer=aa={opacity}[wm];"
            f"[0:v][wm]overlay={x_pixels}:{y_pixels}"
        )
        
        progress_callback(0.3)
        
        # Get video duration for progress
        duration_match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", result.stderr)
        video_duration = 60
        if duration_match:
            h, m, s = duration_match.groups()
            video_duration = int(h) * 3600 + int(m) * 60 + float(s)
        
        # Apply watermark using GPU/CPU encoder
        encoder_args = self.get_video_encoder_args()
        cmd = [
            self.ffmpeg_path, "-y",
            "-i", input_path,
            "-i", watermark_path,
            "-filter_complex", filter_complex,
            *encoder_args,
            "-pix_fmt", "yuv420p",  # Ensure compatibility
            "-c:a", "copy",
            "-movflags", "+faststart",  # Enable streaming
            "-progress", "pipe:1",
            output_path
        ]
        
        self.log_ffmpeg_command(cmd, "Apply Watermark", step="watermark")
        
        # Watermark application is 30-100%
        self.run_ffmpeg_with_progress(cmd, video_duration,
            lambda p: progress_callback(0.3 + p * 0.7))
        
        if not Path(output_path).exists():
            raise Exception("Failed to apply watermark")

    def add_credit_watermark_with_progress(self, input_path: str, output_path: str, progress_callback):
        """Add credit text watermark (channel name) to video with progress tracking"""
        
        if not self.channel_name:
            self.log("  Warning: No channel name available, skipping credit")
            import shutil
            shutil.copy(input_path, output_path)
            return
        
        progress_callback(0.1)
        
        # Get video dimensions
        probe_cmd = [self.ffmpeg_path, "-i", input_path]
        result = subprocess.run(probe_cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS)
        
        res_match = re.search(r'(\d{3,4})x(\d{3,4})', result.stderr)
        if res_match:
            video_width, video_height = int(res_match.group(1)), int(res_match.group(2))
        else:
            video_width, video_height = 1080, 1920
        
        progress_callback(0.2)
        
        # Get credit watermark settings
        size = self.credit_watermark_settings.get("size", 0.03)
        pos_x = self.credit_watermark_settings.get("position_x", 0.5)
        pos_y = self.credit_watermark_settings.get("position_y", 0.95)
        opacity = self.credit_watermark_settings.get("opacity", 0.7)
        
        # Calculate font size in pixels (based on video height)
        font_size = int(video_height * size)
        
        # Calculate position in pixels
        x_pixels = int(pos_x * video_width)
        y_pixels = int(pos_y * video_height)
        
        # Prepare credit text
        credit_text = f"Source: {self.channel_name}"
        # Escape special characters for FFmpeg drawtext
        credit_text_escaped = credit_text.replace("'", "'\\''").replace(":", "\\:")
        
        # Build FFmpeg drawtext filter
        # Use fontfile for portable FFmpeg (avoids fontconfig dependency)
        # Try to find a system font, fallback to built-in if not available
        font_file = None
        if sys.platform == "win32":
            # Windows fonts directory
            windows_fonts = [
                "C:/Windows/Fonts/arial.ttf",
                "C:/Windows/Fonts/segoeui.ttf",
                "C:/Windows/Fonts/tahoma.ttf",
            ]
            for font in windows_fonts:
                if Path(font).exists():
                    font_file = font.replace("\\", "/").replace(":", "\\:")
                    break
        
        # Build filter string
        if font_file:
            filter_str = (
                f"drawtext=fontfile='{font_file}':"
                f"text='{credit_text_escaped}':"
                f"fontsize={font_size}:"
                f"fontcolor=white@{opacity}:"
                f"borderw=2:"
                f"bordercolor=black@{opacity}:"
                f"x={x_pixels}-(text_w/2):"
                f"y={y_pixels}-(text_h/2)"
            )
        else:
            # Fallback without fontfile (may cause fontconfig warning but should still work)
            filter_str = (
                f"drawtext=text='{credit_text_escaped}':"
                f"fontsize={font_size}:"
                f"fontcolor=white@{opacity}:"
                f"borderw=2:"
                f"bordercolor=black@{opacity}:"
                f"x={x_pixels}-(text_w/2):"
                f"y={y_pixels}-(text_h/2)"
            )
        
        progress_callback(0.3)
        
        # Get video duration for progress
        duration_match = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", result.stderr)
        video_duration = 60
        if duration_match:
            h, m, s = duration_match.groups()
            video_duration = int(h) * 3600 + int(m) * 60 + float(s)
        
        # Apply credit text using GPU/CPU encoder
        encoder_args = self.get_video_encoder_args()
        cmd = [
            self.ffmpeg_path, "-y",
            "-i", input_path,
            "-vf", filter_str,
            *encoder_args,
            "-c:a", "copy",
            "-movflags", "+faststart",
            "-progress", "pipe:1",
            output_path
        ]
        
        self.log_ffmpeg_command(cmd, "Apply Credit Watermark", step="credit")
        
        # Credit application is 30-100%
        self.run_ffmpeg_with_progress(cmd, video_duration,
            lambda p: progress_callback(0.3 + p * 0.7))
        
        if not Path(output_path).exists():
            raise Exception("Failed to apply credit watermark")

    def _save_session_data(self, session_data_file, session_data: dict):
        """Persist session data to JSON (called at every milestone)."""
        try:
            with open(session_data_file, "w", encoding="utf-8") as f:
                json.dump(session_data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.log(f"  ⚠ Failed to save session data: {e}")

    def find_highlights_only(self, url: str, num_clips: int = 5, title: str = None,
                             session_dir: Path = None) -> dict:
        """Phase 1: Download subtitle only and find highlights (no video download)
        
        Args:
            url: YouTube video URL
            num_clips: Number of clips to find
            title: Optional video title to use for session folder name
            session_dir: Optional existing session dir to reuse (for retry)
        
        Returns:
            dict with keys:
                - 'session_dir': Path to session directory
                - 'url': YouTube video URL (for later section download)
                - 'srt_path': Path to subtitle file
                - 'highlights': List of highlight dicts with metadata + transcript
                - 'video_info': Video metadata (title, channel, etc.)
        """
        # Use video ID (from URL) as session folder name instead of timestamp+title
        if session_dir:
            # Retry: reuse existing session directory
            session_dir = Path(session_dir)
            session_dir.mkdir(parents=True, exist_ok=True)
        else:
            video_id = extract_video_id(url)
            if not video_id:
                video_id = "unknown"
            session_dir = self.output_dir / "sessions" / video_id
            session_dir.mkdir(parents=True, exist_ok=True)
        
        self.last_session_dir = str(session_dir)
        
        # Update temp_dir to session-specific temp
        self.temp_dir = session_dir / "_temp"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        
        self.log(f"Session directory: {session_dir}")
        
        # Session data is saved at every milestone so failed/cancelled sessions
        # still show up in the session browser instead of being lost.
        session_data_file = session_dir / "session_data.json"
        session_data = {
            "session_dir": str(session_dir),
            "url": url,
            "srt_path": None,
            "highlights": [],
            "video_info": None,
            "created_at": datetime.now().isoformat(),
            "status": "downloading",
        }
        # On retry, preserve previously persisted data (e.g. video_info)
        if session_data_file.exists():
            try:
                with open(session_data_file, "r", encoding="utf-8") as f:
                    old = json.load(f)
                if old.get("video_info"):
                    session_data["video_info"] = old["video_info"]
                if old.get("created_at"):
                    session_data["created_at"] = old["created_at"]
            except Exception:
                pass
        self._save_session_data(session_data_file, session_data)
        
        try:
            # Step 1: Download subtitle only (no video!)
            self.set_progress("Downloading subtitle...", 0.1)

            # Reuse existing subtitle when retrying the same session to skip
            # the download and speed up the process.
            existing_srt = None
            srt_search_dir = self._srt_output_dir()
            for cand in [srt_search_dir / f"source.{self.subtitle_language}.srt"]:
                if cand.exists():
                    existing_srt = cand
                    break
            if existing_srt is None:
                avail = sorted(srt_search_dir.glob("source.*.srt"))
                if avail:
                    existing_srt = avail[0]

            if existing_srt:
                self.log(f"  ⏭ Subtitle already exists, skipping download: {existing_srt.name}")
                srt_path = str(existing_srt)
                video_info = session_data.get("video_info") or {}
                if not video_info:
                    video_info = self.fetch_video_info(url)
            else:
                srt_path, video_info = self.download_subtitle_only(url)
            
            # Persist video metadata (title, channel, etc.) so the session
            # browser and credit watermark can use it later.
            session_data["video_info"] = video_info or {}
            self._save_session_data(session_data_file, session_data)
            
            # Step 2: Find highlights
            self.set_progress("Finding highlights with AI...", 0.5)
            transcript = self.parse_srt(srt_path)
            highlights = self.find_highlights(transcript, video_info, num_clips)
            
            if self.is_cancelled():
                session_data["status"] = "cancelled"
                self._save_session_data(session_data_file, session_data)
                return None
            
            if not highlights:
                raise Exception(
                    "❌ No valid highlights found!\n\n"
                    "Possible causes:\n"
                    "1. AI model failed to generate highlights\n"
                    "2. Video transcript too short or not suitable\n"
                    "3. AI model configuration issue\n\n"
                    "Try:\n"
                    "- Using a different AI model (GPT-4, Gemini, etc.)\n"
                    "- Checking AI API settings\n"
                    "- Using a longer video with more content"
                )
            
            # Extract transcript text for each highlight
            for h in highlights:
                self._snap_highlight_to_subtitles(srt_path, h)
                h["transcript_text"] = self.extract_transcript_for_highlight(srt_path, h)
            
            self.set_progress("Highlights found!", 1.0)
            self.log(f"\n✅ Found {len(highlights)} highlights")
            
            # Save session data to JSON for resume capability
            session_data["highlights"] = highlights
            session_data["status"] = "highlights_found"
            self._save_session_data(session_data_file, session_data)
            
            self.log(f"Session data saved to: {session_data_file}")
            
            return session_data
        except Exception as e:
            # Persist the failure so the session is traceable in the browser
            session_data["status"] = "error"
            session_data["error"] = str(e)[:300]
            self._save_session_data(session_data_file, session_data)
            raise
    
    def process_selected_highlights(self, url: str, selected_highlights: list, 
                                   session_dir: Path, add_captions: bool = True, 
                                   add_hook: bool = True, resolution: str = "1080p"):
        """Phase 2: Download video sections and process selected highlights
        
        Args:
            url: YouTube video URL (for downloading sections)
            selected_highlights: List of highlight dicts to process
            session_dir: Session directory for output
            add_captions: Whether to add captions
            add_hook: Whether to add hook
            resolution: Target download resolution (1080p, 720p, 480p, 360p)
        """
        if not selected_highlights:
            raise Exception("No highlights selected for processing")
        
        self.log(f"\n[Processing {len(selected_highlights)} selected clips]")
        
        # Ensure session_dir is Path object
        if isinstance(session_dir, str):
            session_dir = Path(session_dir)
        
        # Update output_dir to session clips folder
        clips_dir = session_dir / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)
        
        # Update temp_dir to session-specific temp
        self.temp_dir = session_dir / "_temp"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        
        # Mark session as processing right away
        session_data_file = session_dir / "session_data.json"
        session_data = {}
        if session_data_file.exists():
            try:
                with open(session_data_file, "r", encoding="utf-8") as f:
                    session_data = json.load(f)
            except Exception:
                session_data = {}
        session_data["session_dir"] = str(session_dir)
        session_data["status"] = "processing"
        session_data["processing_started_at"] = datetime.now().isoformat()
        self._save_session_data(session_data_file, session_data)

        # Store channel name for credit watermark (loaded from Phase-1 metadata)
        video_info = session_data.get("video_info") or {}
        self.channel_name = video_info.get("channel", "")
        if not self.channel_name and video_info.get("uploader"):
            self.channel_name = video_info.get("uploader", "")
        
        try:
            # Process each selected clip
            total_clips = len(selected_highlights)
            for i, highlight in enumerate(selected_highlights, 1):
                if self.is_cancelled():
                    session_data["status"] = "cancelled"
                    self._save_session_data(session_data_file, session_data)
                    return
                
                # Step A: Download video section for this clip
                self.set_progress(f"Clip {i}/{total_clips}: Downloading video section...", 
                                0.05 + (0.9 * (i - 1) / total_clips))
                self.log(f"\n[Clip {i}/{total_clips}] Downloading: {highlight.get('title', 'Untitled')}")
                
                section_filename = f"section_{i:03d}.mp4"
                section_path = str(self.temp_dir / section_filename)
                
                # Create the clip folder up-front so the video section is
                # downloaded straight into it as landscape.mp4. This skips the
                # intermediate section file + the extra `-c copy` in process_clip.
                clip_title = self._sanitize_name(highlight.get("title", ""), 80)
                if not clip_title:
                    clip_title = f"clip_{i:02d}"
                clip_dir = clips_dir / f"{i:02d}_{clip_title}"
                if clip_dir.exists():
                    clip_dir = clips_dir / f"{i:02d}_{clip_title}_{datetime.now().strftime('%H%M%S')}"
                clip_dir.mkdir(parents=True, exist_ok=True)
                section_path = str(clip_dir / "landscape.mp4")
                
                try:
                    video_path = self.download_video_section(
                        url, 
                        highlight["start_time"], 
                        highlight["end_time"],
                        section_path,
                        resolution
                    )
                except Exception as e:
                    self.log(f"  ✗ Failed to download section: {e}")
                    raise Exception(
                        f"Failed to download video section for clip {i}!\n\n"
                        f"Title: {highlight.get('title', 'Untitled')}\n"
                        f"Time: {highlight['start_time']} → {highlight['end_time']}\n\n"
                        f"Error: {str(e)}"
                    )
                
                # Step B: Process the downloaded section
                # Temporarily override output_dir so process_clip creates
                # the clip folder (named after the clip title) in clips_dir
                original_output_dir = self.output_dir
                self.output_dir = clips_dir
                
                try:
                    # Pass pre_cut=True since we downloaded the section already
                    self.process_clip(video_path, highlight, i, total_clips, 
                                    add_captions=add_captions, add_hook=add_hook,
                                    pre_cut=True, clip_dir=str(clip_dir))
                finally:
                    # Restore original output_dir
                    self.output_dir = original_output_dir
                
                # Section file preserved for inspection (no deletion)
        except Exception as e:
            # Persist the failure so the session is traceable in the browser
            session_data["status"] = "error"
            session_data["error"] = str(e)[:300]
            self._save_session_data(session_data_file, session_data)
            raise
        
        # Skip cleanup - temp files preserved for inspection
        
        # Update session status to completed
        session_data["status"] = "completed"
        session_data["completed_at"] = datetime.now().isoformat()
        session_data["clips_processed"] = total_clips
        session_data.pop("error", None)
        self._save_session_data(session_data_file, session_data)
        
        self.set_progress("Complete!", 1.0)
        self.log(f"\n✅ Created {total_clips} clips in: {clips_dir}")
