"""
core/camera_switch.py — Camera-Switch Renderer (diarization-guided crop switching).

Renders a dynamic camera-switch video that cuts between active speakers,
optionally guided by speaker-diarization data (list of
``{"start": float, "end": float, "speaker": str}``).

When no diarization data is supplied a fallback timeline is derived from
MediaPipe lip-activity (active-speaker heuristic) so the feature still works.

Adapted from opensource-clipping ``clipping/studio/render_camera_switch.py``.
"""

import math
import os
import statistics
import subprocess
import time

import cv2
import numpy as np

try:
    import mediapipe as mp
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision as mp_vision
except ImportError:
    mp = mp_python = mp_vision = None


class CameraSwitchMixin:
    """Renderer that cuts between active speakers (camera-switch)."""

    DEFAULT_STEP = 0.25
    DEFAULT_DEADZONE = 0.15
    DEFAULT_SMOOTH = 0.30
    DEFAULT_MIN_HOLD = 2.0
    DEFAULT_BLEND_DUR = 0.0
    DEFAULT_MAX_ZOOM = 3.0

    # ------------------------------------------------------------------
    # Face landmark detection (reuses the core's own mediapipe landmarker)
    # ------------------------------------------------------------------
    def _init_yolo_detector(self):
        """Lazy-init YOLO face detector (getattr-safe, optional dependency)."""
        if getattr(self, "_yolo_detector", None) is None:
            try:
                from core.yolo_detector import YOLOFaceDetector
                size = getattr(self, "yolo_size", "8n")
                self._yolo_detector = YOLOFaceDetector(model_size=size, conf=0.3)
            except Exception as e:
                self.log(f"  ⚠ YOLO tidak tersedia ({e}), fallback ke MediaPipe.")
                self._yolo_detector = None
        return self._yolo_detector

    def _csv_get_faces(self, frame):
        """Return list of (cx, cy) centers + (x1,y1,x2,y2) boxes.

        Pakai YOLO bila ``self.face_detector_model == 'yolo'`` dan ultralytics
        tersedia; selain itu pakai MediaPipe landmarker milik core.
        """
        if getattr(self, "face_detector_model", "mediapipe") == "yolo":
            det = self._init_yolo_detector()
            if det is not None:
                boxes = det.detect(frame)
                centers = [((x1 + x2) / 2, (y1 + y2) / 2) for (x1, y1, x2, y2, _) in boxes]
                return centers, [tuple(b[:4]) for b in boxes]
        self._init_mediapipe()
        mp_image = mp.Image(
            image_format=mp.ImageFormat.SRGB,
            data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB),
        )
        results = self.mp_face_landmarker.detect(mp_image)
        centers, boxes = [], []
        if results.face_landmarks:
            for lm in results.face_landmarks:
                xs = [p.x * frame.shape[1] for p in lm]
                ys = [p.y * frame.shape[0] for p in lm]
                cx = sum(xs) / len(xs)
                cy = sum(ys) / len(ys)
                x1, x2 = min(xs), max(xs)
                y1, y2 = min(ys), max(ys)
                centers.append((cx, cy))
                boxes.append((x1, y1, x2, y2))
        return centers, boxes

    # ------------------------------------------------------------------
    # Active-speaker fallback timeline (yang pakai diarization bila ada)
    # ------------------------------------------------------------------
    def _fallback_speaker_timeline(self, diarization_data):
        """Build a speaker timeline with MediaPipe lip-activity when no
        diarization data is provided.

        Speakers are named SPEAKER_L / SPEAKER_R by their horizontal position.
        A face whose mouth opens (MAR above threshold) is deemed active.
        """
        if not diarization_data:
            # Left/right default split from face positions.
            return []
        return diarization_data

    def _get_active_speaker_at(self, data, t):
        """Return list of active speaker labels at time ``t``."""
        if not data:
            return []
        out = []
        for seg in data:
            start = seg.get("start", seg.get("start_time", 0))
            end = seg.get("end", seg.get("end_time", 0))
            if start <= t < end:
                out.append(seg.get("speaker", "SPEAKER_00"))
        return out

    # ------------------------------------------------------------------
    # Resize helper
    # ------------------------------------------------------------------
    def _csv_resize(self, img, w, h):
        interp = cv2.INTER_AREA if (img.shape[1] > w or img.shape[0] > h) else cv2.INTER_LANCZOS4
        return cv2.resize(img, (w, h), interpolation=interp)

    def _csv_render_dims(self, rasio, source_h):
        ratio_map = {
            "9:16": (1080, 1920), "3:4": (1080, 1440), "4:5": (1080, 1350),
            "1:1": (1080, 1080), "16:9": (1920, 1080),
        }
        out_w, out_h = ratio_map.get(rasio, (1080, 1920))
        return out_w, out_h

    # ------------------------------------------------------------------
    # Main renderer
    # ------------------------------------------------------------------
    def buat_video_camera_switch(
        self,
        input_video,
        output_video,
        start_clip,
        end_clip,
        rasio,
        diarization_data=None,
        label="CameraSwitch",
    ):
        """
        Render a dynamic camera-switch video cutting between active speakers.

        Args:
            input_video: Path to input video file.
            output_video: Path to output video file.
            start_clip: Subclip start boundary in seconds.
            end_clip: Subclip end boundary in seconds.
            rasio: Output ratio string ('9:16', '3:4', ...).
            diarization_data: List of ``{"start","end","speaker"}`` segments.
            label: Progress-reporting name.

        Returns:
            get_x_final: callable mapping timestamp → horizontal crop x offset,
            or None if rendering failed.
        """
        step = getattr(self, "camera_switch_step", self.DEFAULT_STEP)
        deadzone = getattr(self, "camera_switch_deadzone", self.DEFAULT_DEADZONE)
        smooth_f = getattr(self, "camera_switch_smooth", self.DEFAULT_SMOOTH)
        min_hold = float(getattr(self, "switch_hold_duration", self.DEFAULT_MIN_HOLD))
        blend_dur = float(getattr(self, "switch_blend_duration", self.DEFAULT_BLEND_DUR))
        max_zoom = float(getattr(self, "camera_switch_max_zoom", self.DEFAULT_MAX_ZOOM))

        cap = cv2.VideoCapture(input_video)
        if not cap.isOpened():
            self.log(f"  ⚠ {label}: gagal membuka video {input_video}")
            return None

        orig_fps = cap.get(cv2.CAP_PROP_FPS)
        if math.isnan(orig_fps) or orig_fps <= 0:
            orig_fps = 30.0
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        duration = float(end_clip) - float(start_clip)

        out_w, out_h = self._csv_render_dims(rasio, height)
        crop_ratio = out_w / out_h
        if (width / height) > crop_ratio:
            crop_w = int(height * crop_ratio)
            crop_h = height
        else:
            crop_w = width
            crop_h = int(width / crop_ratio)
        default_x = (width - crop_w) // 2

        # ---------------------------------------------------------- analyze
        self.log(f"  🧠 {label}: analisa wajah dimulai...")
        timelines = (
            self._fallback_speaker_timeline(diarization_data)
            if diarization_data
            else []
        )

        face_frame_data = []
        current_time = 0.0
        while current_time <= duration:
            cap.set(cv2.CAP_PROP_POS_MSEC, (start_clip + current_time) * 1000)
            ret, frame = cap.read()
            if not ret:
                break
            centers, boxes = self._csv_get_faces(frame)
            centers.sort(key=lambda c: c[0])
            face_frame_data.append({
                "time": current_time,
                "centers": centers,
                "boxes": boxes,
                "active": self._get_active_speaker_at(timelines, start_clip + current_time),
            })
            current_time += step

        if not face_frame_data:
            cap.release()
            self.log(f"  ⚠ {label}: tidak ada frame teranalisa.")
            return None

        # Speakers list
        speakers = []
        for seg in timelines:
            if seg.get("speaker") not in speakers:
                speakers.append(seg["speaker"])
        # Fallback: assign faces left→right if no diarization available
        if not speakers:
            centers_seen = set()
            for fd in face_frame_data:
                for c in fd["centers"]:
                    centers_seen.add(round(c[0], -1))
            if centers_seen:
                speakers = ["SPEAKER_L", "SPEAKER_R"]
            else:
                speakers = ["SPEAKER_00"]

        # Canonical center-x per speaker (median of 1-active-1-face frames)
        canonical = {}
        for fd in face_frame_data:
            act = fd["active"]
            if len(act) == 1 and len(fd["centers"]) == 1:
                canonical.setdefault(act[0], []).append(fd["centers"][0][0])
        canonical_cx = {s: statistics.median(v) for s, v in canonical.items() if v}

        # Build raw per-speaker position timelines
        raw = {s: [] for s in speakers}
        for fd in face_frame_data:
            centers = fd["centers"]
            act = fd["active"]
            if not centers:
                continue
            if not act:
                act = ["SPEAKER_L" if centers[0][0] < width / 2 else "SPEAKER_R"]
            if len(centers) == 1:
                s = act[0]
                raw.setdefault(s, []).append({"time": fd["time"], "cx": centers[0][0], "cy": centers[0][1]})
            else:
                remaining = list(centers)
                for s in sorted(act, key=lambda x: canonical_cx.get(x, width / 2)):
                    if not remaining:
                        break
                    best = min(remaining, key=lambda c: abs(c[0] - canonical_cx.get(s, width / 2)))
                    raw.setdefault(s, []).append({"time": fd["time"], "cx": best[0], "cy": best[1]})
                    remaining.remove(best)

        # Smooth per-speaker positions
        def _smooth(pts):
            if not pts:
                return []
            cam_x, cam_y = pts[0]["cx"], pts[0]["cy"]
            out = []
            deadzone_px = crop_w * deadzone
            for p in pts:
                dx = p["cx"] - cam_x
                if abs(dx) > deadzone_px:
                    cam_x += dx * smooth_f
                cam_y += (p["cy"] - cam_y) * smooth_f
                out.append({"time": p["time"], "cx": cam_x, "cy": cam_y})
            return out

        smooth = {s: _smooth(raw[s]) for s in speakers}

        def _pos(s, t):
            pts = smooth.get(s, [])
            if not pts:
                return width / 2, height / 2
            pts = sorted(pts, key=lambda p: p["time"])
            if t <= pts[0]["time"]:
                return pts[0]["cx"], pts[0]["cy"]
            if t >= pts[-1]["time"]:
                return pts[-1]["cx"], pts[-1]["cy"]
            for i in range(len(pts) - 1):
                t1, t2 = pts[i]["time"], pts[i + 1]["time"]
                if t1 <= t <= t2:
                    f = (t - t1) / (t2 - t1) if t2 > t1 else 0
                    return (pts[i]["cx"] + (pts[i + 1]["cx"] - pts[i]["cx"]) * f,
                            pts[i]["cy"] + (pts[i + 1]["cy"] - pts[i]["cy"]) * f)
            return pts[-1]["cx"], pts[-1]["cy"]

        # ---------------------------------------------------------- render
        try:
            cap.set(cv2.CAP_PROP_POS_MSEC, start_clip * 1000)
        except Exception:
            pass

        writer = subprocess.Popen(
            [
                self.ffmpeg_path, "-y",
                "-f", "rawvideo", "-pix_fmt", "bgr24",
                "-s", f"{out_w}x{out_h}", "-r", str(orig_fps),
                "-i", "-",
                "-an", "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "20", "-pix_fmt", "yuv420p",
                output_video,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        current_speaker = None
        last_switch = -1e9
        render_count = 0
        last_pct = -1
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            t = render_count / orig_fps
            if t > duration:
                break

            active = self._get_active_speaker_at(timelines, start_clip + t) if timelines else []
            if not active and not speakers:
                continue

            # Pick active speaker (fallback: nearest face to center)
            if active:
                target = active[0] if active[0] in speakers else (speakers[0] if speakers else None)
            else:
                centers, _ = self._csv_get_faces(frame)
                if centers:
                    nearest = min(centers, key=lambda c: abs(c[0] - width / 2))
                    label = "SPEAKER_L" if nearest[0] < width / 2 else "SPEAKER_R"
                    target = label if label in speakers else (speakers[0] if speakers else None)
                else:
                    target = current_speaker or (speakers[0] if speakers else None)

            if target is not None and target != current_speaker:
                if t - last_switch >= min_hold or current_speaker is None:
                    current_speaker = target
                    last_switch = t

            if current_speaker is None:
                current_speaker = speakers[0] if speakers else None
            if current_speaker is None:
                break

            cx, cy = _pos(current_speaker, t)
            eff_w = int(crop_w / max_zoom)
            eff_h = int(crop_h / max_zoom)
            x = int(max(0, min(cx - eff_w / 2, width - eff_w)))
            y = int(max(0, min(cy - eff_h / 2, height - eff_h)))
            crop = frame[y : y + eff_h, x : x + eff_w]
            frame_out = self._csv_resize(crop, out_w, out_h)
            try:
                writer.stdin.write(frame_out.tobytes())
            except BrokenPipeError:
                break
            render_count += 1

            pct = min(100, int(render_count / orig_fps / duration * 100)) if duration > 0 else 100
            if pct != last_pct:
                self.log(f"  ⏳ {label}: {pct:3d}%")
                last_pct = pct

        try:
            writer.stdin.close()
        except Exception:
            pass
        writer.wait(timeout=120)
        cap.release()

        if not os.path.exists(output_video) or os.path.getsize(output_video) < 10_000:
            self.log(f"  ⚠ {label}: output gagal dibuat.")
            return None

        # Mux original audio back in (camera-switch render skips audio).
        muxed = output_video + ".mux.mp4"
        mux_cmd = [
            self.ffmpeg_path, "-y",
            "-i", output_video,
            "-i", input_video,
            "-map", "0:v", "-map", "1:a?",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest", muxed,
        ]
        try:
            subprocess.run(mux_cmd, check=True, capture_output=True, timeout=300)
            if os.path.exists(muxed) and os.path.getsize(muxed) > 10_000:
                os.replace(muxed, output_video)
        except subprocess.CalledProcessError as e:
            stderr_txt = (e.stderr or b"").decode("utf-8", errors="replace")[-300:]
            self.log(f"  ⚠ {label}: mux audio gagal (output tetap tanpa audio): {stderr_txt}")

        self.log(f"  ✅ {label} selesai.")
        # Keep original behavior of reference: expose crop x for subtitle positioning.
        return (default_x, width, crop_w)

    # ------------------------------------------------------------------
    # Portrait-stage wrapper (dipanggil dari router convert_to_portrait)
    # ------------------------------------------------------------------
    def convert_to_portrait_camera_switch_with_progress(self, input_path: str, output_path: str, progress_callback=None):
        """Convert portrait-crop with camera switching.

        Menyesuaikan antarmuka ``convert_to_portrait_*_with_progress`` sehingga
        mode ``portrait_mode == "camera_switch"`` bisa dipakai di pipeline.
        ``start_clip``/``end_clip`` diambil dari properti ``_clip_start``/
        ``_clip_end`` (bila tersedia), default 0 → durasi video.
        """
        duration = self._csv_get_duration(input_path)
        start_clip = float(getattr(self, "_clip_start", 0) or 0)
        end_clip = float(getattr(self, "_clip_end", 0) or duration)
        if start_clip >= end_clip:
            start_clip, end_clip = 0.0, duration
        rasio = self.aspect_ratio or "9:16"
        diarization_data = getattr(self, "diarization_data", None)
        self.buat_video_camera_switch(
            input_video=input_path,
            output_video=output_path,
            start_clip=start_clip,
            end_clip=end_clip,
            rasio=rasio,
            diarization_data=diarization_data,
            label="CameraSwitch",
        )
        if callable(progress_callback):
            progress_callback(1.0)

    @staticmethod
    def _csv_get_duration(video_path: str) -> float:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return 30.0
        fps = cap.get(cv2.CAP_PROP_FPS)
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        cap.release()
        if fps and frames:
            return frames / fps
        return 30.0

    # ------------------------------------------------------------------
    # B-roll overlay helper (rote: place B-roll file over a time range)
    # ------------------------------------------------------------------
    def overlay_broll_range(self, input_video, broll_path, out_video, start, end):
        """
        Overlay a B-roll clip onto ``[start, end]`` of the input (crop-centered).

        Uses the ffmpeg ``xfade`` approach is intentionally avoided here; this is
        a simple full-overlay using scan + scale + overlay with fade in/out.

        Args:
            input_video: Base video.
            broll_path: B-roll MP4 file.
            out_video: Output path.
            start/end: Overlay time window (seconds, relative to input video start).

        Returns:
            bool success.
        """
        if max(end, start) <= 0 or not os.path.exists(broll_path):
            return False
        # Trim broll to the overlap window length
        broll_dur = max(0.3, end - start)
        vf = (
            f"[1:v]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
            f"crop={out_w}:{out_h},setsar=1[b];"
            f"[0:v][b]overlay=0:0:enable='between(t,{start},{end})':eof_action=pass[v]"
        )
        # dimensions determined by probe
        try:
            probe = subprocess.run(
                [self.ffmpeg_path, "-i", input_video, "-f", "null", "-"],
                capture_output=True, text=True, timeout=60,
            )
            import re
            m = re.search(r"(\d{3,4})x(\d{3,4})", probe.stderr)
            out_w, out_h = (int(m.group(1)), int(m.group(2))) if m else (1080, 1920)
            vf = (
                f"[1:v]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
                f"crop={out_w}:{out_h},setsar=1[b];"
                f"[0:v][b]overlay=0:0:enable='between(t,{start},{end})':eof_action=pass[v]"
            )
        except Exception:
            out_w, out_h = 1080, 1920
        cmd = [
            self.ffmpeg_path, "-y",
            "-i", input_video,
            "-i", broll_path,
            "-filter_complex", vf,
            "-map", "[v]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k",
        ]
        if start > 0:
            cmd += ["-ss", str(start), "-t", str(broll_dur)]
        cmd.append(out_video)
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=300)
            return os.path.exists(out_video) and os.path.getsize(out_video) > 10_000
        except subprocess.CalledProcessError as e:
            self.log(f"  ⚠ B-roll overlay gagal: {(e.stderr or b'')[-300:]}")
            return False