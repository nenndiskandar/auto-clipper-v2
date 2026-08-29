"""
Telegram Bot interface for Auto Clipper (Phase 1 & Phase 2 optimized flow)
"""

import os
import sys
import re
import html
import json
import time
import shutil
import asyncio
import logging
import threading
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path
from logging.handlers import RotatingFileHandler
from typing import Dict, Any

# Fix Windows console encoding for emojis
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import KeyboardButtonStyle
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.request import HTTPXRequest

from config.config_manager import ConfigManager
from clipper_core import AutoClipperCore, SubtitleNotFoundError
from utils.helpers import get_ffmpeg_path, get_ytdlp_path
from openai import OpenAI

LOG_FILE = Path(__file__).parent.resolve() / "telegram-bot.log"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            LOG_FILE,
            maxBytes=5 * 1024 * 1024,   # 5 MB per file
            backupCount=3,              # simpan 3 backup (rotasi otomatis)
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger(__name__)

USER_SESSIONS: Dict[int, Dict[str, Any]] = {}
# Track active phases per chat so the same session is never processed twice in
# parallel (prevents multiple yt-dlp writing to the same section file).
ACTIVE_CHATS: Dict[int, str] = {}
session_lock = threading.Lock()
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


INTERMEDIATE_NAMES = {"captioned", "hook", "portrait", "landscape", "credit", "hooked", "watermark"}


def find_final_clips(clips_dir: Path):
    """Return the final rendered clip(s) per clip subfolder.

    The final clip is named '<clip_title>.mp4' (same as its folder), while
    intermediate files (captioned.mp4, hook.mp4, portrait.mp4, etc.) are
    ignored.
    """
    if not clips_dir.exists():
        return []
    final = []
    for folder in clips_dir.iterdir():
        if not folder.is_dir():
            continue
        named = folder / f"{folder.name}.mp4"
        if named.exists():
            final.append(named)
            continue
        candidates = sorted(p for p in folder.glob("*.mp4")
                            if p.stem.lower() not in INTERMEDIATE_NAMES)
        if candidates:
            final.append(candidates[-1])
    return sorted(final)


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE.sub("", text).strip()


def _probe_video(path: Path) -> tuple:
    """Return (width, height, size_mb) for a video file using ffprobe."""
    width = height = None
    size_mb = 0.0
    try:
        if path.exists():
            size_mb = round(path.stat().st_size / (1024 * 1024), 1)
    except Exception:
        pass
    try:
        ffmpeg = get_ffmpeg_path()
        probe = "ffprobe.exe" if sys.platform.startswith("win") else "ffprobe"
        probe_path = str(Path(ffmpeg).parent / probe)
        if not Path(probe_path).exists():
            probe_path = shutil.which("ffprobe") or probe
        result = subprocess.run(
            [probe_path, "-v", "quiet", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(path)],
            capture_output=True, text=True, timeout=15
        )
        out = result.stdout.strip()
        if out and "x" in out:
            w, h = out.split("x")[:2]
            width, height = int(w), int(h)
    except Exception:
        pass
    return width, height, size_mb


def _human_size(size_mb: float) -> str:
    if size_mb >= 1024:
        return f"{size_mb / 1024:.1f} GB"
    return f"{size_mb:.1f} MB"


def _format_duration(seconds: float) -> str:
    """Format durasi detik → teks ramah, mis. '5 menit 12 detik'."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} detik"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m} menit {s} detik" if s else f"{m} menit"
    h, m = divmod(m, 60)
    return f"{h} jam {m} menit" if m else f"{h} jam"


def _ratio_of(w: int, h: int) -> str:
    if not w or not h:
        return "-"
    try:
        from math import gcd
        g = gcd(w, h)
        return f"{w // g}:{h // g}"
    except Exception:
        return f"{w}x{h}"


def _slugify(text: str) -> str:
    """Convert a title to a url/command-friendly slug (e.g. 'Jokowi Minta Maaf' -> 'jokowi-minta-maaf')."""
    import re as _re
    slug = _re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return slug or "klip"


def _load_config() -> dict:
    app_dir = Path(__file__).parent.resolve()
    cfg_file = app_dir / "config.json"
    if cfg_file.exists():
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _get_owner_id(cfg: dict = None) -> int:
    """ID Telegram pemilik bot (0 = belum diset)."""
    if cfg is None:
        cfg = _load_config()
    try:
        return int(cfg.get("owner_id", 0) or 0)
    except Exception:
        return 0


def _get_allowed_ids(cfg: dict = None) -> list:
    """Daftar ID Telegram yang diizinkan mengakses bot."""
    if cfg is None:
        cfg = _load_config()
    ids = cfg.get("allowed_user_ids", []) or []
    return [int(x) for x in ids if str(x).lstrip("-").isdigit()]


def _is_authorized(chat_id) -> bool:
    """Cek apakah chat_id diizinkan.

    Mode bootstrap: jika owner belum diset dan daftar kosong,
    semua boleh akses (untuk setup pertama).
    """
    try:
        chat_id = int(chat_id)
    except Exception:
        return False
    cfg = _load_config()
    owner = _get_owner_id(cfg)
    allowed = _get_allowed_ids(cfg)
    if not owner and not allowed:
        return True
    return chat_id == owner or chat_id in allowed


async def _deny_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tolak akses pengguna yang tidak terdaftar."""
    if update.callback_query:
        try:
            await update.callback_query.answer("⛔ Akses ditolak", show_alert=True)
        except Exception:
            pass
        return
    if update.message:
        await update.message.reply_text(
            "⛔ *Akses ditolak.*\n\n"
            "Bot ini hanya untuk pengguna terdaftar.\n"
            "Hubungi admin untuk menambahkan ID kamu.",
            parse_mode="Markdown",
        )


def _auth_required(func):
    """Decorator: tolak akses user yang tidak terdaftar."""
    import functools

    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        chat_id = None
        if update.effective_user is not None:
            chat_id = update.effective_user.id
        if chat_id is None and update.effective_chat is not None:
            chat_id = update.effective_chat.id
        if not _is_authorized(chat_id):
            await _deny_access(update, context)
            return
        return await func(update, context, *args, **kwargs)

    return wrapper


def _is_watermark_active(cfg: dict) -> bool:
    wm = cfg.get("watermark", {})
    return bool(
        wm.get("enabled", False)
        and wm.get("image_path")
        and Path(wm["image_path"]).exists()
    )


def _is_credit_active(cfg: dict) -> bool:
    credit = cfg.get("credit_watermark", {})
    return bool(credit.get("enabled", False))


def _watermark_status_for(cfg: dict) -> dict:
    """Status watermark dari config."""
    wm = cfg.get("watermark", {})
    img = str(wm.get("image_path", "") or "")
    return {
        "enabled": bool(wm.get("enabled", False)),
        "has_image": bool(img) and Path(img).exists(),
        "image_path": img,
        "position_x": wm.get("position_x", 0.85),
        "position_y": wm.get("position_y", 0.05),
        "opacity": wm.get("opacity", 0.8),
        "scale": wm.get("scale", 0.15),
    }


def build_watermark_menu(cfg: dict):
    """Menu /watermark: status + tombol upload/hapus/toggle."""
    st = _watermark_status_for(cfg)

    lines = ["💧 *Kelola Watermark*\n"]
    if st["enabled"]:
        lines.append("Status: ✅ *Aktif*")
    else:
        lines.append("Status: ❌ *Nonaktif*")
    if st["has_image"]:
        fname = Path(st["image_path"]).name
        lines.append(f"Gambar: 🖼️ `{fname}`")
        lines.append(
            f"Posisi: `({st['position_x']}, {st['position_y']})`  "
            f"Opacity: `{st['opacity']}`  Scale: `{st['scale']}`"
        )
    else:
        lines.append("Gambar: ❌ *belum ada*")
    lines.append("\n📤 Upload gambar (PNG/JPG/JPEG) untuk dijadikan watermark.")
    lines.append("💡 Watermark hanya tampil jika Status ON dan gambar tersedia.")

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"Status: {'ON' if st['enabled'] else 'OFF'}", callback_data="wm_toggle",
                style=KeyboardButtonStyle.SUCCESS if st["enabled"] else KeyboardButtonStyle.DANGER,
            ),
        ],
        [
            InlineKeyboardButton("📤 Upload", callback_data="wm_up",
                                 style=KeyboardButtonStyle.PRIMARY),
        ],
        (
            [InlineKeyboardButton("🗑️ Hapus Gambar", callback_data="wm_del",
                                  style=KeyboardButtonStyle.DANGER)]
            if st["has_image"] else []
        ),
        [
            InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_main",
                                 style=KeyboardButtonStyle.SUCCESS),
        ],
    ])
    return "\n".join(lines), keyboard


async def watermark_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """💧 Menu kelola watermark (upload / hapus / toggle)."""
    app_dir = Path(__file__).parent.resolve()
    cfg_mgr = ConfigManager(app_dir / "config.json", app_dir / "output")
    cfg = cfg_mgr.config
    text, keyboard = build_watermark_menu(cfg)
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")


async def watermark_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback handler menu watermark (wm_toggle / wm_up / wm_del)."""
    query = update.callback_query
    await query.answer()
    data = query.data.replace("wm_", "")

    app_dir = Path(__file__).parent.resolve()
    cfg_mgr = ConfigManager(app_dir / "config.json", app_dir / "output")
    cfg = cfg_mgr.config

    if data == "toggle":
        cfg.setdefault("watermark", {})["enabled"] = not _watermark_status_for(cfg)["enabled"]
        cfg_mgr.save_config(cfg)
        text, keyboard = build_watermark_menu(cfg)
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
        return

    if data == "up":
        context.user_data["pending_watermark_upload"] = True
        await query.edit_message_text(
            "📤 *Upload gambar watermark*\n\n"
            "Kirim file gambar (PNG/JPG/JPEG) ke chat ini.\n"
            "Gambar akan disimpan sebagai watermark dan path-nya disimpan di config.",
            parse_mode="Markdown",
        )
        return

    if data == "del":
        wm = cfg.setdefault("watermark", {})
        old = wm.get("image_path", "")
        wm["image_path"] = ""
        wm["enabled"] = False
        cfg_mgr.save_config(cfg)
        if old:
            try:
                Path(old).unlink()
            except Exception:
                pass
        text, keyboard = build_watermark_menu(cfg)
        await query.edit_message_text(
            "🗑️ *Watermark dihapus.*",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
        return


def _md_to_html(text: str) -> str:
    """Convert the simple markdown used here (*bold*, `code`) to HTML entities."""
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*([^*\n]+)\*", r"<b>\1</b>", text)
    return text


def _quote_block(text: str) -> str:
    return "<blockquote>" + _md_to_html(text) + "</blockquote>"


def _render_clip_details(clip_data: dict, cp: Path) -> str:
    """Build a tidy, emoji-formatted detail block for one rendered clip."""
    clip_title = clip_data.get("title") or cp.stem
    duration = clip_data.get("duration_seconds") or 0
    ratio_cfg = clip_data.get("aspect_ratio", "-")
    channel = clip_data.get("channel_name") or "-"

    w, h, size_mb = _probe_video(cp)
    res = f"{w}x{h}" if w and h else "-"
    ratio_probe = _ratio_of(w, h)
    ratio = ratio_probe if ratio_probe != "-" else ratio_cfg

    cfg = _load_config()
    watermark_active = _is_watermark_active(cfg)
    credit_active = _is_credit_active(cfg)

    parts = [
        f"{_human_size(size_mb)}  |  {ratio}  |  {res}  |  {channel}  |  {duration:.0f}s",
    ]

    vs = clip_data.get("virality_score")
    if vs is not None:
        try:
            parts.append(f"{int(vs)}/10")
        except (ValueError, TypeError):
            pass

    flags = []
    if clip_data.get("has_hook"):
        flags.append("Hook")
    if clip_data.get("has_captions"):
        flags.append("Caption")
    if watermark_active and clip_data.get("has_watermark"):
        flags.append("Watermark")
    if credit_active and clip_data.get("has_credit"):
        flags.append("Credit")
    if flags:
        parts.append(" | ".join(flags))

    line = "─" * 32
    block = f"🎬 *{clip_title[:50]}*\n{line}\n" + " | ".join(parts)
    return _md_to_html(block)


def make_callbacks(chat_id: int, bot, loop: asyncio.AbstractEventLoop, status_holder: dict):
    DEBOUNCE_SEC = 0.4
    status_msg = {"ref": None, "creating": False, "pending": None, "flush_task": None}

    def _flush():
        status_msg["flush_task"] = None
        text = status_msg["pending"]
        status_msg["pending"] = None
        if not text:
            return
        async def _do():
            nonlocal text
            for attempt in range(3):  # retry transient network errors
                try:
                    if status_msg["ref"] is not None:
                        await status_msg["ref"].edit_text(text)
                    else:
                        if status_msg["creating"]:
                            return
                        status_msg["creating"] = True
                        try:
                            msg = await bot.send_message(chat_id=chat_id, text=text)
                            status_msg["ref"] = msg
                        finally:
                            status_msg["creating"] = False
                    return
                except Exception as e:
                    if attempt == 2:
                        logger.warning(f"status message update failed: {e}")
                    else:
                        logger.warning(f"status message update failed (retry {attempt+1}/3): {e}")
                        await asyncio.sleep(2 * (attempt + 1))
        loop.call_soon_threadsafe(lambda: asyncio.create_task(_do()))

    def _schedule_flush():
        # Debounce: coalesce rapid updates; only the latest line is kept
        if status_msg["flush_task"] is not None:
            return
        status_msg["flush_task"] = True
        async def _debounced():
            await asyncio.sleep(DEBOUNCE_SEC)
            _flush()
        loop.call_soon_threadsafe(lambda: asyncio.create_task(_debounced()))

    def _show(line: str):
        # Replace the status message with just this line (no accumulation)
        status_msg["pending"] = line[-3950:]
        _schedule_flush()

    def log_callback(msg: str):
        clean = strip_ansi(msg)
        logger.info(f"[{chat_id}] {clean}")
        _show(clean[:200])

    def progress_callback(status: str, progress: float):
        pct = int(progress * 100)
        logger.info(f"[{chat_id}] ⚙️ {status} {pct}%")
        now = time.time()
        last_pct = status_holder.get("last_progress_pct", -1)
        if pct - last_pct < 5 and now - status_holder.get("last_progress_send", 0) < DEBOUNCE_SEC:
            return
        status_holder["last_progress_pct"] = pct
        status_holder["last_progress_send"] = now

        bar_filled = int(progress * 10)
        bar = "█" * bar_filled + "░" * (10 - bar_filled)
        clean_status = strip_ansi(status).replace("*", "").replace("`", "")
        _show(f"⚙️ {clean_status} [{bar}] {pct}%")

    return log_callback, progress_callback


def get_core_instance(config_mgr: ConfigManager, log_cb=None, progress_cb=None) -> AutoClipperCore:
    cfg = config_mgr.config
    client = OpenAI(
        api_key=cfg.get("api_key", ""),
        base_url=cfg.get("base_url", "https://api.openai.com/v1")
    )
    core = AutoClipperCore(
        client=client,
        ffmpeg_path=get_ffmpeg_path(),
        ytdlp_path=get_ytdlp_path(),
        output_dir=str(config_mgr.output_dir),
        model=cfg.get("model", "gpt-4.1"),
        tts_model=cfg.get("tts_model", "tts-1"),
        temperature=cfg.get("temperature", 1.0),
        system_prompt=cfg.get("system_prompt"),
        watermark_settings=cfg.get("watermark"),
        credit_watermark_settings=cfg.get("credit_watermark"),
        hook_style_settings=cfg.get("hook_style"),
        face_tracking_mode=cfg.get("face_tracking_mode", "opencv"),
        portrait_mode=cfg.get("portrait_mode", "crop"),
        subtitle_style=cfg.get("subtitle_style", "pop"),
        aspect_ratio=cfg.get("aspect_ratio", "9:16"),
        mediapipe_settings=cfg.get("mediapipe_settings"),
        ai_providers=cfg.get("ai_providers"),
        subtitle_language=cfg.get("subtitle_language", "id"),
        subtitle_sync_offset=cfg.get("subtitle_sync_offset", -0.3),
        log_callback=log_cb,
        progress_callback=progress_cb
    )
    # GPU acceleration (config gpu_acceleration.enabled)
    if cfg.get("gpu_acceleration", {}).get("enabled", False):
        core.enable_gpu_acceleration(True)
    else:
        core.enable_gpu_acceleration(False)
    return core


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled exception:", exc_info=context.error)


MAIN_MENU_TEXT = (
    "📊 <b>Auto Clipper Bot</b>\n\n"
    "Bot pembuat klip short-form otomatis dari link video.\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "📥 <b>Cara Pakai:</b>\n"
    "Kirim link video (YouTube, TikTok, IG, Twitter/X, Reddit, FB, Vimeo, Dailymotion, Twitch, SoundCloud)\n"
    "Bot unduh subtitle & analisis pakai AI\n"
    "Pilih highlight yang mau di-clip\n"
    "Bot render otomatis jadi video short\n"
    "Video dikirim langsung via Local Bot API Server (limit 2GB)\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "⚙️ <b>Perintah:</b>\n"
    "/start - Info bot\n"
    "/help - Panduan lengkap\n"
    "/config - Jumlah klip, resolusi, dll\n"
    "/model - Daftar model AI\n"
    "/watermark - Kelola watermark\n"
    "/result - Lihat hasil klip terakhir\n"
    "/server - Status server\n"
    "/status - Toggle fitur\n"
    "/cancel_session - Batalkan proses aktif\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "💡 <b>Tips:</b> Hasil klip dikirim otomatis ke Telegram setelah selesai.\n"
)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(MAIN_MENU_TEXT, parse_mode="HTML")


def build_admin_menu(cfg: dict, caller_id: int):
    """Menu admin: daftar ID yang diizinkan + tombol tambah/hapus."""
    owner = _get_owner_id(cfg)
    allowed = _get_allowed_ids(cfg)

    lines = ["👑 *Menu Admin*\n"]
    if owner:
        lines.append(f"Owner: `{owner}`")
    else:
        lines.append("Owner: *belum diset*")
    if allowed:
        lines.append(f"\n✅ *Diizinkan ({len(allowed)}):*")
        for uid in allowed:
            tag = " 👑" if uid == owner else ""
            lines.append(f"• `{uid}`{tag}")
    else:
        lines.append("\n⚠️ Belum ada ID lain selain owner.")

    keyboard = []
    if not owner or caller_id == owner:
        if not owner:
            keyboard.append([
                InlineKeyboardButton("👑 Jadikan Saya Owner", callback_data="adm_owner",
                                     style=KeyboardButtonStyle.PRIMARY),
            ])
        keyboard.append([
            InlineKeyboardButton("➕ Tambah ID", callback_data="adm_add",
                                 style=KeyboardButtonStyle.SUCCESS),
        ])
        for uid in allowed:
            if uid != owner:
                keyboard.append([
                    InlineKeyboardButton(f"🗑️ Hapus {uid}", callback_data=f"adm_del_{uid}",
                                         style=KeyboardButtonStyle.DANGER),
                ])
    keyboard.append([
        InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_main",
                             style=KeyboardButtonStyle.SUCCESS),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """👑 Menu admin untuk mengelola ID yang diizinkan."""
    chat_id = update.effective_user.id if update.effective_user else update.effective_chat.id
    if not _is_authorized(chat_id):
        await _deny_access(update, context)
        return
    cfg = _load_config()
    owner = _get_owner_id(cfg)
    if owner and chat_id != owner:
        await update.message.reply_text(
            "⛔ Hanya *owner* yang bisa mengakses menu admin.", parse_mode="Markdown"
        )
        return
    text, keyboard = build_admin_menu(cfg, chat_id)
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")


async def admin_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback menu admin: adm_owner / adm_add / adm_del_<id>."""
    query = update.callback_query
    await query.answer()
    caller_id = query.from_user.id
    data = query.data.replace("adm_", "")

    if not _is_authorized(caller_id):
        await _deny_access(update, context)
        return

    app_dir = Path(__file__).parent.resolve()
    cfg_mgr = ConfigManager(app_dir / "config.json", app_dir / "output")
    cfg = cfg_mgr.config
    owner = _get_owner_id(cfg)

    if owner and caller_id != owner:
        await query.answer("⛔ Hanya owner yang bisa mengelola.", show_alert=True)
        return

    if data == "owner":
        cfg["owner_id"] = caller_id
        allowed = cfg.get("allowed_user_ids", []) or []
        if caller_id not in allowed:
            allowed.append(caller_id)
        cfg["allowed_user_ids"] = allowed
        cfg_mgr.save_config(cfg)
        text, keyboard = build_admin_menu(cfg, caller_id)
        await query.edit_message_text(
            f"👑 Kamu sekarang jadi *owner*!\n\n{text}", reply_markup=keyboard, parse_mode="Markdown"
        )
        return

    if data == "add":
        context.user_data["pending_admin_add_id"] = True
        await query.edit_message_text(
            "➕ *Tambah ID Pengguna*\n\n"
            "Kirimkan *user ID Telegram* (angka) yang ingin diizinkan.\n"
            "Cara dapat ID: gunakan bot @userinfobot lalu kirim contact.\n\n"
            "Kirim angka, misalnya `123456789`:",
            parse_mode="Markdown",
        )
        return

    if data.startswith("del_"):
        uid = data.split("_", 1)[1]
        try:
            uid = int(uid)
        except Exception:
            uid = 0
        allowed = cfg.get("allowed_user_ids", []) or []
        if uid in allowed:
            allowed = [x for x in allowed if x != uid]
            cfg["allowed_user_ids"] = allowed
            cfg_mgr.save_config(cfg)
        text, keyboard = build_admin_menu(cfg, caller_id)
        await query.edit_message_text(
            f"🗑️ ID `{uid}` dihapus dari daftar.\n\n{text}",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
        return

def _back_to_menu_keyboard():
    """Keyboard dengan satu tombol Back menuju menu utama."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Menu Utama", callback_data="menu_main")]
    ])


async def menu_main_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback tombol 'Menu Utama' — tampilkan menu utama."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(MAIN_MENU_TEXT, parse_mode="HTML")


async def result_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show a browsable list of recent sessions with their clips."""
    app_dir = Path(__file__).parent.resolve()
    sessions_dir = (app_dir / "output" / "sessions")
    if not sessions_dir.exists():
        await update.message.reply_text("❌ Belum ada sesi (folder output/sessions tidak ditemukan).")
        return

    keyboard, sessions = load_session_keyboard(sessions_dir)
    if not sessions:
        await update.message.reply_text("❌ Belum ada session untuk ditampilkan.")
        return

    context.user_data["result_sessions"] = sessions
    await update.message.reply_text(
        _session_list_text(sessions),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


def _session_sort_key(sfile: Path):
    """Sort key: created_at from session_data.json (fallback to file mtime)."""
    try:
        with open(sfile, "r", encoding="utf-8") as f:
            data = json.load(f)
        created = data.get("created_at")
        if created:
            return datetime.fromisoformat(created).timestamp()
    except Exception:
        pass
    return sfile.stat().st_mtime


def load_session_keyboard(sessions_dir: Path):
    """Build a keyboard of session buttons, ordered newest first."""
    sessions = [p / "session_data.json" for p in sessions_dir.iterdir() if p.is_dir()]
    sessions = [p for p in sessions if p.exists()]
    sessions.sort(key=_session_sort_key, reverse=True)

    keyboard = _build_session_keyboard(sessions)
    return keyboard, sessions


def _session_status_mark(status: str) -> str:
    return {
        "completed": "✅", "highlights_found": "🎯", "processing": "⚙️",
        "error": "❌", "downloading": "⏳",
    }.get(status, "•")


_STATUS_SHORT = {
    "completed": "OK", "highlights_found": "HL", "processing": "PROC",
    "downloading": "DL", "error": "ERR",
}


def _session_list_text(sessions: list) -> str:
    """Build a numbered blockquote list of sessions with short details."""
    rows = []
    for idx, sfile in enumerate(sessions[:15], 1):
        try:
            with open(sfile, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        vi = data.get("video_info") or {}
        title = (vi.get("title") or sfile.parent.name)[:50]
        channel = (vi.get("channel") or "-")[:25]
        n_hl = len(data.get("highlights") or [])
        clips_dir = Path(data.get("session_dir") or sfile.parent) / "clips"
        n_clips = len(find_final_clips(clips_dir)) if clips_dir.exists() else 0
        rows.append(f"{idx}. {title}\n   📺 {channel} | 🎯 {n_hl} highlight | 🎬 {n_clips} klip")

    total = len(rows)
    body = "\n\n".join(rows) if rows else "(kosong)"
    safe = html.escape(body)
    return f"📁 <b>Session</b>  ({total})\n\n<blockquote>{safe}</blockquote>"


def _build_session_keyboard(sessions: list):
    """Build the inline keyboard: narrow number-only buttons (one per session)
    laid out in compact rows, plus a small Refresh / Menu row.
    """
    keyboard = []
    row = []
    for idx, sfile in enumerate(sessions[:15]):
        try:
            with open(sfile, "r", encoding="utf-8") as f:
                json.load(f)
        except Exception:
            continue
        # Tampilkan angka mulai dari 1, tapi callback tetap 0-based
        # (res_{idx}) agar mapping ke sessions[int(data)] tetap benar.
        row.append(InlineKeyboardButton(f"{idx + 1}", callback_data=f"res_{idx}"))
        if len(row) >= 8:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([
        InlineKeyboardButton("🔄", callback_data="res_refresh"),
        InlineKeyboardButton("🏠", callback_data="menu_main"),
    ])
    return keyboard


async def result_browse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.replace("res_", "")

    sessions = context.user_data.get("result_sessions", [])

    if data == "del_confirm":
        idx = context.user_data.get("result_cur_session")
        if idx is None or not sessions or idx >= len(sessions):
            await query.edit_message_text("❌ Session tidak ditemukan.")
            return
        sfile = sessions[idx]
        keyboard = [
            [InlineKeyboardButton("✅ Ya, Hapus", callback_data="res_del_yes")],
            [InlineKeyboardButton("❌ Batal", callback_data="res_back")],
        ]
        await query.edit_message_text(
            f"⚠️ *Hapus session ini beserta semua clipnya?*\n\n`{sfile.parent.name}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data == "del_yes":
        idx = context.user_data.get("result_cur_session")
        if idx is None or not sessions or idx >= len(sessions):
            await query.edit_message_text("❌ Session tidak ditemukan.")
            return
        sfile = sessions[idx]
        try:
            shutil.rmtree(sfile.parent)
            sessions.pop(idx)
            context.user_data["result_sessions"] = sessions
            context.user_data.pop("result_cur_session", None)
        except Exception as e:
            await query.edit_message_text(
                f"❌ Gagal hapus session:\n`{str(e)[:200]}`", parse_mode="Markdown"
            )
            return
        # Bersihkan referensi session aktif milik chat ini (jika ada)
        with session_lock:
            us = USER_SESSIONS.get(update.effective_chat.id)
            if us and str(us.get("session_data", {}).get("session_dir", "")).lower() == str(sfile.parent).lower():
                USER_SESSIONS.pop(update.effective_chat.id, None)
        if not sessions:
            await query.edit_message_text("🗑️ Session dihapus.\nTidak ada session tersisa.")
            return
        await query.edit_message_text(
            "🗑️ Session dihapus.\n\n" + _session_list_text(sessions),
            reply_markup=InlineKeyboardMarkup(_build_session_keyboard(sessions)),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    if data == "refresh":
        app_dir = Path(__file__).parent.resolve()
        keyboard, sessions = load_session_keyboard(app_dir / "output" / "sessions")
        context.user_data["result_sessions"] = sessions
        if not sessions:
            await query.edit_message_text("❌ Belum ada session untuk ditampilkan.")
            return
        await query.edit_message_text(
            _session_list_text(sessions),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    if data == "regen_hf":
        idx = context.user_data.get("result_cur_session")
        if idx is None or not sessions or idx >= len(sessions):
            await query.edit_message_text("❌ Session tidak ditemukan.")
            return
        sfile = sessions[idx]
        try:
            with open(sfile, "r", encoding="utf-8") as f:
                sdata = json.load(f)
        except Exception as e:
            await query.edit_message_text(
                f"❌ Gagal membaca session:\n`{str(e)[:200]}`", parse_mode="Markdown"
            )
            return
        url = sdata.get("url") or sdata.get("video_info", {}).get("url") or ""
        if not url:
            await query.edit_message_text("❌ URL video tidak tersimpan di session ini.")
            return
        context.user_data["retry_url"] = url
        chat_id = update.effective_chat.id
        await query.edit_message_text(
            "⏳ *[Phase 1] Mencari highlight ulang...*",
            parse_mode="Markdown",
        )
        await run_phase1(chat_id, url, context, query.message)
        return

    if data == "regen_clip":
        """Regenerate Clip: tampilkan ulang highlight dari session tersimpan
        untuk di-select & diproses, tanpa re-run AI."""
        idx = context.user_data.get("result_cur_session")
        if idx is None or not sessions or idx >= len(sessions):
            await query.edit_message_text("❌ Session tidak ditemukan.")
            return
        sfile = sessions[idx]
        try:
            with open(sfile, "r", encoding="utf-8") as f:
                sdata = json.load(f)
        except Exception as e:
            await query.edit_message_text(
                f"❌ Gagal membaca session:\n`{str(e)[:200]}`", parse_mode="Markdown"
            )
            return

        url = sdata.get("url") or sdata.get("video_info", {}).get("url") or ""
        highlights = sdata.get("highlights", []) or []
        if not url:
            await query.edit_message_text("❌ URL video tidak tersimpan di session ini.")
            return
        if not highlights:
            await query.edit_message_text(
                "❌ Session ini tidak punya highlight tersimpan.\n"
                "Gunakan *🔄 Regenerate HF* untuk mencari highlight ulang."
            )
            return

        chat_id = update.effective_chat.id
        with session_lock:
            busy = ACTIVE_CHATS.get(chat_id)
        if busy:
            await query.answer(f"⏳ Masih ada proses berjalan ({busy}).", show_alert=True)
            return

        # Siapkan session baru (ulang pakai highlight tersimpan, tanpa AI)
        app_dir = Path(__file__).parent.resolve()
        cfg_mgr = ConfigManager(app_dir / "config.json", app_dir / "output")
        status_holder = {"last_log_send": 0, "last_progress_pct": -1, "last_progress_send": 0}
        log_cb, prog_cb = make_callbacks(chat_id, context.bot, asyncio.get_running_loop(), status_holder)
        core = get_core_instance(cfg_mgr, log_cb=log_cb, progress_cb=prog_cb)

        highlights = sorted(highlights, key=lambda h: h.get("virality_score", 0) or 0, reverse=True)
        sdata["highlights"] = highlights

        session = {
            "core": core,
            "url": url,
            "session_data": sdata,
            "highlights": highlights,
            "loop": asyncio.get_running_loop(),
            "selected": set(),
            "cancel_event": threading.Event(),
        }
        core.is_cancelled = session["cancel_event"].is_set
        with session_lock:
            USER_SESSIONS[chat_id] = session

        await _render_highlight_menu(chat_id, query.message, session)
        return
    if data == "back":
        if not sessions:
            await query.edit_message_text("❌ Belum ada session untuk ditampilkan.")
            return
        await query.edit_message_text(
            _session_list_text(sessions),
            reply_markup=InlineKeyboardMarkup(_build_session_keyboard(sessions)),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return
    if not sessions or not data.isdigit() or int(data) >= len(sessions):
        await query.edit_message_text("❌ Session tidak ditemukan.")
        return

    session_idx = int(data)
    context.user_data["result_cur_session"] = session_idx
    sfile = sessions[session_idx]
    with open(sfile, "r", encoding="utf-8") as f:
        data = json.load(f)

    vi = data.get("video_info") or {}
    title = vi.get("title") or "Tanpa judul"

    # Clips actually rendered
    clips_dir = Path(data["session_dir"]) / "clips"
    all_rendered = find_final_clips(clips_dir)
    all_rendered = all_rendered[:10]

    # Session highlights (sumber description / virality_score / waktu)
    highlights_src = data.get("highlights", []) or []

    # Bangun blok detail per klip
    clip_blocks = []
    for i, cp in enumerate(all_rendered, 1):
        clip_data = {}
        data_file = cp.parent / "data.json"
        if data_file.exists():
            try:
                with open(data_file, "r", encoding="utf-8") as f:
                    clip_data = json.load(f)
            except Exception:
                pass
        # Gabungkan dengan highlight session agar detail lengkap
        # (process_clip tidak menyimpan description & virality_score).
        if highlights_src:
            try:
                h = highlights_src[min(i - 1, len(highlights_src) - 1)]
                for k in ("title", "description", "start_time", "end_time",
                          "virality_score", "hook_text"):
                    if not clip_data.get(k) and h.get(k):
                        clip_data[k] = h[k]
            except Exception:
                pass
        clip_blocks.append(f"{i}. {_render_clip_details(clip_data, cp)}")

    # Gabungkan header + blok, batasi panjang pesan Telegram (maks 4096 char)
    header_lines = [
        _md_to_html(f"🎬 *{title}*"),
        _md_to_html(f"📺 Channel: `{vi.get('channel', '-')}`"),
        _md_to_html(f"📊 Status: `{data.get('status')}`"),
        _md_to_html(f"🔗 URL: {data.get('url', '-')}"),
    ]
    MAX = 3900
    text = "\n".join(header_lines)
    text += "\n\n" + _md_to_html(f"🎞️ *Klip dirender: {len(all_rendered)}*")
    shown = 0
    for blk in clip_blocks:
        if len(text) + len(blk) + 2 > MAX:
            break
        text += "\n\n" + blk
        shown += 1
    if shown < len(clip_blocks):
        text += f"\n\n…dan {len(clip_blocks) - shown} klip lainnya (total {len(all_rendered)})."

    # Batas keras Telegram (4096) sebagai pengaman terakhir
    if len(text) > 4090:
        text = text[:4090] + "…"

    rendered = all_rendered[:shown] if shown else all_rendered[:1]
    context.user_data["result_rendered"] = rendered

    keyboard = []
    for i, cp in enumerate(rendered, 1):
        keyboard.append([
            InlineKeyboardButton(f"{i}. Download", callback_data=f"dl_{i-1}"),
            InlineKeyboardButton("Metadata", callback_data=f"meta_{i-1}"),
            InlineKeyboardButton("Social Kit", callback_data=f"soc_{i-1}"),
        ])
    keyboard.append([
        InlineKeyboardButton("⬅️ Back", callback_data="res_back"),
        InlineKeyboardButton("🔄 Regenerate HF", callback_data="res_regen_hf",
                             style=KeyboardButtonStyle.PRIMARY),
    ])
    keyboard.append([
        InlineKeyboardButton("🎬 Regenerate Clip", callback_data="res_regen_clip",
                             style=KeyboardButtonStyle.PRIMARY),
        InlineKeyboardButton("🗑️ Hapus", callback_data="res_del_confirm",
                             style=KeyboardButtonStyle.DANGER),
    ])
    await query.edit_message_text(
        text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True,
    )


def find_clip_by_keyword(keyword: str):
    """Find the most recent clip whose title matches the given keyword.

    Returns the Path of the clip file, or None if not found.
    """
    keyword = keyword.lower()
    app_dir = Path(__file__).parent.resolve()
    sessions_dir = app_dir / "output" / "sessions"
    if not sessions_dir.exists():
        return None

    matches = []
    for sfolder in sessions_dir.iterdir():
        if not sfolder.is_dir():
            continue
        for cp in find_final_clips(sfolder / "clips"):
            clip_title = cp.stem
            data_file = cp.parent / "data.json"
            if data_file.exists():
                try:
                    with open(data_file, "r", encoding="utf-8") as f:
                        clip_data = json.load(f)
                    clip_title = clip_data.get("title") or clip_title
                except Exception:
                    pass
            if keyword in clip_title.lower() or keyword in cp.stem.lower():
                matches.append(cp)

    if not matches:
        return None
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0]


TELEGRAM_MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB (Local Bot API Server)




async def _send_clip_video(chat_id: int, context: ContextTypes.DEFAULT_TYPE, clip_path: Path, status_msg=None):
    """
    Send a clip video directly to the chat.
    Local Bot API Server - limit 2GB, tidak perlu layanan ketiga.
    """
    if status_msg is None:
        status_msg = await context.bot.send_message(chat_id=chat_id, text="📤 Mengirim video...")
    try:
        file_size = Path(clip_path).stat().st_size
        size_mb = round(file_size / (1024 * 1024), 1)
        await status_msg.edit_text(f"📤 Mengirim {_human_size(size_mb)}...")
        with open(clip_path, "rb") as f:
            await context.bot.send_video(
                chat_id=chat_id,
                video=f,
                filename=Path(clip_path).name,
                caption=f"🎬 {Path(clip_path).stem[:50]}",
                supports_streaming=True,
                read_timeout=120,
                write_timeout=120,
            )
        await status_msg.edit_text(f"✅ Video terkirim: `{Path(clip_path).stem[:50]}`", parse_mode="Markdown")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception("Failed to send clip")
        try:
            await status_msg.edit_text(f"❌ Gagal kirim video:\n`{str(e)[:200]}`", parse_mode="Markdown")
        except Exception:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Gagal kirim video:\n`{str(e)[:200]}`",
                parse_mode="Markdown",
            )


async def clip_metadata(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show metadata for a rendered clip."""
    query = update.callback_query
    await query.answer()
    data = query.data.replace("meta_", "")
    idx = int(data)
    rendered = context.user_data.get("result_rendered", [])
    if idx >= len(rendered):
        await query.answer("Klip tidak ditemukan.", show_alert=True)
        return
    cp = rendered[idx]
    data_file = cp.parent / "data.json"
    clip_data = {}
    if data_file.exists():
        try:
            with open(data_file, "r", encoding="utf-8") as f:
                clip_data = json.load(f)
        except Exception:
            pass
    title = clip_data.get("title") or cp.stem
    duration = clip_data.get("duration_seconds", 0)
    ratio = clip_data.get("aspect_ratio", "-")
    channel = clip_data.get("channel_name", "-")
    virality = clip_data.get("virality_score", 0)
    highlights = clip_data.get("source_highlights", [])
    hl_summary = str(len(highlights)) + " highlight" if highlights else "N/A"

    text = "<b>📄 Metadata Clip</b>\n\n"
    text += "<b>Judul:</b> " + title + "\n"
    text += "<b>Channel:</b> " + channel + "\n"
    text += "<b>Durasi:</b> " + str(duration) + "s\n"
    text += "<b>Aspect Ratio:</b> " + ratio + "\n"
    text += "<b>Virality Score:</b> " + str(virality) + "\n"
    text += "<b>Highlights:</b> " + hl_summary + "\n"
    text += "<b>File:</b> <code>" + cp.name + "</code>\n"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅ Kembali", callback_data="res_back")]
    ])
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")


async def clip_social_kit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate social kit info for a clip."""
    query = update.callback_query
    await query.answer()
    data = query.data.replace("soc_", "")
    idx = int(data)
    rendered = context.user_data.get("result_rendered", [])
    if idx >= len(rendered):
        await query.answer("Klip tidak ditemukan.", show_alert=True)
        return
    cp = rendered[idx]
    data_file = cp.parent / "data.json"
    clip_data = {}
    if data_file.exists():
        try:
            with open(data_file, "r", encoding="utf-8") as f:
                clip_data = json.load(f)
        except Exception:
            pass
    title = clip_data.get("title") or cp.stem
    channel = clip_data.get("channel_name", "Unknown")
    hashtags = clip_data.get("hashtags", [])
    captions = clip_data.get("caption_suggestions", [])

    text = "<b>📱 Social Kit</b>\n\n"
    text += "<b>Judul:</b> " + title + "\n"
    text += "<b>Channel:</b> " + channel + "\n\n"
    if hashtags:
        text += "<b>Hashtag:</b> " + " ".join(hashtags) + "\n\n"
    else:
        text += "<b>Hashtag:</b> #shorts #viral\n\n"
    text += "<b>Saran Caption:</b>\n"
    if captions:
        for i, cap in enumerate(captions, 1):
            text += str(i) + ". " + cap + "\n"
    else:
        text += "1. Watch till the end! 🥍👍\n"
        text += "2. What did you think? Comment below! 👀\n"
        text += "3. Don't forget to like & subscribe! 🤝\n"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅ Kembali", callback_data="res_back")]
    ])
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")


async def download_clip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send the selected rendered clip to the chat (callback from download buttons)."""
    query = update.callback_query
    await query.answer()
    data = query.data.replace("dl_", "")

    rendered = context.user_data.get("result_rendered", [])
    if not rendered or not data.isdigit() or int(data) >= len(rendered):
        await query.answer("Klip tidak ditemukan. Buka lagi /result.", show_alert=True)
        return

    clip_path = rendered[int(data)]
    if not Path(clip_path).exists():
        await query.answer("File klip sudah tidak ada di server.", show_alert=True)
        return

    chat_id = update.effective_chat.id
    status_msg = await query.edit_message_text("📤 *Mengirim video...*", parse_mode="Markdown")
    await _send_clip_video(chat_id, context, Path(clip_path), status_msg)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "💡 *Cara Pakai:*\n"
        "1. Kirim URL YouTube\n"
        "2. Bot mengunduh *subtitle saja* dan menganalisis highlight dengan AI (Sangat cepat!)\n"
        "3. Pilih highlight yang ingin dipotong\n"
        "4. Bot mengunduh potongan video terpilih dan merendernya lengkap dengan hook & caption!"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=_back_to_menu_keyboard())


async def config_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menu konfigurasi — fungsi ganti /status"""
    return await status_command(update, context)

def _apply_apikey(raw_key: str):
    """Simpan API key baru ke semua provider AI."""
    key = raw_key.strip()
    if not key:
        return None
    app_dir = Path(__file__).parent.resolve()
    cfg_mgr = ConfigManager(app_dir / "config.json", app_dir / "output")
    cfg = cfg_mgr.config
    prov = cfg.setdefault("ai_providers", {})
    for section in ("highlight_finder", "caption_maker", "hook_maker", "youtube_title_maker"):
        prov.setdefault(section, {})["api_key"] = key
    cfg["api_key"] = key
    cfg_mgr.save_config(cfg)
    return key

def _apply_server(raw_url: str):
    """Simpan base URL server AI baru. Caption/TTS/title ikut mengikuti root URL."""
    raw = raw_url.strip().rstrip("/")
    if not re.match(r"^https?://\S+$", raw):
        return None
    app_dir = Path(__file__).parent.resolve()
    cfg_mgr = ConfigManager(app_dir / "config.json", app_dir / "output")
    cfg = cfg_mgr.config
    prov = cfg.setdefault("ai_providers", {})
    prov.setdefault("highlight_finder", {})["base_url"] = raw
    prov.setdefault("youtube_title_maker", {})["base_url"] = raw
    prov.setdefault("caption_maker", {})["base_url"] = raw + "/audio/transcriptions"
    prov.setdefault("hook_maker", {})["base_url"] = raw + "/audio/speech?response_format=json"
    cfg["base_url"] = raw
    cfg_mgr.save_config(cfg)
    return raw

async def server_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menu server AI — set base URL dan/atau API key.
    Usage:
        /server             -> tampilkan status + menu tombol
        /server <url>       -> langsung set base URL
        /server <url> <key> -> set base URL + API key
    """
    app_dir = Path(__file__).parent.resolve()
    cfg_mgr = ConfigManager(app_dir / "config.json", app_dir / "output")
    cfg = cfg_mgr.config
    cur_url = ((cfg.get("ai_providers") or {}).get("highlight_finder") or {}).get("base_url") or cfg.get("base_url", "-")
    cur_key = cfg.get("api_key", "") or ((cfg.get("ai_providers") or {}).get("highlight_finder") or {}).get("api_key", "")
    masked_key = cur_key[:8] + "***" if len(cur_key) > 8 else ("***" if cur_key else "(kosong)")

    # /server <url> <key>
    if len(context.args) >= 2:
        new_url = context.args[0]
        new_key = " ".join(context.args[1:])
        url_ok = _apply_server(new_url)
        key_ok = _apply_apikey(new_key)
        parts = []
        if url_ok:
            parts.append(f"✅ Base URL: `{url_ok}`")
        else:
            parts.append(f"❌ URL tidak valid: `{new_url}`")
        if key_ok:
            parts.append(f"✅ API Key: `{key_ok[:8]}***`")
        else:
            parts.append(f"❌ API Key kosong")
        await update.message.reply_text(
            "\n".join(parts) + f"\n🤖 Model: `{_hf_model(cfg)}`",
            parse_mode="Markdown",
            reply_markup=_back_to_menu_keyboard(),
        )
        return

    # /server <url>
    if context.args:
        new_srv = _apply_server(" ".join(context.args))
        if new_srv is None:
            await update.message.reply_text(
                "❌ URL tidak valid. Contoh: `/server http://192.168.10.74:20128/v1`",
                parse_mode="Markdown",
            )
            return
        await update.message.reply_text(
            f"✅ Base URL diubah ke:\n`{new_srv}`\n🤖 Model: `{_hf_model(cfg)}`",
            parse_mode="Markdown",
            reply_markup=_back_to_menu_keyboard(),
        )
        return

    # /server — tampilkan status + tombol
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🌐 Ganti Base URL", callback_data="sv_url"),
            InlineKeyboardButton("🔑 Ganti API Key", callback_data="sv_key"),
        ],
        [
            InlineKeyboardButton("⬅️ Menu Utama", callback_data="menu_main",
                                 style=KeyboardButtonStyle.SUCCESS),
        ],
    ])
    await update.message.reply_text(
        f"🌐 *Server AI:*\n\n"
        f"Base URL: `{cur_url}`\n"
        f"API Key: `{masked_key}`\n"
        f"🤖 Model: `{_hf_model(cfg)}`\n\n"
        f"Ketik `/server <url>` atau `/server <url> <key>`",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    app_dir = Path(__file__).parent.resolve()
    cfg_mgr = ConfigManager(app_dir / "config.json", app_dir / "output")
    cfg = cfg_mgr.config
    text, keyboard = build_status(cfg)
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menampilkan daftar model highlight-finder yang tersedia.

    Usage:
        /model              → tampilkan daftar model
        /model <nama>       → set model aktif
    """
    app_dir = Path(__file__).parent.resolve()
    cfg_mgr = ConfigManager(app_dir / "config.json", app_dir / "output")
    cfg = cfg_mgr.config

    # Mode set: /model <nama>
    if context.args:
        requested = " ".join(context.args).strip()
        models, _live = _hf_available_models(cfg)
        match = None
        for m in models:
            if m.lower() == requested.lower():
                match = m
                break
        if match is None:
            fuzzy = [m for m in models if requested.lower() in m.lower()]
            match = fuzzy[0] if fuzzy else None

        if match:
            _set_hf_model(cfg, match)
            cfg_mgr.save_config(cfg)
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("⬅️ Kembali", callback_data="model_list",
                                         style=KeyboardButtonStyle.PRIMARY),
                    InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_main",
                                         style=KeyboardButtonStyle.SUCCESS),
                ]
            ])
            await update.message.reply_text(
                f"✅ Model highlight-finder diubah ke:\n`{match}`",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            return
        else:
            await update.message.reply_text(
                f"❌ Model `{requested}` tidak ditemukan.\n"
                "Ketik `/model` untuk melihat daftar model yang tersedia.",
                parse_mode="Markdown",
            )
            return

    # Mode daftar: /model — fetch realtime dari server
    current = _hf_model(cfg)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    models, live = await asyncio.get_running_loop().run_in_executor(None, _hf_available_models, cfg)

    src = "🟢 live dari server" if live else "🟠 server tidak merespon — daftar fallback"
    lines = [f"*🤖 Model Highlight-Finder ({len(models)}):*", f"_{src}_", ""]
    lines.append("💡 Klik nama model untuk langsung mengganti.")

    keyboard = _build_model_keyboard(current, models, user_key=str(update.effective_user.id) if update.effective_user else "default")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)


async def model_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback tombol 'Kembali' — tampilkan ulang daftar model."""
    query = update.callback_query
    await query.answer()

    app_dir = Path(__file__).parent.resolve()
    cfg_mgr = ConfigManager(app_dir / "config.json", app_dir / "output")
    cfg = cfg_mgr.config
    current = _hf_model(cfg)
    models, live = await asyncio.get_running_loop().run_in_executor(None, _hf_available_models, cfg)

    src = "🟢 live dari server" if live else "🟠 server tidak merespon — daftar fallback"
    lines = [f"*🤖 Model Highlight-Finder ({len(models)}):*", f"_{src}_", ""]
    lines.append("💡 Klik nama model untuk langsung mengganti.")

    keyboard = _build_model_keyboard(current, models, user_key=str(update.effective_user.id) if update.effective_user else "default")
    await query.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)


def _status_val(cfg: dict, key: str, default):
    return cfg.get(key, default)


def _hf_available_models(cfg: dict):
    """Fetch daftar model live dari endpoint yang dikonfigurasi.

    Returns (models, live). Fallback hanya saat endpoint unreachable.
    """
    hf = (cfg.get("ai_providers") or {}).get("highlight_finder", {})
    base_url = str(hf.get("base_url", "") or "").rstrip("/")
    api_key = hf.get("api_key", "")
    try:
        req = urllib.request.Request(base_url + "/models")
        if api_key:
            req.add_header("Authorization", "Bearer " + str(api_key))
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        ids = [m.get("id") for m in data.get("data", []) if m.get("id")]
        if ids:
            return ids, True
    except Exception:
        pass
    return ["AUTO", "OPENCODE", "GEMINI"], False


def _hf_model(cfg: dict) -> str:
    return ((cfg.get("ai_providers") or {}).get("highlight_finder") or {}).get("model", "AUTO")


def _set_hf_model(cfg: dict, model: str):
    cfg.setdefault("ai_providers", {}).setdefault("highlight_finder", {})["model"] = model

_model_map: Dict[str, list] = {}  # ponytail: user_id -> daftar model terakhir (callback_data Telegram limit 64 byte)

def _build_model_keyboard(current: str, models: list, user_key: str = "default") -> InlineKeyboardMarkup:
    """Keyboard daftar model — klik untuk langsung mengganti.

    callback_data pakai index karena nama model bisa >64 byte (Button_data_invalid).
    Mapping index -> nama disimpan di _model_map[user_key].
    """
    _model_map[user_key] = list(models)
    rows = []
    row = []
    for i, m in enumerate(models):
        label = ("✅ " if str(m) == str(current) else "") + str(m)
        row.append(InlineKeyboardButton(label[:60], callback_data=f"mdl_{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_main",
                             style=KeyboardButtonStyle.SUCCESS),
    ])
    return InlineKeyboardMarkup(rows)

async def model_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback klik tombol model (mdl_<index>) — set model highlight-finder."""
    query = update.callback_query
    await query.answer()
    raw = (query.data or "")[len("mdl_"):]
    user_key = str(update.effective_user.id) if update.effective_user else "default"
    app_dir = Path(__file__).parent.resolve()
    cfg_mgr = ConfigManager(app_dir / "config.json", app_dir / "output")
    cfg = cfg_mgr.config

    # Resolusi: index dari _model_map, fallback ke pencocokan nama
    mapped = _model_map.get(user_key) or []
    if raw.isdigit() and mapped:
        idx = int(raw)
        match = mapped[idx] if 0 <= idx < len(mapped) else None
    else:
        models, _live = await asyncio.get_running_loop().run_in_executor(None, _hf_available_models, cfg)
        match = next((m for m in models if str(m).lower() == raw.lower()), None)
    if match is None:
        await query.edit_message_text(
            f"❌ Model `{raw}` tidak ditemukan.\nKetik `/model` untuk melihat daftar.",
            parse_mode="Markdown",
        )
        return
    _set_hf_model(cfg, match)
    cfg_mgr.save_config(cfg)
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬅️ Daftar Model", callback_data="model_list",
                                 style=KeyboardButtonStyle.PRIMARY),
            InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_main",
                                 style=KeyboardButtonStyle.SUCCESS),
        ]
    ])
    await query.edit_message_text(
        f"✅ Model highlight-finder diubah ke:\n`{match}`",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


FW_MODEL_SIZES = ["tiny", "base", "small", "medium", "large"]
FW_MODEL_LABELS = {
    "tiny": "Paling cepat (akurasi rendah)",
    "base": "Cepat (akurasi cukup)",
    "small": "Seimbang (disarankan)",
    "medium": "Akurat (lambat di CPU)",
    "large": "Paling akurat (sangat lambat)",
}


def _fw_model_size(cfg: dict) -> str:
    return (
        (cfg.get("ai_providers") or {}).get("caption_maker") or {}
    ).get("faster_whisper", {}).get("model_size", "small")


def _set_fw_model_size(cfg: dict, size: str):
    cfg.setdefault("ai_providers", {}).setdefault("caption_maker", {}).setdefault(
        "faster_whisper", {}
    )["model_size"] = size


def _fw_model_installed(size: str) -> bool:
    """Cek apakah model Faster-Whisper ukuran tertentu sudah terunduh lokal."""
    try:
        from utils.dependency_manager import check_dependency
        app_dir = Path(__file__).parent.resolve()
        return check_dependency(f"faster_whisper_model_{size}", app_dir)
    except Exception:
        return False


def build_fw_menu(cfg: dict):
    """Menu /fw: daftar ukuran model + status terunduh + tombol download."""
    current = _fw_model_size(cfg)

    lines = ["🎙️ *Faster-Whisper (Caption Lokal)*\n"]
    lines.append(f"Aktif: `{current}`\n")
    for size in FW_MODEL_SIZES:
        marker = "✅" if size == current else "·"
        installed = _fw_model_installed(size)
        status = "🟢 terunduh" if installed else "🔴 belum ada"
        lines.append(f"{marker} `{size}` — {status}")
        lines.append(f"    ↳ {FW_MODEL_LABELS[size]}")

    lines.append("\n💡 Pilih ukuran untuk dipakai, atau download yang belum ada.")

    keyboard = []
    for size in FW_MODEL_SIZES:
        row = []
        row.append(InlineKeyboardButton(
            f"Pilih {size}", callback_data=f"fw_sel_{size}",
            style=KeyboardButtonStyle.PRIMARY if size == current else None,
        ))
        if not _fw_model_installed(size):
            row.append(InlineKeyboardButton(
                f"⬇️ Download {size}", callback_data=f"fw_dl_{size}",
                style=KeyboardButtonStyle.SUCCESS,
            ))
        keyboard.append(row)
    keyboard.append([
        InlineKeyboardButton("⬅️ Kembali", callback_data="st_refresh",
                             style=KeyboardButtonStyle.PRIMARY),
        InlineKeyboardButton("🏠 Menu Utama", callback_data="menu_main",
                             style=KeyboardButtonStyle.SUCCESS),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


async def fw_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🎙️ Menu Faster-Whisper: pilih ukuran & download model."""
    app_dir = Path(__file__).parent.resolve()
    cfg_mgr = ConfigManager(app_dir / "config.json", app_dir / "output")
    cfg = cfg_mgr.config
    text, keyboard = build_fw_menu(cfg)
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")


async def fw_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback menu faster-whisper: fw_sel_<size> / fw_dl_<size>."""
    query = update.callback_query
    await query.answer()
    data = query.data.replace("fw_", "")

    app_dir = Path(__file__).parent.resolve()
    cfg_mgr = ConfigManager(app_dir / "config.json", app_dir / "output")
    cfg = cfg_mgr.config

    if data.startswith("sel_"):
        size = data.split("_", 1)[1]
        if size in FW_MODEL_SIZES:
            if not _fw_model_installed(size):
                await query.edit_message_text(
                    f"❌ Model `{size}` belum terunduh.\n"
                    f"Klik *⬇️ Download {size}* untuk mengunduhnya dulu, "
                    "atau pilih ukuran lain.",
                    parse_mode="Markdown",
                )
                return
            _set_fw_model_size(cfg, size)
            cfg_mgr.save_config(cfg)
            text, keyboard = build_fw_menu(cfg)
            await query.edit_message_text(
                f"✅ Model Faster-Whisper diubah ke `{size}`.\n\n{text}",
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
        return

    if data.startswith("dl_"):
        size = data.split("_", 1)[1]
        if size in FW_MODEL_SIZES:
            await query.edit_message_text(
                f"⏳ Mengunduh model `{size}`...\n"
                "Bisa memakan waktu beberapa menit, tunggu ya.",
                parse_mode="Markdown",
            )
            try:
                from utils.dependency_manager import setup_faster_whisper_model
                success = await asyncio.get_running_loop().run_in_executor(
                    None, setup_faster_whisper_model, app_dir, size
                )
            except Exception as e:
                logger.exception("Failed to download faster-whisper model")
                success = False
            cfg = cfg_mgr.config
            if success:
                _set_fw_model_size(cfg, size)
                cfg_mgr.save_config(cfg)
                text, keyboard = build_fw_menu(cfg)
                await query.edit_message_text(
                    f"✅ Model `{size}` berhasil diunduh & diaktifkan!\n\n{text}",
                    reply_markup=keyboard,
                    parse_mode="Markdown",
                )
            else:
                text, keyboard = build_fw_menu(cfg)
                await query.edit_message_text(
                    f"❌ Gagal mengunduh model `{size}`.\n"
                    "Cek koneksi internet atau coba lagi.\n\n{text}",
                    reply_markup=keyboard,
                    parse_mode="Markdown",
                )
        return


def _binaries_status_lines() -> list:
    """Status binari eksternal: yt-dlp, deno, ffmpeg (bundled atau system PATH)."""
    import shutil
    import subprocess

    app_dir = Path(__file__).parent.resolve()
    lines = []

    # yt-dlp — utamakan python module, fallback ke binary di PATH
    try:
        import yt_dlp
        ver = getattr(getattr(yt_dlp, "version", None), "__version__", "?")
        lines.append(f"✅ yt-dlp `v{ver}` (module)")
    except Exception:
        yp = shutil.which("yt-dlp")
        if yp:
            lines.append(f"✅ yt-dlp `{yp}`")
        else:
            lines.append("❌ yt-dlp tidak ditemukan (`pip install yt-dlp`)")

    # ffmpeg — cek bundled dulu (sama seperti GUI), lalu system PATH
    from utils.dependency_manager import check_dependency
    if check_dependency("ffmpeg", app_dir):
        lines.append(f"✅ ffmpeg (bundled) `{app_dir / 'ffmpeg'}`")
    else:
        fp = shutil.which("ffmpeg")
        if fp:
            try:
                out = subprocess.run([fp, "-version"], capture_output=True,
                                     text=True, timeout=5)
                first = (out.stdout or "").splitlines()[0] if out.stdout else ""
                parts = first.split()
                ver = parts[2] if len(parts) > 2 and parts[0] == "ffmpeg" else ""
            except Exception:
                ver = ""
            label = f"✅ ffmpeg `v{ver}`" if ver else "✅ ffmpeg"
            lines.append(f"{label} `{fp}`")
        else:
            lines.append("❌ ffmpeg tidak ditemukan (`apt install ffmpeg`)")

    # deno — cek bundled (app_dir/bin), lalu system PATH
    if check_dependency("deno", app_dir):
        lines.append(f"✅ deno (bundled) `{app_dir / 'bin' / 'deno'}`")
    else:
        dp = shutil.which("deno")
        if dp:
            try:
                out = subprocess.run([dp, "--version"], capture_output=True,
                                     text=True, timeout=5)
                first = (out.stdout or "").splitlines()[0] if out.stdout else "deno"
            except Exception:
                first = "deno"
            lines.append(f"✅ deno `{first}` `{dp}`")
        else:
            lines.append("❌ deno tidak ditemukan (butuh buat fetch YouTube)")

    return lines

def build_status(cfg: dict):
    """Build the /status message text and its inline toggle keyboard."""
    wm = _is_watermark_active(cfg)
    credit = _is_credit_active(cfg)
    hook = _status_val(cfg, "hook_enabled", True)
    subtitle = _status_val(cfg, "subtitle_enabled", True)
    resolution = _status_val(cfg, "resolution", "1080p")
    ratio = _status_val(cfg, "aspect_ratio", "9:16")
    style = _status_val(cfg, "subtitle_style", "pop")
    sync = _status_val(cfg, "subtitle_sync_offset", -0.3)
    portrait = _status_val(cfg, "portrait_mode", "crop")
    face = _status_val(cfg, "face_tracking_mode", "opencv")
    gpu = cfg.get("gpu_acceleration", {}).get("enabled", False)
    num_clips = _status_val(cfg, "num_clips", 5)
    mp = cfg.get("mediapipe_settings", {})
    smooth = mp.get("smooth_follow", True)
    pan = mp.get("pan_speed_limit", 1.8)
    center = mp.get("center_weight", 0.15)
    switch = mp.get("switch_threshold", 0.18)
    shot = mp.get("min_shot_duration", 45)
    lip = mp.get("lip_activity_threshold", 0.08)
    hf_model = _hf_model(cfg)
    srv = ((cfg.get("ai_providers") or {}).get("highlight_finder") or {}).get("base_url") or cfg.get("base_url", "-")
    fw_size = _fw_model_size(cfg)
    fw_installed = _fw_model_installed(fw_size)
    bin_lines = _binaries_status_lines()

    ck = "✅"
    no = "❌"

    text = _quote_block(
        "⚙️ *Konfigurasi Aktif:*\n\n"
        f"{ck if wm else no} Watermark\n"
        f"{ck if credit else no} Credit Watermark\n"
        f"{ck if hook else no} Hook (Thumbnail Intro)\n"
        f"{ck if subtitle else no} Subtitle (Caption)\n\n"
        f"🎬 Jumlah Clips: `{num_clips}`\n"
        f"🎬 Resolusi: `{resolution}`\n"
        f"📐 Rasio: `{ratio}`\n"
        f"✏️ Gaya Subtitle: `{style}`\n"
        f"⏱️ Sync Offset: `{sync}`s\n"
        f"🖼️ Portrait Mode: `{portrait}`\n"
        f"🎯 Face Tracking: `{face}`\n"
        f"🎯 Smooth Follow: `{'On' if smooth else 'Off'}`\n"
        f"🎯 Pan Speed Limit: `{pan}`\n"
        f"🎯 Center Weight: `{center}`\n"
        f"🎯 Switch Threshold: `{switch}`\n"
        f"🎯 Min Shot Duration: `{shot}`\n"
        f"🎯 Lip Activity: `{lip}`\n"
        f"🤖 HF Model: `{hf_model}`\n"
        f"🌐 Server: `{srv}`\n"
        f"🎙️ Faster-Whisper: `{fw_size}` {'🟢' if fw_installed else '🔴'}\n"
        f"⚡ GPU Accel: `{'On' if gpu else 'Off'}`\n\n"
        f"🧰 *Biner:*\n" + "\n".join(bin_lines)
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"Watermark: {'On' if wm else 'Off'}", callback_data="st_watermark"),
            InlineKeyboardButton(f"Credit: {'On' if credit else 'Off'}", callback_data="st_credit"),
        ],
        [
            InlineKeyboardButton(f"Hook: {'On' if hook else 'Off'}", callback_data="st_hook"),
            InlineKeyboardButton(f"Subtitle: {'On' if subtitle else 'Off'}", callback_data="st_subtitle"),
        ],
        [
            InlineKeyboardButton(f"Jumlah Clips {num_clips}", callback_data="st_numclips"),
            InlineKeyboardButton(f"Resolusi {resolution}", callback_data="st_resolution"),
        ],
        [
            InlineKeyboardButton(f"Rasio {ratio}", callback_data="st_ratio"),
            InlineKeyboardButton(f"Style {style}", callback_data="st_style"),
        ],
        [
            InlineKeyboardButton(f"Portrait {portrait}", callback_data="st_portrait"),
            InlineKeyboardButton(f"Face {face}", callback_data="st_face"),
        ],
        [
            InlineKeyboardButton(f"Sync {sync}s", callback_data="st_sync"),
            InlineKeyboardButton(f"⚡ GPU {'On' if gpu else 'Off'}", callback_data="st_gpu"),
        ],
        [
            InlineKeyboardButton(f"Smooth {'On' if smooth else 'Off'}", callback_data="st_smooth"),
            InlineKeyboardButton(f"Pan {pan}", callback_data="st_pan"),
        ],
        [
            InlineKeyboardButton(f"Center {center}", callback_data="st_center"),
            InlineKeyboardButton(f"Switch {switch}", callback_data="st_switch"),
        ],
        [
            InlineKeyboardButton(f"MinShot {shot}", callback_data="st_shot"),
            InlineKeyboardButton(f"Lip {lip}", callback_data="st_lip"),
        ],
        [
            InlineKeyboardButton(f"HF Model {hf_model[:12]}", callback_data="st_hfmodel"),
            InlineKeyboardButton("🎙️ Faster Whisper", callback_data="st_fwmenu"),
        ],
        [
            InlineKeyboardButton("🌐 Set Server", callback_data="st_server"),
            InlineKeyboardButton("🤖 Daftar Model", callback_data="model_list"),
        ],
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="st_refresh"),
        ],
        [
            InlineKeyboardButton("⬅️ Menu Utama", callback_data="menu_main"),
        ],
    ])
    return text, keyboard


async def status_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id

    app_dir = Path(__file__).parent.resolve()
    cfg_mgr = ConfigManager(app_dir / "config.json", app_dir / "output")
    cfg = cfg_mgr.config
    data = query.data.replace("st_", "")

    if data == "refresh":
        text, keyboard = build_status(cfg)
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
        return

    if data == "watermark":
        cfg["watermark"]["enabled"] = not cfg.get("watermark", {}).get("enabled", False)
    elif data == "credit":
        cfg["credit_watermark"]["enabled"] = not cfg.get("credit_watermark", {}).get("enabled", False)
    elif data == "hook":
        cfg["hook_enabled"] = not _status_val(cfg, "hook_enabled", True)
    elif data == "subtitle":
        cfg["subtitle_enabled"] = not _status_val(cfg, "subtitle_enabled", True)
    elif data == "numclips":
        cur = int(_status_val(cfg, "num_clips", 5))
        cur = cur + 1 if cur < 20 else 1
        cfg["num_clips"] = cur
    elif data == "resolution":
        opts = ["auto", "2160p", "1440p", "1080p", "720p", "480p", "360p", "240p", "144p"]
        cur = str(cfg.get("resolution", "1080p"))
        cfg["resolution"] = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else "1080p"
    elif data == "ratio":
        opts = ["9:16", "1:1", "4:5", "16:9"]
        cur = str(cfg.get("aspect_ratio", "9:16"))
        cfg["aspect_ratio"] = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else "9:16"
    elif data == "style":
        opts = ["pop", "pop_bounce", "typewriter", "karaoke", "highlight"]
        cur = str(cfg.get("subtitle_style", "pop"))
        cfg["subtitle_style"] = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else "pop"
    elif data == "sync":
        opts = [round(-0.3 + i * 0.1, 1) for i in range(7)]  # -0.3 .. 0.3 step 0.1
        cur = round(float(cfg.get("subtitle_sync_offset", -0.3)), 1)
        idx = opts.index(cur) if cur in opts else 0
        cfg["subtitle_sync_offset"] = opts[(idx + 1) % len(opts)]
    elif data == "portrait":
        opts = ["crop", "blur"]
        cur = str(cfg.get("portrait_mode", "crop"))
        cfg["portrait_mode"] = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else "crop"
    elif data == "face":
        opts = ["mediapipe", "opencv"]
        cur = str(cfg.get("face_tracking_mode", "opencv"))
        cfg["face_tracking_mode"] = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else "opencv"
    elif data == "gpu":
        cfg["gpu_acceleration"]["enabled"] = not cfg.get("gpu_acceleration", {}).get("enabled", False)
    elif data == "smooth":
        mp = cfg.setdefault("mediapipe_settings", {})
        mp["smooth_follow"] = not mp.get("smooth_follow", True)
    elif data == "pan":
        mp = cfg.setdefault("mediapipe_settings", {})
        opts = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0]
        cur = float(mp.get("pan_speed_limit", 1.8))
        mp["pan_speed_limit"] = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else opts[0]
    elif data == "center":
        mp = cfg.setdefault("mediapipe_settings", {})
        opts = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
        cur = float(mp.get("center_weight", 0.15))
        mp["center_weight"] = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else opts[0]
    elif data == "switch":
        mp = cfg.setdefault("mediapipe_settings", {})
        opts = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
        cur = float(mp.get("switch_threshold", 0.18))
        mp["switch_threshold"] = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else opts[0]
    elif data == "shot":
        mp = cfg.setdefault("mediapipe_settings", {})
        opts = [30, 45, 60, 90, 120, 150, 180]
        cur = int(mp.get("min_shot_duration", 45))
        mp["min_shot_duration"] = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else opts[0]
    elif data == "lip":
        mp = cfg.setdefault("mediapipe_settings", {})
        opts = [0.05, 0.1, 0.15, 0.2, 0.25, 0.3]
        cur = float(mp.get("lip_activity_threshold", 0.08))
        mp["lip_activity_threshold"] = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else opts[0]
    elif data == "hfmodel":
        opts, _live = _hf_available_models(cfg)
        cur = _hf_model(cfg)
        idx = opts.index(cur) if cur in opts else -1
        _set_hf_model(cfg, opts[(idx + 1) % len(opts)])
    elif data == "server":
        context.user_data["pending_config_input"] = "sv_url"
        await query.edit_message_text(
            "🌐 *Ganti Base URL*\n\n"
            "Kirim URL baru, contoh:\n"
            "`http://192.168.10.74:20128/v1`\n\n"
            "Endpoint transkripsi/model otomatis mengikuti.\n"
            "Ketik `batal` untuk membatalkan.",
            parse_mode="Markdown",
        )
        return
    elif data == "sv_url":
        context.user_data["pending_config_input"] = "sv_url"
        await query.edit_message_text(
            "🌐 *Ganti Base URL*\n\n"
            "Kirim URL baru, contoh:\n"
            "`http://192.168.10.74:20128/v1`\n\n"
            "Ketik `batal` untuk membatalkan.",
            parse_mode="Markdown",
        )
        return
    elif data == "sv_key":
        context.user_data["pending_config_input"] = "sv_key"
        await query.edit_message_text(
            "🔑 *Ganti API Key*\n\n"
            "Kirim API key baru.\n"
            "Key disimpan ke semua provider (highlight, caption, hook, title).\n"
            "Ketik `batal` untuk membatalkan.",
            parse_mode="Markdown",
        )
        return
    elif data == "fwmenu":
        text, keyboard = build_fw_menu(cfg)
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
        return

    cfg_mgr.save_config(cfg)

    text, keyboard = build_status(cfg)
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")


REQUIRED_AUTH_COOKIES = ["SID", "HSID", "SSID", "APISID", "SAPISID", "LOGIN_INFO"]
SECURE_COOKIE_PREFIXES = ["__Secure-1P", "__Secure-3P"]

# (key, label, domains) — setiap platform punya file cookies sendiri
PLATFORMS = [
    ("youtube", "YouTube", ("youtube.com", "youtu.be")),
    ("tiktok", "TikTok", ("tiktok.com",)),
    ("instagram", "Instagram", ("instagram.com",)),
    ("facebook", "Facebook", ("facebook.com", "fb.watch")),
    ("twitter", "Twitter/X", ("x.com", "twitter.com")),
    ("reddit", "Reddit", ("reddit.com",)),
    ("vimeo", "Vimeo", ("vimeo.com",)),
    ("dailymotion", "Dailymotion", ("dailymotion.com",)),
    ("twitch", "Twitch", ("twitch.tv",)),
    ("soundcloud", "SoundCloud", ("soundcloud.com",)),
]


def _app_dir() -> Path:
    return Path(__file__).parent.resolve()


def _cookie_file_for(platform: str) -> Path:
    """Path file cookies untuk platform. YouTube tetap pakai cookies.txt (legacy/GUI)."""
    app_dir = _app_dir()
    if platform == "youtube":
        return app_dir / "cookies.txt"
    return app_dir / f"cookies_{platform}.txt"


def _detect_platform(url: str):
    """Kembalikan (key, label) platform dari URL, atau (None, None)."""
    for key, label, domains in PLATFORMS:
        if any(d in url for d in domains):
            return key, label
    return None, None


def _found_auth_cookies(path: Path) -> list:
    """Cek cookies autentikasi YouTube di file."""
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    found = []
    for cookie in REQUIRED_AUTH_COOKIES:
        if f"\t{cookie}\t" in content or content.endswith(f"\t{cookie}"):
            found.append(cookie)
        else:
            for prefix in SECURE_COOKIE_PREFIXES:
                secure_name = f"{prefix}{cookie}"
                if f"\t{secure_name}\t" in content or content.endswith(f"\t{secure_name}"):
                    found.append(secure_name)
                    break
    return found


def _cookies_status_for(platform: str) -> dict:
    """Status file cookies untuk satu platform."""
    path = _cookie_file_for(platform)
    if not path.exists():
        return {"platform": platform, "exists": False, "valid": False, "found": [], "size_mb": 0, "mtime": None}
    if platform == "youtube":
        found = _found_auth_cookies(path)
        valid = bool(found)
    else:
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            content = ""
        valid = bool(content.strip())
        found = []
    return {
        "platform": platform,
        "exists": True,
        "valid": valid,
        "found": found,
        "size_mb": round(path.stat().st_size / (1024 * 1024), 3),
        "mtime": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
    }


def build_cookies_menu(selected_platform: str = None):
    """Menu /cookies: daftar status tiap platform. Jika selected_platform, tampilkan detail-nya."""
    if selected_platform:
        label = dict((k, lbl) for k, lbl, _ in PLATFORMS).get(selected_platform, selected_platform)
        st = _cookies_status_for(selected_platform)

        if not st["exists"]:
            head = f"❌ *{label}: belum ada cookies*"
            detail = f"Upload `cookies.txt` untuk {label} lewat tombol Upload di bawah."
        elif st["valid"]:
            head = f"✅ *{label}: cookies Valid*"
            detail = (
                f"Ukuran: `{st['size_mb']} MB`  |  Diperbarui: `{st['mtime']}`"
                + (f"\nAuth cookies: `{', '.join(st['found'][:4])}`" if st["found"] else "")
            )
        else:
            head = f"⚠️ *{label}: cookies tidak valid*"
            detail = (
                "File ada tapi isinya tidak sesuai.\n"
                "Export ulang cookies saat sudah login di platform tersebut."
            )

        text = (
            "🍪 *Kelola Cookies Per Platform*\n\n"
            f"{head}\n"
            f"{detail}\n\n"
            "📤 Klik *Upload* lalu kirim file cookies platform ini.\n"
            "🗑️ *Hapus* untuk menghapus cookies platform ini."
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Upload", callback_data=f"ck_up_{selected_platform}")],
            [InlineKeyboardButton("🗑️ Hapus", callback_data=f"ck_del_{selected_platform}")],
            [InlineKeyboardButton("⬅️ Daftar platform", callback_data="ck_refresh")],
            [InlineKeyboardButton("🏠 Menu Bot", callback_data="ck_back")],
        ])
        return text, keyboard

    lines = ["🍪 *Kelola Cookies Per Platform*\n"]
    for key, label, _ in PLATFORMS:
        st = _cookies_status_for(key)
        if not st["exists"]:
            mark = "❌"
        elif st["valid"]:
            mark = "✅"
        else:
            mark = "⚠️"
        lines.append(f"{mark} {label}")
    lines.append("\nPilih platform untuk upload / cek / hapus:")

    keyboard_rows = []
    for i in range(0, len(PLATFORMS), 2):
        row = []
        for key, label, _ in PLATFORMS[i:i + 2]:
            row.append(InlineKeyboardButton(label, callback_data=f"ck_sel_{key}"))
        keyboard_rows.append(row)
    keyboard_rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="ck_refresh")])
    keyboard_rows.append([InlineKeyboardButton("🏠 Menu Bot", callback_data="ck_back")])

    return "\n".join(lines), InlineKeyboardMarkup(keyboard_rows)


async def cookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🍪 Menu kelola cookies per platform (upload / cek / hapus untuk yt-dlp)."""
    context.user_data["pending_cookie_platform"] = None
    text, keyboard = build_cookies_menu()
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")


async def cookies_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback handler untuk menu cookies (ck_sel_ / ck_up_ / ck_del_ / ck_refresh / ck_back)."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("ck_sel_"):
        platform = data[len("ck_sel_"):]
        text, keyboard = build_cookies_menu(selected_platform=platform)
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
        return

    if data.startswith("ck_up_"):
        platform = data[len("ck_up_"):]
        label = dict((k, lbl) for k, lbl, _ in PLATFORMS).get(platform, platform)
        context.user_data["pending_cookie_platform"] = platform
        await query.edit_message_text(
            f"📤 *Upload cookies untuk {label}*\n\n"
            f"Kirim file cookies kamu ke chat ini.\n"
            f"File akan disimpan sebagai `cookies_{platform}.txt`.",
            parse_mode="Markdown",
        )
        return

    if data.startswith("ck_del_"):
        platform = data[len("ck_del_"):]
        label = dict((k, lbl) for k, lbl, _ in PLATFORMS).get(platform, platform)
        path = _cookie_file_for(platform)
        if path.exists():
            try:
                path.unlink()
                text, keyboard = build_cookies_menu(selected_platform=platform)
                await query.edit_message_text(
                    f"🗑️ *Cookies {label} berhasil dihapus.*",
                    reply_markup=keyboard,
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.exception(f"Failed to delete cookies for {platform}")
                await query.edit_message_text(f"❌ Gagal menghapus cookies:\n`{str(e)[:200]}`", parse_mode="Markdown")
        else:
            text, keyboard = build_cookies_menu(selected_platform=platform)
            await query.edit_message_text(
                f"ℹ️ Tidak ada cookies {label} untuk dihapus.",
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
        return

    if data == "ck_refresh":
        context.user_data["pending_cookie_platform"] = None
        text, keyboard = build_cookies_menu()
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
        return

    if data == "ck_back":
        await query.edit_message_text(MAIN_MENU_TEXT, parse_mode="Markdown")
        return


def prepare_cookies_for_url(url: str) -> str:
    """Salin cookies platform sesuai URL ke cookies.txt (yang dibaca clipper_core).

    YouTube: wajib, jika tidak ada cookies.txt bot kirim pesan.
    Non-YouTube: jika ada cookies_<platform>.txt salin; jika tidak, buat cookies.txt
    kosong supaya clipper_core tidak crash (tanpa autentikasi).
    """
    key, label = _detect_platform(url)
    app_dir = _app_dir()
    dest = app_dir / "cookies.txt"
    if key == "youtube":
        src = _cookie_file_for("youtube")
        if src.exists():
            if src.resolve() != dest.resolve():
                shutil.copy(src, dest)
            return label
        return None
    if key:
        src = _cookie_file_for(key)
        if src.exists():
            shutil.copy(src, dest)
        else:
            if dest.exists() and dest.stat().st_size > 0:
                dest.write_text("", encoding="utf-8")
        return label
    return None


async def receive_cookies_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle upload file cookies dan simpan ke cookies_<platform>.txt.

    Juga menangani upload gambar watermark saat tombol 'Upload' di menu
    watermark ditekan (pending_watermark_upload set).
    """
    doc = update.message.document
    fname = (doc.file_name or "").lower()

    # Watermark upload mode
    if context.user_data.get("pending_watermark_upload"):
        context.user_data["pending_watermark_upload"] = None
        if not fname.endswith((".png", ".jpg", ".jpeg")):
            await update.message.reply_text(
                "❌ File watermark harus berupa gambar `.png`, `.jpg`, atau `.jpeg`.\n"
                "Kirim ulang gambar yang benar.",
                parse_mode="Markdown",
            )
            return
        status_msg = await update.message.reply_text(
            "⏳ Menyimpan gambar watermark...", parse_mode="Markdown"
        )
        try:
            file = await doc.get_file()
            app_dir = _app_dir()
            watermarks_dir = app_dir / "assets" / "watermarks"
            watermarks_dir.mkdir(parents=True, exist_ok=True)
            dest = watermarks_dir / "bot_watermark.png"
            await file.download_to_drive(custom_path=str(dest))
            logger.info(f"Watermark saved to {dest}")

            cfg_mgr = ConfigManager(app_dir / "config.json", app_dir / "output")
            cfg = cfg_mgr.config
            cfg.setdefault("watermark", {})["image_path"] = str(dest)
            cfg["watermark"]["enabled"] = True
            cfg_mgr.save_config(cfg)

            text, keyboard = build_watermark_menu(cfg)
            await status_msg.edit_text(
                f"✅ *Watermark berhasil di-upload!*\n\nGambar: 🖼️ `{dest.name}`\n"
                "Status otomatis diaktifkan.",
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.exception("Failed to save watermark document")
            await status_msg.edit_text(
                f"❌ Gagal menyimpan watermark:\n`{str(e)[:200]}`", parse_mode="Markdown"
            )
        return

    if not fname.endswith(".txt"):
        await update.message.reply_text(
            "❌ File harus berupa `.txt`.\n"
            "Export cookies dengan extension browser lalu kirim file cookies.",
            parse_mode="Markdown",
            reply_markup=_back_to_menu_keyboard(),
        )
        return

    platform = context.user_data.get("pending_cookie_platform")
    if not platform or platform not in [k for k, _, _ in PLATFORMS]:
        await update.message.reply_text(
            "❌ Pilih platform dulu lewat `/cookies` sebelum upload cookies.",
            parse_mode="Markdown",
            reply_markup=_back_to_menu_keyboard(),
        )
        return

    context.user_data["pending_cookie_platform"] = None
    label = dict((k, lbl) for k, lbl, _ in PLATFORMS).get(platform, platform)
    status_msg = await update.message.reply_text(
        f"⏳ Menyimpan cookies {label}...", parse_mode="Markdown"
    )
    try:
        file = await doc.get_file()
        dest = _cookie_file_for(platform)
        await file.download_to_drive(custom_path=str(dest))
        logger.info(f"Cookies saved to {dest} ({doc.file_size} bytes)")

        st = _cookies_status_for(platform)
        if st["valid"]:
            await status_msg.edit_text(
                f"✅ *cookies_{platform}.txt berhasil di-upload & valid!*\n\n"
                f"Ukuran: `{st['size_mb']} MB`"
                + (f"\nAuth cookies: `{', '.join(st['found'][:6])}`" if st["found"] else "")
                + "\n\nKamu bisa langsung kirim link.",
                parse_mode="Markdown",
                reply_markup=_back_to_menu_keyboard(),
            )
        else:
            text, keyboard = build_cookies_menu(selected_platform=platform)
            await status_msg.edit_text(
                f"⚠️ *File tersimpan tapi tidak valid.*\n"
                f"cookies_{platform}.txt isinya tidak sesuai.\n"
                "Export ulang saat sudah login di platform tersebut.\n\n"
                "Lihat menu:",
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
    except Exception as e:
        logger.exception("Failed to save cookies document")
        await status_msg.edit_text(f"❌ Gagal menyimpan cookies:\n`{str(e)[:200]}`", parse_mode="Markdown")


async def process_youtube_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    # Mode set server AI: menunggu URL dari tombol 🌐 di /config atau /server
    if context.user_data.get("pending_config_input") in ("server", "sv_url"):
        context.user_data["pending_config_input"] = None
        if url.lower() in ("batal", "cancel"):
            await update.message.reply_text("❌ Batal.")
            return
        new_srv = _apply_server(url)
        if new_srv is None:
            context.user_data["pending_config_input"] = "sv_url"
            await update.message.reply_text(
                "❌ URL tidak valid. Contoh: `http://192.168.10.74:20128/v1`\nCoba lagi atau ketik `batal`.",
                parse_mode="Markdown",
            )
            return
        await update.message.reply_text(
            f"✅ Base URL diubah ke:\n`{new_srv}`",
            parse_mode="Markdown",
            reply_markup=_back_to_menu_keyboard(),
        )
        return

    # Mode set API key dari tombol 🔑 di /server
    if context.user_data.get("pending_config_input") == "sv_key":
        context.user_data["pending_config_input"] = None
        if url.lower() in ("batal", "cancel"):
            await update.message.reply_text("❌ Batal.")
            return
        new_key = _apply_apikey(url)
        if new_key is None:
            context.user_data["pending_config_input"] = "sv_key"
            await update.message.reply_text(
                "❌ API key kosong. Coba lagi atau ketik `batal`.",
                parse_mode="Markdown",
            )
            return
        masked = new_key[:8] + "***" if len(new_key) > 8 else "***"
        await update.message.reply_text(
            f"✅ API Key diubah ke:\n`{masked}`",
            parse_mode="Markdown",
            reply_markup=_back_to_menu_keyboard(),
        )
        return

    # Mode tambah ID admin: user mengirim angka ID
    if context.user_data.get("pending_admin_add_id"):
        context.user_data["pending_admin_add_id"] = False
        if not url.isdigit():
            await update.message.reply_text(
                "❌ Bukan angka valid. Kirim *user ID* dalam bentuk angka, contoh: `123456789`.",
                parse_mode="Markdown",
            )
            return
        chat_id = update.effective_user.id if update.effective_user else update.effective_chat.id
        app_dir = Path(__file__).parent.resolve()
        cfg_mgr = ConfigManager(app_dir / "config.json", app_dir / "output")
        cfg = cfg_mgr.config
        allowed = cfg.get("allowed_user_ids", []) or []
        new_id = int(url)
        if new_id in allowed:
            await update.message.reply_text(
                f"ℹ️ ID `{new_id}` sudah ada di daftar.", parse_mode="Markdown"
            )
            return
        allowed.append(new_id)
        cfg["allowed_user_ids"] = allowed
        cfg_mgr.save_config(cfg)
        text, keyboard = build_admin_menu(cfg, chat_id)
        await update.message.reply_text(
            f"✅ ID `{new_id}` ditambahkan ke daftar yang diizinkan.\n\n{text}",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
        return

    supported = (
        "youtube.com", "youtu.be",
        "tiktok.com", "instagram.com",
        "x.com", "twitter.com",
        "reddit.com", "facebook.com", "fb.watch",
        "vimeo.com", "dailymotion.com",
        "twitch.tv", "soundcloud.com",
    )
    if not any(d in url for d in supported):
        await update.message.reply_text(
            "❌ Link tidak didukung.\n"
            "Kirimkan link dari:\n"
            "YouTube, TikTok, Instagram, Twitter/X, Reddit,\n"
            "Facebook, Vimeo, Dailymotion, Twitch, SoundCloud",
            reply_markup=_back_to_menu_keyboard(),
        )
        return

    chat_id = update.effective_chat.id
    loop = asyncio.get_running_loop()

    # Siapkan cookies sesuai platform URL ini (YouTube wajib, lainnya opsional)
    platform_label = prepare_cookies_for_url(url)
    if platform_label is None:
        await update.message.reply_text(
            "🍪 *cookies.txt untuk YouTube belum ada.*\n\n"
            "Kirim `/cookies`, pilih YouTube, lalu upload cookies kamu.\n"
            "Cara export cookies: lihat `COOKIES.md` di proyek ini.",
            parse_mode="Markdown",
            reply_markup=_back_to_menu_keyboard(),
        )
        return

    # Cek apakah ini YouTube atau non-YouTube
    is_youtube = any(d in url for d in ("youtube.com", "youtu.be"))

    # Simpan URL pending untuk tombol Proses
    context.user_data["pending_url"] = {
        "url": url,
        "is_youtube": is_youtube,
        "platform_label": platform_label,
    }

    # Tampilkan config aktif + tombol Proses sebelum mulai
    text, keyboard = _proc_confirm_payload(context)
    await update.message.reply_text(
        text,
        reply_markup=keyboard,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


def _proc_confirm_payload(context: ContextTypes.DEFAULT_TYPE):
    """Bangun teks config + keyboard Ubah/Proses untuk link pending."""
    url = context.user_data.get("pending_url", {}).get("url", "?")
    platform_label = context.user_data.get("pending_url", {}).get("platform_label", "-")

    app_dir = Path(__file__).parent.resolve()
    cfg_mgr = ConfigManager(app_dir / "config.json", app_dir / "output")
    cfg_text, _ = build_status(cfg_mgr.config)

    header = _quote_block(
        f"🔗 *Link diterima:* `{url[:80]}`\n"
        f"📺 Platform: `{platform_label}`"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⚙️ Ubah", callback_data="proc_cfg", style=KeyboardButtonStyle.PRIMARY),
            InlineKeyboardButton("🚀 Proses", callback_data="proc_start", style=KeyboardButtonStyle.SUCCESS),
        ],
        [InlineKeyboardButton("⬅️ Batal", callback_data="proc_cancel")],
    ])
    return header + "\n\n" + cfg_text, keyboard


async def handle_proc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Jalankan proses setelah user menekan tombol Proses pada config preview."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    data = query.data.replace("proc_", "")

    pending = context.user_data.get("pending_url")

    if data == "cancel":
        context.user_data.pop("pending_url", None)
        await query.edit_message_text("❌ Proses dibatalkan.")
        return

    if data == "cfg":
        app_dir = Path(__file__).parent.resolve()
        cfg_mgr = ConfigManager(app_dir / "config.json", app_dir / "output")
        text, keyboard = build_status(cfg_mgr.config)
        extra = [
            [
                InlineKeyboardButton("⬅️ Kembali", callback_data="proc_cfg_back", style=KeyboardButtonStyle.PRIMARY),
                InlineKeyboardButton("🚀 Proses", callback_data="proc_start", style=KeyboardButtonStyle.SUCCESS),
            ],
        ]
        keyboard = InlineKeyboardMarkup(list(keyboard.inline_keyboard) + extra)
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
        return

    if data == "cfg_back":
        text, keyboard = _proc_confirm_payload(context)
        await query.edit_message_text(
            text,
            reply_markup=keyboard,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    if not pending:
        await query.edit_message_text("❌ Tidak ada link pending. Kirim ulang link.")
        return

    url = pending["url"]
    is_youtube = pending["is_youtube"]
    platform_label = pending["platform_label"]
    context.user_data.pop("pending_url", None)

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    if is_youtube:
        status_msg = await query.edit_message_text(
            "⏳ *[Phase 1] Mengunduh subtitle & analisis AI (Tanpa download video penuh)...*", parse_mode="Markdown"
        )
        await run_phase1(chat_id, url, context, status_msg)
    else:
        status_msg = await query.edit_message_text(
            f"⏳ *[{platform_label}] Download video & proses clip otomatis...*", parse_mode="Markdown"
        )
        await run_non_youtube(chat_id, url, context, status_msg)


async def run_phase1(chat_id: int, url: str, context: ContextTypes.DEFAULT_TYPE, status_msg):
    loop = asyncio.get_running_loop()
    status_holder: dict = {"last_log_send": 0, "last_progress_pct": -1, "last_progress_send": 0}
    log_cb, prog_cb = make_callbacks(chat_id, context.bot, loop, status_holder)

    with session_lock:
        busy = ACTIVE_CHATS.get(chat_id)
    if busy:
        await status_msg.edit_text(
            f"⏳ Masih ada proses yang berjalan: {busy}.\n"
            "Tunggu selesai atau batalkan dengan /cancel_session.",
            parse_mode="Markdown",
        )
        return
    with session_lock:
        ACTIVE_CHATS[chat_id] = "Phase 1 - Analisis Highlight"

    app_dir = Path(__file__).parent.resolve()
    cfg_mgr = ConfigManager(app_dir / "config.json", app_dir / "output")
    core = get_core_instance(cfg_mgr, log_cb=log_cb, progress_cb=prog_cb)

    try:
        # Phase 1: find_highlights_only (subtitle only + AI highlight detection)
        num_clips = _status_val(cfg_mgr.config, "num_clips", 5)
        session_data = await loop.run_in_executor(None, core.find_highlights_only, url, num_clips)

        if not session_data or not session_data.get("highlights"):
            context.user_data["retry_url"] = url
            keyboard = [[InlineKeyboardButton("🔄 Coba Lagi", callback_data="retry_phase1")]]
            await status_msg.edit_text(
                "❌ AI tidak menemukan highlight dari video ini.\n"
                "Coba lagi dengan model AI berbeda atau video lain.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )
            return

        highlights = session_data["highlights"]

        highlights.sort(
            key=lambda h: h.get("virality_score", 0) or 0,
            reverse=True
        )
        session_data["highlights"] = highlights

        USER_SESSIONS[chat_id] = {
            "core": core,
            "url": url,
            "session_data": session_data,
            "highlights": highlights,
            "loop": loop,
            "selected": set(),
            "cancel_event": threading.Event(),
        }
        session = USER_SESSIONS[chat_id]
        # Wire core cancellation to the session's cancel event
        core.is_cancelled = session["cancel_event"].is_set
        session["cancel_event"].clear()

        await _render_highlight_menu(chat_id, status_msg, session)

    except Exception as e:
        logger.exception("Error processing video URL (Phase 1)")
        short_err = str(e)[:300].replace("`", "'")
        context.user_data["retry_url"] = url
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Coba Lagi", callback_data="retry_phase1")]])
        err_text = f"❌ *Error Phase 1:*\n`{short_err}`"
        try:
            await status_msg.edit_text(err_text, reply_markup=keyboard, parse_mode="Markdown")
        except Exception:
            # ponytail: teks error bisa mengandung _/* yang merusak Markdown
            await status_msg.edit_text("❌ Error Phase 1:\n" + short_err, reply_markup=keyboard)
    finally:
        with session_lock:
            ACTIVE_CHATS.pop(chat_id, None)


async def run_non_youtube(chat_id: int, url: str, context: ContextTypes.DEFAULT_TYPE, status_msg):
    """Non-YouTube path: download video, split into clips, render without AI/subtitle."""
    loop = asyncio.get_running_loop()
    status_holder = {"last_log_send": 0, "last_progress_pct": -1, "last_progress_send": 0}
    log_cb, prog_cb = make_callbacks(chat_id, context.bot, loop, status_holder)

    with session_lock:
        busy = ACTIVE_CHATS.get(chat_id)
    if busy:
        await status_msg.edit_text(
            "Masih ada proses yang berjalan: " + str(busy) + ".\nTunggu selesai atau batalkan dengan /cancel_session."
        )
        return

    with session_lock:
        ACTIVE_CHATS[chat_id] = "Non-YouTube - Download and Process"

    app_dir = Path(__file__).parent.resolve()
    cfg_mgr = ConfigManager(app_dir / "config.json", app_dir / "output")
    core = get_core_instance(cfg_mgr, log_cb=log_cb, progress_cb=prog_cb)

    session_id = url.split("/")[-1].split("?")[0] or datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = cfg_mgr.output_dir / "sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    session_data = {
        "session_dir": str(session_dir),
        "url": url,
        "video_info": {"title": session_id, "channel": "Unknown"},
        "highlights": [],
        "created_at": datetime.now().isoformat(),
        "status": "downloading",
    }
    session_data_file = session_dir / "session_data.json"
    with open(session_data_file, "w", encoding="utf-8") as f:
        json.dump(session_data, f, indent=2, ensure_ascii=False)

    try:
        await status_msg.edit_text("Downloading video...")
        core.temp_dir = session_dir / "_temp"
        core.temp_dir.mkdir(parents=True, exist_ok=True)
        core.last_session_dir = str(session_dir)

        video_path, srt_path, video_info = await loop.run_in_executor(None, core.download_video, url)
        if not video_path or not Path(video_path).exists():
            await status_msg.edit_text("Download video gagal.")
            return

        session_data["video_info"] = video_info or {}
        session_data["status"] = "processing"
        with open(session_data_file, "w", encoding="utf-8") as f:
            json.dump(session_data, f, indent=2, ensure_ascii=False)

        await status_msg.edit_text("Analisis durasi dan membagi clip...")
        probe_cmd = [
            core.ffmpeg_path or "ffmpeg", "-i", video_path,
            "-show_entries", "format=duration",
            "-v", "quiet", "-of", "csv=p=0"
        ]
        result = subprocess.run(probe_cmd, capture_output=True, text=True, creationflags=1, timeout=30)
        total_duration = float(result.stdout.strip() or "0")

        if total_duration <= 0:
            await status_msg.edit_text("Gagal mendeteksi durasi video.")
            return

        clip_duration = 80
        clips_count = max(1, int(total_duration // clip_duration))

        highlights = []
        for i in range(clips_count):
            start_sec = i * clip_duration
            end_sec = min((i + 1) * clip_duration, total_duration)
            highlights.append({
                "title": "Clip " + str(i + 1) + "/" + str(clips_count),
                "start_time": "%02d:%02d:%06.3f" % (int(start_sec // 3600), int((start_sec % 3600) // 60), start_sec % 60),
                "end_time": "%02d:%02d:%06.3f" % (int(end_sec // 3600), int((end_sec % 3600) // 60), end_sec % 60),
                "start_second": start_sec,
                "end_second": end_sec,
                "duration_seconds": round(end_sec - start_sec, 1),
                "virality_score": 8,
            })
        session_data["highlights"] = highlights

        selected = set(range(len(highlights)))
        USER_SESSIONS[chat_id] = {
            "core": core,
            "url": url,
            "session_data": session_data,
            "highlights": highlights,
            "loop": loop,
            "selected": selected,
            "cancel_event": threading.Event(),
        }
        core.is_cancelled = USER_SESSIONS[chat_id]["cancel_event"].is_set
        USER_SESSIONS[chat_id]["cancel_event"].clear()

        keyboard = []
        for idx, h in enumerate(highlights):
            keyboard.append([InlineKeyboardButton(
                "[ " + h["start_time"] + " - " + h["end_time"] + " ] " + h["title"],
                callback_data="sel_" + str(idx)
            )])
        keyboard.append([
            InlineKeyboardButton("Proses Selected", callback_data="sel_process"),
        ])
        await status_msg.edit_text(
            "Video dibagi jadi " + str(len(highlights)) + " clip (" + str(int(total_duration)) + "s total).\nSemua clip sudah dipilih. Klik Proses Selected untuk mulai render:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except Exception as e:
        logger.exception("Error in non-YouTube flow")
        short_err = str(e)[:300].replace("`", "'")
        await status_msg.edit_text("Error: " + short_err)
    finally:
        with session_lock:
            ACTIVE_CHATS.pop(chat_id, None)


async def _render_highlight_menu(chat_id: int, status_msg, session: dict):
    highlights = session["highlights"]
    selected = session.get("selected", set())

    keyboard = []
    for idx, h in enumerate(highlights):
        start = h.get("start_time", h.get("start", "00:00"))
        end = h.get("end_time", h.get("end", "00:00"))
        title = h.get("title", f"Highlight #{idx+1}")
        score = h.get("virality_score", 0) or 0
        dur = h.get("duration_seconds") or 0
        dur_str = f"{int(dur)}s" if dur else ""
        mark = "☑️" if idx in selected else "⬜"
        keyboard.append([InlineKeyboardButton(
            f"{mark} ⭐{score} {dur_str} {title}" if dur_str else
            f"{mark} ⭐{score} {title}",
            callback_data=f"sel_{idx}"
        )])

    all_selected = len(selected) == len(highlights)
    keyboard.append([
        InlineKeyboardButton(
            "✅ Semua" if all_selected else "⬜ Semua",
            callback_data="sel_all"
        ),
        InlineKeyboardButton(
            "🎬 Proses Selected", callback_data="sel_process"
        ),
    ])

    header = (
        f"🎯 *Ditemukan {len(highlights)} Highlight!*\n"
        f"Centang highlight yang ingin dipotong ({len(selected)}/{len(highlights)}):"
    )
    await status_msg.edit_text(
        header,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


async def server_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle tombol 🌐/🔑 di menu /server: minta input URL atau API key."""
    query = update.callback_query
    await query.answer()
    app_dir = Path(__file__).parent.resolve()
    cfg = ConfigManager(app_dir / "config.json", app_dir / "output").config
    cur_url = ((cfg.get("ai_providers") or {}).get("highlight_finder") or {}).get("base_url") or cfg.get("base_url", "-")
    cur_key = ((cfg.get("ai_providers") or {}).get("highlight_finder") or {}).get("api_key", "") or cfg.get("api_key", "")
    masked_key = cur_key[:8] + "***" if len(cur_key) > 8 else ("***" if cur_key else "(kosong)")

    if query.data == "sv_url":
        context.user_data["pending_config_input"] = "sv_url"
        await query.edit_message_text(
            f"🌐 *Ganti Base URL*\n\nSekarang: `{cur_url}`\n\n"
            "Kirim URL baru, contoh:\n"
            "`http://192.168.10.74:20128/v1`\n\n"
            "Ketik `batal` untuk membatalkan.",
            parse_mode="Markdown",
        )
    elif query.data == "sv_key":
        context.user_data["pending_config_input"] = "sv_key"
        await query.edit_message_text(
            f"🔑 *Ganti API Key*\n\nSekarang: `{masked_key}`\n\n"
            "Kirim API key baru. Disimpan ke semua provider.\n"
            "Ketik `batal` untuk membatalkan.",
            parse_mode="Markdown",
        )

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id

    if chat_id not in USER_SESSIONS:
        await query.edit_message_text("❌ Sesi kedaluwarsa. Kirim ulang link YouTube.")
        return

    session = USER_SESSIONS[chat_id]
    data = query.data

    if data.startswith("sel_"):
        action = data[4:]
        highlights = session["highlights"]
        selected = session.setdefault("selected", set())

        if action == "all":
            if len(selected) == len(highlights):
                selected.clear()
            else:
                selected.update(range(len(highlights)))
        elif action == "process":
            if not selected:
                await query.answer("⚠️ Belum ada highlight yang dipilih", show_alert=True)
                return
            await _process_selected(update, context, session)
            return
        else:
            idx = int(action)
            if idx in selected:
                selected.discard(idx)
            else:
                selected.add(idx)

        await _render_highlight_menu(chat_id, query.message, session)
        return

    await _process_selected(update, context, session)


async def _process_selected(update: Update, context: ContextTypes.DEFAULT_TYPE, session: dict):
    query = update.callback_query
    chat_id = update.effective_chat.id
    core: AutoClipperCore = session["core"]
    url = session["url"]
    session_data = session["session_data"]
    highlights = session["highlights"]
    loop = asyncio.get_running_loop()

    selected = session.get("selected", set())
    if not selected:
        await query.answer("⚠️ Belum ada highlight yang dipilih", show_alert=True)
        return
    selected_highlights = [highlights[i] for i in sorted(selected)]

    with session_lock:
        busy = ACTIVE_CHATS.get(chat_id)
    if busy:
        await query.answer(f"⏳ Masih ada proses berjalan ({busy}).", show_alert=True)
        return
    with session_lock:
        ACTIVE_CHATS[chat_id] = "Phase 2 - Render Klip"

    status_holder: dict = {"last_log_send": 0, "last_progress_pct": -1, "last_progress_send": 0}
    log_cb, prog_cb = make_callbacks(chat_id, context.bot, loop, status_holder)
    core.log = log_cb
    core.set_progress = prog_cb

    render_start = time.time()

    await query.edit_message_text(
        f"⚡ *[Phase 2] Memproses {len(selected_highlights)} klip terpilih...*\n"
        "Mengunduh bagian video terpilih dan merender (Crop 9:16 + Hook + Caption)...",
        parse_mode="Markdown"
    )

    # In-chat indicator during potentially long processing
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    try:
        # Phase 2: process_selected_highlights (downloads sections only & renders)
        clips_dir = Path(session_data["session_dir"]) / "clips"

        app_dir = Path(__file__).parent.resolve()
        cfg_mgr = ConfigManager(app_dir / "config.json", app_dir / "output")
        cfg = cfg_mgr.config
        add_captions = _status_val(cfg, "subtitle_enabled", True)
        add_hook = _status_val(cfg, "hook_enabled", True)
        resolution = _status_val(cfg, "resolution", "1080p")
        if resolution == "auto":
            resolution = "auto (best)"

        # Run process_selected_highlights in background thread
        await loop.run_in_executor(
            None,
            core.process_selected_highlights,
            url,
            selected_highlights,
            Path(session_data["session_dir"]),
            add_captions,
            add_hook,
            resolution
        )

        # Tampilkan hasil: daftar nama klip saja (tanpa kirim file video)
        generated_clips = find_final_clips(clips_dir)
        if not generated_clips:
            await context.bot.send_message(chat_id=chat_id, text="❌ Tidak ada klip yang berhasil dirender.")
            return

        elapsed = time.time() - render_start
        dur_str = _format_duration(elapsed)
        session_id = Path(session_data["session_dir"]).name
        lines = [f"✅ *Berhasil merender {len(generated_clips)} klip dalam waktu {dur_str}:*", ""]
        for clip_path in generated_clips:
            lines.append(f"🎬 • `{clip_path.stem}`")
        lines.append("")
        lines.append("📂 /result auto-trigger untuk lihat detail & unduh.")

        await context.bot.send_message(
            chat_id=chat_id,
            text="\n".join(lines),
            parse_mode="Markdown",
        )

        # Auto-trigger /result: build session list + send session list message
        # simulating /result command result
        app_dir = Path(__file__).parent.resolve()
        sessions_dir = app_dir / "output" / "sessions"
        keyboard, sessions = load_session_keyboard(sessions_dir)
        context.user_data["result_sessions"] = sessions
        await context.bot.send_message(
            chat_id=chat_id,
            text=_session_list_text(sessions),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    except Exception as e:
        logger.exception("Error processing selected highlights (Phase 2)")
        short_err = str(e)[:300].replace("`", "'")
        await context.bot.send_message(chat_id=chat_id, text=f"❌ *Gagal proses klip (Phase 2):*\n`{short_err}`", parse_mode="Markdown")
    finally:
        with session_lock:
            ACTIVE_CHATS.pop(chat_id, None)


async def cancel_session_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel an active session by ID (video/session folder name) or chat ID.
    Usage: /cancel_session <id>  or  /cancel_session all"""
    args = context.args
    cancel_all = "all" in args or not args

    # Map session id -> (chat_id, session)
    matched = []
    for chat_id, session in list(USER_SESSIONS.items()):
        sdata = session.get("session_data", {})
        session_dir = str(sdata.get("session_dir", ""))
        folder_id = Path(session_dir).name
        if cancel_all:
            matched.append((chat_id, session, folder_id))
        else:
            for arg in args:
                if arg == str(chat_id) or arg == folder_id or (arg in session.get("url", "")):
                    matched.append((chat_id, session, folder_id))
                    break

    if not matched:
        text = "❌ Tidak ada session aktif yang cocok.\n"
        if USER_SESSIONS:
            text += "Session aktif:\n" + "\n".join(
                f"• `{Path(str(s.get('session_data', {}).get('session_dir', ''))).name}` "
                f"(chat {cid})" for cid, s in list(USER_SESSIONS.items())
            )
        else:
            text += "Tidak ada session berjalan saat ini."
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=_back_to_menu_keyboard())
        return

    for chat_id, session, folder_id in matched:
        ev = session.get("cancel_event")
        if ev is not None:
            ev.set()
        core = session.get("core")
        if core is not None:
            core.is_cancelled = lambda: True
        await update.message.reply_text(
            f"🗂️ *Session dibatalkan:* `{folder_id}` (chat {chat_id})",
            parse_mode="Markdown",
            reply_markup=_back_to_menu_keyboard(),
        )

    if cancel_all and USER_SESSIONS:
        USER_SESSIONS.clear()
        await update.message.reply_text("🧹 Semua session aktif telah dibersihkan.", parse_mode="Markdown", reply_markup=_back_to_menu_keyboard())


async def handle_retry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    url = context.user_data.get("retry_url")

    if not url:
        await query.edit_message_text("❌ URL tidak tersedia. Kirim ulang link YouTube.")
        return

    await query.edit_message_text(
        "⏳ *[Phase 1] Mencoba lagi...*",
        parse_mode="Markdown",
    )
    await run_phase1(chat_id, url, context, query.message)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")

    if not token:
        app_dir = Path(__file__).parent.resolve()
        config_file = app_dir / "config.json"
        if config_file.exists():
            with open(config_file, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                token = cfg.get("telegram_bot_token")

    if not token:
        print("❌ Token Bot Telegram tidak ditemukan!")
        print("Set TELEGRAM_BOT_TOKEN atau tambahkan 'telegram_bot_token' di config.json.")
        sys.exit(1)

    print("🤖 Auto Clipper Bot (Optimized Flow) berjalan...")

    # Telegram Bot API Server lokal (limit file naik dari 50MB → 2GB)
    # Telegram Bot API Server lokal (limit file naik dari 50MB → 2GB)
    _local_api = os.environ.get("TELEGRAM_BOT_API_URL", "http://127.0.0.1:8081/bot")
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=120.0,
        write_timeout=120.0,
        pool_timeout=30.0,
    )

    async def post_init(application):
        notify_chat_id = int(os.environ.get("STARTUP_NOTIFY_CHAT_ID", "0") or 0)
        if not notify_chat_id:
            try:
                cfg_file = Path(__file__).parent.resolve() / "config.json"
                if cfg_file.exists():
                    with open(cfg_file, "r", encoding="utf-8") as f:
                        notify_chat_id = int(json.load(f).get("startup_notify_chat_id") or 0)
            except Exception as e:
                logger.warning(f"Could not read startup chat id: {e}")
        if notify_chat_id:
            try:
                await application.bot.send_message(
                    chat_id=notify_chat_id,
                    text="🤖 *Auto Clipper Bot aktif!* Siap menerima link YouTube.",
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.warning(f"Startup notification failed: {e}")

        # Register bot command menu (the ☰ button next to input)
        from telegram import BotCommand
        commands = [
            BotCommand("start", "Selamat datang & mulai clip"),
            BotCommand("help", "Panduan penggunaan"),
            BotCommand("config", "Lihat & ubah konfigurasi aktif"),
            BotCommand("server", "Set server AI (base URL)"),
            BotCommand("model", "Daftar model AI tersedia"),
            BotCommand("fw", "Faster-Whisper (pilih & download model)"),
            BotCommand("cookies", "Kelola cookies YouTube (upload/cek/hapus)"),
            BotCommand("admin", "Menu admin (kelola ID)"),
            BotCommand("result", "Hasil klip terakhir"),
            BotCommand("cancel_session", "Batalkan proses yang sedang jalan"),
        ]
        await application.bot.set_my_commands(commands)
        logger.info("✅ Bot command menu registered")

    def register_handlers(application):
        application.add_handler(CommandHandler("start", _auth_required(start_command)))
        application.add_handler(CommandHandler("help", _auth_required(help_command)))
        application.add_handler(CommandHandler("config", _auth_required(config_command)))
        application.add_handler(CommandHandler("server", _auth_required(server_command)))
        application.add_handler(CommandHandler("status", _auth_required(status_command)))
        application.add_handler(CommandHandler("model", _auth_required(model_command)))
        application.add_handler(CommandHandler("fw", _auth_required(fw_command)))
        application.add_handler(CommandHandler("cookies", _auth_required(cookies_command)))
        application.add_handler(CommandHandler("watermark", _auth_required(watermark_command)))
        application.add_handler(CommandHandler("admin", admin_command))
        application.add_handler(CommandHandler("result", _auth_required(result_command)))
        application.add_handler(CommandHandler("cancel_session", _auth_required(cancel_session_command)))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _auth_required(process_youtube_url)))
        application.add_handler(MessageHandler(filters.Document.ALL, _auth_required(receive_cookies_document)))
        application.add_handler(CallbackQueryHandler(_auth_required(menu_main_callback), pattern=r"^menu_main$"))
        application.add_handler(CallbackQueryHandler(_auth_required(model_list_callback), pattern=r"^model_list$"))
        application.add_handler(CallbackQueryHandler(_auth_required(model_select_callback), pattern=r"^mdl_"))
        application.add_handler(CallbackQueryHandler(_auth_required(handle_button), pattern=r"^sel_"))
        application.add_handler(CallbackQueryHandler(_auth_required(server_callback), pattern=r"^sv_"))
        application.add_handler(CallbackQueryHandler(_auth_required(result_browse), pattern=r"^res_"))
        application.add_handler(CallbackQueryHandler(_auth_required(download_clip), pattern=r"^dl_"))
        application.add_handler(CallbackQueryHandler(_auth_required(clip_metadata), pattern=r"^meta_"))
        application.add_handler(CallbackQueryHandler(_auth_required(clip_social_kit), pattern=r"^soc_"))
        application.add_handler(CallbackQueryHandler(_auth_required(status_toggle), pattern=r"^st_"))
        application.add_handler(CallbackQueryHandler(_auth_required(cookies_menu), pattern=r"^ck_"))
        application.add_handler(CallbackQueryHandler(_auth_required(watermark_menu), pattern=r"^wm_"))
        application.add_handler(CallbackQueryHandler(_auth_required(fw_menu_callback), pattern=r"^fw_"))
        application.add_handler(CallbackQueryHandler(admin_menu_callback, pattern=r"^adm_"))
        application.add_handler(CallbackQueryHandler(_auth_required(handle_retry), pattern=r"^retry_phase1$"))
        application.add_handler(CallbackQueryHandler(_auth_required(handle_proc), pattern=r"^proc_"))
        application.add_error_handler(error_handler)

    RESTART_DELAY = 5
    attempt = 0
    while True:
        attempt += 1
        application = (
            Application.builder()
            .token(token)
            .base_url(_local_api)
            .request(request)
            .concurrent_updates(True)
            .post_init(post_init)
            .build()
        )
        register_handlers(application)
        try:
            logger.info(f"🤖 Bot mulai (attempt #{attempt})...")
            application.run_polling(drop_pending_updates=True)
            break  # keluar bersih (bot di-stop)
        except KeyboardInterrupt:
            logger.info("🛑 Bot dihentikan oleh user (Ctrl+C).")
            break
        except Exception as e:
            logger.exception(f"💥 Bot crash: {e}")
            logger.info(f"🔄 Restart otomatis dalam {RESTART_DELAY}s...")
            time.sleep(RESTART_DELAY)


if __name__ == "__main__":
    main()
