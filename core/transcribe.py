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




class TranscribeMixin:
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
        
            # Check Caption Maker / Whisper client is configured (resolves to the
            # primary AI provider when Caption Maker api_key is left empty)
            if not getattr(self.caption_client, "api_key", ""):
                raise Exception(
                    "Caption Maker / Whisper tidak terkonfigurasi!\n\n"
                    "Silakan atur Caption Maker (atau provider AI utama) di:\n"
                    "Settings → AI API Settings"
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

        def _transcribe_full_faster_whisper(self, video_path: str) -> str:
            """Transcribe a full video locally with Faster-Whisper (no API needed).

            Used as a fallback when the Whisper API / Caption Maker endpoint is
            unavailable (e.g. proxy doesn't support /audio/transcriptions). Returns
            the same SRT-like transcript format consumed by find_highlights().

            Uses ``vad_filter=False`` (unlike the caption path) so short spoken
            phrases are not dropped, giving the highlight finder more context.
            """
            if not FASTER_WHISPER_AVAILABLE:
                raise Exception("Faster-Whisper tidak tersedia untuk transkripsi lokal.")

            cm_config = self.ai_providers.get("caption_maker", {})
            fw_settings = cm_config.get("faster_whisper", {})
            model_size = fw_settings.get("model_size", "small")
            if not self._init_faster_whisper_model(model_size):
                raise Exception(f"Gagal inisialisasi Faster-Whisper '{model_size}'")

            audio_file = tempfile.NamedTemporaryFile(suffix='.wav', delete=False).name
            cmd = [
                self.ffmpeg_path, "-y", "-i", video_path,
                "-vn", "-ar", "16000", "-ac", "1", "-f", "wav", audio_file,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS)
            if result.returncode != 0:
                if os.path.exists(audio_file):
                    os.unlink(audio_file)
                raise Exception(f"Gagal ekstrak audio: {result.stderr[:200]}")
            try:
                lang = getattr(self, "subtitle_language", None)
                if lang in (None, "none"):
                    lang = None
                segments_gen, info = self.faster_whisper_model.transcribe(
                    audio_file, word_timestamps=False, vad_filter=False, language=lang
                )
                raw_segments = list(segments_gen)
            finally:
                try:
                    os.unlink(audio_file)
                except Exception:
                    pass

            lines = []
            for seg in raw_segments:
                text = (seg.text or "").strip()
                if not text:
                    continue
                start_ts = self._seconds_to_srt_timestamp(seg.start)
                end_ts = self._seconds_to_srt_timestamp(seg.end)
                lines.append(f"[{start_ts} - {end_ts}] {text}")
            transcript = "\n".join(lines)
            if not transcript:
                raise Exception("Faster-Whisper mengembalikan transkrip kosong.")
            self.log(f"  ✓ Transkripsi lokal selesai: {len(lines)} segmen (lang={info.language})")
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

        def transcribe_words(self, audio_path: str, progress_callback=None):
            """Transcribe audio with word-level timestamps using local Faster-Whisper.

            Captions now always run fully offline via Faster-Whisper (no Whisper API).
            """
            return self._transcribe_words_faster_whisper(audio_path, progress_callback=progress_callback)

        def _transcribe_words_faster_whisper(self, audio_path: str, progress_callback=None):
            """Transcribe an audio file with word-level timestamps using local Faster-Whisper with VAD.
        
            Returns an object exposing .words and .segments (mirroring the SDK response shape).
            """
            from types import SimpleNamespace
            import time as _time
        
            # Get model size from config
            cm_config = self.ai_providers.get("caption_maker", {})
            fw_settings = cm_config.get("faster_whisper", {})
            model_size = fw_settings.get("model_size", "small")
        
            # Initialize / reload model if config changed
            if not self.faster_whisper_model or getattr(self, 'faster_whisper_model_size', None) != model_size:
                if self.faster_whisper_model and self.faster_whisper_model_size != model_size:
                    self.log(f"  [Caption] Switching Faster-Whisper model {self.faster_whisper_model_size} → {model_size}...")
                else:
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
                language=lang,
                log_progress=False,
            )

            # Consume generator, reporting progress from segment timestamps.
            total_dur = float(getattr(info, "duration", 0) or 0)
            raw_segments = []
            for seg in segments_gen:
                raw_segments.append(seg)
                if progress_callback is not None and total_dur > 0:
                    try:
                        progress_callback(min(1.0, float(seg.end) / total_dur))
                    except Exception:
                        pass
        
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
            """Sanitize to safe file name tanpa spasi/symbol."""
            import re as re_module
            safe = re_module.sub(r'[^\w\-]+', '_', str(name or "").strip(), flags=re_module.UNICODE)
            safe = re_module.sub(r'_+', '_', safe).strip('_')
            safe = re_module.sub(r'^_+|_+$', '', safe)
            if not safe: safe = 'clip'
            return safe[:max_len]

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
            # unload previous model if size changed
            if self.faster_whisper_model is not None and self.faster_whisper_model_size != model_size:
                try: del self.faster_whisper_model
                except Exception: pass
                self.faster_whisper_model = None
            self.faster_whisper_model_size = model_size
            self.faster_whisper_model = WhisperModel(
                str(model_dir),
                device=device,
                compute_type=self.faster_whisper_compute_type,
                local_files_only=True
            )
            self.log(f"  ✓ Faster-Whisper model '{model_size}' loaded.")
            return True
