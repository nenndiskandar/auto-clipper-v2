"""
Global button styling applied once at startup.

Patches CTkButton so that EVERY button in the app (all pages) gets:
  - font size scaled by SCALE (default 1.5 = 50% larger)
  - emoji / symbol characters stripped from the button text
  - image (and compound) removed when the button also has text
  - icon-only buttons ("←", "🗑️", ...) replaced with plain word labels
"""

import re
import customtkinter as ctk

SCALE = 1.1
DEFAULT_SIZE = 13  # theme default (Roboto 13)

# Emojis, arrows, dingbats and misc symbols used across the app
_SYMBOL_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # emoji blocks
    "\u2190-\u21FF"          # arrows
    "\u2600-\u27BF"          # misc symbols / dingbats
    "\u2B00-\u2BFF"          # arrow blocks
    "\uFE0F\u2764\u2728\u2B50\u2B55\u2705\u274C\u2757\u26A0\u25B6\u25C0\u24C2"
    "\u2795\u2796\u2714\u2716\u23F3\u2793"
    "]",
    re.UNICODE,
)

# Icon-only buttons keep a plain word label instead of becoming empty
_ICON_REPLACEMENTS = {
    "←": "Back",
    "→": "Next",
    "▶": "Play",
    "🗑": "Delete",
    "🗑️": "Delete",
    "🔄": "Refresh",
    "❌": "Close",
}


def _scaled_font(font):
    """Return a CTkFont with size scaled by SCALE."""
    size = DEFAULT_SIZE
    weight = "normal"
    if isinstance(font, ctk.CTkFont):
        try:
            size = int(font.cget("size"))
            weight = str(font.cget("weight"))
        except Exception:
            pass
    elif isinstance(font, (tuple, list)) and len(font) >= 2:
        try:
            size = int(font[1])
        except Exception:
            pass
    return ctk.CTkFont(size=max(10, int(round(size * SCALE))), weight=weight)


def _clean_text(text: str) -> str:
    cleaned = _SYMBOL_RE.sub("", str(text)).strip()
    if cleaned:
        return cleaned
    stripped = str(text).strip()
    return _ICON_REPLACEMENTS.get(stripped, "")


def apply():
    """Patch CTkButton so all buttons get scaled fonts and no icon clutter."""
    if getattr(ctk.CTkButton, "_button_style_applied", False):
        return
    ctk.CTkButton._button_style_applied = True

    _orig_init = ctk.CTkButton.__init__

    def _patched_init(self, master=None, **kwargs):
        keep_image = bool(kwargs.pop("_keep_image", False))
        text = kwargs.get("text")
        if text is not None and not keep_image:
            kwargs["text"] = _clean_text(str(text))
        if "image" in kwargs and kwargs.get("text") and not keep_image:
            kwargs.pop("image", None)
            kwargs.pop("compound", None)
        kwargs["font"] = _scaled_font(kwargs.get("font"))
        _orig_init(self, master, **kwargs)

    ctk.CTkButton.__init__ = _patched_init
