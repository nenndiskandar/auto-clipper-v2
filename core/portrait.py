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
            lip_threshold = self.mediapipe_settings.get("lip_activity_threshold", 0.15)
            switch_threshold = self.mediapipe_settings.get("switch_threshold", 0.3)
            min_shot_duration = self.mediapipe_settings.get("min_shot_duration", 90)
            center_weight = self.mediapipe_settings.get("center_weight", 0.3)
        
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
