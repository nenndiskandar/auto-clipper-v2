"""
Helper utility functions for Auto Clipper
"""

import sys
import re
import shutil
from pathlib import Path
from utils.logger import debug_log


def get_app_dir():
    """Get application data directory
    
    On macOS (.app bundle): ~/Library/Application Support/AutoClipper/
    On Windows/Linux or dev mode: directory containing the executable/script
    
    This ensures user data (config, downloads, output) persists across app updates on macOS.
    """
    if getattr(sys, 'frozen', False):
        if sys.platform == "darwin":
            # macOS: use Application Support (survives .app replacement)
            app_support = Path.home() / "Library" / "Application Support" / "AutoClipper"
            app_support.mkdir(parents=True, exist_ok=True)
            return app_support
        return Path(sys.executable).parent
    return Path(__file__).parent.parent


def get_bundle_dir():
    """Get bundled resources directory"""
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS) if hasattr(sys, '_MEIPASS') else get_app_dir()
    return get_app_dir()


def get_ffmpeg_path():
    """Get FFmpeg executable path
    
    Checks in order:
    1. Bundled ffmpeg in app_dir/ffmpeg/ folder (downloaded via Library page)
    2. ffmpeg in system PATH
    3. Default "ffmpeg" command
    """
    app_dir = get_app_dir()
    
    # Check bundled ffmpeg (works for both frozen and development)
    if sys.platform.startswith('win'):
        bundled = app_dir / "ffmpeg" / "ffmpeg.exe"
    else:
        bundled = app_dir / "ffmpeg" / "ffmpeg"
    
    if bundled.exists():
        return str(bundled)
    
    # Try to find ffmpeg in PATH
    ffmpeg_in_path = shutil.which("ffmpeg")
    if ffmpeg_in_path:
        return ffmpeg_in_path
    
    # Fallback to command name
    return "ffmpeg"


def get_ytdlp_path():
    """Get yt-dlp executable path or check if module is available
    
    Checks in order:
    1. yt-dlp Python module (preferred - bundled with PyInstaller)
    2. Bundled yt-dlp.exe (Windows)
    3. yt-dlp in system PATH
    4. Default "yt-dlp" command
    
    Returns:
        str: Path to yt-dlp executable, or "yt_dlp_module" if using Python module
    """
    # First check if yt-dlp is available as Python module
    try:
        import yt_dlp
        return "yt_dlp_module"  # Special marker to use module instead of subprocess
    except ImportError:
        pass
    
    if getattr(sys, 'frozen', False):
        bundled = get_app_dir() / "yt-dlp.exe"
        if bundled.exists():
            return str(bundled)
    
    # Try to find yt-dlp in PATH
    yt_dlp_path = shutil.which("yt-dlp")
    if yt_dlp_path:
        return yt_dlp_path
    
    # Fallback to command name (will work if it's in system PATH)
    return "yt-dlp"


def is_ytdlp_module_available():
    """Check if yt-dlp Python module is available"""
    try:
        import yt_dlp
        return True
    except ImportError:
        return False


def get_deno_path():
    """Get Deno executable path (required for yt-dlp --remote-components)
    
    Checks in order:
    1. Bundled deno in app_dir/bin/ folder (downloaded via Library page)
    2. deno in system PATH
    3. None if not found
    """
    app_dir = get_app_dir()
    
    # Check bundled deno (works for both frozen and development)
    if sys.platform.startswith('win'):
        bundled = app_dir / "bin" / "deno.exe"
    else:
        bundled = app_dir / "bin" / "deno"
    
    if bundled.exists():
        return str(bundled)
    
    # Try to find deno in PATH
    deno_path = shutil.which("deno")
    if deno_path:
        return deno_path
    
    # Not found
    return None


def get_mediapipe_model_path():
    """Get path to face_landmarker.task model, auto-downloading if missing"""
    app_dir = get_app_dir()
    model_path = app_dir / "bin" / "face_landmarker.task"
    
    if model_path.exists():
        return str(model_path)
    
    # Check bundle dir (e.g. PyInstaller temp dir)
    bundle_model = get_bundle_dir() / "bin" / "face_landmarker.task"
    if bundle_model.exists():
        return str(bundle_model)
    
    # Auto-download using dependency manager if missing
    try:
        from utils.dependency_manager import setup_mediapipe_model
        if setup_mediapipe_model(app_dir):
            if model_path.exists():
                return str(model_path)
    except Exception as e:
        debug_log(f"Error auto-downloading MediaPipe model: {e}")
        
    return str(model_path)


def extract_video_id(url: str) -> str:
    """Extract video ID with multi-platform prefix (fb_xxx, tiktok_xxx, ig_xxx)."""
    # YouTube 11-char
    for pat in [r'(?:v=|\/)([0-9A-Za-z_-]{11}).*', r'(?:youtu\.be\/)([0-9A-Za-z_-]{11})']:
        m=re.search(pat, url)
        if m: return m.group(1)
    # FB
    if 'facebook.com' in url or 'fb.watch' in url:
        m=re.search(r'/videos/(\d+)|/reel/(\d+)|v=(\d+)', url)
        if m:
            for g in m.groups():
                if g: return f'fb_{g[:12]}'
        import hashlib; return 'fb_'+hashlib.md5(url.encode()).hexdigest()[:10]
    if 'tiktok.com' in url:
        m=re.search(r'/video/(\d+)', url)
        if m: return f'tiktok_{m.group(1)[-10:]}'
        import hashlib; return 'tiktok_'+hashlib.md5(url.encode()).hexdigest()[:10]
    if 'instagram.com' in url:
        m=re.search(r'/(?:p|reel)/([^/?#&]+)', url)
        if m: return f'ig_{m.group(1)[:12]}'
        import hashlib; return 'ig_'+hashlib.md5(url.encode()).hexdigest()[:10]
    if 'twitter.com' in url or 'x.com' in url:
        m=re.search(r'/status/(\d+)', url)
        if m: return f'x_{m.group(1)[-10:]}'
        import hashlib; return 'x_'+hashlib.md5(url.encode()).hexdigest()[:10]
    import hashlib
    return hashlib.md5(url.encode()).hexdigest()[:12]
