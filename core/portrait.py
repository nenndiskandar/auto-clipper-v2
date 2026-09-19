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
        def _hold_sampled_values(sampled_values: list, sampled_indices: list, total_frames: int) -> list:
            """Expand sparse samples to one value per frame via STEP-HOLD (no linear interp).

            Step-hold avoids the "mid-face" smear that linear interpolation causes when
            the tracked face switches (2-speaker footage): the camera cuts straight to the
            new face instead of panning through the empty gap between the two faces.
            """
            if not sampled_values:
                return []
            if total_frames <= 0:
                return list(sampled_values)
            out = [sampled_values[0]] * total_frames
            # hold each sample until the next sample index
            for i in range(len(sampled_indices) - 1):
                start = min(int(sampled_indices[i]), total_frames)
                end = min(int(sampled_indices[i + 1]), total_frames)
                val = sampled_values[i]
                for j in range(start, end):
                    out[j] = val
            if sampled_indices:
                last = min(int(sampled_indices[-1]), total_frames)
                last_val = sampled_values[-1]
                for j in range(last, total_frames):
                    out[j] = last_val
            return out

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

        def _encode_portrait_single_pass(self, input_path: str, output_path: str,
                                         crop_positions: list, crop_w: int, crop_h: int,
                                         out_w: int, out_h: int,
                                         progress_callback=None, duration: float = 0,
                                         min_run: int = 1, quantize: int = 1,
                                         crop_ys=None):
            """Crop + scale + encode + audio mux in ONE ffmpeg pass via sendcmd for 100% smooth camera motion.

            Eliminates segment trim/concat snapping and discrete quantization.
            """
            total = len(crop_positions)
            if crop_ys is None:
                crop_ys = [0] * total

            if total == 0:
                crop_positions = [0]
                crop_ys = [0]
                total = 1

            fps = (total / duration) if (duration and duration > 0) else 30.0

            fd_cmd, cmd_path = tempfile.mkstemp(suffix=".txt", prefix="portrait_sendcmd_", text=True)
            fd_script, script_path = tempfile.mkstemp(suffix=".txt", prefix="portrait_fc_", text=True)

            try:
                prev_x = None
                prev_y = None
                with os.fdopen(fd_cmd, "w", encoding="utf-8") as f:
                    for i in range(total):
                        x = int(round(crop_positions[i]))
                        y = int(round(crop_ys[i]))
                        if x != prev_x or y != prev_y:
                            t_sec = i / fps
                            f.write(f"{t_sec:.4f} [enter] crop x {x}, crop y {y};\n")
                            prev_x, prev_y = x, y

                escaped_cmd_path = cmd_path.replace("\\", "/").replace(":", "\\:")
                init_x = int(round(crop_positions[0]))
                init_y = int(round(crop_ys[0]))

                filter_content = (
                    f"sendcmd=f='{escaped_cmd_path}',"
                    f"crop=w={crop_w}:h={crop_h}:x={init_x}:y={init_y},"
                    f"scale={out_w}:{out_h}:flags=bicubic,setsar=1,format=yuv420p[v]"
                )

                with os.fdopen(fd_script, "w", encoding="utf-8") as f:
                    f.write(filter_content)

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
                self.log_ffmpeg_command(cmd, "Portrait Crop+Encode (sendcmd smooth pass)", step="portrait")
                if progress_callback is not None:
                    self.run_ffmpeg_with_progress(cmd, duration, progress_callback)
                else:
                    result = self._run_ffmpeg_subprocess(cmd)
                    if result.returncode != 0:
                        stderr = (result.stderr or "")[-2000:]
                        raise Exception(f"Portrait encode failed:\n{stderr}")
            finally:
                for p in [cmd_path, script_path]:
                    try:
                        os.unlink(p)
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
            if self.face_tracking_mode == "detector":
                self.log(f"  Using BlazeFace Detector (face center, tanpa lip)")
                return self.convert_to_portrait_detector(input_path, output_path)
            try:
                self.log("  Using MediaPipe (Active Speaker Detection)")
                return self.convert_to_portrait_mediapipe(input_path, output_path)
            except Exception as e:
                self.log(f"  ⚠ MediaPipe failed: {e}")
                self.log("  Falling back to OpenCV mode...")
                return self.convert_to_portrait_opencv(input_path, output_path)

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
                    haar = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
                    if haar.empty():
                        raise Exception("Haar cascade empty")
                    # Mimic MediaPipe FaceDetection API (.process -> .detections -> relative_bounding_box)
                    # ponytail: Interface compat supaya loop lama (.process(rgb)) jalan tanpa rewrite besar.
                    class _HaarMPAdapter:
                        def process(self, rgb):
                            gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
                            faces = haar.detectMultiScale(gray, 1.1, 5, minSize=(24, 24))
                            class _D:  # datum detection
                                def __init__(self, x, y, w, h, iw, ih):
                                    self.location_data = type('L', (), {'relative_bounding_box': type('B', (), {'xmin': x/iw, 'ymin': y/ih, 'width': w/iw, 'height': h/ih})()})()
                            return type('R', (), {'detections': [_D(x, y, w, h, rgb.shape[1], rgb.shape[0]) for (x, y, w, h) in faces]})()
                    self.mp_face_detector = _HaarMPAdapter()
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
            crop_positions = self._smooth_follow_positions(crop_positions, 1.6, fps=fps or 30.0)
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
            prev_best_face = None  # utk hold-on-silence
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
                            # silence → HOLD posisi terakhir (jangan drift ke tengah kosong / area kosong).
                            # ponytail: hold-on-silence mencegah kamera pindah ke area kosong saat diam;
                            # kalau mau selalu balik tengah, ganti ke min(abs(f['x']-orig_w/2)).
                            best_face = prev_best_face
                            if best_face is None:
                                best_face = min(faces_data, key=lambda f: abs(f['x'] - orig_w/2))
                        prev_best_face = best_face
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
                    self.mediapipe_settings.get("pan_speed_limit", 1.8),
                    fps=fps or 30.0
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

        def _smooth_follow_positions(self, positions: list, pan_speed_limit: float = 1.8, fps: float = 30.0):
            """Smooth continuous camera pan — cinematic camera tracking.

            1. Hanning window low-pass filter eliminates face tracking jitter.
            2. Proportional ease-in/ease-out glides smoothly without staircases or overshoot.
            3. Adaptive dead-zone prevents camera buzzing when subject is still.
            """
            if not positions or len(positions) < 2:
                return positions

            arr = np.array(positions, dtype=float)
            kernel_size = max(5, int(fps * 0.4) | 1)
            kernel = np.hanning(kernel_size)
            kernel /= kernel.sum()

            padded = np.pad(arr, (kernel_size // 2, kernel_size // 2), mode='edge')
            smoothed = np.convolve(padded, kernel, mode='valid')

            dt = 1.0 / max(1.0, fps)
            max_speed_px_sec = 180.0 * max(0.5, pan_speed_limit)
            max_step = max_speed_px_sec * dt

            dead_zone = 6.0
            result = []
            current = float(smoothed[0])

            for target in smoothed:
                diff = target - current
                if abs(diff) > dead_zone:
                    step = diff * 0.15
                    step = float(np.clip(step, -max_step, max_step))
                    current += step
                else:
                    current += diff * 0.04
                result.append(float(current))

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
            # User kebijakan: 720p lebih cepat & cukup untuk sosial (lihat MEMORY.md).
            # Map eksplisit agar tak tergantung resolution config yang tak konsisten
            # antar jalur download/portrait. 9:16 -> 720x1280, dst.
            _dims = {"9:16": (720, 1280), "1:1": (720, 720), "4:5": (720, 900),
                     "3:4": (720, 960), "16:9": (1280, 720)}
            return _dims.get(getattr(self, "aspect_ratio", "9:16"), (720, 1280))

        def _get_crop_window(self, orig_w: int, orig_h: int, zoom_factor: float = 1.0):
            """Compute (crop_w, crop_h) for the configured aspect ratio, clamped to the
            source video dimensions so the crop never exceeds the frame.

            ``zoom_factor`` (0..1] makes the crop window smaller than the full frame
            (zoom-in) so the tracked face can be centered on BOTH axes (X + Y) and
            follow micro-movement without vertical clipping. 1.0 = full frame (legacy).
            """
            out_w, out_h = self._get_ratio_dimensions()
            target_ratio = out_w / out_h
            crop_h = int(orig_h * zoom_factor)
            crop_w = int(crop_h * target_ratio)
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
            if self.face_tracking_mode == "detector":
                self.log(f"  Using BlazeFace Detector (face center, tanpa lip)")
                return self.convert_to_portrait_detector_with_progress(input_path, output_path, progress_callback)
            try:
                self.log("  Using MediaPipe (Active Speaker Detection)")
                return self.convert_to_portrait_mediapipe_with_progress(input_path, output_path, progress_callback)
            except Exception as e:
                self.log(f"  ⚠ MediaPipe failed: {e}")
                self.log("  Falling back to OpenCV mode...")
                return self.convert_to_portrait_opencv_with_progress(input_path, output_path, progress_callback)

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
            """Convert landscape to 9:16 portrait with active speaker detection and progress (MediaPipe).
            Updated: Tracks X+Y center and uses 75% zoom for better vertical centering.
            """
            self._init_mediapipe()
            debug_log("[DEBUG] Starting MediaPipe portrait conversion (X+Y Center + Zoom)...")
            
            cap = cv2.VideoCapture(input_path)
            if not cap.isOpened():
                raise Exception(f"Failed to open video: {input_path}")
            
            orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            
            # Zoom-in: use 75% of height to allow vertical movement
            zoom_factor = 0.75
            crop_w, crop_h = self._get_crop_window(orig_w, orig_h, zoom_factor=zoom_factor)
            out_w, out_h = self._get_ratio_dimensions()
            
            lip_threshold = self.mediapipe_settings.get("lip_activity_threshold", 0.08)
            center_weight = self.mediapipe_settings.get("center_weight", 0.15)
            
            analyzed_indices = []
            analyzed_positions_x = []
            analyzed_positions_y = []
            analyzed_activities = []
            frames_read = 0
            prev_lip_distances = {}
            prev_best_face = None
            
            ANALYSIS_STEP = 5
            scale = min(1.0, 640 / orig_w)
            import time
            last_log_time = 0

            while True:
                if self.is_cancelled():
                    cap.release()
                    raise Exception("Cancelled by user")
                
                if frames_read % ANALYSIS_STEP != 0:
                    if not cap.grab(): break
                    frames_read += 1
                    continue
                
                ret, frame = cap.read()
                if not ret: break
                
                small = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1.0 else frame
                rgb_frame = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
                mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)
                results = self.mp_face_landmarker.detect(mp_image)
                
                best_face_x = orig_w / 2
                best_face_y = orig_h / 2
                max_activity = 0
                
                if results.face_landmarks:
                    faces_data = []
                    sorted_faces = sorted(results.face_landmarks, key=lambda lm: lm[1].x)
                    for face_id, face_landmarks in enumerate(sorted_faces):
                        activity = self._calculate_lip_activity(face_landmarks, orig_w, orig_h, prev_lip_distances.get(face_id))
                        face_x = face_landmarks[1].x * orig_w
                        face_y = face_landmarks[1].y * orig_h
                        center_score = 1.0 - abs(face_x - orig_w / 2) / (orig_w / 2)
                        combined_score = (activity * (1 - center_weight)) + (center_score * center_weight)
                        
                        faces_data.append({'x': face_x, 'y': face_y, 'activity': activity, 'score': combined_score})
                        prev_lip_distances[face_id] = abs(face_landmarks[13].y - face_landmarks[14].y)
                    
                    active = [f for f in faces_data if f['activity'] > lip_threshold]
                    if active:
                        best_face = max(active, key=lambda f: f['activity'])
                    else:
                        best_face = prev_best_face or min(faces_data, key=lambda f: abs(f['x'] - orig_w/2))
                    
                    prev_best_face = best_face
                    best_face_x, best_face_y = best_face['x'], best_face['y']
                    max_activity = best_face['activity']
                
                crop_x = int(best_face_x - crop_w / 2)
                crop_y = int(best_face_y - crop_h / 2)
                analyzed_indices.append(frames_read)
                analyzed_positions_x.append(max(0, min(crop_x, orig_w - crop_w)))
                analyzed_positions_y.append(max(0, min(crop_y, orig_h - crop_h)))
                analyzed_activities.append(max_activity)
                frames_read += 1

                if frames_read % 150 == 0 or (time.time() - last_log_time) > 2:
                    progress_callback((frames_read / total_frames) * 0.4 if total_frames else 0.4)
                    last_log_time = time.time()

            crop_positions = self._interpolate_sampled(analyzed_positions_x, analyzed_indices, frames_read)
            crop_ys = self._interpolate_sampled(analyzed_positions_y, analyzed_indices, frames_read)
            face_activities = self._interpolate_sampled(analyzed_activities, analyzed_indices, frames_read)
            
            if self.mediapipe_settings.get("smooth_follow", True):
                pan_limit = self.mediapipe_settings.get("pan_speed_limit", 1.8)
                crop_positions = self._smooth_follow_positions(crop_positions, pan_limit, fps=fps or 30.0)
                crop_ys = self._smooth_follow_positions(crop_ys, pan_limit * 0.8, fps=fps or 30.0)
            
            self._encode_portrait_single_pass(
                input_path, output_path, crop_positions, crop_w, crop_h, out_w, out_h,
                crop_ys=crop_ys, progress_callback=lambda p: progress_callback(0.45 + p * 0.4),
                duration=frames_read / fps,
                **({"min_run": 3, "quantize": 2} if self.mediapipe_settings.get("smooth_follow", True) else {})
            )
            cap.release()
            progress_callback(1.0)
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
                # CPU encoding — ultrafast utk render maks. cepat (720p sosial).
                # ponytail: kualitas cukup utk TikTok/Reels; naikkan preset/crf kalau mau HQ.
                return ['-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '26', '-maxrate', '3M', '-bufsize', '6M', '-threads', '0']

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
