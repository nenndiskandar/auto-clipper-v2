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




class HighlightMixin:
        @staticmethod
        def get_default_prompt():
            """Get default system prompt for highlight detection"""
            return """Kamu adalah asisten AI untuk menemukan highlight video. Tugasmu adalah memilih SEMUA momen terbaik dari transcript yang layak dijadikan klip viral — jumlahnya OTOMATIS, tentukan sendiri berdasarkan kualitas dan panjang video. Banyak momen bagus = tampilkan lebih banyak; sedikit momen bagus = tampilkan lebih sedikit. Jangan memaksa jumlah tertentu.

    SYARAT:
    1. Durasi tiap klip antara 60 hingga 120 detik (hitung dari timestamp).
    2. Pilih momen yang menarik, lucu, atau memiliki statement penting.
    3. Tiap klip harus punya momen inti yang berbeda (hindari klip yang hampir sama/berulang).
    4. Format waktu: HH:MM:SS,mmm.

    OUTPUT HARUS BERUPA JSON ARRAY TANPA TEKS LAIN:
    [
      {
        "start_time": "00:01:10,000",
        "end_time": "00:02:15,000",
        "title": "Judul Klip Menarik",
        "description": "Deskripsi singkat klip ini.",
        "virality_score": 95,
        "virality_reason": "Topik ini sangat relevan dan kontroversial saat ini.",
        "hook_text": "Kalimat pendek yang menarik"
      }
    ]

    ====================
    {video_context}

    Transcript:
    {transcript}"""

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

        def find_highlights_with_transcription(self, video_path: str, video_info: dict,
                                                num_clips: int, session_dir: str = None,
                                                url: str = None) -> dict:
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
                "url": url,
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
                # Step 1: Transcribe. Coba lokal Faster-Whisper dulu (cepat & tanpa
                # bergantung proxy), fallback ke Whisper API bila lokal tidak tersedia.
                self.set_progress("Transcribing video with AI...", 0.3)
                try:
                    transcript = self._transcribe_full_faster_whisper(video_path)
                except Exception as e:
                    self.log(f"  Transkripsi lokal gagal ({e}), coba Whisper API...")
                    transcript = self.transcribe_full_video(video_path)
            
                if self.is_cancelled():
                    session_data["status"] = "cancelled"
                    self._save_session_data(session_data_file, session_data)
                    return None

                # Transkrip terlalu pendek (mis. video tanpa speech / hanya tag) ->
                # tidak cukup konten untuk AI highlight, lewati agar tidak hang / halu.
                word_count = len((transcript or "").split())
                if not transcript or word_count < 15:
                    self.log(f"  Transkrip terlalu pendek ({word_count} kata) — tidak cukup konten untuk AI highlight.")
                    raise ValueError("Transkrip terlalu pendek untuk AI highlight.")

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

        def find_highlights(self, transcript: str, video_info: dict, num_clips) -> list:
            """Find highlights using AI (OpenAI-compatible API)"""
            self.log(f"[2/4] Finding highlights (using {self.model})...")
        
            # Parse num_clips. "auto" → AI decides the count itself (no cap).
            auto_mode = False
            val = None
            try:
                if num_clips is not None and str(num_clips).lower() != "auto":
                    val = int(num_clips)
                    if val <= 0:
                        val = None
            except (ValueError, TypeError):
                val = None

            if val is None:
                auto_mode = True
                duration = video_info.get("duration", 0) if video_info else 0
                # Request budget: generous so AI has room to return several good clips.
                if duration > 0:
                    if duration < 300:
                        request_clips = 8
                    elif duration < 600:
                        request_clips = 10
                    elif duration < 1200:
                        request_clips = 12
                    elif duration < 2400:
                        request_clips = 15
                    else:
                        request_clips = 18
                else:
                    request_clips = 10
                num_clips = request_clips
                self.log(f"  🤖 Auto mode: AI bebas menentukan jumlah highlight (durasi video {duration}s, request budget {request_clips})")
            else:
                num_clips = val
                request_clips = num_clips

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
            
                if not auto_mode and len(valid) >= num_clips:
                    break
        
            # If we don't have enough valid clips, warn user (manual mode only)
            if not auto_mode and len(valid) < num_clips:
                self.log(f"\n⚠️ WARNING: Only found {len(valid)} valid clips out of {num_clips} requested!")
                self.log(f"   AI returned many segments that were too short (< 58s).")
                self.log(f"   Consider using a better AI model or adjusting the prompt.")
        
            if auto_mode:
                # Auto mode: AI decides the count — return every valid highlight found.
                self.log(f"  🤖 Auto selesai: {len(valid)} highlight valid ditemukan (AI menentukan jumlah).")
                return valid
            return valid[:num_clips]

        def _get_non_youtube_info(self, url: str, video_id: str) -> dict:
            """Ambil metadata (title/channel/duration) untuk URL non-YouTube via yt-dlp."""
            video_info = {"title": video_id, "channel": "Unknown", "duration": 0}
            try:
                import yt_dlp
                with yt_dlp.YoutubeDL({'quiet': True, 'skip_download': True}) as ydl:
                    info = ydl.extract_info(url, download=False)
                    video_info = {
                        "title": info.get('title') or video_id,
                        "channel": info.get('uploader') or "Unknown",
                        "duration": info.get('duration') or 0,
                    }
            except Exception:
                pass
            return video_info

        def _find_source_video(self, session_dir) -> object:
            """Cari video sumber (belum dipotong) di session dir untuk transkripsi.
            Prioritaskan file di root session yang namanya mengandung 'full'.
            """
            from pathlib import Path as _P
            d = _P(session_dir)
            if not d.exists():
                return None
            cands = [p for p in d.glob("*.mp4") if p.is_file()]
            if not cands:
                return None
            fulls = [c for c in cands if 'full' in c.name.lower()]
            if fulls:
                return max(fulls, key=lambda p: p.stat().st_size)
            return max(cands, key=lambda p: p.stat().st_size)

        def find_highlights_only(self, url: str, num_clips = "auto", title: str = None,
                                 session_dir: Path = None, progress_callback=None) -> dict:
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
            # Wire live progress: bila callback diberikan, setiap pemanggilan
            # self.set_progress() (download subtitle / transkripsi / deteksi highlight)
            # akan menembakkan persen secara langsung ke log proses.
            if progress_callback is not None:
                self.set_progress = progress_callback
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
        
            is_youtube = 'youtube.com' in url or 'youtu.be' in url
            if not is_youtube:
                # Non-YouTube: jika video sumber sudah ada (hasil phase 2 / re-generate),
                # pakai transkripsi Whisper + AI highlight supaya Re-generate menghasilkan
                # beberapa momen, bukan cuma 1 klip penuh.
                source_video = self._find_source_video(session_dir)
                if source_video:
                    try:
                        self.log("  Non-YouTube + video sumber ditemukan — transkripsi Whisper + AI highlight.")
                        video_info = self._get_non_youtube_info(url, video_id)
                        sd = self.find_highlights_with_transcription(
                            str(source_video), video_info, num_clips, str(session_dir), url=url
                        )
                        if sd and sd.get("highlights"):
                            return sd
                    except Exception as e:
                        self.log(f"  Transkripsi/AI gagal, fallback 1 klip penuh: {e}")

                # Fallback: 1 highlight full video (tanpa AI) — menjaga phase-1 create tetap ringan
                self.log("  Non-YouTube URL — skip subtitle/AI, 1 highlight full video.")
                video_info = self._get_non_youtube_info(url, video_id)
                dur = int(video_info.get('duration') or 60)
                highlights=[{"start_time":"00:00:00,000","end_time": f"{dur//3600:02d}:{(dur%3600)//60:02d}:{dur%60:02d},000", "title": video_info["title"][:50], "description":"Full video (non-YouTube)", "virality_score": 8, "hook_text": video_info["title"][:40], "duration_seconds": dur, "transcript_text": ""}]
                session_data.update({"video_info": video_info, "highlights": highlights, "status": "highlights_ready"})
                self._save_session_data(session_data_file, session_data)
                return {"session_dir": str(session_dir), "url": url, "srt_path": None, "highlights": highlights, "video_info": video_info}

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
                if not srt_path:
                    # Video tanpa subtitle bahasa target -> stop proses (tanpa fallback)
                    session_data["status"] = "failed"
                    self._save_session_data(session_data_file, session_data)
                    raise Exception(
                        f"\u274c Video ini tidak punya subtitle '{self.subtitle_language}' "
                        "(manual maupun otomatis). Proses dihentikan."
                    )
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
                
                    is_youtube = 'youtube.com' in url or 'youtu.be' in url
                    is_tiktok_fb = 'tiktok.com' in url or 'facebook.com' in url or 'fb.watch' in url
                    try:
                        if is_tiktok_fb:
                            # TikTok/FB: full res tanpa section, tanpa resolusi filter
                            self.log(f"  TikTok/FB detected — full download tanpa section (auto res)")
                            full_tmp = str(session_dir / f"_full_{i}.mp4")
                            self._download_full_video(url, full_tmp)
                            s=self._srt_to_sec(highlight["start_time"]); ee=self._srt_to_sec(highlight["end_time"])
                            dur=ee-s if (ee>s) else 60
                            cut_cmd=[self.ffmpeg_path,"-y","-ss",str(max(0,s)),"-i",full_tmp,"-t",str(dur),"-c","copy",section_path]
                            subprocess.run(cut_cmd, check=True, creationflags=SUBPROCESS_FLAGS)
                            video_path=section_path
                        else:
                            video_path = self.download_video_section(
                                url, 
                                highlight["start_time"], 
                                highlight["end_time"],
                                section_path,
                                resolution
                            )
                    except Exception as e:
                        if not is_youtube:
                            self.log(f"  ⚠ Section download failed for non-YouTube, fallback ke full download + ffmpeg cut")
                            try:
                                full_tmp = str(session_dir / f"_full_{i}.mp4")
                                self._download_full_video(url, full_tmp)
                                s=self._srt_to_sec(highlight["start_time"]); ee=self._srt_to_sec(highlight["end_time"])
                                dur=ee-s if (ee>s) else 60
                                cut_cmd=[self.ffmpeg_path,"-y","-ss",str(max(0,s)),"-i",full_tmp,"-t",str(dur),"-c","copy",section_path]
                                subprocess.run(cut_cmd, check=True, creationflags=SUBPROCESS_FLAGS)
                                video_path=section_path
                            except Exception as e2:
                                self.log(f"  ✗ Fallback also failed: {e2}")
                                raise e
                        else:
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
