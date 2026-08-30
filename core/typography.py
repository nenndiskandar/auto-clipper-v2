"""
core/typography.py — Font Preset System & Google Fonts Downloader.

Provides predefined caption/hook font presets (similar to CapCut "typography"
styles) plus a robust Google Fonts downloader with retry & integrity checks.

Adapted from opensource-clipping ``clipping/studio/typography.py``.

Usage (config)::

    "font_preset": {
        "style": "HORMOZI",                      # one of FONT_PRESETS keys
        "dir": "<project>/assets/fonts",
        "fallback": "C:/Windows/Fonts/corbelb.ttf"
    }
"""

import os
import shutil
import subprocess
import time

import requests

from utils.logger import debug_log
from utils.helpers import get_app_dir

FIREFOX_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:148.0) Gecko/20100101 Firefox/148.0"

# ==============================================================================
# FONT PRESETS  (primary + accent font per style)
#
# URLs point to Fontsource-hosted TTF files (direct download, no @font-face JS).
# ==============================================================================
FONT_PRESETS = {
    "DEFAULT": {
        "nama": "Klasik",
        "utama": {
            "url": "https://fontsource.org/fonts/inter/latin-700-normal.ttf",
            "file": "Inter-Bold.ttf",
        },
        "khusus": {
            "url": "https://fontsource.org/fonts/inter/latin-400-normal.ttf",
            "file": "Inter-Regular.ttf",
        },
    },
    "HORMOZI": {
        "nama": "Groovy / Kedengaran Enak",
        "utama": {
            "url": "https://fontsource.org/fonts/fredoka/latin-700-normal.ttf",
            "file": "Fredoka-Bold.ttf",
        },
        "khusus": {
            "url": "https://fontsource.org/fonts/fredoka/latin-500-normal.ttf",
            "file": "Fredoka-Medium.ttf",
        },
    },
    "STORYTELLER": {
        "nama": "Cinematic Storytelling",
        "utama": {
            "url": "https://fontsource.org/fonts/cormorant-garamond/latin-700-normal.ttf",
            "file": "CormorantGaramond-Bold.ttf",
        },
        "khusus": {
            "url": "https://fontsource.org/fonts/cormorant-garamond/latin-400-normal.ttf",
            "file": "CormorantGaramond-Regular.ttf",
        },
    },
    "CINEMATIC": {
        "nama": "Bold Impact Style",
        "utama": {
            "url": "https://fontsource.org/fonts/oswald/latin-700-normal.ttf",
            "file": "Oswald-Bold.ttf",
        },
        "khusus": {
            "url": "https://fontsource.org/fonts/oswald/latin-500-normal.ttf",
            "file": "Oswald-Medium.ttf",
        },
    },
}


def get_font_dir(app_dir: str = None) -> str:
    """Return (and create) the project fonts directory."""
    base = app_dir or str(get_app_dir())
    font_dir = os.path.join(base, "assets", "fonts")
    os.makedirs(font_dir, exist_ok=True)
    return font_dir


def download_google_font(
    url: str,
    output_filename: str,
    font_dir: str,
    max_retry: int = 10,
    min_valid_size: int = 1000,
) -> bool:
    """
    Download a Google font file with retry and basic integrity checks.

    Args:
        url: Direct download URL for the font file.
        output_filename: Local filename to write the downloaded font.
        font_dir: Destination directory.
        max_retry: Maximum network retry attempts before failing.
        min_valid_size: Minimum file size (bytes) to consider the download valid.

    Returns:
        True if the font file was downloaded and validated, otherwise False.
    """
    file_path = os.path.join(font_dir, output_filename)
    temp_path = file_path + ".part"

    def is_valid(path):
        return os.path.exists(path) and os.path.getsize(path) > min_valid_size

    if is_valid(file_path):
        debug_log(f"[Font] '{output_filename}' sudah ada dan valid.")
        return True

    headers = {
        "User-Agent": FIREFOX_UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://fontsource.org/",
        "Connection": "keep-alive",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
    }

    for percobaan in range(1, max_retry + 1):
        try:
            debug_log(f"[Font] Mengunduh '{output_filename}'... ({percobaan}/{max_retry})")
            for p in [temp_path, file_path]:
                if os.path.exists(p) and not is_valid(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass

            with requests.get(url, headers=headers, stream=True, timeout=45, allow_redirects=True) as r:
                r.raise_for_status()
                with open(temp_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)

            if not is_valid(temp_path):
                ukuran = os.path.getsize(temp_path) if os.path.exists(temp_path) else 0
                raise ValueError(f"file hasil download tidak valid ({ukuran} byte)")

            os.replace(temp_path, file_path)
            if is_valid(file_path):
                debug_log(f"[Font] '{output_filename}' berhasil diunduh.")
                return True
            raise FileNotFoundError(f"File final '{output_filename}' tidak valid di {font_dir}")
        except Exception as e:
            debug_log(f"[Font] Gagal '{output_filename}' percobaan {percobaan}: {e}")
            for p in [temp_path, file_path]:
                if os.path.exists(p):
                    try:
                        if os.path.getsize(p) <= min_valid_size:
                            os.remove(p)
                    except Exception:
                        pass
            if percobaan < max_retry:
                time.sleep(1.5)

    debug_log(f"[Font] Gagal total: '{output_filename}' setelah {max_retry} percobaan.")
    return False


def register_fonts_for_libass(font_dir: str) -> None:
    """Copy downloaded fonts to system user font dir & refresh cache (Linux/mac)."""
    if os.name == "nt":
        return
    user_font_dir = os.path.expanduser("~/.local/share/fonts")
    os.makedirs(user_font_dir, exist_ok=True)
    for fn in os.listdir(font_dir):
        if fn.lower().endswith((".ttf", ".otf")):
            src = os.path.join(font_dir, fn)
            dst = os.path.join(user_font_dir, fn)
            try:
                shutil.copy2(src, dst)
            except Exception:
                pass
    subprocess.run(["fc-cache", "-f", "-v"], capture_output=True, check=False)


def siapkan_font_tipografi(style: str = None, font_dir: str = None) -> tuple[str, str]:
    """
    Ensure the fonts for the selected style are downloaded & registered.

    Args:
        style: One of ``FONT_PRESETS`` keys. Defaults to 'HORMOZI'.
        font_dir: Destination directory. Defaults to ``assets/fonts``.

    Returns:
        Tuple ``(path_utama, path_khusus)`` — absolute paths to the two TTF files.
        Raises RuntimeError if the primary font cannot be obtained.
    """
    style = style or "HORMOZI"
    if style not in FONT_PRESETS:
        debug_log(f"[Font] Gaya '{style}' tidak dikenal, fallback ke HORMOZI.")
        style = "HORMOZI"

    font_dir = font_dir or get_font_dir()
    os.makedirs(font_dir, exist_ok=True)

    daftar = FONT_PRESETS[style]
    f_utama = daftar["utama"]
    f_khusus = daftar["khusus"]

    ok_utama = download_google_font(f_utama["url"], f_utama["file"], font_dir)
    ok_khusus = download_google_font(f_khusus["url"], f_khusus["file"], font_dir)

    path_utama = os.path.join(font_dir, f_utama["file"])
    path_khusus = os.path.join(font_dir, f_khusus["file"])

    if not (ok_utama and os.path.exists(path_utama) and os.path.getsize(path_utama) > 1000):
        raise RuntimeError(f"Font utama gagal disiapkan: {path_utama}")

    register_fonts_for_libass(font_dir)
    debug_log(f"[Font] Preset '{style}' siap di: {font_dir}")
    return path_utama, path_khusus


def resolve_preset_font(style: str = None, font_dir: str = None) -> str:
    """
    Best-effort: return primary font path for the preset (downloading if needed).
    On any failure returns an empty string (caller falls back to system font).
    """
    try:
        path_utama, _ = siapkan_font_tipografi(style, font_dir)
        return path_utama
    except Exception as e:
        debug_log(f"[Font] Gagal menyiapkan preset font ({style}): {e}")
        return ""