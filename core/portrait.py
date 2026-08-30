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




class PortraitMixin:
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

        def _build_portrait_filter_script(self, crop_positions, crop_w, crop_h,
                                          out_w, out_h, min_run=20, quantize=4) -> str:
            """Build a ffmpeg filter_complex script that crops a tracking window.

            Uses a segment-based ``split`` + per-segment ``trim``/``crop`` + ``concat``
            chain instead of a single nested ``if(lt(n,...))`` crop expression. The
            nested form overflows FFmpeg's expression parser for long videos (hundreds
            of tracking runs), whereas the segmented form scales linearly and decodes
            the source only once (via ``split``).
            """
            total = len(crop_positions)
            if total == 0:
                crop_positions = [0]
                total = 1
            quantize = max(1, int(quantize))
            # Collapse into piecewise-constant runs (quantized, short runs merged).
            runs = []
            prev_val = None
            for i, x in enumerate(crop_positions):
                q = int(round(x / quantize) * quantize)
                if prev_val is None or q != prev_val:
                    runs.append([i, q])
                    prev_val = q
            filtered = [runs[0]]
            for start, val in runs[1:]:
                if start - filtered[-1][0] < min_run:
                    continue
                filtered.append([start, val])

            if len(filtered) == 1:
                x = max(0, int(filtered[0][1]))
                return (
                    f"[0:v]crop={crop_w}:{crop_h}:x={x}:y=0,"
                    f"scale={out_w}:{out_h}:flags=bicubic,setsar=1,format=yuv420p[v]"
                )

            n = len(filtered)

            def seg_chain(k: int) -> str:
                start = filtered[k][0]
                end = filtered[k + 1][0] if k + 1 < n else total
                x = max(0, int(filtered[k][1]))
                return (
                    f"[s{k}]trim=start_frame={start}:end_frame={end},"
                    f"setpts=PTS-STARTPTS,"
                    f"crop={crop_w}:{crop_h}:x={x}:y=0,"
                    f"scale={out_w}:{out_h}:flags=bicubic,setsar=1,format=yuv420p[t{k}]"
                )

            split = f"[0:v]split={n}" + "".join(f"[s{k}]" for k in range(n))
            chains = [split] + [seg_chain(k) for k in range(n)]
            labels = "".join(f"[t{k}]" for k in range(n))
            concat = f"{labels}concat=n={n}:v=1:a=0[v]"
            return ";\n".join(chains) + ";\n" + concat

        def _encode_portrait_single_pass(self, input_path: str, output_path: str,
                                         crop_positions: list, crop_w: int, crop_h: int,
                                         out_w: int, out_h: int,
                                         progress_callback=None, duration: float = 0,
                                         min_run: int = 20, quantize: int = 4):
            """Crop + scale + encode + audio mux in ONE ffmpeg pass.

            Replaces the old two-step flow (OpenCV VideoWriter temp file, then a
            second full re-encode for audio merge) with a single encode, which is
            roughly twice as fast and no longer depends on OpenCV's H.264 writer.
            """
            script = self._build_portrait_filter_script(
                crop_positions, crop_w, crop_h, out_w, out_h,
                min_run=min_run, quantize=quantize,
            )
            fd, script_path = tempfile.mkstemp(suffix=".txt", prefix="portrait_crop_", text=True)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(script)
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
            if self._source_is_portrait(input_path):
                self._passthrough_portrait(input_path, output_path, None)
                return
            if self.portrait_mode == "split_podcast_dynamic":
                self.log(f"  Using Split Screen Dynamic (OpusClip-like, active-speaker)")
                return self.convert_to_portrait_split_dynamic(input_path, output_path)
            if self.portrait_mode in ("split", "split_game", "split_podcast"):
                self.log(f"  Using Split Screen mode: {self.portrait_mode}")
                return self.convert_to_portrait_split(input_path, output_path)
            if self.portrait_mode == "center":
                self.log(f"  Using Center Face Follow (wajah di tengah rapih)")
                return self.convert_to_portrait_center(input_path, output_path)
            if self.face_tracking_mode == "detector":
                self.log(f"  Using BlazeFace Detector (face center, tanpa lip)")
                return self.convert_to_portrait_detector(input_path, output_path)
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
            min_shot_duration = 45  # minimum frames (~3 seconds) before allowing switch
        
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
                        num_faces=3,
                        min_face_detection_confidence=0.3,
                        min_face_presence_confidence=0.3,
                        min_tracking_confidence=0.3
                    )
                    self.mp_face_landmarker = vision.FaceLandmarker.create_from_options(options)
                    self.log("  MediaPipe Face Landmarker initialized successfully")
                except Exception as e:
                    raise Exception(f"Failed to initialize MediaPipe Face Landmarker: {e}")

        def _init_face_detector(self):
            """Haar fallback ringan — dipakai untuk mode detector (tanpa landmark)."""
            if getattr(self, 'mp_face_detector', None) is None:
                try:
                    # mediapipe.solutions dihapus di 1.0, pakai Haar yang sudah proven
                    self.mp_face_detector = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
                    if self.mp_face_detector.empty():
                        raise Exception("Haar cascade empty")
                    self.log("  Face Detector (Haar) initialized для center/detector")
                except Exception as e:
                    raise Exception(f"Face Detector init failed: {e}")

        def convert_to_portrait_detector(self, input_path: str, output_path: str):
            return self.convert_to_portrait_detector_with_progress(input_path, output_path, None)

        def convert_to_portrait_detector_with_progress(self, input_path: str, output_path: str, progress_callback):
            """BlazeFace detector — wajah di tengah, tanpa lip, paling stabil (fallback ringan)."""
            self._init_face_detector()
            import mediapipe as mp
            cap = cv2.VideoCapture(input_path)
            if not cap.isOpened():
                raise Exception(f"Failed to open video: {input_path}")
            orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
            crop_w, crop_h = self._get_crop_window(orig_w, orig_h)
            out_w, out_h = self._get_ratio_dimensions()
            if total_frames == 0 or fps == 0:
                cap.release()
                raise Exception(f"Invalid video: {total_frames} frames, {fps} fps")
            self.log("  BlazeFace: analyzing every 5th frame...")
            analyzed_indices = []
            analyzed_positions = []
            frames_read = 0
            current_target = orig_w / 2
            ANALYSIS_STEP = 5
            scale = min(1.0, 640 / orig_w)
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
                small = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else frame
                rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
                results = self.mp_face_detector.process(rgb)
                if results.detections:
                    # pick largest detection
                    best = max(results.detections, key=lambda d: d.location_data.relative_bounding_box.width * d.location_data.relative_bounding_box.height)
                    bbox = best.location_data.relative_bounding_box
                    # bbox is normalized to small image, map to orig
                    cx = (bbox.xmin + bbox.width/2) * small.shape[1] / scale if scale < 1 else (bbox.xmin + bbox.width/2) * orig_w
                    # alternative: use small width
                    if scale < 1:
                        cx = (bbox.xmin + bbox.width/2) * (small.shape[1] / scale)  # small width *1/scale = orig
                        # simpler: bbox is relative to small, so orig x = bbox.x * orig_w
                        cx = (bbox.xmin + bbox.width/2) * orig_w
                    else:
                        cx = (bbox.xmin + bbox.width/2) * orig_w
                    current_target = float(cx)
                # else keep previous
                crop_x = int(current_target - crop_w/2)
                crop_x = max(0, min(crop_x, orig_w - crop_w))
                analyzed_indices.append(frames_read)
                analyzed_positions.append(crop_x)
                frames_read += 1
                if progress_callback and frames_read % 150 == 0 and total_frames:
                    try:
                        progress_callback(min(0.45, (frames_read/total_frames)*0.45))
                    except Exception:
                        pass
            cap.release()
            if not analyzed_positions:
                raise Exception("No faces detected by BlazeFace")
            crop_positions = self._interpolate_sampled(analyzed_positions, analyzed_indices, frames_read)
            crop_positions = self._smooth_follow_positions(crop_positions, 1.6)
            self.log(f"  BlazeFace tracked {len(analyzed_positions)} samples → {len(crop_positions)} frames")
            self._encode_portrait_single_pass(input_path, output_path, crop_positions, crop_w, crop_h, out_w, out_h, duration=frames_read/fps if fps else 0, progress_callback=lambda p: progress_callback(0.5 + p*0.5) if progress_callback else None)

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
            lip_threshold = self.mediapipe_settings.get("lip_activity_threshold", 0.08)
            switch_threshold = self.mediapipe_settings.get("switch_threshold", 0.18)
            min_shot_duration = self.mediapipe_settings.get("min_shot_duration", 45)
            center_weight = self.mediapipe_settings.get("center_weight", 0.15)
        
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
            # Fallback Haar for when MediaPipe misses (early frames, small face)
            try:
                face_cascade_fb = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
            except Exception:
                face_cascade_fb = None
        
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
                
                    # Sort faces left-to-right by nose tip (landmark 1) x coordinate to ensure consistent face IDs
                    sorted_faces = sorted(results.face_landmarks, key=lambda lm: lm[1].x)
                    for face_id, face_landmarks in enumerate(sorted_faces):
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
                
                    # OpusClip-accurate: prioritize active speaker (activity > thresh), not center
                    if faces_data:
                        active = [f for f in faces_data if f['activity'] > lip_threshold]
                        if active:
                            # most active speaker — ignore center bias when someone is talking
                            best_face = max(active, key=lambda f: f['activity'])
                        else:
                            # silence → stay on center face (fallback)
                            best_face = min(faces_data, key=lambda f: abs(f['x'] - orig_w/2))
                        best_face_x = best_face['x']
                        max_activity = best_face['activity']
                    # 0 faces → jangan jadi (stay previous/center, tidak paksa Haar). Lip pasti ada kalau ada yang ngomong, kalau 0 ya memang tidak ada wajah → stay.
            
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
                    self.mediapipe_settings.get("pan_speed_limit", 1.8)
                )
            else:
                crop_positions = self._stabilize_positions_with_activity(
                    crop_positions, 
                    face_activities,
                    min_shot_duration,
                    switch_threshold,
                    orig_w
                )
        
            # Second pass: single ffmpeg command (crop + scale + encode + audio)
            self.log("  Pass 2: Encoding portrait video (single ffmpeg pass, crop + audio)...")
            self._encode_portrait_single_pass(
                input_path, output_path, crop_positions, crop_w, crop_h, out_w, out_h,
                duration=frame_idx / fps if fps else 0,
                **({"min_run": 3, "quantize": 2} if self.mediapipe_settings.get("smooth_follow", True) else {}),
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

        def _stabilize_positions_with_activity(self, positions, activities, min_shot_duration, switch_threshold, orig_w):
            """Stabilize crop positions based on activity scores.
            
            - Uses a pixel-scaled switch threshold.
            - Performs a clean cut (instant jump) on speaker change.
            - Performs a smooth pan (spring/exponential dampening) for small-to-medium movements.
            - Features a dead-zone to eliminate micro-jitter when speaker is relatively still.
            """
            if not positions:
                return positions

            # Convert switch_threshold to pixels
            pixel_switch_threshold = switch_threshold * orig_w if switch_threshold < 1.0 else switch_threshold
            
            # Dead zone: 5% of screen width. Within this zone, the camera doesn't move.
            dead_zone = 0.05 * orig_w
            
            # Smooth positions with a window to reduce frame-to-frame noise
            window_size = 15
            smoothed = []
            for i in range(len(positions)):
                start = max(0, i - window_size // 2)
                end = min(len(positions), i + window_size // 2)
                smoothed.append(int(np.median(positions[start:end])))

            final = []
            current_pos = smoothed[0]
            shot_start = 0
            
            # For smooth panning within a shot
            pan_speed = 0.1  # Smoothing factor for continuous follow

            for i in range(len(smoothed)):
                target_pos = smoothed[i]
                activity = activities[i] if i < len(activities) else 0
                frames_since_switch = i - shot_start

                # Calculate difference between current camera position and target position
                diff = abs(target_pos - current_pos)

                if diff > pixel_switch_threshold and activity > 0.05:
                    if frames_since_switch >= min_shot_duration:
                        # SPEAKER SWITCH: Perform a clean cut to the new speaker
                        current_pos = target_pos
                        shot_start = i
                    elif frames_since_switch < 8:
                        # Snapping during the median filter transition window
                        current_pos = target_pos
                else:
                    # SAME SPEAKER / SMALL REFRAMING:
                    # Apply dead zone: if movement is small, hold camera still
                    if diff < dead_zone:
                        # Hold position to eliminate micro-jitter
                        pass
                    else:
                        # Smooth pan towards target
                        current_pos = current_pos + (target_pos - current_pos) * pan_speed

                final.append(int(round(current_pos)))

            return final

        def _smooth_follow_positions(self, positions: list, pan_speed_limit: float = 1.8):
            """Smooth continuous camera pan — professional-grade tracking.

            Features (inspired by DaVinci Resolve / Premiere Pro smooth follow):
            1. Critically damped spring — no oscillation, natural momentum
            2. Dead zone — camera holds when subject is near center (anti micro-jitter)
            3. Velocity-adaptive — fast subjects get responsive tracking, slow subjects get heavy smoothing
            4. Ease-in/ease-out — natural acceleration curves, no hard starts/stops
            """
            if not positions or len(positions) < 2:
                return positions

            # === Spring parameters ===
            k = max(0.5, 30.0 / max(pan_speed_limit, 0.5))  # stiffness
            c = 2.0 * np.sqrt(k)                              # critical damping

            # === Dead zone (pixels from center before camera reacts) ===
            dead_zone = max(2.0, 8.0 / max(pan_speed_limit, 0.5))  # adaptive dead zone

            # Sub-pixel precision throughout
            current = float(positions[0])
            velocity = 0.0
            result = [current]

            for i in range(1, len(positions)):
                target = float(positions[i])
                displacement = target - current

                # Dead zone: if subject is within dead zone of current position, hold
                if abs(displacement) < dead_zone:
                    # Gently decay velocity (ease-out) instead of freezing
                    velocity *= 0.85
                    current += velocity
                    result.append(current)
                    continue

                # Velocity-adaptive damping:
                # When moving fast → less damping (responsive)
                # When moving slow → more damping (smooth)
                speed = abs(velocity)
                adaptive_c = c * (0.7 + 0.3 * min(speed / max(pan_speed_limit, 0.5), 1.0))

                # Spring force with adaptive damping — clamp to avoid overflow NaN
                try:
                    acceleration = -k * displacement - adaptive_c * velocity
                    # clamp extreme values
                    if not np.isfinite(acceleration):
                        acceleration = np.clip(acceleration, -50, 50) if np.isfinite(acceleration) else 0
                    acceleration = float(np.clip(acceleration, -100, 100))
                    velocity = float(np.clip(velocity + acceleration, -50, 50))
                    current = float(np.clip(current + velocity, 0, 1920))
                except Exception:
                    velocity = 0
                    acceleration = 0
                result.append(current)

            return result

        def stabilize_video(self, input_path: str, output_path: str, shakiness: int = 5, smoothing: int = 10):
            """Two-pass video stabilization using ffmpeg vidstab.
        
            Pass 1: Detect motion (vidstabdetect)
            Pass 2: Apply stabilization (vidstabtransform)
            """
            if self.is_cancelled():
                return
        
            transforms_file = str(Path(output_path).parent / "transforms.trf")
            duration = self._get_duration(input_path)
        
            # Pass 1: Detect
            cmd_detect = [
                self.ffmpeg_path, "-y",
                "-i", input_path,
                "-vf", f"vidstabdetect=shakiness={shakiness}:accuracy=15:result={transforms_file}",
                "-f", "null", "-"
            ]
            self.run_ffmpeg_with_progress(cmd_detect, duration, lambda p: None)
        
            if self.is_cancelled():
                return
        
            # Pass 2: Apply
            cmd_apply = [
                self.ffmpeg_path, "-y",
                "-i", input_path,
                "-vf", f"vidstabtransform=input={transforms_file}:smoothing={smoothing}:interpol=bicubic",
                "-c:a", "copy",
                output_path
            ]
            self.run_ffmpeg_with_progress(cmd_apply, duration, lambda p: None)
        
            # Cleanup transforms file
            try:
                os.remove(transforms_file)
            except Exception:
                pass

        def stabilize_video_with_progress(self, input_path: str, output_path: str, progress_callback, shakiness: int = 5, smoothing: int = 10):
            """Stabilize video with progress callback."""
            if self.is_cancelled():
                return
        
            transforms_file = str(Path(output_path).parent / "transforms.trf")
            duration = self._get_duration(input_path)
        
            cmd_detect = [
                self.ffmpeg_path, "-y",
                "-i", input_path,
                "-vf", f"vidstabdetect=shakiness={shakiness}:accuracy=15:result={transforms_file}",
                "-f", "null", "-"
            ]
            self.log_ffmpeg_command(cmd_detect, "Stabilize (detect)", step="stabilize")
            self.run_ffmpeg_with_progress(cmd_detect, duration,
                lambda p: progress_callback(p * 0.5))
        
            if self.is_cancelled():
                return
        
            cmd_apply = [
                self.ffmpeg_path, "-y",
                "-i", input_path,
                "-vf", f"vidstabtransform=input={transforms_file}:smoothing={smoothing}:interpol=bicubic",
                "-c:a", "copy",
                output_path
            ]
            self.log_ffmpeg_command(cmd_apply, "Stabilize (apply)", step="stabilize")
            self.run_ffmpeg_with_progress(cmd_apply, duration,
                lambda p: progress_callback(0.5 + p * 0.5))
        
            try:
                os.remove(transforms_file)
            except Exception:
                pass

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

        def _source_is_portrait(self, input_path: str) -> bool:
            """True bila video sumber sudah ~rasio portrait target (mis. 9:16).
            Dipakai untuk melewati crop/face-track yang sia-sia pada TikTok/Reels/Shorts.
            Probe pakai ffprobe (lebih robust dari cv2.VideoCapture yang bisa salah
            interpretasi path berangka sebagai image-sequence)."""
            try:
                from pathlib import Path
                import subprocess, json
                ff = self.ffmpeg_path or "ffmpeg"
                probe = str(Path(ff).parent / "ffprobe.exe") if ff.lower().endswith(".exe") else str(Path(ff).parent / "ffprobe")
                out = subprocess.run(
                    [probe, "-v", "error", "-show_entries", "stream=width,height,codec_type", "-of", "json", input_path],
                    capture_output=True, text=True, timeout=30)
                data = json.loads(out.stdout or "{}")
                for s in data.get("streams", []):
                    if s.get("codec_type") == "video":
                        w = int(s.get("width", 0)); h = int(s.get("height", 0))
                        if w <= 0 or h <= 0:
                            continue
                        out_w, out_h = self._get_ratio_dimensions()
                        tgt = out_w / out_h
                        src = w / h
                        return abs(src - tgt) / tgt < 0.05
            except Exception:
                return False
            return False

        def _passthrough_portrait(self, input_path: str, output_path: str, progress_callback):
            """Sumber sudah portrait: lewati crop/face-track. Stream-copy bila resolusi
            sudah sama dengan target; bila beda, scale ke target (tanpa reframing)."""
            import cv2
            cap = cv2.VideoCapture(input_path)
            s_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)); s_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            cap.release()
            out_w, out_h = self._get_ratio_dimensions()
            if s_w == out_w and s_h == out_h:
                self.log(f"  ✓ Lewati konversi portrait (stream copy, sudah {out_w}:{out_h})")
                cmd = [self.ffmpeg_path, "-y", "-i", input_path, "-c", "copy", "-map", "0", output_path]
            else:
                self.log(f"  ✓ Lewati crop portrait (scale {s_w}x{s_h} -> {out_w}x{out_h})")
                vf = (f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
                      f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p")
                encoder_args = self.get_video_encoder_args()
                cmd = [self.ffmpeg_path, "-y", "-i", input_path, "-vf", vf, *encoder_args,
                       "-c:a", "aac", "-b:a", "192k", output_path]
            self.log_ffmpeg_command(cmd, "Portrait Passthrough", step="portrait")
            if progress_callback is not None:
                self.run_ffmpeg_with_progress(cmd, 0, progress_callback)
            else:
                result = self._run_ffmpeg_subprocess(cmd)
                if result.returncode != 0:
                    raise Exception((result.stderr or "")[-2000:])

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

        def convert_to_portrait_split(self, input_path: str, output_path: str):
            """Convert landscape to 9:16 portrait using Split Screen:
            Top: speaker/main crop. Bottom: blurred background or secondary fit."""
            return self.convert_to_portrait_split_with_progress(input_path, output_path, None)

        def convert_to_portrait_split_with_progress(self, input_path: str, output_path: str, progress_callback):
            """Split-screen conversion (Stacked top & bottom) with progress.
            If mode is split_podcast, stacks Left speaker (top) and Right speaker (bottom).
            """
            out_w, out_h = self._get_ratio_dimensions()
            half_h = out_h // 2
            fd, script_path = tempfile.mkstemp(suffix=".txt", prefix="portrait_split_", text=True)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    if getattr(self, "portrait_mode", "") in ("split_podcast", "split_game"):
                        # Podcast mode: Left half of frame (Top), Right half of frame (Bottom)
                        f.write(
                            f"[0:v]split=2[top_in][bot_in];"
                            f"[top_in]crop=iw/2:ih:0:0,scale={out_w}:{half_h}:force_original_aspect_ratio=increase,crop={out_w}:{half_h}[top];"
                            f"[bot_in]crop=iw/2:ih:iw/2:0,scale={out_w}:{half_h}:force_original_aspect_ratio=increase,crop={out_w}:{half_h}[bot];"
                            f"[top][bot]vstack=inputs=2,format=yuv420p[v]"
                        )
                    else:
                        f.write(
                            f"[0:v]split=2[top_in][bot_in];"
                            f"[top_in]scale={out_w}:{half_h}:force_original_aspect_ratio=increase,crop={out_w}:{half_h}[top];"
                            f"[bot_in]scale={out_w}:{half_h}:force_original_aspect_ratio=increase,crop={out_w}:{half_h},gblur=sigma=12[bot];"
                            f"[top][bot]vstack=inputs=2,format=yuv420p[v]"
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
                self.log_ffmpeg_command(cmd, "Portrait Split Screen", step="portrait")
                if progress_callback is not None:
                    self.run_ffmpeg_with_progress(cmd, 0, progress_callback)
                else:
                    result = self._run_ffmpeg_subprocess(cmd)
                    if result.returncode != 0:
                        stderr = (result.stderr or "")[-2000:]
                        raise Exception(f"Portrait split encode failed:\n{stderr}")
            finally:
                try:
                    os.unlink(script_path)
                except OSError:
                    pass
            self.log("  Split screen conversion complete")

        # ── DYNAMIC PODCAST SPLIT (OpusClip-like) ────────────────────────
        @staticmethod
        def _interpolate_labels(sampled_labels: list, sampled_indices: list, total_frames: int) -> list:
            """Step-hold interpolation for label timelines (left/right/both)."""
            if not sampled_labels or total_frames <= 0:
                return ["both"] * total_frames
            out = []
            cur = sampled_labels[0]
            next_i = 0
            for f in range(total_frames):
                while next_i < len(sampled_indices) - 1 and sampled_indices[next_i + 1] <= f:
                    next_i += 1
                    cur = sampled_labels[next_i]
                # also handle first sample may start >0
                if f < sampled_indices[0]:
                    cur = sampled_labels[0]
                out.append(cur)
            return out

        def _stabilize_speaker_timeline(self, labels: list, min_shot_duration: int = 45) -> list:
            """Voting + min-duration stabilizer to avoid flicker (opusclip-style hold)."""
            if not labels:
                return labels
            n = len(labels)
            out = []
            cur = labels[0]
            shot_start = 0
            for i, lab in enumerate(labels):
                if lab != cur and (i - shot_start) >= min_shot_duration:
                    # look-ahead majority: if candidate persists, switch
                    ahead = labels[i:min(n, i + 15)]
                    if ahead.count(lab) >= 8:
                        # flush previous shot
                        out.extend([cur] * (i - shot_start))
                        shot_start = i
                        cur = lab
            out.extend([cur] * (n - shot_start))
            # merge tiny runs < 15 frames (~0.5s) into prev
            # second pass: run-length
            if len(out) != n:
                # fallback: ensure length
                out = (out + [cur] * n)[:n]
            runs = []
            for idx, lab in enumerate(out):
                if not runs or runs[-1][1] != lab:
                    runs.append([idx, lab])
            filtered = [runs[0]] if runs else []
            for start, lab in runs[1:]:
                if start - filtered[-1][0] < 15:
                    continue
                filtered.append([start, lab])
            # rebuild from filtered runs
            if len(filtered) == len(runs):
                return out
            rebuilt = []
            for k, (s, lab) in enumerate(filtered):
                e = filtered[k + 1][0] if k + 1 < len(filtered) else n
                rebuilt.extend([lab] * (e - s))
            # pad if lost frames due to merging
            if len(rebuilt) < n:
                rebuilt.extend([rebuilt[-1]] * (n - len(rebuilt)))
            return rebuilt[:n]

        def _build_split_dynamic_filter_script(self, stabilized_labels: list, total_frames: int, out_w: int, half_h: int, fps: float = 30.0, orig_w: int = 1920, orig_h: int = 1080, left_positions: list = None, right_positions: list = None) -> str:
            """Build segmented filter_complex for dynamic split — neat OpusClip style.

            - Face-centered crop per pane (using left/right x) instead of static half
            - Active pane bright, inactive dim + slight blur (no border)
            - 8px black gap + bicubic, no head cut, setsar=1
            """
            # pane crop size matching pane ratio (keeps heads, no black bars)
            pane_ratio = out_w / half_h if half_h else 1.0
            if orig_h * pane_ratio <= orig_w:
                crop_w = int(orig_h * pane_ratio)
                crop_h = orig_h
            else:
                crop_w = orig_w
                crop_h = int(orig_w / pane_ratio)
            crop_w = max(64, min(crop_w, orig_w))
            crop_h = max(64, min(crop_h, orig_h))

            def median_x(pos_list, s, e):
                if not pos_list or s >= len(pos_list):
                    return None
                seg = pos_list[s:e] if e <= len(pos_list) else pos_list[s:]
                seg = [x for x in seg if x is not None]
                if not seg:
                    return None
                return int(np.median(seg))

            def crop_for_x(cx):
                if cx is None:
                    cx = orig_w // 2
                x = int(cx - crop_w / 2)
                x = max(0, min(x, orig_w - crop_w))
                y = max(0, (orig_h - crop_h) // 2)
                return f"crop={crop_w}:{crop_h}:{x}:{y}"

            # fallback if no timeline
            if not stabilized_labels or total_frames <= 0:
                lx = orig_w // 4 if not left_positions or not left_positions[0] else int(left_positions[0])
                rx = orig_w * 3 // 4 if not right_positions or not right_positions[0] else int(right_positions[0])
                return f"[0:v]split=2[top_in][bot_in];[top_in]{crop_for_x(lx)},scale={out_w}:{half_h}:flags=bicubic:force_original_aspect_ratio=increase,crop={out_w}:{half_h},setsar=1[top];[bot_in]{crop_for_x(rx)},scale={out_w}:{half_h}:flags=bicubic:force_original_aspect_ratio=increase,crop={out_w}:{half_h},setsar=1[bot];[top][bot]vstack=inputs=2:shortest=1,pad=iw:ih+8:0:0:color=black,setsar=1,format=yuv420p[v]"
            # build runs
            runs = []
            prev = None
            for i, lab in enumerate(stabilized_labels):
                if prev is None or lab != prev:
                    runs.append([i, lab])
                    prev = lab
            if not runs:
                runs = [[0, "both"]]
            n = len(runs)

            def top_bot_for(lab, s, e):
                lx = median_x(left_positions, s, e)
                rx = median_x(right_positions, s, e)
                if lx is None:
                    lx = orig_w // 4
                if rx is None:
                    rx = orig_w * 3 // 4
                top_crop = crop_for_x(lx)
                bot_crop = crop_for_x(rx)
                if lab == "left":
                    top_f = f"{top_crop},scale={out_w}:{half_h}:flags=bicubic:force_original_aspect_ratio=increase,crop={out_w}:{half_h},setsar=1,eq=brightness=0.04:saturation=1.05:contrast=1.03"
                    bot_f = f"{bot_crop},scale={out_w}:{half_h}:flags=bicubic:force_original_aspect_ratio=increase,crop={out_w}:{half_h},setsar=1,eq=brightness=-0.14:saturation=0.94:contrast=0.97,gblur=sigma=0.6"
                elif lab == "right":
                    top_f = f"{top_crop},scale={out_w}:{half_h}:flags=bicubic:force_original_aspect_ratio=increase,crop={out_w}:{half_h},setsar=1,eq=brightness=-0.14:saturation=0.94:contrast=0.97,gblur=sigma=0.6"
                    bot_f = f"{bot_crop},scale={out_w}:{half_h}:flags=bicubic:force_original_aspect_ratio=increase,crop={out_w}:{half_h},setsar=1,eq=brightness=0.04:saturation=1.05:contrast=1.03"
                else:  # both
                    top_f = f"{top_crop},scale={out_w}:{half_h}:flags=bicubic:force_original_aspect_ratio=increase,crop={out_w}:{half_h},setsar=1"
                    bot_f = f"{bot_crop},scale={out_w}:{half_h}:flags=bicubic:force_original_aspect_ratio=increase,crop={out_w}:{half_h},setsar=1"
                return top_f, bot_f

            # single-run fast path
            if n == 1:
                s, lab = runs[0]
                e = total_frames
                top_f, bot_f = top_bot_for(lab, s, e)
                return f"[0:v]split=2[top_in][bot_in];[top_in]{top_f}[top];[bot_in]{bot_f}[bot];[top][bot]vstack=inputs=2:shortest=1,pad=iw:ih+8:0:0:color=black,setsar=1,format=yuv420p[v]"

            # multi-run: split source into N trimmed segments
            split = f"[0:v]split={n}" + "".join(f"[s{k}]" for k in range(n))
            chains = [split]
            vlabels = []
            for k, (start, lab) in enumerate(runs):
                end = runs[k + 1][0] if k + 1 < n else total_frames
                top_f, bot_f = top_bot_for(lab, start, end)
                chains.append(
                    f"[s{k}]trim=start_frame={start}:end_frame={end},setpts=PTS-STARTPTS,split=2[t{k}_in][b{k}_in];"
                    f"[t{k}_in]{top_f}[t{k}];"
                    f"[b{k}_in]{bot_f}[b{k}];"
                    f"[t{k}][b{k}]vstack=inputs=2:shortest=1,pad=iw:ih+8:0:0:color=black,setsar=1,format=yuv420p[v{k}]"
                )
                vlabels.append(f"[v{k}]")
            concat = "".join(vlabels) + f"concat=n={n}:v=1:a=0[v]"
            return ";\n".join(chains) + ";\n" + concat

        def convert_to_portrait_split_dynamic(self, input_path: str, output_path: str):
            return self.convert_to_portrait_split_dynamic_with_progress(input_path, output_path, None)

        def convert_to_portrait_split_dynamic_with_progress(self, input_path: str, output_path: str, progress_callback):
            """OpusClip-like dynamic podcast split: active speaker highlighted via MediaPipe lip activity — neat.

            - Detects up to 2 faces per sampled frame (every 5th, 640px fast path) + tracks x per pane
            - Uses _calculate_lip_activity() per face to decide left/right/both
            - Face-centered crop per pane (no static half), 8px black gap, bicubic
            - Stabilizes timeline (min_shot_duration) to avoid flicker; bright active / dim inactive
            Falls back to static split if MediaPipe unavailable or not enough faces.
            """
            out_w, out_h = self._get_ratio_dimensions()
            half_h = out_h // 2
            # try dynamic path, fallback to static
            try:
                if vision is None or python is None:
                    raise Exception("MediaPipe not available")
                self._init_mediapipe()
            except Exception as e:
                self.log(f"  ⚠ Dynamic split requires MediaPipe ({e}), fallback static split_podcast")
                return self.convert_to_portrait_split_with_progress(input_path, output_path, progress_callback)

            cap = cv2.VideoCapture(input_path)
            if not cap.isOpened():
                raise Exception(f"Failed to open video: {input_path}")
            orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
            if total_frames == 0:
                cap.release()
                return self.convert_to_portrait_split_with_progress(input_path, output_path, progress_callback)

            lip_thresh = self.mediapipe_settings.get("lip_activity_threshold", 0.08)
            min_shot = self.mediapipe_settings.get("min_shot_duration", 45)
            # dynamic uses shorter hold (~1.5s) for snappier opusclip feel, but respect config
            dyn_min = max(15, min(min_shot, 45))

            ANALYSIS_STEP = 5
            ANALYSIS_MAX_WIDTH = 640
            scale = min(1.0, ANALYSIS_MAX_WIDTH / orig_w)

            analyzed_indices = []
            analyzed_labels = []
            analyzed_left_x = []
            analyzed_right_x = []
            prev_lip = {}
            frames_read = 0
            sampled = 0
            self.log(f"  Dynamic split: analyzing lip activity (every {ANALYSIS_STEP}th frame, {ANALYSIS_MAX_WIDTH}px)...")
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
                if scale < 1.0:
                    small = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                else:
                    small = frame
                rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
                mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                try:
                    results = self.mp_face_landmarker.detect(mp_image)
                except Exception:
                    results = None

                label = "both"
                left_x = orig_w // 4
                right_x = orig_w * 3 // 4
                if results and results.face_landmarks:
                    # collect up to 2 faces sorted by x
                    faces = []
                    for fid, lm in enumerate(results.face_landmarks[:3]):
                        act = self._calculate_lip_activity(lm, orig_w, orig_h, prev_lip.get(fid, None))
                        x = lm[1].x * orig_w
                        faces.append((x, act, fid, lm))
                        # update prev
                        try:
                            prev_lip[fid] = abs(lm[13].y - lm[14].y)
                        except Exception:
                            pass
                    faces.sort(key=lambda f: f[0])
                    if len(faces) >= 2:
                        left_x = faces[0][0]
                        right_x = faces[1][0]
                        left_act = faces[0][1]
                        right_act = faces[1][1]
                        if left_act < lip_thresh and right_act < lip_thresh:
                            label = "both"
                        elif left_act > right_act + 0.03:
                            label = "left"
                        elif right_act > left_act + 0.03:
                            label = "right"
                        else:
                            # ambiguous -> keep both (no highlight flicker)
                            label = "both"
                    elif len(faces) == 1:
                        act = faces[0][1]
                        x = faces[0][0]
                        # single face: assign to its side, other side keep previous
                        if x < orig_w / 2:
                            left_x = x
                            # keep right_x as previous or default
                            if analyzed_right_x:
                                right_x = analyzed_right_x[-1]
                        else:
                            right_x = x
                            if analyzed_left_x:
                                left_x = analyzed_left_x[-1]
                        if act < lip_thresh:
                            label = "both"
                        else:
                            label = "left" if x < orig_w / 2 else "right"
                    else:
                        label = "both"
                        if analyzed_left_x:
                            left_x = analyzed_left_x[-1]
                            right_x = analyzed_right_x[-1]
                else:
                    label = "both"
                    if analyzed_left_x:
                        left_x = analyzed_left_x[-1]
                        right_x = analyzed_right_x[-1]

                analyzed_indices.append(frames_read)
                analyzed_labels.append(label)
                analyzed_left_x.append(float(left_x))
                analyzed_right_x.append(float(right_x))
                sampled += 1
                frames_read += 1
                if progress_callback and sampled % 30 == 0 and total_frames:
                    try:
                        progress_callback(min(0.45, (frames_read / total_frames) * 0.45))
                    except Exception:
                        pass

            cap.release()
            if not analyzed_labels:
                self.log("  ⚠ No faces sampled, fallback static split")
                return self.convert_to_portrait_split_with_progress(input_path, output_path, progress_callback)

            # if we saw <2 faces at any point, fallback static? keep dynamic but will be mostly "both"
            # interpolate + stabilize (labels + x positions)
            total = frames_read  # actual decoded frames
            per_frame = self._interpolate_labels(analyzed_labels, analyzed_indices, total)
            stabilized = self._stabilize_speaker_timeline(per_frame, dyn_min)
            # interpolate left/right x to per-frame and smooth
            left_per_frame = self._interpolate_sampled(analyzed_left_x, analyzed_indices, total)
            right_per_frame = self._interpolate_sampled(analyzed_right_x, analyzed_indices, total)
            # light median smooth for x (reduce jitter)
            def smooth_x(arr):
                w = 15
                sm = []
                for i in range(len(arr)):
                    s = max(0, i - w // 2); e = min(len(arr), i + w // 2)
                    sm.append(float(np.median(arr[s:e])))
                return sm
            left_per_frame = smooth_x(left_per_frame)
            right_per_frame = smooth_x(right_per_frame)
            # stats
            cnt_left = stabilized.count("left")
            cnt_right = stabilized.count("right")
            cnt_both = stabilized.count("both")
            self.log(f"  Speaker timeline: left {cnt_left/total*100:.0f}% right {cnt_right/total*100:.0f}% both {cnt_both/total*100:.0f}% over {total} frames")
            if cnt_left + cnt_right < total * 0.15:
                self.log("  ⚠ Low active-speaker confidence (maybe single speaker / far cam), using static split highlight 'both'")
                # keep as is, no fallback

            script = self._build_split_dynamic_filter_script(stabilized, total, out_w, half_h, fps, orig_w, orig_h, left_per_frame, right_per_frame)
            fd, script_path = tempfile.mkstemp(suffix=".txt", prefix="portrait_split_dyn_", text=True)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(script)
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
                self.log_ffmpeg_command(cmd, "Portrait Split Dynamic (active-speaker)", step="portrait")
                if progress_callback is not None:
                    # progress 0.5..1.0 for encode
                    self.run_ffmpeg_with_progress(cmd, total / fps if fps else 0, lambda p: progress_callback(0.5 + p * 0.5))
                else:
                    result = self._run_ffmpeg_subprocess(cmd)
                    if result.returncode != 0:
                        stderr = (result.stderr or "")[-2000:]
                        raise Exception(f"Portrait split dynamic encode failed:\n{stderr}")
            finally:
                try:
                    os.unlink(script_path)
                except OSError:
                    pass
            self.log("  Dynamic split conversion complete")

        # ── CENTER FACE FOLLOW (wajah di tengah rapih, anti-kacau) ────────
        def convert_to_portrait_center(self, input_path: str, output_path: str):
            return self.convert_to_portrait_center_with_progress(input_path, output_path, None)

        def convert_to_portrait_center_with_progress(self, input_path: str, output_path: str, progress_callback):
            """Portrait center — wajah selalu di tengah frame, smooth spring, deadzone kecil, head di upper-third.
            Pakai MediaPipe jika ada, fallback OpenCV. Lebih rapih dari 'crop' biasa (quantize kecil, min_run besar).
            """
            # paksa setting neat center
            orig_mp = dict(self.mediapipe_settings) if isinstance(self.mediapipe_settings, dict) else {}
            neat = dict(orig_mp)
            neat.update({"smooth_follow": True, "pan_speed_limit": 1.6, "center_weight": 0.10, "switch_threshold": 0.18, "min_shot_duration": 45, "lip_activity_threshold": 0.08})
            saved = self.mediapipe_settings
            self.mediapipe_settings = neat
            try:
                if self.face_tracking_mode == "mediapipe":
                    return self.convert_to_portrait_mediapipe_with_progress(input_path, output_path, progress_callback)
                elif self.face_tracking_mode == "detector":
                    return self.convert_to_portrait_detector_with_progress(input_path, output_path, progress_callback)
                else:
                    # OpenCV juga dibikin smooth: paksa stabilisasi window besar
                    return self.convert_to_portrait_opencv_with_progress(input_path, output_path, progress_callback)
            finally:
                self.mediapipe_settings = saved


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
            if self._source_is_portrait(input_path):
                self._passthrough_portrait(input_path, output_path, progress_callback)
                return
            if self.portrait_mode == "split_podcast_dynamic":
                self.log(f"  Using Split Screen Dynamic (OpusClip-like, active-speaker)")
                return self.convert_to_portrait_split_dynamic_with_progress(input_path, output_path, progress_callback)
            if self.portrait_mode in ("split", "split_game", "split_podcast"):
                self.log(f"  Using Split Screen mode: {self.portrait_mode}")
                return self.convert_to_portrait_split_with_progress(input_path, output_path, progress_callback)
            if self.portrait_mode == "center":
                self.log(f"  Using Center Face Follow (wajah di tengah rapih)")
                return self.convert_to_portrait_center_with_progress(input_path, output_path, progress_callback)
            if self.face_tracking_mode == "detector":
                self.log(f"  Using BlazeFace Detector (face center, tanpa lip)")
                return self.convert_to_portrait_detector_with_progress(input_path, output_path, progress_callback)
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
            debug_log("[DEBUG] Starting portrait conversion...")
            debug_log(f"[DEBUG] Input: {input_path}")
            debug_log(f"[DEBUG] Output: {output_path}")
            sys.stdout.flush()
        
            cap = cv2.VideoCapture(input_path)
            if not cap.isOpened():
                raise Exception(f"Failed to open video: {input_path}")
        
            orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = cap.get(cv2.CAP_PROP_FPS)
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        
            self.log(f"[DEBUG] Video: {orig_w}x{orig_h}, {fps}fps, {total_frames} frames")
            debug_log(f"[DEBUG] Video: {orig_w}x{orig_h}, {fps}fps, {total_frames} frames")
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
            debug_log("[DEBUG] Pass 1: Analyzing frames... (fast mode: every 5th frame)")
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
                    debug_log(f"[DEBUG] Pass 1 progress: {progress*100:.1f}% ({frames_read}/{total_frames} frames)")
                    sys.stdout.flush()
                    progress_callback(progress)
                    last_log_time = current_time
        
            debug_log(f"[DEBUG] Analyzed {frame_count} frames (sampled)")
            sys.stdout.flush()
        
            # Interpolate positions for every frame
            crop_positions = self._interpolate_sampled(analyzed_positions, analyzed_indices, frames_read)
        
            # Stabilize positions
            crop_positions = self.stabilize_positions(crop_positions)
            progress_callback(0.45)
        
            # Second pass: single ffmpeg command (45-85%)
            debug_log("[DEBUG] Pass 2: Encoding portrait video (single ffmpeg pass, crop + audio)...")
            sys.stdout.flush()
        
            self._encode_portrait_single_pass(
                input_path, output_path, crop_positions, crop_w, crop_h, out_w, out_h,
                progress_callback=lambda p: progress_callback(0.45 + p * 0.4),
                duration=frames_read / fps if fps else 0,
            )
            cap.release()
        
            debug_log("[DEBUG] Portrait encode complete")
            sys.stdout.flush()
        
            progress_callback(0.85)
        
            debug_log("[DEBUG] Portrait conversion complete")
            sys.stdout.flush()

        def convert_to_portrait_mediapipe_with_progress(self, input_path: str, output_path: str, progress_callback):
            """Convert landscape to 9:16 portrait with active speaker detection and progress (MediaPipe)"""
        
            # Initialize MediaPipe
            self._init_mediapipe()
        
            self.log("[DEBUG] Starting MediaPipe portrait conversion...")
            debug_log("[DEBUG] Starting MediaPipe portrait conversion...")
            sys.stdout.flush()
        
            cap = cv2.VideoCapture(input_path)
            if not cap.isOpened():
                raise Exception(f"Failed to open video: {input_path}")
        
            orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = cap.get(cv2.CAP_PROP_FPS)
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        
            self.log(f"[DEBUG] Video: {orig_w}x{orig_h}, {fps}fps, {total_frames} frames")
            debug_log(f"[DEBUG] Video: {orig_w}x{orig_h}, {fps}fps, {total_frames} frames")
            sys.stdout.flush()
        
            if total_frames == 0 or fps == 0:
                cap.release()
                raise Exception(f"Invalid video properties: {total_frames} frames, {fps} fps")
        
            # Calculate crop dimensions
            crop_w, crop_h = self._get_crop_window(orig_w, orig_h)
            out_w, out_h = self._get_ratio_dimensions()
        
            # MediaPipe settings
            lip_threshold = self.mediapipe_settings.get("lip_activity_threshold", 0.08)
            switch_threshold = self.mediapipe_settings.get("switch_threshold", 0.18)
            min_shot_duration = self.mediapipe_settings.get("min_shot_duration", 45)
            center_weight = self.mediapipe_settings.get("center_weight", 0.15)
        
            # First pass: analyze frames with MediaPipe (0-40%)
            debug_log("[DEBUG] Pass 1: Analyzing lip movements with MediaPipe... (fast mode: every 5th frame)")
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
                
                    # Sort faces left-to-right by nose tip (landmark 1) x coordinate to ensure consistent face IDs
                    sorted_faces = sorted(results.face_landmarks, key=lambda lm: lm[1].x)
                    for face_id, face_landmarks in enumerate(sorted_faces):
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
                
                    # OpusClip-accurate: prioritize active speaker (activity > thresh), not center
                    if faces_data:
                        active = [f for f in faces_data if f['activity'] > lip_threshold]
                        if active:
                            # most active speaker — ignore center bias when someone is talking
                            best_face = max(active, key=lambda f: f['activity'])
                        else:
                            # silence → stay on center face (fallback)
                            best_face = min(faces_data, key=lambda f: abs(f['x'] - orig_w/2))
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
                    debug_log(f"[DEBUG] Pass 1 progress: {progress*100:.1f}% ({frames_read}/{total_frames} frames)")
                    sys.stdout.flush()
                    progress_callback(progress)
                    last_log_time = current_time
        
            debug_log(f"[DEBUG] Analyzed {frame_count} frames with MediaPipe (sampled)")
            sys.stdout.flush()
        
            # Interpolate to one position/activity per frame
            crop_positions = self._interpolate_sampled(analyzed_positions, analyzed_indices, frames_read)
            face_activities = self._interpolate_sampled(analyzed_activities, analyzed_indices, frames_read)
        
            # Stabilize positions (40-45%)
            progress_callback(0.4)
            if self.mediapipe_settings.get("smooth_follow", True):
                debug_log("[DEBUG] Smooth face follow enabled: camera pans continuously")
                sys.stdout.flush()
                crop_positions = self._smooth_follow_positions(
                    crop_positions,
                    self.mediapipe_settings.get("pan_speed_limit", 1.8)
                )
            else:
                crop_positions = self._stabilize_positions_with_activity(
                    crop_positions,
                    face_activities,
                    min_shot_duration,
                    switch_threshold,
                    orig_w
                )
            progress_callback(0.45)
        
            # Second pass: single ffmpeg command (45-85%)
            debug_log("[DEBUG] Pass 2: Encoding portrait video (single ffmpeg pass, crop + audio)...")
            sys.stdout.flush()
        
            self._encode_portrait_single_pass(
                input_path, output_path, crop_positions, crop_w, crop_h, out_w, out_h,
                progress_callback=lambda p: progress_callback(0.45 + p * 0.4),
                duration=frames_read / fps if fps else 0,
                **({"min_run": 3, "quantize": 2} if self.mediapipe_settings.get("smooth_follow", True) else {}),
            )
            cap.release()
        
            debug_log("[DEBUG] Portrait encode complete")
            sys.stdout.flush()
        
            progress_callback(0.85)
        
            debug_log("[DEBUG] MediaPipe portrait conversion complete")
            sys.stdout.flush()

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
                # Default CPU encoding - CRF 23 provides good quality with optimal file size
                return ['-c:v', 'libx264', '-preset', 'superfast', '-crf', '23', '-maxrate', '4M', '-bufsize', '8M']

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
