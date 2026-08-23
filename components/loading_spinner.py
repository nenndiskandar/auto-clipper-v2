"""
Animated loading spinner component (character-based, theme-safe)
"""

import customtkinter as ctk

FRAMES = ["◐", "◓", "◑", "◒"]


class LoadingSpinner(ctk.CTkLabel):
    """A rotating loading spinner label. Start/stop the animation manually."""

    def __init__(self, parent, size: int = 16, color=("#00A878", "#007A56"), **kwargs):
        super().__init__(parent, text=FRAMES[0],
                         font=ctk.CTkFont(size=size, weight="bold"),
                         text_color=color, **kwargs)
        self._index = 0
        self._after_id = None
        self._running = False

    def start(self, interval_ms: int = 100):
        """Start the spinner animation (idempotent)."""
        if self._running:
            return
        self._running = True
        self._tick(interval_ms)

    def _tick(self, interval_ms: int):
        if not self._running:
            return
        self.configure(text=FRAMES[self._index % len(FRAMES)])
        self._index += 1
        self._after_id = self.after(interval_ms, lambda: self._tick(interval_ms))

    def stop(self):
        """Stop the spinner animation (safe to call when not running)."""
        self._running = False
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
