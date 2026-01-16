"""
Auto Clipper Core - Processing logic
Refactored to use OpenAI Whisper API instead of local model
"""

import subprocess
import os
import re
import json
import cv2
import numpy as np
import tempfile
import sys
from pathlib import Path
from datetime import datetime
from openai import OpenAI

# Hide console window on Windows
SUBPROCESS_FLAGS = 0
if sys.platform == "win32":
    SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW


class AutoClipperCore:
    """Core processing logic for Auto Clipper"""
    
    def __init__(
        self,
        client: OpenAI,
        ffmpeg_path: str = "ffmpeg",
        ytdlp_path: str = "yt-dlp",
        output_dir: str = "output",
        model: str = "gpt-4.1",
        log_callback=None,
        progress_callback=None,
        token_callback=None,
        cancel_check=None
    ):
        self.client = client
        self.ffmpeg_path = ffmpeg_path
        self.ytdlp_path = ytdlp_path
        self.output_dir = Path(output_dir)
        self.model = model
        self.log = log_callback or print
        self.set_progress = progress_callback or (lambda s, p: None)
        self.report_tokens = token_callback or (lambda gi, go, w, t: None)
        self.is_cancelled = cancel_check or (lambda: False)
        
        # Create temp directory
        self.temp_dir = self.output_dir / "_temp"
        self.temp_dir.mkdir(parents=True, exist_ok=True)
    
    def process(self, url: str, num_clips: int = 5):
        """Main processing pipeline"""
        
        # Step 1: Download video
        self.set_progress("Downloading video...", 0.1)
        video_path, srt_path, video_info = self.download_video(url)
        
        if self.is_cancelled():
            return
        
        if not srt_path:
            raise Exception("No Indonesian subtitle found!")
        
        # Step 2: Find highlights
        self.set_progress("Finding highlights...", 0.3)
        transcript = self.parse_srt(srt_path)
        highlights = self.find_highlights(transcript, video_info, num_clips)
        
        if self.is_cancelled():
            return
        
        if not highlights:
            raise Exception("No valid highlights found!")
        
        # Step 3: Process each clip
        total_clips = len(highlights)
        for i, highlight in enumerate(highlights, 1):
            if self.is_cancelled():
                return
            self.process_clip(video_path, highlight, i, total_clips)
        
        # Cleanup
        self.set_progress("Cleaning up...", 0.95)
        self.cleanup()
        
        self.set_progress("Complete!", 1.0)
        self.log(f"\n✅ Created {total_clips} clips in: {self.output_dir}")
    
    def download_video(self, url: str) -> tuple:
        """Download video and subtitle with progress"""
        self.log("[1/4] Downloading video & subtitle...")
        
        # Get video metadata
        self.log("  Fetching video info...")
        meta_cmd = [self.ytdlp_path, "--dump-json", "--no-download", url]
        
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
        self.log("  Downloading video...")
        cmd = [
            self.ytdlp_path,
            "-f", "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
            "--write-sub", "--write-auto-sub",
            "--sub-lang", "id",
            "--convert-subs", "srt",
            "--merge-output-format", "mp4",
            "--newline",  # Progress on new lines
            "-o", str(self.temp_dir / "source.%(ext)s"),
            url
        ]
        
        # Run with realtime progress output
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=SUBPROCESS_FLAGS
        )
        
        last_progress = ""
        while True:
            # Check for cancellation
            if self.is_cancelled():
                process.terminate()
                process.wait()
                raise Exception("Cancelled by user")
            
            line = process.stdout.readline()
            if not line and process.poll() is not None:
                break
            
            line = line.strip()
            if not line:
                continue
                
            # Parse download progress
            if "[download]" in line and "%" in line:
                # Extract percentage
                match = re.search(r'(\d+\.?\d*)%', line)
                if match:
                    percent = match.group(1)
                    progress_text = f"  Downloading: {percent}%"
                    if progress_text != last_progress:
                        self.set_progress(f"Downloading video... {percent}%", 0.05 + float(percent) / 100 * 0.2)
                        last_progress = progress_text
            elif "[Merger]" in line or "Merging" in line:
                self.log("  Merging video & audio...")
                self.set_progress("Merging video & audio...", 0.25)
        
        if process.returncode != 0:
            raise Exception("Download failed!")
        
        video_path = self.temp_dir / "source.mp4"
        srt_path = self.temp_dir / "source.id.srt"
        
        if not srt_path.exists():
            srt_path = None
            self.log("  Warning: No Indonesian subtitle found")
        
        return str(video_path), str(srt_path) if srt_path else None, video_info
    
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
    
    def find_highlights(self, transcript: str, video_info: dict, num_clips: int) -> list:
        """Find highlights using GPT"""
        self.log(f"[2/4] Finding highlights (using {self.model})...")
        
        request_clips = num_clips + 3
        
        video_context = ""
        if video_info:
            video_context = f"""
INFO VIDEO:
- Judul: {video_info.get('title', 'Unknown')}
- Channel: {video_info.get('channel', 'Unknown')}
- Deskripsi: {video_info.get('description', '')[:500]}
"""
        
        prompt = f"""Kamu adalah editor video profesional untuk konten PODCAST. Dari transcript video berikut, pilih {request_clips} segment yang paling menarik untuk dijadikan short-form content (TikTok/Reels/Shorts).
{video_context}
Kriteria segment yang bagus:
- Ada punchline atau momen lucu
- Ada insight atau informasi menarik  
- Ada momen emosional atau dramatis
- Ada quote yang memorable
- Cerita atau topik yang lengkap (ada awal, tengah, akhir)

⚠️ ATURAN DURASI - SANGAT PENTING:
- Setiap clip WAJIB berdurasi MINIMAL 60 detik dan MAKSIMAL 120 detik
- TARGET durasi ideal: 90 detik (1.5 menit)

⚠️ HOOK TEXT:
Untuk setiap segment, buat juga "hook_text" yang akan ditampilkan di awal video sebagai teaser.
- Maksimal 15 kata, singkat dan catchy
- Bahasa Indonesia casual/gaul
- JANGAN pakai emoji

Transcript:
{transcript}

Return dalam format JSON array:
[
  {{
    "start_time": "00:01:23,000",
    "end_time": "00:02:15,000", 
    "title": "Judul singkat",
    "reason": "Alasan kenapa menarik",
    "hook_text": "Teks hook yang catchy"
  }}
]

Return HANYA JSON array, tanpa text lain."""

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
        )
        
        # Report token usage (input and output separately)
        if response.usage:
            self.report_tokens(response.usage.prompt_tokens, response.usage.completion_tokens, 0, 0)
        
        result = response.choices[0].message.content.strip()
        if result.startswith("```"):
            result = re.sub(r"```json?\n?", "", result)
            result = re.sub(r"```\n?", "", result)
        
        highlights = json.loads(result)
        
        # Filter by duration
        valid = []
        for h in highlights:
            duration = self.parse_timestamp(h["end_time"]) - self.parse_timestamp(h["start_time"])
            h["duration_seconds"] = round(duration, 1)
            if duration >= 58:
                valid.append(h)
                self.log(f"  ✓ {h['title']} ({duration:.0f}s)")
            
            if len(valid) >= num_clips:
                break
        
        return valid[:num_clips]
    
    def process_clip(self, video_path: str, highlight: dict, index: int, total_clips: int = 1):
        """Process a single clip: cut, portrait, hook, captions"""
        
        # Check cancel before starting
        if self.is_cancelled():
            return
        
        # Create output folder
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S") + f"{index:02d}"
        clip_dir = self.output_dir / timestamp
        clip_dir.mkdir(parents=True, exist_ok=True)
        
        start = highlight["start_time"].replace(",", ".")
        end = highlight["end_time"].replace(",", ".")
        
        self.log(f"\n[Clip {index}] {highlight['title']}")
        
        # Helper to report sub-progress
        def clip_progress(step_name: str, step_num: int, total_steps: int = 4):
            # Calculate overall progress: base (30%) + clip progress (60%)
            clip_base = 0.3 + (0.6 * (index - 1) / total_clips)
            clip_portion = 0.6 / total_clips
            step_progress = clip_portion * (step_num / total_steps)
            overall = clip_base + step_progress
            self.set_progress(f"Clip {index}/{total_clips}: {step_name}", overall)
        
        # Step 1: Cut video (25%)
        if self.is_cancelled():
            return
        clip_progress("Cutting video...", 0)
        landscape_file = clip_dir / "temp_landscape.mp4"
        cmd = [
            self.ffmpeg_path, "-y",
            "-i", video_path,
            "-ss", start, "-to", end,
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k",
            str(landscape_file)
        ]
        subprocess.run(cmd, capture_output=True, creationflags=SUBPROCESS_FLAGS)
        self.log("  ✓ Cut video")
        
        # Step 2: Convert to portrait (50%)
        if self.is_cancelled():
            return
        clip_progress("Converting to portrait...", 1)
        portrait_file = clip_dir / "temp_portrait.mp4"
        self.convert_to_portrait(str(landscape_file), str(portrait_file))
        self.log("  ✓ Portrait conversion")
        
        # Step 3: Add hook (75%)
        if self.is_cancelled():
            return
        clip_progress("Adding hook...", 2)
        hooked_file = clip_dir / "temp_hooked.mp4"
        hook_text = highlight.get("hook_text", highlight["title"])
        hook_duration = self.add_hook(str(portrait_file), hook_text, str(hooked_file))
        self.log(f"  ✓ Added hook ({hook_duration:.1f}s)")
        
        # Step 4: Add captions (100%)
        if self.is_cancelled():
            return
        clip_progress("Adding captions...", 3)
        final_file = clip_dir / "master.mp4"
        self.add_captions_api(str(hooked_file), str(final_file), str(portrait_file), hook_duration)
        self.log("  ✓ Added captions")
        
        # Mark complete
        clip_progress("Done", 4)
        
        # Cleanup temp files
        landscape_file.unlink(missing_ok=True)
        portrait_file.unlink(missing_ok=True)
        hooked_file.unlink(missing_ok=True)
        
        # Save metadata
        metadata = {
            "title": highlight["title"],
            "hook_text": hook_text,
            "start_time": highlight["start_time"],
            "end_time": highlight["end_time"],
            "duration_seconds": highlight["duration_seconds"],
        }
        
        with open(clip_dir / "data.json", "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)
    
    def convert_to_portrait(self, input_path: str, output_path: str):
        """Convert landscape to 9:16 portrait with speaker tracking"""
        
        cap = cv2.VideoCapture(input_path)
        orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        
        # Calculate crop dimensions
        target_ratio = 9 / 16
        crop_w = int(orig_h * target_ratio)
        crop_h = orig_h
        out_w, out_h = 1080, 1920
        
        # Face detector
        face_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
        )
        
        # First pass: analyze frames
        crop_positions = []
        current_target = orig_w / 2
        
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = face_cascade.detectMultiScale(gray, 1.1, 5, minSize=(50, 50))
            
            if len(faces) > 0:
                # Find largest face
                largest = max(faces, key=lambda f: f[2] * f[3])
                current_target = largest[0] + largest[2] / 2
            
            crop_x = int(current_target - crop_w / 2)
            crop_x = max(0, min(crop_x, orig_w - crop_w))
            crop_positions.append(crop_x)
        
        # Stabilize positions
        crop_positions = self.stabilize_positions(crop_positions)
        
        # Second pass: create video
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        temp_video = tempfile.NamedTemporaryFile(suffix='.mp4', delete=False).name
        
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        out = cv2.VideoWriter(temp_video, fourcc, fps, (out_w, out_h))
        
        frame_idx = 0
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            
            crop_x = crop_positions[frame_idx] if frame_idx < len(crop_positions) else crop_positions[-1]
            cropped = frame[0:crop_h, crop_x:crop_x+crop_w]
            resized = cv2.resize(cropped, (out_w, out_h), interpolation=cv2.INTER_LANCZOS4)
            out.write(resized)
            frame_idx += 1
        
        cap.release()
        out.release()
        
        # Merge with audio
        cmd = [
            self.ffmpeg_path, "-y",
            "-i", temp_video,
            "-i", input_path,
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k",
            "-map", "0:v:0", "-map", "1:a:0",
            "-shortest",
            output_path
        ]
        subprocess.run(cmd, capture_output=True, creationflags=SUBPROCESS_FLAGS)
        os.unlink(temp_video)
    
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
            
            # Only allow switch if enough time has passed AND position changed significantly
            if i > 0 and frames_since_last_switch >= min_shot_duration:
                if abs(stabilized[i] - stabilized[shot_start]) > threshold:
                    # Shot change detected - lock previous shot to median
                    shot_positions = stabilized[shot_start:i]
                    if shot_positions:
                        shot_median = int(np.median(shot_positions))
                        final.extend([shot_median] * len(shot_positions))
                    shot_start = i
        
        # Handle last shot
        shot_positions = stabilized[shot_start:]
        if shot_positions:
            shot_median = int(np.median(shot_positions))
            final.extend([shot_median] * len(shot_positions))
        
        return final if final else stabilized
    
    def add_hook(self, input_path: str, hook_text: str, output_path: str) -> float:
        """Add hook scene at the beginning with multi-line yellow text (Fajar Sadboy style)"""
        
        # Report TTS character usage
        self.report_tokens(0, 0, 0, len(hook_text))
        
        # Generate TTS audio
        tts_response = self.client.audio.speech.create(
            model="tts-1",
            voice="nova",
            input=hook_text,
            speed=1.0
        )
        
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
            drawtext_filters.append(
                f"drawtext=text='{escaped_line}':"
                f"fontfile='C\\:/Windows/Fonts/arialbd.ttf':"
                f"fontsize={font_size}:"
                f"fontcolor=#FFD700:"  # Golden yellow
                f"box=1:"
                f"boxcolor=white@0.95:"  # White background
                f"boxborderw=12:"  # Padding around text
                f"x=(w-text_w)/2:"
                f"y={y_pos}"
            )
        
        filter_chain = ",".join(drawtext_filters)
        
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
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "18",
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
        subprocess.run(cmd, capture_output=True, creationflags=SUBPROCESS_FLAGS)
        
        # Step 2: Re-encode main video to EXACT same format (critical for concat)
        main_reencoded = tempfile.NamedTemporaryFile(suffix='.mp4', delete=False).name
        cmd = [
            self.ffmpeg_path, "-y",
            "-i", input_path,
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "18",
            "-r", str(fps),
            "-s", f"{width}x{height}",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", "44100",
            "-ac", "2",
            main_reencoded
        ]
        subprocess.run(cmd, capture_output=True, creationflags=SUBPROCESS_FLAGS)
        
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
            cmd = [
                self.ffmpeg_path, "-y",
                "-i", hook_video,
                "-i", main_reencoded,
                "-filter_complex",
                "[0:v:0][0:a:0][1:v:0][1:a:0]concat=n=2:v=1:a=1[outv][outa]",
                "-map", "[outv]",
                "-map", "[outa]",
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "18",
                "-c:a", "aac",
                "-b:a", "192k",
                output_path
            ]
            subprocess.run(cmd, capture_output=True, creationflags=SUBPROCESS_FLAGS)
        
        # Cleanup
        os.unlink(tts_file)
        os.unlink(hook_video)
        os.unlink(main_reencoded)
        os.unlink(concat_list)
        
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
        
        # Transcribe using OpenAI Whisper API with word-level timestamps
        try:
            with open(audio_file, "rb") as f:
                transcript = self.client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                    language="id",
                    response_format="verbose_json",
                    timestamp_granularities=["word"]
                )
        except Exception as e:
            self.log(f"  Warning: Whisper API error: {e}")
            import shutil
            shutil.copy(input_path, output_path)
            os.unlink(audio_file)
            return
        
        os.unlink(audio_file)
        
        # Create ASS subtitle file with time offset for hook
        ass_file = tempfile.NamedTemporaryFile(mode='w', suffix='.ass', delete=False, encoding='utf-8').name
        self.create_ass_subtitle_capcut(transcript, ass_file, time_offset)
        
        # Burn subtitles into video
        # Escape path for FFmpeg on Windows
        ass_path_escaped = ass_file.replace('\\', '/').replace(':', '\\:')
        
        cmd = [
            self.ffmpeg_path, "-y",
            "-i", input_path,
            "-vf", f"ass='{ass_path_escaped}'",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "18",
            "-c:a", "copy",
            output_path
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, creationflags=SUBPROCESS_FLAGS)
        os.unlink(ass_file)
        
        if result.returncode != 0:
            self.log(f"  Warning: Caption burn failed, copying without captions")
            import shutil
            shutil.copy(input_path, output_path)
    
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
    
    def parse_timestamp(self, ts: str) -> float:
        """Convert timestamp to seconds"""
        ts = ts.replace(",", ".")
        parts = ts.split(":")
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    
    def cleanup(self):
        """Clean up temp files"""
        import shutil
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)
