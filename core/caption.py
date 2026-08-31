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




class CaptionMixin:
        def add_hook(self, input_path: str, hook_text: str, output_path: str) -> float:
            """Add hook scene at the beginning using add_hook_with_progress"""
            return self.add_hook_with_progress(input_path, hook_text, output_path, lambda p: None)

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

        def add_hook_with_progress(self, input_path: str, hook_text: str, output_path: str, progress_callback) -> float:
            """Add hook scene at the beginning with progress tracking"""
        
            # Report TTS character usage (skip, no TTS)
            # self.report_tokens(0, 0, 0, len(hook_text))
        
            # Generate silent audio (10% progress)
            progress_callback(0.1)
            
            tts_file = str(Path(output_path).parent / "hook_tts.mp3")
            # Generate silent mp3 using ffmpeg
            silent_cmd = [
                self.ffmpeg_path, "-y",
                "-f", "lavfi",
                "-i", "anullsrc=r=44100:cl=stereo",
                "-t", "3.0",
                "-c:a", "libmp3lame",
                tts_file
            ]
            subprocess.run(silent_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=SUBPROCESS_FLAGS)
        
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
                hook_duration = float(style.get("duration", 1.0))
        
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
            bg_opacity = max(0, min(100, int(style.get("bg_opacity", 100))))
            bg_alpha = int(bg_opacity * 255 / 100)
            box_mode = style.get("box_mode", "fit_text") # fit_text, full_width, half_screen
            glitch_on = bool(style.get("glitch"))
            GLITCH_CYAN = (37, 244, 238)   # TikTok cyan
            GLITCH_RED = (254, 44, 85)     # TikTok red

            # Per-line geometry (closure supaya bisa diukur ulang saat font menyusut)
            margin_x = max(16, int(width * 0.04))

            def _measure(fnt, fpx):
                pad = max(10, int(fpx * 0.22))
                ls = max(6, int(fpx * 0.25))
                ms = []
                for line in lines:
                    try:
                        bbox = fnt.getbbox(line)
                    except AttributeError:
                        w0, h0 = fnt.getsize(line)
                        bbox = (0, 0, w0, h0)
                    text_w = bbox[2] - bbox[0]
                    text_h = bbox[3] - bbox[1]
                    ms.append({"text": line, "bbox": bbox, "box_w": text_w + pad * 2, "box_h": text_h + pad * 2})
                t_h = sum(m["box_h"] for m in ms)
                if len(ms) > 1:
                    t_h += ls * (len(ms) - 1)
                return ms, pad, ls, t_h

            line_metrics, padding, line_spacing, total_h = _measure(pil_font, font_px)

            # Anti-terpotong: kecilkan font sampai baris terlebar muat di dalam margin frame
            widest = max(m["box_w"] for m in line_metrics)
            while widest > width - 2 * margin_x and font_px > 20:
                font_px = max(20, int(font_px * 0.92))
                try:
                    pil_font = ImageFont.truetype(pil_font.path, font_px)
                    self.log(f"  Hook font disusutkan otomatis ke {font_px}px agar tidak terpotong")
                except Exception:
                    break
                line_metrics, padding, line_spacing, total_h = _measure(pil_font, font_px)
                widest = max(m["box_w"] for m in line_metrics)

            center_x = int(pos_x * width)
            center_y = int(pos_y * height)
            # Clamp blok vertikal agar tetap sepenuhnya di dalam frame
            top_min = max(16, int(height * 0.04))
            bot_max = height - top_min
            block_top = max(top_min, min(center_y - total_h // 2, bot_max - total_h))

            # Compose the static overlay (transparent everywhere except the hook boxes)
            overlay_img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay_img)

            if box_mode == "half_screen":
                # Render satu kotak background besar setengah layar (atau blok penuh)
                half_h = max(total_h + padding * 4, int(height * 0.45))
                box_y1 = max(0, min(center_y - half_h // 2, height - half_h))
                box_y2 = box_y1 + half_h
                box_x1 = 0
                box_x2 = width
                if corner_radius > 0 and hasattr(draw, "rounded_rectangle"):
                    draw.rounded_rectangle([box_x1, box_y1, box_x2, box_y2], radius=corner_radius, fill=(*bg_color_rgb, bg_alpha))
                else:
                    draw.rectangle([box_x1, box_y1, box_x2, box_y2], fill=(*bg_color_rgb, bg_alpha))

            cur_y = block_top
            for m in line_metrics:
                box_w = width if box_mode == "full_width" else m["box_w"]
                box_h = m["box_h"]
                
                if box_mode == "full_width":
                    box_x1 = 0
                    box_x2 = width
                else:
                    box_x1 = max(margin_x, min(center_x - box_w // 2, width - margin_x - box_w))
                    box_x2 = box_x1 + box_w
                
                box_y1 = cur_y
                box_y2 = box_y1 + box_h

                if box_mode != "half_screen":
                    if corner_radius > 0 and hasattr(draw, "rounded_rectangle") and box_mode != "full_width":
                        r = min(corner_radius, box_w // 2, box_h // 2)
                        draw.rounded_rectangle(
                            [box_x1, box_y1, box_x2, box_y2],
                            radius=r,
                            fill=(*bg_color_rgb, bg_alpha),
                        )
                    else:
                        draw.rectangle(
                            [box_x1, box_y1, box_x2, box_y2],
                            fill=(*bg_color_rgb, bg_alpha),
                        )

                # Center text inside line box
                text_actual_w = m["bbox"][2] - m["bbox"][0]
                text_x = box_x1 + (box_w - text_actual_w) // 2 - m["bbox"][0]
                text_y = box_y1 + (box_h - (m["bbox"][3] - m["bbox"][1])) // 2 - m["bbox"][1]
                if glitch_on:
                    # Efek glitch ala TikTok: salinan cyan & merah digeser diagonal
                    off = max(2, font_px // 28)
                    draw.text(
                        (text_x - off, text_y - off),
                        m["text"],
                        font=pil_font,
                        fill=(*GLITCH_CYAN, 255),
                    )
                    draw.text(
                        (text_x + off, text_y + off),
                        m["text"],
                        font=pil_font,
                        fill=(*GLITCH_RED, 255),
                    )
                # Stroke outline hitam tebal untuk keterbacaan maksimal
                stroke_w = max(2, int(font_px * 0.08))
                draw.text(
                    (text_x, text_y),
                    m["text"],
                    font=pil_font,
                    fill=(*font_color_rgb, 255),
                    stroke_width=stroke_w,
                    stroke_fill=(0, 0, 0, 255),
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

            # --- Hook V2: intro flash + glitch (efek khas short-form) ---
            style = self.hook_style_settings or {}
            if style.get("v2") and self._hook_v2_available():
                try:
                    intro = min(
                        float(style.get("v2_intro_duration", 0.4)),
                        max(0.1, hook_duration - 0.2),
                    )
                    self._apply_hook_v2_effect(
                        overlay_video, fps, intro,
                        flash=bool(style.get("v2_flash", True)),
                        glitch=bool(style.get("v2_glitch", True)),
                    )
                    self.log(f"  Hook V2: intro flash+glitch ({intro:.2f}s)")
                except Exception as e:
                    self.log(f"  ⚠ Hook V2 intro effect skipped: {e}")

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

        @staticmethod
        def _hook_v2_available() -> bool:
            try:
                import cv2  # noqa: F401
                return True
            except Exception:
                return False

        def _apply_hook_v2_effect(self, video_path: str, fps: float, intro_seconds: float,
                                  flash: bool = True, glitch: bool = True) -> None:
            """Terapkan intro flash + RGB-split glitch pada hook (in-place re-encode).

            Menyerupai fitur white-flash & glitch intro dari opensource-clipping
            ``v2_helpers``:
              - flash: beberapa frame putih flicker cepat di awal.
              - glitch: geser channel R/B pada kumpulan frame pertama.
            """
            cap = cv2.VideoCapture(video_path)
            if not cap.isOpened():
                raise RuntimeError("Hook V2: gagal membuka video hook.")
            out_path = video_path + ".v2.mp4"
            writer = subprocess.Popen(
                [self.ffmpeg_path, "-y",
                 "-f", "rawvideo", "-pix_fmt", "bgr24",
                 "-s", f"{int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}",
                 "-r", str(fps), "-i", "-",
                 "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                 "-pix_fmt", "yuv420p", out_path],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            idx = 0
            intro_frames = max(1, int(intro_seconds * fps))
            h, w = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)), int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            shift = max(2, w // 320)
            while True:
                ret, frame = cap.read()
                if not ret:
                    break
                if idx < intro_frames:
                    if flash and idx % 2 == 0:
                        frame = np.full((h, w, 3), 255, dtype=np.uint8)
                    if glitch and idx % 3 == 0:
                        b, g, r = cv2.split(frame)
                        shifted = np.zeros_like(frame)
                        shifted[..., 0] = np.roll(b, -shift, axis=1)
                        shifted[..., 1] = g
                        shifted[..., 2] = np.roll(r, shift, axis=1)
                        frame = shifted
                try:
                    writer.stdin.write(frame.tobytes())
                except BrokenPipeError:
                    break
                idx += 1
            cap.release()
            try:
                writer.stdin.close()
            except Exception:
                pass
            writer.wait(timeout=120)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 10_000:
                os.replace(out_path, video_path)
            else:
                if os.path.exists(out_path):
                    os.unlink(out_path)
                raise RuntimeError("Hook V2: output gagal dibuat.")

        def _cut_with_segments(self, video_path, start_base, scenes, output_path, progress_callback=None):
            """Segment trimming: potong beberapa sub-segmen (keep_segments) lalu
            concat jadi satu clip. scenes = list [(start_offset, end_offset)]."""
            parts = []
            seg_dir = str(self.temp_dir / f"segs_{int(time.time() * 1000)}")
            os.makedirs(seg_dir, exist_ok=True)
            encoder_args = self.get_video_encoder_args()
            for i, (s, e) in enumerate(scenes):
                part = os.path.join(seg_dir, f"seg_{i}.mp4")
                cmd = [
                    self.ffmpeg_path, "-y",
                    "-i", video_path,
                    "-ss", f"{start_base + s:.3f}", "-to", f"{start_base + e:.3f}",
                    *encoder_args,
                    "-c:a", "aac", "-b:a", "192k",
                    part,
                ]
                self._run_ffmpeg_subprocess(cmd)
                if os.path.exists(part) and os.path.getsize(part) > 10_000:
                    parts.append(part)
            if not parts:
                return False
            if len(parts) == 1:
                os.replace(parts[0], output_path)
            else:
                list_file = os.path.join(seg_dir, "list.txt")
                with open(list_file, "w", encoding="utf-8") as f:
                    for p in parts:
                        f.write(f"file '{os.path.abspath(p).replace(chr(92), '/')}'\n")
                cmd = [self.ffmpeg_path, "-y", "-f", "concat", "-safe", "0", "-i", list_file,
                       "-c", "copy", output_path]
                try:
                    self._run_ffmpeg_subprocess(cmd)
                except Exception:
                    # fallback re-encode concat bila -c copy bermasalah
                    esc = output_path.replace('\\', '/').replace(':', '\\:')
                    cmd = [self.ffmpeg_path, "-y", "-f", "concat", "-safe", "0", "-i", list_file,
                           *encoder_args, "-c:a", "aac", "-b:a", "192k", esc]
                    self._run_ffmpeg_subprocess(cmd)
            import shutil
            shutil.rmtree(seg_dir, ignore_errors=True)
            if progress_callback:
                progress_callback(1.0)
            return os.path.exists(output_path) and os.path.getsize(output_path) > 10_000

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
                transcript = self.transcribe_words(
                    audio_file,
                    progress_callback=lambda p: progress_callback(0.3 + p * 0.2) if progress_callback else None,
                )
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

        def add_watermark_with_progress(self, input_path: str, output_path: str, progress_callback):
            """Add watermark overlay to video with progress tracking.
            Supports:
              - image watermark (PNG/JPG/WebP)
              - teks watermark (bila ``image_path`` kosong dan ``text`` diisi)
              - 9 posisi ("position": 0-8 atau nama tl/tc/tr/ml/mc/mr/bl/bc/br)
              - padding dinormalisasi (0-1) terhadap ukuran video
            """
            watermark_path = self.watermark_settings.get("image_path", "")
            watermark_text = self.watermark_settings.get("text", "")
            if (not watermark_path or not Path(watermark_path).exists()) and not watermark_text:
                self.log("  Warning: Watermark kosong (image/text), skipping")
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

            scale = self.watermark_settings.get("scale", 0.15)
            opacity = self.watermark_settings.get("opacity", 0.8)
            padding = float(self.watermark_settings.get("padding", 0.02))

            # --- 9-posisi layout ---
            # (px, py) dalam koordinat kartesian: 0=atas/kiri, 1=bawah/kanan.
            pos = self.watermark_settings.get("position", "")
            pos_map = {
                "tl": (0.0, 0.0), "nw": (0.0, 0.0),
                "tc": (0.5, 0.0), "n": (0.5, 0.0),
                "tr": (1.0, 0.0), "ne": (1.0, 0.0),
                "ml": (0.0, 0.5), "w": (0.0, 0.5),
                "mc": (0.5, 0.5), "c": (0.5, 0.5),
                "mr": (1.0, 0.5), "e": (1.0, 0.5),
                "bl": (0.0, 1.0), "sw": (0.0, 1.0),
                "bc": (0.5, 1.0), "s": (0.5, 1.0),
                "br": (1.0, 1.0), "se": (1.0, 1.0),
            }
            ids = {"0": "tl", "1": "tc", "2": "tr", "3": "ml", "4": "mc",
                   "5": "mr", "6": "bl", "7": "bc", "8": "br"}
            pos_key = str(pos).strip().lower()
            if pos_key in ids:
                pos_key = ids[pos_key]
            if pos_key in pos_map:
                px, py = pos_map[pos_key]
            else:
                px = float(self.watermark_settings.get("position_x", 0.85))
                py = float(self.watermark_settings.get("position_y", 0.05))

            pad_x = int(padding * video_width)
            pad_y = int(padding * video_height)

            # Build watermark asset (image or text)
            watermark_asset = watermark_path
            if (not watermark_path or not Path(watermark_path).exists()) and watermark_text:
                watermark_asset = self._build_text_watermark_png(
                    watermark_text, video_width, video_height, scale, opacity
                )
                if not watermark_asset:
                    import shutil
                    shutil.copy(input_path, output_path)
                    return

            watermark_width = int(video_width * scale)

            # --- Compute position as overlay expressions (eval=frame) ---
            # Kartesian: px/py 0=atas/kiri, 1=bawah/kanan. Overlay expressions
            # memakai main_w/main_h/overlay_w/overlay_h (untungnya robust
            # terhadap ukuran overlay yang dinamis hasil scale).
            x_expr = {
                "l": f"{pad_x}", "c": "(W-w)/2", "r": "W-w-{pad}".format(pad=pad_x),
            }[("l" if px <= 0.0 else ("r" if px >= 1.0 else "c"))]
            y_expr = {
                "t": f"{pad_y}", "m": "(H-h)/2", "b": "H-h-{pad}".format(pad=pad_y),
            }[("t" if py <= 0.0 else ("b" if py >= 1.0 else "m"))]

            watermark_escaped = watermark_asset.replace('\\', '/').replace(':', '\\:')

            filter_complex = (
                f"[1:v]scale={watermark_width}:-1,format=rgba,"
                f"colorchannelmixer=aa={opacity}[wm];"
                f"[0:v][wm]overlay='{x_expr}':'{y_expr}':eof_action=pass"
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
                "-i", watermark_asset if not watermark_text else watermark_asset,
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

        def _build_text_watermark_png(self, text: str, video_width: int, video_height: int,
                                      scale: float, opacity: float) -> str | None:
            """Render teks watermark menjadi PNG transparan (pakai PIL)."""
            try:
                from PIL import Image, ImageDraw, ImageFont
            except ImportError:
                return None
            try:
                from core.typography import resolve_preset_font
            except Exception:
                resolve_preset_font = None

            height_px = max(24, int(video_height * scale))
            fpath = None
            if resolve_preset_font:
                try:
                    fpath = resolve_preset_font("DEFAULT")
                except Exception:
                    fpath = None
            if not fpath:
                fpath = self._find_system_font_bold()
            try:
                font = ImageFont.truetype(fpath, height_px) if fpath else ImageFont.load_default()
            except Exception:
                font = ImageFont.load_default()
            bbox = font.getbbox(text)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            pad = int(height_px * 0.4)
            img = Image.new("RGBA", (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.text((pad - bbox[0], pad - bbox[1]), text, font=font, fill=(255, 255, 255, int(opacity * 255)))
            out = str(self.temp_dir / f"text_wm_{int(time.time() * 1000)}.png")
            img.save(out, "PNG")
            self.log(f"  Watermark teks: '{text}' ({tw}x{th}px)")
            return out

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
    
            # Calculate total steps based on options (include Social Kit so overall goes 0→100 sequentially)
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
            has_social = bool(self.client)
            if has_social:
                total_steps += 1  # Social Kit metadata
        
            # Helper to report sub-progress with percentage (clamp 0-100, anti gila >100)
            def clip_progress(step_name: str, step_num: int, sub_progress: float = 0):
                # Calculate overall progress: base (30%) + clip progress (60%) + 10% final
                clip_base = 0.3 + (0.6 * (index - 1) / total_clips)
                clip_portion = 0.6 / total_clips
                step_progress = clip_portion * ((step_num + sub_progress) / max(1, total_steps))
                overall = min(1.0, max(0.0, clip_base + step_progress))
            
                # Format with percentage
                percent = int(sub_progress * 100)
                if percent > 0:
                    status = f"Clip {index}/{total_clips}: {step_name} ({percent}%)"
                else:
                    status = f"Clip {index}/{total_clips}: {step_name}"
            
                debug_log(f"[DEBUG] clip_progress: {status} (overall: {overall*100:.1f}%)")
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
            
                # Segment trimming (Hook V2 / keep_segments): potong sub-segmen lalu concat.
                keep_segments = highlight.get("keep_segments") or []
                if keep_segments and isinstance(keep_segments, list):
                    self.log(f"  Segment trimming: {len(keep_segments)} sub-segmen")
                    start_base = self.parse_timestamp(start)
                    end_base = self.parse_timestamp(end)
                    scenes = []
                    for seg in keep_segments:
                        if isinstance(seg, dict):
                            s = self.parse_timestamp(str(seg.get("start", 0)))
                            e = self.parse_timestamp(str(seg.get("end", 0)))
                        else:
                            s, e = (float(seg[0]), float(seg[1]))
                        e = min(e, end_base - start_base)
                        if e > s:
                            scenes.append((s, e))
                    if scenes:
                        ok = self._cut_with_segments(
                            video_path, start_base, scenes, str(landscape_file))
                        if ok:
                            current_step += 1
                            self.log(self.colorize("  ✓ Cut video (segments)", "cut"))
                    duration = sum(e - s for s, e in scenes) if scenes else duration

                if not landscape_file.exists() or os.path.getsize(str(landscape_file)) < 10_000:
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
            # Info untuk camera-switch: bounds clip di source original (bila bukan pre-cut).
            if not pre_cut:
                self._clip_start = self.parse_timestamp(start)
                self._clip_end = self.parse_timestamp(end)
            else:
                self._clip_start = 0
                self._clip_end = 0
            self.convert_to_portrait_with_progress(str(landscape_file), str(portrait_file),
                lambda p: clip_progress("Converting to portrait...", current_step, p))
            self.log(self.colorize("  ✓ Portrait conversion", "portrait"))
            current_step += 1
        
            # Track which file is the current output
            current_output = portrait_file
            hook_duration = 0
        
            # Step 2.5: Stabilization — DIMATIKAN permanen (render stabilizer dihapus).
            # ponytail: blok dipertahankan utk referensi; aktifkan lagi seting if usernya mau.
            if False and self.pro_settings.get("stabilize"):
                if self.is_cancelled(): return
                stab_file = clip_dir / "stabilized.mp4"
                self.stabilize_video_with_progress(str(current_output), str(stab_file),
                    lambda p: clip_progress("Stabilizing...", current_step, p))
                if stab_file.exists():
                    current_output = stab_file
                    self.log(self.colorize("  ✓ Stabilized", "ok"))
            current_step += 1
        
            # Step 2.6: Pro features — speed ramp
            sr_start = self.pro_settings.get("speed_ramp_start", 0)
            sr_end = self.pro_settings.get("speed_ramp_end", 0)
            if sr_start > 0 or sr_end > 0:
                if self.is_cancelled(): return
                sr_file = clip_dir / "speedramp.mp4"
                self.apply_speed_ramp_with_progress(str(current_output), str(sr_file),
                    lambda p: clip_progress("Speed ramp...", current_step, p),
                    slow_start=sr_start, slow_end=sr_end,
                    speed_factor=self.pro_settings.get("speed_factor", 0.5))
                if sr_file.exists():
                    current_output = sr_file
                    self.log(self.colorize("  ✓ Speed ramp", "ok"))
            current_step += 1
        
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
        
            # Step 4.5: Post-processing pro features (after captions, before watermark)
            # --- Color Grade ---
            cg_style = self.pro_settings.get("color_grade", "none")
            if cg_style and cg_style != "none":
                if self.is_cancelled():
                    return
                cg_file = clip_dir / "colorgraded.mp4"
                clip_progress("Applying color grade...", current_step, 0.5)
                self.apply_color_grade_with_progress(str(current_output), str(cg_file),
                    lambda p: clip_progress("Color grading...", current_step, p), style=cg_style)
                if cg_file.exists():
                    current_output = cg_file
                    self.log(self.colorize(f"  ✓ Color grade: {cg_style}", "colorgrade"))
        
            # --- Motion Blur ---
            mb_strength = self.pro_settings.get("motion_blur", 0)
            if mb_strength > 0:
                if self.is_cancelled():
                    return
                mb_file = clip_dir / "motionblur.mp4"
                clip_progress("Applying motion blur...", current_step, 0.5)
                self.apply_motion_blur_with_progress(str(current_output), str(mb_file),
                    lambda p: clip_progress("Motion blur...", current_step, p), strength=mb_strength)
                if mb_file.exists():
                    current_output = mb_file
                    self.log(self.colorize(f"  ✓ Motion blur ({mb_strength})", "motionblur"))
        
            # --- Vignette — DIMATIKAN permanen (render vignette dihapus).
            # ponytail: blok dipertahankan utk referensi; aktifkan lagi seting if usernya mau.
            vig_angle = self.pro_settings.get("vignette", 0)
            if False and vig_angle > 0:
                if self.is_cancelled():
                    return
                vig_file = clip_dir / "vignette.mp4"
                clip_progress("Applying vignette...", current_step, 0.5)
                self.apply_vignette_with_progress(str(current_output), str(vig_file),
                    lambda p: clip_progress("Vignette...", current_step, p), angle=vig_angle)
                if vig_file.exists():
                    current_output = vig_file
                    self.log(self.colorize(f"  ✓ Vignette ({vig_angle})", "vignette"))
        
            # --- Audio Ducking ---
            duck_level = self.pro_settings.get("ducking_level_db", -15)
            music_path = self.pro_settings.get("music_path", "")
            if music_path and Path(music_path).exists():
                if self.is_cancelled():
                    return
                duck_file = clip_dir / "ducked.mp4"
                clip_progress("Audio ducking...", current_step, 0.5)
                self.duck_audio_with_progress(str(current_output), str(duck_file),
                    lambda p: clip_progress("Audio ducking...", current_step, p),
                    music_path=music_path, duck_level_db=duck_level)
                if duck_file.exists():
                    current_output = duck_file
                    self.log(self.colorize(f"  ✓ Audio ducking ({duck_level}dB)", "duck"))

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
                "has_stabilize": self.pro_settings.get("stabilize", False),
                "color_grade": self.pro_settings.get("color_grade", "none"),
                "motion_blur": self.pro_settings.get("motion_blur", 0),
                "vignette": self.pro_settings.get("vignette", 0),
                "speed_ramp": bool(self.pro_settings.get("speed_ramp_start", 0) or self.pro_settings.get("speed_ramp_end", 0)),
                "audio_ducking": bool(self.pro_settings.get("music_path", "")),
                "hook_v2": bool((self.hook_style_settings or {}).get("v2")),
                "watermark_position": (self.watermark_settings or {}).get("position"),
                "portrait_mode": getattr(self, "portrait_mode", None),
                "channel_name": self.channel_name,
                "aspect_ratio": self.aspect_ratio,
            }
        
            # Auto generate social kit metadata if client is available (sequential overall 0→100)
            if self.client:
                try:
                    # sequential 90→100 for last clip, not langsung 100
                    if index == total_clips:
                        self.set_progress(f"Clip {index}/{total_clips}: Generating Social Kit... (15%) (overall: 91.5%)", 0.915)
                        debug_log(f"[DEBUG] clip_progress: Clip {index}/{total_clips}: Generating Social Kit... (15%) (overall: 91.5%)")
                    else:
                        clip_progress("Generating Social Kit...", current_step, 0.15)
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
                    if index == total_clips:
                        self.set_progress(f"Clip {index}/{total_clips}: Social Kit... (85%) (overall: 98.5%)", 0.985)
                        debug_log(f"[DEBUG] clip_progress: Clip {index}/{total_clips}: Social Kit... (85%) (overall: 98.5%)")
                        self.log("  Social Kit metadata generated successfully! (overall: 98.5%)")
                    else:
                        clip_progress("Social Kit...", current_step, 0.85)
                        self.log("  Social Kit metadata generated successfully!")
                except Exception as e:
                    if index == total_clips:
                        self.set_progress(f"Clip {index}/{total_clips}: Social Kit... (85%) (overall: 98.5%)", 0.985)
                        debug_log(f"[DEBUG] clip_progress: Clip {index}/{total_clips}: Social Kit... (85%) (overall: 98.5%)")
                    else:
                        clip_progress("Social Kit...", current_step, 0.85)
                    self.log(f"  Warning: Failed to auto-generate Social Kit: {e}")

            # Mark complete — sequential 0→100 (Social Kit is last step)
            if index == total_clips:
                debug_log(f"[DEBUG] clip_progress: Clip {index}/{total_clips}: Social Kit done (overall: 100.0%)")
                self.set_progress(f"Clip {index}/{total_clips}: Social Kit done (overall: 100.0%)", 1.0)
            else:
                clip_progress("Done", current_step, 1.0)

            # Feature 10 — Metadata enrichment + klasifikasi akun (best-effort)
            if (self.metadata_settings or {}).get("save_preview", True):
                try:
                    from core.metadata import normalize_and_validate, klasifikasikan_akun
                    normalized = normalize_and_validate(highlight)
                    metadata["metadata_final"] = {
                        k: v for k, v in normalized.items()
                        if k not in ("title", "hook_text", "start_time", "end_time", "duration_seconds")
                    }
                    klas = klasifikasikan_akun(normalized)
                    metadata["akun_tujuan"] = klas.get("akun_tujuan")
                    metadata["tipe_akun"] = klas.get("tipe_akun")
                except Exception as e:
                    debug_log(f"[Metadata] normalize gagal: {e}")

            with open(clip_dir / "data.json", "w", encoding="utf-8") as f:
                json.dump(metadata, f, ensure_ascii=False, indent=2)
