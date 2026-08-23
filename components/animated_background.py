"""
Animated background color drift for the app.

Tk frames are opaque, so a real "see-through" gradient canvas is not possible
on top of CTk frames. Instead this animates the fg_color of every registered
background-role widget (container, page roots, transparent content frames) with
a smoothly drifting color sampled from a dark gradient palette. The whole
background area therefore flows through the gradient colors over time.
"""

from customtkinter import CTkBaseClass

# White/light palette: near-white base with a subtle neutral drift so the
# animated background stays clean and light across all pages.
DEFAULT_PALETTE = [
    "#ffffff",  # base white
    "#fbfcfd",  # faint cool white
    "#fdfdfd",  # neutral white
    "#f9fafb",  # faint gray-white
    "#fefefe",  # pure white
]


def _hex_to_rgb(hex_color: str):
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _rgb_to_hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(c))) for c in rgb)


def _lerp(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def _smooth(t: float) -> float:
    return t * t * (3 - 2 * t)


class AnimatedBackground:
    """Drives a slowly shifting background color on registered widgets."""

    def __init__(self, master, palette: list = None, cycle_seconds: float = 12.0,
                 tick_ms: int = 50):
        self.master = master
        palette = palette or DEFAULT_PALETTE
        self.palette_rgb = [_hex_to_rgb(c) for c in palette]
        self.n_colors = len(self.palette_rgb)
        self.cycle_seconds = max(2.0, float(cycle_seconds))
        self.tick_ms = max(16, int(tick_ms))
        self._t = 0.0
        self._widgets = set()
        self._ticking = False

    # -- registration ------------------------------------------------------

    def attach(self, widget):
        """Register a CTk widget whose fg_color follows the animated color."""
        if isinstance(widget, CTkBaseClass):
            self._widgets.add(widget)

    def detach(self, widget):
        self._widgets.discard(widget)

    def clear(self):
        self._widgets.clear()

    def clear_attached(self):
        """Detach everything (used when switching pages)."""
        self._widgets.clear()

    def attach_transparent_children(self, widget, max_depth: int = 4):
        """Attach all CTk descendants whose fg_color is 'transparent' so they
        follow the animated background color too."""
        queue = [(widget, 0)]
        while queue:
            parent, depth = queue.pop(0)
            if depth >= max_depth:
                continue
            for child in parent.winfo_children():
                try:
                    if isinstance(child, CTkBaseClass) and child.cget("fg_color") == "transparent":
                        self.attach(child)
                        queue.append((child, depth + 1))
                except Exception:
                    continue

    # -- animation loop -----------------------------------------------------

    def start(self):
        if not self._ticking:
            self._ticking = True
            self._tick()

    def stop(self):
        self._ticking = False

    def _tick(self):
        if not self._ticking:
            return
        try:
            color = self._current_color()
            for w in list(self._widgets):
                try:
                    if w.winfo_exists():
                        w.configure(fg_color=color)
                except Exception:
                    self._widgets.discard(w)
            self._t += self.tick_ms / 1000.0
            self.master.after(self.tick_ms, self._tick)
        except Exception:
            self._ticking = False

    def _current_color(self) -> str:
        """Sample the palette with a smooth, looping progression."""
        if self.n_colors == 1:
            return _rgb_to_hex(self.palette_rgb[0])
        pos = (self._t / self.cycle_seconds) % 1.0
        x = pos * self.n_colors
        idx = min(int(x), self.n_colors - 1)
        frac = _smooth(x - idx)
        nxt = (idx + 1) % self.n_colors
        return _rgb_to_hex(_lerp(self.palette_rgb[idx], self.palette_rgb[nxt], frac))
