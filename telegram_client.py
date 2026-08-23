"""
Telegram CLIENT (userbot) interface for YT-Short-Clipper.

Mode ini TIDAK memakai Telegram Bot API / bot token (@BotFather).
Bot login sebagai akun Telegram pribadi (nomor HP) lewat MTProto (Telethon),
jadi cukup pakai api_id + api_hash gratis dari https://my.telegram.org.

Semua logika bisnis (highlight detection, render clip, session, cookies, dll)
di-reuse dari telegram_bot.py melalui adapter, sehingga fitur tetap identik:
  /start /help /config /status /cookies /result /cancel_session

Cara pakai:
  1. Dapatkan api_id & api_hash di https://my.telegram.org (login → API development tools).
  2. Isi di config.json: telegram_api_id, telegram_api_hash, telegram_phone
     (atau set env TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_PHONE).
  3. Jalankan:  python telegram_client.py
     - Pertama kali akan minta kode verifikasi yang dikirim Telegram ke akunmu.
     - Sesion disimpan (ytclipper_client.session) — login berikutnya otomatis.
"""

import os
import re
import sys
import json
import time
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Dict

from telethon import TelegramClient, events, Button
from telethon.tl.types import KeyboardButtonCallback

# Reuse semua logika bot lama (fungsi murni + handler PTB-style).
# Aman karena main() di telegram_bot.py di-guard `if __name__ == "__main__"`.
from telegram_bot import (
    MAIN_MENU_TEXT,
    start_command,
    help_command,
    config_command,
    status_command,
    cookies_command,
    result_command,
    cancel_session_command,
    process_youtube_url,
    receive_cookies_document,
    menu_main_callback,
    handle_button,
    result_browse,
    download_clip,
    status_toggle,
    cookies_menu,
    handle_retry,
)

logger = logging.getLogger("telegram_client")

# ---------------------------------------------------------------------------
# Adapter: tiru objek python-telegram-bot (Update/Context/Message/...)
# di atas Telethon, supaya handler di telegram_bot.py bisa dipakai apa adanya.
# ---------------------------------------------------------------------------

_MD2_BOLD = re.compile(r"\*([^*\n]+)\*")


def _conv_text(text, parse_mode):
    """Konversi parse_mode PTB legacy 'Markdown' -> Telethon md.

    PTB legacy Markdown memakai *bold*, sedangkan Telethon memakai **bold**.
    Untuk 'HTML' dibiarkan apa adanya.
    """
    if text is None:
        return None
    pm = (parse_mode or "").lower()
    if pm in ("html",):
        return text
    return _MD2_BOLD.sub(r"**\1**", text)


def _tg_parse_mode(parse_mode):
    pm = (parse_mode or "").lower()
    return "html" if pm == "html" else "md"


class ReplyMarkupConverter:
    """Ubah InlineKeyboardMarkup python-telegram-bot -> list of lists Telethon.

    Telethon menerima tombol inline sebagai daftar baris berisi
    KeyboardButtonCallback (sama dengan hasil Button.inline(...)), dan
    otomatis mengubahnya jadi ReplyInlineMarkup saat kirim/edit.
    """

    @staticmethod
    def convert(markup):
        if markup is None:
            return None
        rows = None
        if hasattr(markup, "inline_keyboard"):
            rows = markup.inline_keyboard
        elif isinstance(markup, (list, tuple)):
            rows = markup
        if not rows:
            return None
        kb = []
        for row in rows:
            btns = []
            for b in row:
                cb = getattr(b, "callback_data", None)
                if not cb:
                    continue
                btns.append(KeyboardButtonCallback(text=b.text, data=cb.encode("utf-8")))
            if btns:
                kb.append(btns)
        return kb if kb else None

    @staticmethod
    def to_reply(markup):
        """Ubah InlineKeyboardMarkup -> (reply_keyboard Telethon, mapping label->data).

        Userbot (akun pribadi via MTProto) TIDAK bisa mengirim inline keyboard
        (Telegram membuangnya). Solusinya pakai reply keyboard (on-screen) yang
        memang didukung akun pribadi; saat tombol ditekan, teks tombol dikirim
        sebagai pesan dan dipetakan kembali ke callback_data lewat `mapping`.
        """
        if markup is None:
            return None, None
        rows = None
        if hasattr(markup, "inline_keyboard"):
            rows = markup.inline_keyboard
        elif isinstance(markup, (list, tuple)):
            rows = markup
        if not rows:
            return None, None
        kb = []
        mapping = {}
        for row in rows:
            btns = []
            for b in row:
                label = getattr(b, "text", None)
                cb = getattr(b, "callback_data", None)
                if not label:
                    continue
                btns.append(Button.text(label, single_use=True, resize=True))
                if cb:
                    mapping[str(label)] = cb
            if btns:
                kb.append(btns)
        if not kb:
            return None, None
        return kb, mapping


class DocAdapter:
    """Tiruan telegram.Document untuk receive_cookies_document."""

    def __init__(self, client, msg):
        self._client = client
        self._msg = msg
        self.file_name = ""
        self.file_size = 0
        try:
            f = msg.file
            self.file_name = getattr(f, "name", "") or ""
            self.file_size = getattr(f, "size", 0) or 0
        except Exception:
            pass

    def get_file(self):
        return self

    async def download_to_drive(self, custom_path):
        await self._client.download_media(self._msg, file=custom_path)


class MsgAdapter:
    """Tiruan telegram.Message (edit_text / reply_text / text / document)."""

    def __init__(self, client, msg, chat_id):
        self._client = client
        self._msg = msg
        self.chat_id = chat_id
        self.msg_id = getattr(msg, "id", None)

    @property
    def text(self):
        try:
            return self._msg.text if self._msg is not None else None
        except Exception:
            return None

    @property
    def document(self):
        try:
            if self._msg is not None and getattr(self._msg, "document", None) is not None:
                return DocAdapter(self._client, self._msg)
        except Exception:
            pass
        return None

    async def edit_text(self, text, parse_mode=None, reply_markup=None,
                        disable_web_page_preview=None):
        kb, mapping = ReplyMarkupConverter.to_reply(reply_markup)
        if kb:
            CHAT_BUTTONS[self.chat_id] = mapping
        txt = _conv_text(text, parse_mode)
        pm = _tg_parse_mode(parse_mode)
        if self._msg is not None:
            await self._msg.edit(text=txt, parse_mode=pm, buttons=kb, link_preview=False)
        elif self.msg_id is not None:
            await self._client.edit_message(
                self.chat_id, self.msg_id, txt, parse_mode=pm, buttons=kb, link_preview=False
            )

    async def reply_text(self, text, parse_mode=None, reply_markup=None,
                         disable_web_page_preview=None):
        kb, mapping = ReplyMarkupConverter.to_reply(reply_markup)
        if kb:
            CHAT_BUTTONS[self.chat_id] = mapping
        txt = _conv_text(text, parse_mode)
        pm = _tg_parse_mode(parse_mode)
        if self._msg is not None:
            sent = await self._msg.reply(txt, parse_mode=pm, buttons=kb, link_preview=False)
        else:
            sent = await self._client.send_message(
                self.chat_id, txt, parse_mode=pm, buttons=kb, link_preview=False
            )
        return MsgAdapter(self._client, sent, self.chat_id)


class CallbackAdapter:
    """Tiruan telegram.CallbackQuery (answer / data / edit_message_text / message)."""

    def __init__(self, client, event):
        self._client = client
        self._event = event
        raw = event.data
        self.data = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else str(raw)
        self.message = MsgAdapter(client, event.message, event.chat_id)

    async def answer(self, text=None, show_alert=False):
        try:
            await self._event.answer(text, alert=bool(show_alert))
        except Exception as e:
            logger.warning(f"callback answer failed: {e}")

    async def edit_message_text(self, text, parse_mode=None, reply_markup=None,
                                disable_web_page_preview=None):
        kb, mapping = ReplyMarkupConverter.to_reply(reply_markup)
        if kb and self.message.chat_id is not None:
            CHAT_BUTTONS[self.message.chat_id] = mapping
        txt = _conv_text(text, parse_mode)
        pm = _tg_parse_mode(parse_mode)
        try:
            await self._event.edit(text=txt, parse_mode=pm, buttons=kb, link_preview=False)
        except Exception:
            # Fallback: edit lewat id pesan
            if self.message.msg_id is not None:
                await self._client.edit_message(
                    self.message.chat_id, self.message.msg_id, txt,
                    parse_mode=pm, buttons=kb, link_preview=False,
                )
            else:
                raise


class _TextCallbackEvent:
    """Event tiruan: pesan teks dari tap reply keyboard -> dianggap CallbackQuery.

    Userbot tidak bisa menerima callback asli (inline keyboard dibuang Telegram),
    jadi tombol reply keyboard mengirim teks; kita petakan teks itu ke
    callback_data lewat CHAT_BUTTONS dan tirukan event CallbackQuery.
    """

    _ytclipper_cb = True

    def __init__(self, message_event, data: str):
        self._msg_event = message_event
        self.data = data.encode("utf-8")
        self.chat_id = message_event.chat_id
        self.message = message_event.message

    async def answer(self, text=None, show_alert=False):
        return None

    async def edit(self, *args, **kwargs):
        return await self._msg_event.edit(*args, **kwargs)


class UpdateAdapter:
    """Tiruan telegram.Update (message / callback_query / effective_chat)."""

    def __init__(self, client, event):
        self.message = None
        self.callback_query = None
        self.effective_chat = SimpleNamespace(id=event.chat_id)
        if isinstance(event, events.CallbackQuery.Event) or getattr(event, "_ytclipper_cb", False):
            self.callback_query = CallbackAdapter(client, event)
        else:
            self.message = MsgAdapter(client, event.message, event.chat_id)


class BotAdapter:
    """Tiruan application.bot (send_message / send_video / send_chat_action)."""

    def __init__(self, client):
        self._client = client

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None,
                           disable_web_page_preview=None):
        kb, mapping = ReplyMarkupConverter.to_reply(reply_markup)
        if kb:
            CHAT_BUTTONS[chat_id] = mapping
        msg = await self._client.send_message(
            chat_id,
            _conv_text(text, parse_mode),
            parse_mode=_tg_parse_mode(parse_mode),
            buttons=kb,
            link_preview=False,
        )
        return MsgAdapter(self._client, msg, chat_id)

    async def send_video(self, chat_id, video, filename=None, caption=None,
                         supports_streaming=False, read_timeout=None, write_timeout=None):
        timeout = max(int(read_timeout or 60), int(write_timeout or 60))
        msg = await self._client.send_file(
            chat_id,
            video,
            caption=caption,
            supports_streaming=bool(supports_streaming),
            timeout=timeout,
        )
        return MsgAdapter(self._client, msg, chat_id)

    async def send_chat_action(self, chat_id, action="typing"):
        try:
            from telethon.tl.functions.messages import SetTypingRequest
            from telethon.tl.types import SendMessageTypingAction
            await self._client(SetTypingRequest(peer=chat_id, action=SendMessageTypingAction()))
        except Exception as e:
            logger.warning(f"send_chat_action failed: {e}")


class Ctx:
    """Tiruan ContextTypes.DEFAULT_TYPE (bot / user_data / args)."""

    def __init__(self, client, user_data, args=None):
        self.bot = BotAdapter(client)
        self.user_data = user_data
        self.args = args or []


USER_DATA: Dict[int, dict] = {}
client: TelegramClient = None  # diisi saat _run()

# Mapping terakhir per chat: label tombol reply keyboard -> callback_data.
# Dipakai untuk mengubah pesan teks hasil tap tombol jadi callback.
CHAT_BUTTONS: Dict[int, dict] = {}


# ---------------------------------------------------------------------------
# Dispatcher (mirror register_handlers di telegram_bot.py)
# ---------------------------------------------------------------------------

COMMAND_HANDLERS = {
    "/start": start_command,
    "/help": help_command,
    "/config": config_command,
    "/status": status_command,
    "/cookies": cookies_command,
    "/result": result_command,
    "/cancel_session": cancel_session_command,
}


async def _on_new_message(event):
    chat_id = event.chat_id
    ctx = Ctx(client, USER_DATA.setdefault(chat_id, {}), [])
    try:
        # Dokumen (upload cookies) -> receive_cookies_document
        if event.message.document is not None:
            await receive_cookies_document(UpdateAdapter(client, event), ctx)
            return

        text = (event.raw_text or "").strip()
        if not text:
            return

        # Tombol reply keyboard: teks cocok dengan callback terakhir -> tirukan callback
        mapping = CHAT_BUTTONS.get(chat_id)
        if mapping and text in mapping:
            data = mapping.pop(text)
            fake = _TextCallbackEvent(event, data)
            await _on_callback(fake)
            return

        # Bersihkan mapping lama agar label tombol kadaluarsa tak memicu lagi
        CHAT_BUTTONS.pop(chat_id, None)

        if text.startswith("/"):
            parts = text.split()
            cmd = parts[0].split("@")[0].lower()
            ctx.args = parts[1:]
            handler = COMMAND_HANDLERS.get(cmd)
            if handler:
                await handler(UpdateAdapter(client, event), ctx)
            else:
                await event.reply("❌ Perintah tidak dikenal. Ketik /start untuk menu.")
        else:
            # Link video -> process_youtube_url
            await process_youtube_url(UpdateAdapter(client, event), ctx)
    except Exception as e:
        logger.exception("Error handling message")
        try:
            await event.reply(f"❌ Error: {str(e)[:200]}")
        except Exception:
            pass


async def _on_callback(event):
    chat_id = event.chat_id
    update = UpdateAdapter(client, event)
    ctx = Ctx(client, USER_DATA.setdefault(chat_id, {}), [])
    try:
        data = update.callback_query.data
        if data == "menu_main":
            await menu_main_callback(update, ctx)
        elif data.startswith("sel_"):
            await handle_button(update, ctx)
        elif data.startswith("res_"):
            await result_browse(update, ctx)
        elif data.startswith("dl_"):
            await download_clip(update, ctx)
        elif data.startswith("st_"):
            await status_toggle(update, ctx)
        elif data.startswith("ck_"):
            await cookies_menu(update, ctx)
        elif data.startswith("retry_phase1"):
            await handle_retry(update, ctx)
        else:
            await update.callback_query.answer()
    except Exception as e:
        logger.exception("Error handling callback")
        try:
            await event.answer("Terjadi error: " + str(e)[:100], alert=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Kredensial & main loop
# ---------------------------------------------------------------------------

def _load_config() -> tuple:
    cfg_file = Path(__file__).parent.resolve() / "config.json"
    cfg = {}
    if cfg_file.exists():
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    return cfg_file, cfg


def _ensure_credentials(cfg_file: Path, cfg: dict):
    """Baca api_id/hash/phone dari env atau config.json; jika kosong, minta di console."""
    api_id = os.environ.get("TELEGRAM_API_ID") or cfg.get("telegram_api_id") or ""
    api_hash = os.environ.get("TELEGRAM_API_HASH") or cfg.get("telegram_api_hash") or ""
    phone = os.environ.get("TELEGRAM_PHONE") or cfg.get("telegram_phone") or ""

    changed = False
    if not api_id or not api_hash:
        print("=" * 64)
        print("🔑 Kamu butuh Telegram API ID & API Hash (GRATIS).")
        print("   1. Buka https://my.telegram.org lalu login dengan nomor HP")
        print("   2. Klik menu 'API development tools'")
        print("   3. Isi form (bebas), lalu salin 'api_id' dan 'api_hash'")
        print("=" * 64)
        if not api_id:
            api_id = input("api_id   : ").strip()
        if not api_hash:
            api_hash = input("api_hash : ").strip()
        cfg["telegram_api_id"] = api_id
        cfg["telegram_api_hash"] = api_hash
        changed = True

    if not phone:
        phone = input("Nomor HP Telegram (format internasional, mis. +6281234567890): ").strip()
        cfg["telegram_phone"] = phone
        changed = True

    if changed:
        try:
            with open(cfg_file, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
            logger.info("✅ Kredensial disimpan ke config.json")
        except Exception as e:
            logger.warning(f"Gagal simpan kredensial: {e}")

    return str(api_id), api_hash, phone


async def _send_startup_notify(cfg: dict):
    notify = cfg.get("startup_notify_chat_id")
    if not notify:
        return
    try:
        await client.send_message(
            int(notify),
            "🤖 **YT-Short-Clipper Client aktif!** (userbot, tanpa Bot API)\n"
            "Siap menerima link video. Ketik /start untuk menu.",
            parse_mode="md",
        )
    except Exception as e:
        logger.warning(f"Startup notification failed: {e}")


async def _run(session_path: str, api_id: str, api_hash: str, phone: str):
    global client
    client = TelegramClient(
        session_path,
        int(api_id),
        api_hash,
        device_model="Desktop",
        app_version="1.0.0",
        lang_code="en",
    )

    await client.start(
        phone=lambda: phone or input("Nomor HP Telegram: ").strip(),
        code_callback=lambda: input("📨 Kode verifikasi dari Telegram: ").strip(),
        password=lambda: input("🔒 Password 2FA (Enter jika tidak ada): ").strip() or None,
    )

    me = await client.get_me()
    logger.info(f"✅ Login sukses sebagai {getattr(me, 'first_name', '')} (@{getattr(me, 'username', '')})")
    logger.info("🤖 YT-Short-Clipper Telegram Client berjalan (userbot, tanpa Bot API)...")
    print("🤖 Client aktif! Kirim link video ke chat Telegrammu (Saved Messages / chat pribadi).")

    client.add_event_handler(_on_new_message, events.NewMessage(incoming=True))
    client.add_event_handler(_on_callback, events.CallbackQuery())

    _cfg_file, _cfg = _load_config()
    await _send_startup_notify(_cfg)

    await client.run_until_disconnected()


def main():
    cfg_file, cfg = _load_config()
    api_id, api_hash, phone = _ensure_credentials(cfg_file, cfg)
    session_name = cfg.get("telegram_session", "ytclipper_client")
    session_path = str(Path(__file__).parent.resolve() / session_name)

    print("🚀 Menghubungkan ke Telegram (MTProto)...")

    RESTART_DELAY = 5
    attempt = 0
    while True:
        attempt += 1
        logger.info(f"🤖 Telegram Client mulai (attempt #{attempt})...")
        try:
            asyncio.run(_run(session_path, api_id, api_hash, phone))
            break  # keluar bersih
        except KeyboardInterrupt:
            logger.info("🛑 Client dihentikan oleh user (Ctrl+C).")
            break
        except Exception as e:
            logger.exception(f"💥 Client crash: {e}")
            logger.info(f"🔄 Restart otomatis dalam {RESTART_DELAY}s...")
            time.sleep(RESTART_DELAY)


if __name__ == "__main__":
    main()
