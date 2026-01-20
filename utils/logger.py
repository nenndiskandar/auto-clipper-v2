"""
Logging utilities for YT Short Clipper
"""

import sys


# Enable console logging when running from terminal (not frozen)
DEBUG_MODE = not getattr(sys, 'frozen', False)


def debug_log(msg):
    """Log to console only in debug mode (running from terminal)"""
    if DEBUG_MODE:
        print(f"[DEBUG] {msg}")
