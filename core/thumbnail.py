"""
core/thumbnail.py — Thumbnail Generator.

Extracts a representative frame from a rendered clip, darkens it, and
composites the clip title on top as a YouTube-thumbnail-style image.

Adapted from opensource-clipping ``clipping/studio/thumbnail.py``.
"""

import os
import textwrap
import urllib.request

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from utils.logger import debug_log

# Default thumbnail font (system-fallback to a bold font is attempted first).
THUMBNAIL_FONT_URL = (
    "https://fontsource.org/fonts/inter/latin-800-normal.ttf"
)
THUMBNAIL_FONT_FILE = "Inter-ExtraBold.ttf"


def _pick_thumbnail_font(font_dir: str = None) -> str | None:
    """Resolve a bold TTF usable for thumbnails (downloads Inter ExtraBold if needed)."""
    candidates = []
    if font_dir:
        candidates.append(os.path.join(font_dir, THUMBNAIL_FONT_FILE))
    # Windows system bold fonts
    if os.name == "nt":
        candidates += [
            r"C:\Windows\Fonts\arialbd.ttf",
            r"C:\Windows\Fonts\impact.ttf",
            r"C:\Windows\Fonts\verdanab.ttf",
        ]
    for c in candidates:
        if c and os.path.exists(c) and os.path.getsize(c) > 1000:
            return c

    dest = None
    if not font_dir:
        from utils.helpers import get_app_dir
        font_dir = os.path.join(str(get_app_dir()), "assets", "fonts")
        os.makedirs(font_dir, exist_ok=True)
        dest = os.path.join(font_dir, THUMBNAIL_FONT_FILE)

    if dest and os.path.exists(dest) and os.path.getsize(dest) > 1000:
        return dest
    try:
        urllib.request.urlretrieve(THUMBNAIL_FONT_URL, dest)
        if os.path.getsize(dest) > 1000:
            return dest
    except Exception as e:
        debug_log(f"[Thumbnail] Gagal unduh font: {e}")
    return None


def buat_thumbnail(
    video_path: str,
    output_image_path: str,
    teks: str,
    font_path: str = None,
    frame_ms: int = 5000,
    overlay_alpha: int = 128,
) -> str | None:
    """
    Extract a frame from the video, composite the clip title, save as JPEG/PNG.

    Args:
        video_path: Path to the rendered clip video.
        output_image_path: Destination for the thumbnail image.
        teks: Title text to write on the thumbnail.
        font_path: Optional path to a TTF. Auto-resolves a bold font if empty.
        frame_ms: Timestamp (ms) of the frame to extract.
        overlay_alpha: Dark overlay alpha 0-255 (128 = slightly dark).

    Returns:
        Path to the created image, or None if creation fails.
    """
    if not os.path.exists(video_path):
        debug_log(f"[Thumbnail] Video tidak ditemukan: {video_path}")
        return None

    font_file = font_path or _pick_thumbnail_font()
    pil_font = None
    cap = cv2.VideoCapture(video_path)
    cap.set(cv2.CAP_PROP_POS_MSEC, frame_ms)
    ret, frame = cap.read()
    cap.release()

    if not ret:
        debug_log("[Thumbnail] Tidak bisa membaca frame (pastikan duration >= 5s).")
        return None

    img = Image.alpha_composite(
        Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)).convert("RGBA"),
        Image.new("RGBA", (frame.shape[1], frame.shape[0]), (0, 0, 0, overlay_alpha)),
    ).convert("RGB")

    draw = ImageDraw.Draw(img)
    font_sz = int(img.size[0] * 0.12)
    if font_file and os.path.exists(font_file):
        try:
            pil_font = ImageFont.truetype(font_file, font_sz)
        except Exception as e:
            debug_log(f"[Thumbnail] Gagal load font {font_file}: {e}")
            pil_font = None
    if pil_font is None:
        pil_font = ImageFont.load_default()

    lines = textwrap.wrap(str(teks or ""), width=12) or ["Clip"]
    y_text = (img.size[1] - (len(lines) * (font_sz + 10))) // 2
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=pil_font)
        line_w = bbox[2] - bbox[0]
        x_text = (img.size[0] - line_w) // 2
        draw.text(
            (x_text, y_text),
            line,
            font=pil_font,
            fill="white",
            stroke_width=5,
            stroke_fill="black",
        )
        y_text += font_sz + 10

    img.save(output_image_path)
    debug_log(f"[Thumbnail] Disimpan: {output_image_path}")
    return output_image_path