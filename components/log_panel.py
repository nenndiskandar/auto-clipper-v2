"""
Collapsible scrollable log console panel (right side).
Displays application logs/debug output in a space-saving side panel.
"""

import re

import customtkinter as ctk
from datetime import datetime

# Map ANSI SGR color codes -> hex colors visible on both light and dark themes
ANSI_COLORS = {
    "30": "#6e6e6e",  # black/gray
    "31": "#e5484d",  # red
    "32": "#46a758",  # green
    "33": "#f5a524",  # yellow
    "34": "#3e63dd",  # blue
    "35": "#d6409f",  # magenta
    "36": "#0ea5e9",  # cyan
    "37": "#d4d4d4",  # white
    "90": "#9e9e9e",  # bright black
    "91": "#ff6369",  # bright red
    "92": "#66d9a1",  # bright green
    "93": "#ffd60a",  # bright yellow
    "94": "#93b4ff",  # bright blue
    "95": "#ff9ef5",  # bright magenta
    "96": "#4dd8ff",  # bright cyan
    "97": "#f2f2f2",  # bright white
}

ANSI_RE = re.compile(r"\x1b\[([0-9;]*)m")


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from a string."""
    return ANSI_RE.sub("", text)


class LogPanel(ctk.CTkFrame):
    """Collapsible scrollable console that streams log messages.

    Collapsed: a slim vertical strip on the right edge with a toggle button.
    Expanded: a ~320px wide panel with a scrollable read-only log textbox.
    """

    # Rotating color palette for plain log lines (no ANSI step-color).
    # Each non-ANSI line gets the next color, so every line is visually distinct.
    LINE_COLORS = [
        "#d4d4d4",  # white
        "#7dd3fc",  # sky blue
        "#a7f3d0",  # mint green
        "#fcd34d",  # amber
        "#f9a8d4",  # pink
        "#c4b5fd",  # violet
        "#67e8f9",  # cyan
        "#fca5a5",  # coral
        "#86efac",  # green
        "#fdba74",  # orange
    ]
    _TIMESTAMP_COLOR = "#6b6b6b"

    def __init__(self, parent, expanded_width: int = 320, max_lines: int = 800):
        super().__init__(parent, fg_color=("gray85", "#111114"), corner_radius=5)
        self.expanded_width = expanded_width
        self.max_lines = max_lines
        self._expanded = False
        self._pending = []  # buffered while not yet realized on screen
        self._realized = False
        self._line_idx = 0  # rotating color counter for plain lines
        # Fixed set of rotating line tags (reused per line to avoid tag bloat)
        self._line_tags = [f"line_{i}" for i in range(len(self.LINE_COLORS))]

        self._create_header()
        self._create_body()
        self.set_expanded(True)

        self.after_idle(self._on_realized)

    def _create_header(self):
        """Header strip with toggle, count and clear controls."""
        self.header = ctk.CTkFrame(self, fg_color="transparent", height=30)
        self.header.pack(fill="x", padx=(4, 4), pady=(4, 4))
        self.header.pack_propagate(False)

        self.toggle_btn = ctk.CTkButton(
            self.header,
            text="Console",
            font=ctk.CTkFont(size=11, weight="bold"),
            height=18,
            fg_color=("gray75", "#2a2a30"),
            hover_color=("gray65", "#3a3a40"),
            text_color=("gray15", "#e0e0e0"),
            corner_radius=5,
            command=self.toggle,
        )
        self.toggle_btn.pack(side="left")

        self.count_label = ctk.CTkLabel(
            self.header,
            text="0 lines",
            font=ctk.CTkFont(size=11),
            text_color=("gray30", "#8a8a8a"),
        )
        self.count_label.pack(side="left", padx=(4, 0))

        self.clear_btn = ctk.CTkButton(
            self.header,
            text="Clear",
            font=ctk.CTkFont(size=11),
            height=18,
            width=62,
            fg_color=("gray75", "#2a2a30"),
            hover_color=("gray65", "#3a3a40"),
            text_color=("gray15", "#e0e0e0"),
            corner_radius=5,
            command=self.clear,
        )
        self.clear_btn.pack(side="right")

    def _create_body(self):
        """Scrollable read-only textbox for the log history."""
        self.body = ctk.CTkFrame(self, fg_color=("gray90", "#0b0b0c"), border_width=1, border_color=("#2a2a30", "#2a2a30"))
        self.body.pack(fill="both", expand=True)

        self.textbox = ctk.CTkTextbox(
            self.body,
            fg_color=("gray90", "#0b0b0c"),
            text_color=("gray10", "#d4d4d4"),
            font=ctk.CTkFont(family="Consolas", size=11),
            wrap="word",
            border_width=0,
            state="disabled",
        )
        self.textbox.pack(fill="both", expand=True, padx=4, pady=(4, 4))

    def _on_realized(self):
        """Flush buffered lines once the widget exists on screen."""
        self._realized = True
        if self._pending:
            lines = self._pending
            self._pending = []
            for line in lines:
                self._append_safe(line)

    def append(self, msg: str):
        """Thread-safe: append a log line (called from any thread)."""
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        if not self._realized:
            self._pending.append(line)
            return
        try:
            self.after(0, lambda: self._append_safe(line))
        except Exception:
            pass

    def _append_safe(self, line: str):
        """Append on the Tk main thread with ANSI color tags + per-line rotation, auto-scroll."""
        try:
            self.textbox.configure(state="normal")
            # Split timestamp prefix [HH:MM:SS] from message body.
            ts_match = re.match(r'^\[([^\]]+)\]\s*(.*)', line, re.DOTALL)
            if ts_match:
                ts_part = f"[{ts_match.group(1)}] "
                msg_part = ts_match.group(2)
            else:
                ts_part = ""
                msg_part = line

            # Insert timestamp in gray
            if ts_part:
                self._insert_colored(ts_part, "_ts")

            # Determine if message already has ANSI step-colors
            parts = list(ANSI_RE.finditer(msg_part))
            if parts:
                # ANSI-colored segments (step colors)
                pos = 0
                current_tag = None
                for m in parts:
                    before = msg_part[pos:m.start()]
                    if before:
                        self._insert_colored(before, current_tag)
                    code = m.group(1)
                    if code == "0" or code == "":
                        current_tag = None
                    elif code in ANSI_COLORS:
                        current_tag = f"ansi_{code}"
                    pos = m.end()
                tail = msg_part[pos:]
                if tail:
                    self._insert_colored(tail, current_tag)
            else:
                # No ANSI: apply rotating line color
                line_tag = self._next_line_tag()
                self._insert_colored(msg_part, line_tag)

            self.textbox.insert("end", "\n")
            self.textbox.see("end")
            self._trim_lines()
            self._update_count()
            self.textbox.configure(state="disabled")
        except Exception:
            pass

    def _next_line_tag(self) -> str:
        """Return a tag name for the next rotating line color and advance the counter."""
        tag = self._line_tags[self._line_idx % len(self.LINE_COLORS)]
        color = self.LINE_COLORS[self._line_idx % len(self.LINE_COLORS)]
        self._line_idx += 1
        if tag not in self.textbox.tag_names():
            self.textbox.tag_config(tag, foreground=color)
        return tag

    def _insert_colored(self, text: str, tag: str):
        """Insert text, creating the color tag on first use."""
        self.textbox.insert("end", text)
        if tag and text:
            if tag not in self.textbox.tag_names():
                if tag == "_ts":
                    color = self._TIMESTAMP_COLOR
                elif tag.startswith("ansi_"):
                    color = ANSI_COLORS.get(tag.replace("ansi_", ""), "#d4d4d4")
                else:
                    color = "#d4d4d4"
                self.textbox.tag_config(tag, foreground=color)
            start = "end-%dc" % len(text)
            self.textbox.tag_add(tag, start, "end")

    def _trim_lines(self):
        """Keep the textbox under max_lines to stay responsive."""
        try:
            line_count = int(self.textbox.index("end-1c").split(".")[0])
            if line_count > self.max_lines:
                remove_up_to = line_count - self.max_lines
                self.textbox.delete("1.0", f"{remove_up_to + 1}.0")
        except Exception:
            pass

    def _update_count(self):
        try:
            line_count = int(self.textbox.index("end-1c").split(".")[0])
            self.count_label.configure(text=f"{line_count} line{'s' if line_count != 1 else ''}")
        except Exception:
            pass

    def clear(self):
        """Clear all log lines."""
        try:
            self.textbox.configure(state="normal")
            self.textbox.delete("1.0", "end")
            self.textbox.configure(state="disabled")
            self.count_label.configure(text="0 lines")
        except Exception:
            pass

    def toggle(self):
        """Expand or collapse the panel."""
        self.set_expanded(not self._expanded)

    def set_expanded(self, expanded: bool):
        """Show or hide the log body to save space."""
        self._expanded = expanded
        if expanded:
            self.configure(width=self.expanded_width)
            self.body.pack(fill="both", expand=True)
            self.toggle_btn.configure(text="Console", width=86)
            self.count_label.pack(side="left", padx=(4, 0))
            self.clear_btn.pack(side="right")
            try:
                self.textbox.see("end")
            except Exception:
                pass
        else:
            self.body.pack_forget()
            self.configure(width=34)
            self.toggle_btn.configure(text="", width=22)
            self.count_label.pack_forget()
            self.clear_btn.pack_forget()
        self.pack_propagate(False)
