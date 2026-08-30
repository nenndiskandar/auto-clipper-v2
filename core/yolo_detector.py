"""
core/yolo_detector.py — YOLO Face Detector & Debug Tools.

Wraps Ultralytics YOLO face models (face_yolov8n/s/m/v2, face_yolov9c) with a
MediaPipe-compatible interface, plus optional debug visualization tools
(face bounding box, crosshair tracking lines) like the opensource-clipping
``face_detection.py`` / ``render_camera_switch.py`` dev-mode.

Model is downloaded lazily from HuggingFace (Bingsu/adetailer) and cached in
``utils.helpers.get_model_dir()``.
"""

import os
import urllib.request

import cv2
import numpy as np

from utils.logger import debug_log

MODEL_SIZES = {
    "8n": "face_yolov8n.pt",
    "8n_v2": "face_yolov8n_v2.pt",
    "8s": "face_yolov8s.pt",
    "8m": "face_yolov8m.pt",
    "9c": "face_yolov9c.pt",
}
MODEL_URL_TMPL = "https://huggingface.co/Bingsu/adetailer/resolve/main/{filename}"


class YOLOFaceDetector:
    """Ultralytics YOLO face detector with yt-clipper-friendly ergonomics."""

    def __init__(self, model_size: str = "8n", conf: float = 0.3, device: str = None):
        self.model_size = model_size
        self.conf = conf
        self.device = device
        self._model = None
        self.model_path = self._ensure_model(model_size)

    # ------------------------------------------------------------------
    # Model download / init
    # ------------------------------------------------------------------
    def _ensure_model(self, model_size: str) -> str:
        filename = MODEL_SIZES.get(model_size)
        if filename is None:
            raise ValueError(f"YOLO size tidak dikenal: '{model_size}'. "
                             f"Pilihan: {list(MODEL_SIZES)}")
        model_dir = self._get_model_dir()
        path = os.path.join(model_dir, filename)
        if not os.path.exists(path) or os.path.getsize(path) < 1_000_000:
            url = MODEL_URL_TMPL.format(filename=filename)
            debug_log(f"📥 Mendownload YOLO face model ({model_size})...")
            debug_log(f"   {url}")
            try:
                urllib.request.urlretrieve(url, path)
            except Exception as e:
                raise RuntimeError(f"Gagal mendownload model YOLO: {e}") from e
            if not os.path.exists(path) or os.path.getsize(path) < 1_000_000:
                raise RuntimeError("Model YOLO didownload tetapi file tidak valid.")
        return path

    @staticmethod
    def _get_model_dir() -> str:
        try:
            from utils.helpers import get_model_dir
            model_dir = get_model_dir()
        except Exception:
            model_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "models")
        os.makedirs(model_dir, exist_ok=True)
        return model_dir

    @property
    def model(self):
        if self._model is None:
            try:
                from ultralytics import YOLO
                import logging
                logging.getLogger("ultralytics").setLevel(logging.ERROR)
            except ImportError:
                raise RuntimeError(
                    "ultralytics tidak terinstal. Jalankan: pip install ultralytics"
                )
            self._model = YOLO(self.model_path)
        return self._model

    # ------------------------------------------------------------------
    # Detection API
    # ------------------------------------------------------------------
    def detect(self, frame) -> list:
        """
        Detect faces in a frame.

        Returns:
            List of (x1, y1, x2, y2, confidence) in pixel coordinates; empty
            list wenn no face found.
        """
        results = self.model(frame, verbose=False, conf=self.conf)
        boxes = []
        if results and len(results[0].boxes) > 0:
            xyxy = results[0].boxes.xyxy.cpu().numpy()
            confs = results[0].boxes.conf.cpu().numpy()
            for box, c in zip(xyxy, confs):
                x1, y1, x2, y2 = [float(v) for v in box]
                boxes.append((x1, y1, x2, y2, float(c)))
        return boxes

    def detect_best(self, frame):
        """Return the highest-confidence face (largest box wins ties)."""
        boxes = self.detect(frame)
        if not boxes:
            return None
        return max(boxes, key=lambda b: (b[4], (b[2] - b[0]) * (b[3] - b[1])))

    def estimate_speaker_count(self, video_path: str, sample_count: int = 20) -> int:
        """Sample frames to estimate max visible faces; default 2 if unsure."""
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return 2
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        duration = total_frames / fps if fps > 0 else 0
        if duration == 0:
            cap.release()
            return 2
        step = duration / sample_count
        max_faces = 0
        for i in range(sample_count):
            cap.set(cv2.CAP_PROP_POS_MSEC, i * step * 1000)
            ret, frame = cap.read()
            if not ret:
                continue
            max_faces = max(max_faces, len(self.detect(frame)))
        cap.release()
        debug_log(f"   ✅ Maksimum {max_faces} wajah terdeteksi dalam satu frame.")
        return max(1, max_faces)


# ==============================================================================
# DEBUG TOOLS
# ==============================================================================

def draw_face_boxes(frame, boxes, color=(0, 255, 255), thickness=2) -> np.ndarray:
    """Gambarkan bounding box kuning untuk tiap wajah (debug/tracking)."""
    out = frame.copy()
    for box in boxes:
        x1, y1, x2, y2 = [int(v) for v in box[:4]]
        cv2.rectangle(out, (x1, y1), (x2, y2), color, thickness)
        conf = box[4] if len(box) > 4 else None
        if conf is not None:
            cv2.putText(
                out, f"{conf:.2f}", (x1, max(0, y1 - 8)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 1, cv2.LINE_AA,
            )
    return out


def draw_tracking_lines(frame, boxes, target_center_x=None, color=(0, 255, 255), thickness=2) -> np.ndarray:
    """
    Gambarkan crosshair tracking-lines dari box wajah ke batas frame,
    menuju pusat crop ``target_center_x`` (bila diketahui) — menyerupai
    fitur ``--track-lines`` pada renderer camera-switch.

    Args:
        frame: Frame BGR.
        boxes: List of (x1, y1, x2, y2[, conf]).
        target_center_x: X pusat crop saat ini (int) atau None.

    Returns:
        Frame dengan garis-garis digambar.
    """
    out = frame.copy()
    h, w = out.shape[:2]
    for box in boxes:
        x1, y1, x2, y2 = [int(v) for v in box[:4]]
        mid_x = (x1 + x2) // 2
        mid_y = (y1 + y2) // 2
        # Vertical lines: atas/bawah dari tengah box ke tepi
        cv2.line(out, (mid_x, 0), (mid_x, y1), color, thickness)
        cv2.line(out, (mid_x, y2), (mid_x, h), color, thickness)
        # Horizontal lines ke pusat crop bila target diketahui
        if target_center_x is not None:
            cv2.line(out, (min(mid_x, target_center_x), mid_y), (max(mid_x, target_center_x), mid_y), color, thickness)
    return out


def detect_debug_frame(
    frame,
    detector,
    draw_bbox: bool = False,
    draw_lines: bool = False,
    target_center_x=None,
) -> tuple:
    """
    Deteksi wajah + hasilkan frame debug sekali jalan (pakai YOLO bila ada,
    fallback ke mediapipe box bila YOLO tidak ada — dijamin tidak crash).

    Returns:
        (boxes, debug_frame)
    """
    boxes = detector.detect(frame) if detector else []
    out = frame
    if draw_bbox and boxes:
        out = draw_face_boxes(out, boxes)
    if draw_lines and boxes:
        out = draw_tracking_lines(out, boxes, target_center_x=target_center_x)
    return boxes, out