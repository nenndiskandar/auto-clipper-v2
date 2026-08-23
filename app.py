"""
YT Short Clipper Desktop App
"""

import customtkinter as ctk
import threading
import json
import os
import sys
import subprocess
import re
import urllib.request
import io
from pathlib import Path
from tkinter import filedialog, messagebox
from openai import OpenAI
from PIL import Image, ImageTk
from clipper_core import AutoClipperCore

# Import version info
from version import __version__, UPDATE_CHECK_URL

# Import utilities
from utils.helpers import get_app_dir, get_bundle_dir, get_ffmpeg_path, get_ytdlp_path, extract_video_id
from utils.logger import debug_log, setup_error_logging, log_error, get_error_log_path, set_log_sink, strip_ansi
from config.config_manager import ConfigManager
from dialogs.model_selector import SearchableModelDropdown
from dialogs.youtube_upload import YouTubeUploadDialog
#from dialogs.terms_of_service import TermsOfServiceDialog
#from dialogs.autoklip_promo import AutoKlipPromoDialog
from components.progress_step import ProgressStep
from components.log_panel import LogPanel
from components.animated_background import AnimatedBackground
from pages.settings_page import SettingsPage
from pages.browse_page import BrowsePage
from pages.results_page import ResultsPage
from pages.status_pages import APIStatusPage, LibStatusPage
from pages.processing_page import ProcessingPage
from pages.clipping_page import ClippingPage
from pages.contact_page import ContactPage
from pages.highlight_selection_page import HighlightSelectionPage
from pages.session_browser_page import SessionBrowserPage

# Fix for PyInstaller windowed mode (console=False)
# When built with console=False, sys.stdout and sys.stderr are None
# This causes 'NoneType' object has no attribute 'flush' errors
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w')

APP_DIR = get_app_dir()
BUNDLE_DIR = get_bundle_dir()

# Setup error logging to file (for production builds)
setup_error_logging(APP_DIR)

CONFIG_FILE = APP_DIR / "config.json"
OUTPUT_DIR = APP_DIR / "output"
ASSETS_DIR = BUNDLE_DIR / "assets"
ICON_PATH = ASSETS_DIR / "icon.png"
ICON_ICO_PATH = ASSETS_DIR / "icon.ico"
COOKIES_FILE = APP_DIR / "cookies.txt"  # NEW: Cookies file path


class YTShortClipperApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        
        self.config = ConfigManager(CONFIG_FILE, OUTPUT_DIR)
        self.client = None
        self.current_thumbnail = None
        self.processing = False
        self.cancelled = False
        self.token_usage = {"gpt_input": 0, "gpt_output": 0, "whisper_seconds": 0, "tts_chars": 0}
        self.youtube_connected = False
        self.youtube_channel = None
        self.ytdlp_path = get_ytdlp_path()  # NEW: Store yt-dlp path for subtitle fetching
        self.cookies_path = COOKIES_FILE  # NEW: Store cookies path
        
        # Session data for highlight selection flow
        self.session_data = None  # Will store result from find_highlights_only
        self._retry_context = None  # (url, num_clips, output_dir, model, subtitle_lang, title, session_dir)
        
        self.title("YT Short Clipper")
        self.geometry("960x640")
        self.minsize(880, 540)
        self.resizable(True, True)
        
        ctk.set_appearance_mode("light")
        theme_file = ASSETS_DIR / "theme_lokaclip.json"
        if theme_file.exists():
            ctk.set_default_color_theme(str(theme_file))
        else:
            ctk.set_default_color_theme("blue")

        # Global button styling: consistent fonts, no icons/emoji on buttons
        from components.button_style import apply as apply_button_style
        apply_button_style()
        
        # Set app icon after window is created
        self.after(200, self.set_app_icon)
        
        # Animated background color drift (drives fg_color of background frames)
        self.animated_bg = AnimatedBackground(self)
        self.animated_bg.start()

        # Left sidebar navigation
        self.sidebar = ctk.CTkFrame(self, width=210, fg_color=("#ffffff", "#ffffff"),
            border_width=0)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        self.create_sidebar()

        # In-app scrollable log console (collapsible right-side panel).
        # Packed BEFORE the container so the page area takes the remaining space.
        self.log_panel = LogPanel(self)
        self.log_panel.pack(side="right", fill="y")
        set_log_sink(self.log_panel.append)
        self.bind("<Control-l>", lambda e: self.toggle_log_panel())
        self.log_panel.append(f"YT Short Clipper v{__version__} started")

        self.container = ctk.CTkFrame(self, fg_color="transparent")
        self.container.pack(fill="both", expand=True)
        self.animated_bg.attach(self.container)
        
        self.pages = {}
        self.create_home_page()
        self.create_processing_page()
        self.create_clipping_page()
        self.create_highlight_selection_page()
        self.create_session_browser_page()
        self.create_results_page()
        self.create_browse_page()
        self.create_settings_page()
        self.create_api_status_page()
        self.create_lib_status_page()
        self.create_contact_page()
        
        self.show_page("home")
        self.load_config()
        self.check_youtube_status()
        
        # Update start button state based on cookies
        self.update_start_button_state()
        
        # Check for updates on startup
        threading.Thread(target=self.check_update_silent, daemon=True).start()
        
        # Show Terms of Service if not yet accepted
        if not self.config.get("tos_accepted", False):
            self.after(300, self._show_tos_dialog)
        else:
            # ToS already accepted in a previous session — show AutoKlip promo
            # one time only.
            self.after(500, self._maybe_show_autoklip_promo)
    
    def _show_tos_dialog(self):
        """Show Terms of Service dialog and block app usage until accepted."""
        def on_accept():
            self.config.set("tos_accepted", True)
            # Queue the AutoKlip promo right after ToS acceptance
            self.after(400, self._maybe_show_autoklip_promo)
        
        TermsOfServiceDialog(self, on_accept)
    
    def _maybe_show_autoklip_promo(self):
        """Show AutoKlip promo modal exactly once per install."""
        if self.config.get("autoklip_promo_shown", False):
            return
        try:
            AutoKlipPromoDialog(self)
        finally:
            # Persist immediately so it never shows again, even if user
            # closes the app without clicking the CTA.
            self.config.set("autoklip_promo_shown", True)
    
    def set_app_icon(self):
        """Set window icon"""
        try:
            if sys.platform == "win32":
                # Use .ico file directly on Windows
                if ICON_ICO_PATH.exists():
                    self.iconbitmap(str(ICON_ICO_PATH))
                elif ICON_PATH.exists():
                    # Convert PNG to ICO if needed
                    img = Image.open(ICON_PATH)
                    ico_path = ASSETS_DIR / "icon.ico"
                    img.save(str(ico_path), format='ICO', sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
                    self.iconbitmap(str(ico_path))
            else:
                if ICON_PATH.exists():
                    icon_img = Image.open(ICON_PATH)
                    photo = ImageTk.PhotoImage(icon_img)
                    self.iconphoto(True, photo)
                    self._icon_photo = photo
        except Exception as e:
            print(f"Icon error: {e}")
    
    def show_page(self, name):
        if name == "processing":
            self.show_processing_embed()
            return
        for page in self.pages.values():
            page.pack_forget()
        # Add padding to every page
        if name == "home":
            self.pages[name].pack(fill="both", expand=True, padx=12, pady=8)
        else:
            self.pages[name].pack(fill="both", expand=True, padx=12, pady=8)
        # Keep background-role frames following the animated color
        self.animated_bg.clear_attached()
        self.animated_bg.attach(self.container)
        self.animated_bg.attach(self.pages[name])
        self.animated_bg.attach_transparent_children(self.pages[name])
        
        # Sidebar active state
        if name in getattr(self, "sidebar_buttons", {}):
            self.set_sidebar_active(name)
        else:
            self.set_sidebar_active(None)
        
        # Refresh browse list when showing browse page
        if name == "browse":
            self.pages["browse"].refresh_list()
        
        # Refresh API status when showing api_status page
        if name == "api_status":
            self.pages["api_status"].refresh_status()
        
        # Refresh lib status when showing lib_status page
        if name == "lib_status":
            self.pages["lib_status"].refresh_status()
        
        # Reset home page state when returning to home
        if name == "home":
            self.reset_home_page()
    
    def create_sidebar(self):
        """Create the left sidebar navigation menu."""
        # Right border separator (sidebar frame uses no border; draw a thin line)
        right_border = ctk.CTkFrame(self.sidebar, width=2, corner_radius=0, fg_color=("#d9dbe0", "#2a2a30"))
        right_border.pack(side="right", fill="y")
        
        inner = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        inner.pack(fill="x", padx=6, pady=(8, 6))
        
        # App logo
        try:
            icon_img = Image.open(ASSETS_DIR / "icon.png")
            icon_img.thumbnail((32, 32), Image.Resampling.LANCZOS)
            self._sidebar_icon = ctk.CTkImage(light_image=icon_img, dark_image=icon_img, size=(32, 32))
            ctk.CTkLabel(inner, image=self._sidebar_icon, text="").pack(pady=(0, 6))
        except Exception:
            pass
        
        # App title
        ctk.CTkLabel(inner, text="YT SHORT CLIPPER",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=("#00A878", "#00A878"),
            anchor="w").pack(fill="x", padx=2, pady=(0, 4))
        
        # Tagline
        ctk.CTkLabel(inner,
            text="Turn long YouTube videos\ninto viral shorts — Powered by AI",
            font=ctk.CTkFont(size=11),
            text_color=("#8a8a8a", "#8a8a8a"),
            anchor="w", justify="left", wraplength=180).pack(fill="x", padx=2, pady=(0, 12))
        # Sidebar icon helper
        sidebar_icons_dir = ASSETS_DIR / "sidebar_icons"
        def load_icon(name, size=20):
            try:
                img = Image.open(sidebar_icons_dir / f"{name}.png")
                return ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))
            except Exception:
                return None
        
        # Separator
        ctk.CTkFrame(inner, height=1, fg_color=("#d9dbe0", "#2a2a30")).pack(fill="x", pady=(0, 8))
        
        # Navigation buttons
        self.sidebar_buttons = {}
        items = [
            ("Home", "home", "home"),
            ("Processing", "processing", "processing"),
            ("Result Find Highlight", "highlight_selection", "highlight"),
            ("Processing Clip", "clipping", "clipping"),
            ("Result", "results", "results"),
            ("Result Session", "session_browser", "session"),
        ]
        for label, name, icon_name in items:
            btn = ctk.CTkButton(inner, text=label, anchor="w", height=34, corner_radius=5,
                fg_color="transparent", hover_color=("#e8eaee", "#2a2a30"),
                border_width=0, font=ctk.CTkFont(size=13),
                image=load_icon(icon_name), compound="left", _keep_image=True,
                text_color=("#1a1a1e", "#FFFFFF"),
                command=lambda n=name: self.show_page(n))
            btn.pack(fill="x", pady=2)
            self.sidebar_buttons[name] = btn
        
        # Utility navigation
        ctk.CTkFrame(inner, height=1, fg_color=("#d9dbe0", "#2a2a30")).pack(fill="x", pady=(8, 6))
        
        nav_items = [
            ("Settings", "settings", "settings"),
            ("API Status", "api_status", "api"),
            ("Library", "lib_status", "library"),
        ]
        for label, name, icon_name in nav_items:
            btn = ctk.CTkButton(inner, text=label, anchor="w", height=34, corner_radius=5,
                fg_color="transparent", hover_color=("#e8eaee", "#2a2a30"),
                border_width=0, font=ctk.CTkFont(size=13),
                image=load_icon(icon_name), compound="left", _keep_image=True,
                text_color=("#5a5a5e", "#a3a3a3"),
                command=lambda n=name: self.show_page(n))
            btn.pack(fill="x", pady=2)
            self.sidebar_buttons[name] = btn
    
    def set_sidebar_active(self, name):
        """Highlight the active sidebar menu item (None = none active)."""
        for n, btn in self.sidebar_buttons.items():
            if n == name:
                btn.configure(fg_color=("#00A878", "#00A878"), hover_color=("#008F66", "#008F66"),
                              text_color=("#0B0B0C", "#0B0B0C"), border_width=0)
            else:
                btn.configure(fg_color="transparent", hover_color=("#e8eaee", "#2a2a30"),
                              text_color=("#1a1a1e", "#FFFFFF"), border_width=0)
    
    def show_processing_embed(self):
        """Show the processing progress view embedded on the home page."""
        # Ensure the home page is visible when navigating here from another page
        if self.pages["home"] not in self.container.winfo_children() or not self.pages["home"].winfo_ismapped():
            for p in self.pages.values():
                p.pack_forget()
            self.animated_bg.clear_attached()
            self.animated_bg.attach(self.container)
            self.pages["home"].pack(fill="both", expand=True, padx=16, pady=12)
            self.animated_bg.attach(self.pages["home"])
        self.home_form.pack_forget()
        self.pages["processing"].pack(fill="both", expand=True, padx=4, pady=(6, 4))
        self.set_sidebar_active("processing")
    
    def show_home_form(self):
        """Restore the home page input form (hide the embedded processing view)."""
        self.pages["processing"].pack_forget()
        self.home_form.pack(fill="both", expand=True)
        self.set_sidebar_active("home")
    
    def reset_home_page(self):
        """Reset home page to initial state"""
        # Show the embedded processing view while a job is running
        if self.processing:
            self.show_processing_embed()
        else:
            self.show_home_form()
        
        # Clear URL input
        self.url_var.set("")
        
        # Clear title input
        self.video_title_var.set("")
        
        # Reset thumbnail - recreate preview placeholder
        self.current_thumbnail = None
        self.create_preview_placeholder()
        
        # Reset subtitle state (keep visible but disabled)
        self.subtitle_loaded = False
        self.subtitle_loading.pack_forget()
        self.subtitle_dropdown.configure(state="disabled", values=["id - Indonesian"])
        self.subtitle_var.set("id - Indonesian")
        
        # Reset clips input to default
        self.clips_var.set("1")
        
        # Update start button state
        self.update_start_button_state()

    def create_home_page(self):
        page = ctk.CTkFrame(self.container, fg_color=("#ffffff", "#0b0b0c"), border_width=1, border_color=("#2a2a30", "#2a2a30"), corner_radius=5)
        self.pages["home"] = page
        
        # Import header and footer components
        from components.page_layout import PageHeader, PageFooter
        
        # Home page is header-less: logo/title + nav menu live in the sidebar.
        # Form area (hidden while the processing view is embedded on this page)
        self.home_form = ctk.CTkFrame(page, fg_color="transparent")
        self.home_form.pack(fill="both", expand=True, padx=4, pady=4)
        
        # Load icons for buttons
        try:
            play_img = Image.open(ASSETS_DIR / "play.png")
            play_img.thumbnail((20, 20), Image.Resampling.LANCZOS)
            self.play_icon = ctk.CTkImage(light_image=play_img, dark_image=play_img, size=(20, 20))
            
            refresh_img = Image.open(ASSETS_DIR / "refresh.png")
            refresh_img.thumbnail((20, 20), Image.Resampling.LANCZOS)
            self.refresh_icon = ctk.CTkImage(light_image=refresh_img, dark_image=refresh_img, size=(20, 20))
        except Exception as e:
            debug_log(f"Icon load error: {e}")
            self.play_icon = None
            self.refresh_icon = None
        
        # ===== TOP ROW: Left config + Right thumbnail =====
        top_row = ctk.CTkFrame(self.home_form, fg_color="transparent")
        top_row.pack(fill="x", padx=4, pady=(4, 6))
        
        # Left column - URL, Subtitle, Clip Count
        left_col = ctk.CTkFrame(top_row, fg_color="transparent")
        left_col.pack(side="left", fill="y", padx=(0, 8))
        
        # YouTube URL
        ctk.CTkLabel(left_col, text="YOUTUBE URL", font=ctk.CTkFont(size=11, weight="bold"), 
            text_color=("#6b6b6b", "#a3a3a3"),
            anchor="w").pack(fill="x", pady=(0, 4))
        
        # URL input container
        url_input_container = ctk.CTkFrame(left_col, fg_color="transparent")
        url_input_container.pack(fill="x", pady=(0, 6))
        
        self.url_var = ctk.StringVar()
        self.url_var.trace_add("write", self.on_url_change)
        self.url_entry = ctk.CTkEntry(url_input_container, textvariable=self.url_var, 
            placeholder_text="Paste YouTube link...", width=220, height=24, border_width=1,
            corner_radius=5,
            border_color=("#c9ccd1", "#2a2a30"), fg_color=("#ffffff", "#0b0b0c"))
        self.url_entry.pack(side="left", padx=(0, 6))
        
        self.paste_btn = ctk.CTkButton(url_input_container, text="Paste", width=70, height=22,
            fg_color=("#17171b", "#17171b"), hover_color=("#2a2a30", "#2a2a30"),
            border_width=1, border_color=("#3a3a40", "#2a2a30"), corner_radius=5,
            font=ctk.CTkFont(size=11), command=self.paste_url, text_color=("#FFFFFF", "#FFFFFF"))
        self.paste_btn.pack(side="left")
        
        # Detected video title (auto-filled from yt-dlp)
        self.video_title_var = ctk.StringVar(value="")
        self.video_title_label = ctk.CTkLabel(left_col, textvariable=self.video_title_var, 
            font=ctk.CTkFont(size=11), text_color="gray", anchor="w", wraplength=290, justify="left")
        self.video_title_label.pack(fill="x", pady=(0, 4))
        
        # Subtitle Language
        ctk.CTkLabel(left_col, text="SUBTITLE LANGUAGE", font=ctk.CTkFont(size=11, weight="bold"), 
            text_color=("#6b6b6b", "#a3a3a3"),
            anchor="w").pack(fill="x", pady=(4, 4))
        
        self.subtitle_frame = ctk.CTkFrame(left_col, fg_color="transparent")
        self.subtitle_frame.pack(fill="x", pady=(0, 6))
        self.subtitle_loaded = False
        
        self.subtitle_var = ctk.StringVar(value="id - Indonesian")
        self.subtitle_dropdown = ctk.CTkOptionMenu(self.subtitle_frame, 
            variable=self.subtitle_var, values=["id - Indonesian"], width=298,
            height=26, corner_radius=5,
            fg_color=("#ffffff", "#0b0b0c"),
            button_color=("#e8eaee", "#17171b"), button_hover_color=("#d9dbe0", "#2a2a30"),
            state="disabled")
        self.subtitle_dropdown.pack(anchor="w")
        
        self.subtitle_loading = ctk.CTkLabel(self.subtitle_frame, text="⏳ Loading...", 
            font=ctk.CTkFont(size=11), text_color="gray")
        
        # Clip Count
        ctk.CTkLabel(left_col, text="CLIP COUNT", font=ctk.CTkFont(size=11, weight="bold"), 
            text_color=("#6b6b6b", "#a3a3a3"),
            anchor="w").pack(fill="x", pady=(4, 4))
        
        clips_input_frame = ctk.CTkFrame(left_col, fg_color="transparent")
        clips_input_frame.pack(fill="x", pady=(0, 4))
        
        self.clips_var = ctk.StringVar(value="5")
        clips_entry = ctk.CTkEntry(clips_input_frame, textvariable=self.clips_var, width=60, height=24,
            fg_color=("#ffffff", "#0b0b0c"), border_width=1, corner_radius=5,
            border_color=("#c9ccd1", "#2a2a30"), justify="center")
        clips_entry.pack(side="left", padx=(0, 6))
        
        ctk.CTkLabel(clips_input_frame, text="(1-10)", font=ctk.CTkFont(size=11), 
            text_color="gray").pack(side="left")
        
        # Right column - Thumbnail 16:9
        right_col = ctk.CTkFrame(top_row, fg_color="transparent")
        right_col.pack(side="right", fill="y")
        
        # Video preview frame 16:9 (400x225)
        self.thumb_frame = ctk.CTkFrame(right_col, width=400, height=225, 
            fg_color=("#f5f6f8", "#0b0b0c"), corner_radius=5,
            border_width=1, border_color=("#c9ccd1", "#2a2a30"))
        self.thumb_frame.pack(anchor="ne")
        self.thumb_frame.pack_propagate(False)
        
        self.create_preview_placeholder()
        
        # ===== MIDDLE ROW: Cookies only (full width) =====
        middle_row = ctk.CTkFrame(self.home_form, fg_color="transparent")
        middle_row.pack(fill="x", padx=4, pady=(0, 6))
        
        # YouTube Cookies card (full width)
        cookies_frame = ctk.CTkFrame(middle_row, fg_color=("#f5f6f8", "#0b0b0c"), corner_radius=5,
            border_width=1, border_color=("#c9ccd1", "#2a2a30"))
        cookies_frame.pack(fill="x")
        
        cookies_header = ctk.CTkFrame(cookies_frame, fg_color="transparent")
        cookies_header.pack(fill="x", padx=4, pady=(4, 4))
        
        ctk.CTkLabel(cookies_header, text="YOUTUBE COOKIES", font=ctk.CTkFont(size=11, weight="bold"), 
            text_color=("#6b6b6b", "#a3a3a3"),
            anchor="w").pack(side="left")
        
        upload_cookies_btn = ctk.CTkButton(cookies_header, text="Upload", height=18, width=140,
            fg_color=("#17171b", "#17171b"), hover_color=("#2a2a30", "#2a2a30"),
            border_width=1, border_color=("#3a3a40", "#2a2a30"), corner_radius=5,
            font=ctk.CTkFont(size=11), command=self.upload_cookies, text_color=("#FFFFFF", "#FFFFFF"))
        upload_cookies_btn.pack(side="right")
        
        self.cookies_status_label = ctk.CTkLabel(cookies_frame, text="🍪 No cookies", 
            font=ctk.CTkFont(size=11), anchor="w", text_color="gray")
        self.cookies_status_label.pack(fill="x", padx=4, pady=(0, 6))
        
        # ===== BOTTOM: Generate button + Browse =====
        bottom_section = ctk.CTkFrame(self.home_form, fg_color="transparent")
        bottom_section.pack(fill="x", padx=4, pady=(0, 4))
        
        self.start_btn = ctk.CTkButton(bottom_section, text="Find Highlights", 
            font=ctk.CTkFont(size=11, weight="bold"),
            width=180, height=26, command=self.start_processing, state="disabled", 
            fg_color="gray", hover_color="gray", corner_radius=5, text_color=("#FFFFFF", "#FFFFFF"))
        self.start_btn.pack(pady=(0, 4))
        
        # ===== LIB STATUS =====
        self.lib_status_frame = ctk.CTkFrame(self.home_form, fg_color="transparent")
        self.lib_status_frame.pack(fill="x", padx=4, pady=(4, 0))
        
        self.lib_status_label = ctk.CTkLabel(self.lib_status_frame, text="", 
            font=ctk.CTkFont(size=11), cursor="hand2")
        self.lib_status_label.pack()
        self.lib_status_label.bind("<Button-1>", lambda e: self.show_page("lib_status"))
        
        # Check and update lib status
        self.check_lib_status()
        
        # Check cookies status
        self.check_cookies_status()
        
        # Footer
        footer = PageFooter(page, self)
        footer.pack(fill="x", padx=4, pady=(4, 6), side="bottom")
    
    def create_preview_placeholder(self):
        """Create placeholder content for video preview"""
        # Clear existing content
        for widget in self.thumb_frame.winfo_children():
            widget.destroy()
        
        # Preview content container - centered
        preview_container = ctk.CTkFrame(self.thumb_frame, fg_color="transparent")
        preview_container.place(relx=0.5, rely=0.5, anchor="center")
        
        # Placeholder text
        self.thumb_label = ctk.CTkLabel(preview_container, 
            text="📺 Video thumbnail will appear here", 
            font=ctk.CTkFont(size=11), text_color="gray", justify="center")
        self.thumb_label.pack()
    
    def paste_url(self):
        """Paste URL from clipboard"""
        # Check if cookies exist first
        if not self.cookies_path.exists():
            # Show custom dialog with buttons
            self.show_cookies_required_dialog()
            return
        
        try:
            # Get clipboard content
            clipboard_text = self.clipboard_get()
            if clipboard_text:
                self.url_var.set(clipboard_text.strip())
        except Exception as e:
            debug_log(f"Paste error: {e}")
            # If clipboard is empty or error, do nothing
            pass
    
    def show_cookies_required_dialog(self):
        """Show custom dialog for cookies requirement with clickable buttons"""
        import webbrowser
        
        # Create dialog window
        dialog = ctk.CTkToplevel(self)
        dialog.title("YouTube Cookies Required")
        dialog.geometry("500x220")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()
        
        # Center dialog on parent window
        dialog.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() // 2) - (dialog.winfo_width() // 2)
        y = self.winfo_y() + (self.winfo_height() // 2) - (dialog.winfo_height() // 2)
        dialog.geometry(f"+{x}+{y}")
        
        # Main content frame
        content_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        content_frame.pack(fill="both", expand=True, padx=4, pady=4)
        
        # Warning message
        ctk.CTkLabel(content_frame, 
            text="⚠️ Please upload YouTube cookies first!",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=("#e74c3c", "#e74c3c")).pack(pady=(0, 8))
        
        ctk.CTkLabel(content_frame,
            text="Click a button below to open the setup guide:",
            font=ctk.CTkFont(size=11)).pack(pady=(0, 8))
        
        # Buttons frame
        buttons_frame = ctk.CTkFrame(content_frame, fg_color="transparent")
        buttons_frame.pack(pady=(0, 6))
        
        # English guide button
        english_btn = ctk.CTkButton(buttons_frame,
            text="English Guide",
            width=140,
            height=18,
            font=ctk.CTkFont(size=11),
            fg_color=("#00A878", "#00A878"),
            hover_color=("#008F66", "#008F66"),
            command=lambda: [
                webbrowser.open("https://github.com/jipraks/yt-short-clipper/blob/master/GUIDE.md#3-setup-youtube-cookies"),
                dialog.destroy()
            ], text_color=("#0B0B0C", "#0B0B0C"))
        english_btn.pack(side="left", padx=4)
        
        # Indonesian guide button
        indonesian_btn = ctk.CTkButton(buttons_frame,
            text="Bahasa Indonesia",
            width=140,
            height=18,
            font=ctk.CTkFont(size=11),
            fg_color=("#00A878", "#00A878"),
            hover_color=("#008F66", "#008F66"),
            command=lambda: [
                webbrowser.open("https://github.com/jipraks/yt-short-clipper/blob/master/PANDUAN.md#3-setup-cookies-youtube"),
                dialog.destroy()
            ], text_color=("#0B0B0C", "#0B0B0C"))
        indonesian_btn.pack(side="left", padx=4)
        
        # Close button
        close_btn = ctk.CTkButton(content_frame,
            text="Close",
            width=100,
            height=18,
            font=ctk.CTkFont(size=11),
            fg_color=("#6c757d", "#5a6268"),
            hover_color=("#5a6268", "#4e555b"),
            command=dialog.destroy, border_width=1, border_color=("#3a3a40", "#2a2a30"), text_color=("#FFFFFF", "#FFFFFF"))
        close_btn.pack(pady=(4, 0))
    
    def upload_cookies(self):
        """Upload cookies.txt file"""
        file_path = filedialog.askopenfilename(
            title="Select cookies.txt file",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
        )
        
        if file_path:
            try:
                # Copy file to app directory
                import shutil
                shutil.copy(file_path, self.cookies_path)
                debug_log(f"Cookies uploaded: {file_path}")
                
                # Update status
                self.check_cookies_status()
                
                # Show success message
                messagebox.showinfo("Success", "cookies.txt uploaded successfully!")
                
            except Exception as e:
                debug_log(f"Upload cookies error: {e}")
                messagebox.showerror("Upload Failed", f"Failed to upload cookies.txt:\n{str(e)}")
    
    def check_cookies_status(self):
        """Check if cookies.txt exists and update UI"""
        if self.cookies_path.exists():
            self.cookies_status_label.configure(
                text="✅ cookies.txt loaded",
                text_color=("#27ae60", "#2ecc71")  # Green
            )
            # Update start button state when cookies status changes
            self.update_start_button_state()
            return True
        else:
            self.cookies_status_label.configure(
                text="🍪 No cookies.txt found",
                text_color="gray"
            )
            # Update start button state when cookies status changes
            self.update_start_button_state()
            return False
    
    def create_processing_page(self):
        """Create processing page embedded on the home page (toggled by show_processing_embed)."""
        self.pages["processing"] = ProcessingPage(
            self.pages["home"],
            self.cancel_processing,
            lambda: self.show_page("home"),
            self.open_output,
            self.open_current_clips,
            self.retry_find_highlights
        )
        # Keep reference to steps for update_progress
        self.steps = self.pages["processing"].steps
    
    def create_clipping_page(self):
        """Create clipping page as embedded frame"""
        self.pages["clipping"] = ClippingPage(
            self.container,
            self.cancel_processing,
            lambda: self.show_page("home"),
            self.open_output,
            lambda: self.show_page("session_browser"),
            self.open_current_clips
        )
    
    def create_highlight_selection_page(self):
        """Create highlight selection page as embedded frame"""
        self.pages["highlight_selection"] = HighlightSelectionPage(
            self.container,
            lambda: self.show_page("home"),  # Back to home
            self.process_selected_highlights  # Process callback
        )
        # Load portrait mode from config
        pm = self.config.get("portrait_mode", "crop")
        self.pages["highlight_selection"].portrait_mode_var.set(
            "Blurred Background" if pm == "blur" else "Smart Crop")
        # Load subtitle style from config
        ss = self.config.get("subtitle_style", "pop")
        self.pages["highlight_selection"].subtitle_style_var.set(
            {"karaoke": "Karaoke", "bounce": "Bounce", "animated": "Bounce + Word-by-Word", "pop_bounce": "Pop + Bounce"}.get(ss, "Pop Highlight"))
        # Load subtitle sync offset from config
        sync_vals = ["-0.5", "-0.4", "-0.3", "-0.2", "-0.1", "0", "+0.1", "+0.2"]
        sync_val = self.config.get("subtitle_sync_offset", -0.3)
        sync_str = f"{float(sync_val):+.1f}"
        self.pages["highlight_selection"].sync_var.set(
            sync_str if sync_str in sync_vals else "0")
        # Load face tracking mode from config
        ft = self.config.get("face_tracking_mode", "opencv")
        self.pages["highlight_selection"].face_tracking_var.set(
            "MediaPipe (Smart)" if ft == "mediapipe" else "OpenCV (Fast)")
        # Load aspect ratio from config
        ar = self.config.get("aspect_ratio", "9:16")
        self.pages["highlight_selection"].aspect_ratio_var.set(ar)
    
    def create_session_browser_page(self):
        """Create session browser page as embedded frame"""
        self.pages["session_browser"] = SessionBrowserPage(
            self.container,
            self.config,
            lambda: self.show_page("home"),  # Back to home
            self.resume_session,  # Resume callback
            self  # Pass app reference
        )
    
    def create_results_page(self):
        """Create results page as embedded frame"""
        self.pages["results"] = ResultsPage(
            self.container,
            self.config,
            self.client,
            lambda: self.show_page("home"),
            self.new_clip_from_results,
            self.open_output,
            self.get_youtube_client
        )

    def new_clip_from_results(self):
        """'New Clip' on results page: go back to highlight selection for the current session."""
        if self.session_data and self.session_data.get("highlights"):
            self.show_highlight_selection()
        else:
            self.show_page("home")
    
    def create_settings_page(self):
        """Create settings page as embedded frame"""
        self.pages["settings"] = SettingsPage(
            self.container, 
            self.config, 
            self.on_settings_saved,
            lambda: self.show_page("home"),
            OUTPUT_DIR,
            self.check_update_manual
        )
    
    def create_api_status_page(self):
        """Create API status page as embedded frame"""
        self.pages["api_status"] = APIStatusPage(
            self.container,
            lambda: self.client,
            lambda: self.config,
            lambda: (self.youtube_connected, self.youtube_channel),
            lambda: self.show_page("home"),
            self.refresh_icon
        )
    
    def create_lib_status_page(self):
        """Create library status page as embedded frame"""
        self.pages["lib_status"] = LibStatusPage(
            self.container,
            lambda: self.show_page("home"),
            self.refresh_icon
        )
    
    def create_browse_page(self):
        """Create browse page as embedded frame"""
        self.pages["browse"] = BrowsePage(
            self.container,
            self.config,
            self.client,
            lambda: self.show_page("home"),
            self.refresh_icon,
            self.get_youtube_client
        )
    
    def create_contact_page(self):
        """Create contact page as embedded frame"""
        self.pages["contact"] = ContactPage(
            self.container,
            lambda: self.config.get("installation_id", "unknown"),
            lambda: self.show_page("home")
        )
    
    def load_config(self):
        api_key = self.config.get("api_key", "")
        base_url = self.config.get("base_url", "https://api.openai.com/v1")
        model = self.config.get("model", "")
        
        if api_key:
            try:
                self.client = OpenAI(api_key=api_key, base_url=base_url)
                # Only update UI if widgets exist
                if hasattr(self, 'api_dot'):
                    self.api_dot.configure(text_color="#27ae60")  # Green
                    self.api_status_label.configure(text=model[:15] if model else "Connected")
            except:
                if hasattr(self, 'api_dot'):
                    self.api_dot.configure(text_color="#e74c3c")  # Red
                    self.api_status_label.configure(text="Invalid key")
        else:
            if hasattr(self, 'api_dot'):
                self.api_dot.configure(text_color="#e74c3c")  # Red
                self.api_status_label.configure(text="Not configured")
    
    def check_youtube_status(self):
        """Check YouTube connection status"""
        try:
            from youtube_uploader import YouTubeUploader
            uploader = YouTubeUploader()
            
            if uploader.is_authenticated():
                channel = uploader.get_channel_info()
                if channel:
                    self.youtube_connected = True
                    self.youtube_channel = channel
                    
                    # Only update UI if widgets exist
                    if hasattr(self, 'yt_dot'):
                        self.yt_dot.configure(text_color="#27ae60")  # Green
                        
                        # Show channel name
                        channel_name = channel['title']
                        self.yt_status_label_home.configure(text=f"{channel_name[:20]}")
                    return
            
            self.youtube_connected = False
            if hasattr(self, 'yt_dot'):
                self.yt_dot.configure(text_color="#e74c3c")  # Red
                self.yt_status_label_home.configure(text="Not connected")
        except:
            self.youtube_connected = False
            if hasattr(self, 'yt_dot'):
                self.yt_dot.configure(text_color="#e74c3c")  # Red
                self.yt_status_label_home.configure(text="Not available")
    
    def update_connection_status(self):
        """Update connection status cards (called after settings change)"""
        self.load_config()
        self.check_youtube_status()
    
    def on_settings_saved(self, updated_config):
        """Handle settings saved - accepts config dict"""
        # Update internal config
        if isinstance(updated_config, dict):
            self.config.config.update(updated_config)
            self.config.save()
            
            # Update OpenAI client if highlight_finder config changed
            ai_providers = updated_config.get("ai_providers", {})
            hf_config = ai_providers.get("highlight_finder", {})
            if hf_config.get("api_key"):
                self.client = OpenAI(
                    api_key=hf_config.get("api_key"),
                    base_url=hf_config.get("base_url", "https://api.openai.com/v1")
                )
    
    def get_youtube_client(self):
        """Get OpenAI client for YouTube title generation"""
        ai_providers = self.config.get("ai_providers", {})
        yt_config = ai_providers.get("youtube_title_maker", {})
        
        if yt_config.get("api_key"):
            return OpenAI(
                api_key=yt_config.get("api_key"),
                base_url=yt_config.get("base_url", "https://api.openai.com/v1")
            )
        else:
            # Fallback to main client for backward compatibility
            return self.client
    
    def fetch_video_title(self, url):
        """Fetch video title in a background thread."""
        try:
            cookies_path = str(self.cookies_path) if self.cookies_path.exists() else None
            title = AutoClipperCore.get_video_title(url, self.ytdlp_path, cookies_path)
            # Update UI on main thread
            self.after(0, lambda: self.video_title_var.set(title))
        except Exception as e:
            debug_log(f"Error fetching title: {e}")
            self.after(0, lambda: self.video_title_var.set(""))
    
    def on_url_change(self, *args):
        url = self.url_var.get().strip()
        video_id = extract_video_id(url)
        if video_id:
            # Reset subtitle loaded flag when URL changes
            self.subtitle_loaded = False
            self.load_thumbnail(video_id)
            self.load_subtitles(url)  # Fetch available subtitles
            # Auto-detect video title
            threading.Thread(target=self.fetch_video_title, args=(url,), daemon=True).start()
        else:
            self.current_thumbnail = None
            self.subtitle_loaded = False
            # Recreate placeholder
            self.create_preview_placeholder()
            # Reset subtitle dropdown to disabled state
            self.subtitle_loading.pack_forget()
            self.subtitle_dropdown.configure(state="disabled", values=["id - Indonesian"])
            self.subtitle_var.set("id - Indonesian")
            # Disable start button when URL is invalid or cookies missing
            self.update_start_button_state()
    
    def update_start_button_state(self):
        """Update start button state based on URL, cookies, and library validation"""
        has_cookies = self.cookies_path.exists()
        libs_ok = getattr(self, 'libs_installed', True)  # Default True if not checked yet
        
        # Always keep paste button enabled (so user can see alert)
        self.paste_btn.configure(state="normal")
        
        # If no cookies, disable URL entry and start button
        if not has_cookies:
            self.url_entry.configure(state="disabled")
            self.start_btn.configure(state="disabled", fg_color="gray", hover_color="gray")
            return
        
        # Cookies exist - enable URL input
        self.url_entry.configure(state="normal")
        
        # Check if URL is valid, subtitle is loaded, and libs are installed
        url = self.url_var.get().strip()
        video_id = extract_video_id(url)
        
        if video_id and self.subtitle_loaded and libs_ok:
            self.start_btn.configure(state="normal", fg_color=("#00A878", "#00A878"), 
                                    hover_color=("#008F66", "#008F66"),
                                    text_color=("#0B0B0C", "#0B0B0C"))
        else:
            self.start_btn.configure(state="disabled", fg_color="gray", hover_color="gray")
    
    def check_lib_status(self):
        """Check library installation status and update UI"""
        from utils.dependency_manager import check_dependency
        from utils.helpers import get_app_dir, is_ytdlp_module_available
        
        app_dir = get_app_dir()
        
        # Check each dependency
        ffmpeg_ok = check_dependency('ffmpeg', app_dir)
        deno_ok = check_dependency('deno', app_dir)
        ytdlp_ok = is_ytdlp_module_available()
        
        all_ok = ffmpeg_ok and deno_ok and ytdlp_ok
        self.libs_installed = all_ok
        
        if all_ok:
            # All installed - hide lib status
            self.lib_status_frame.pack_forget()
        else:
            # Clear existing widgets
            for widget in self.lib_status_frame.winfo_children():
                widget.destroy()
            
            # Create status row with colored indicators
            status_row = ctk.CTkFrame(self.lib_status_frame, fg_color="transparent")
            status_row.pack()
            
            ctk.CTkLabel(status_row, text="Lib Status:", font=ctk.CTkFont(size=11), 
                text_color="gray").pack(side="left", padx=(0, 4))
            
            # Deno
            deno_color = "#4ade80" if deno_ok else "#f87171"
            ctk.CTkLabel(status_row, text=f"Deno {'✓' if deno_ok else '✗'}", 
                font=ctk.CTkFont(size=11), text_color=deno_color).pack(side="left", padx=(0, 6))
            
            # YT-DLP
            ytdlp_color = "#4ade80" if ytdlp_ok else "#f87171"
            ctk.CTkLabel(status_row, text=f"YT-DLP {'✓' if ytdlp_ok else '✗'}", 
                font=ctk.CTkFont(size=11), text_color=ytdlp_color).pack(side="left", padx=(0, 6))
            
            # FFmpeg
            ffmpeg_color = "#4ade80" if ffmpeg_ok else "#f87171"
            ctk.CTkLabel(status_row, text=f"FFmpeg {'✓' if ffmpeg_ok else '✗'}", 
                font=ctk.CTkFont(size=11), text_color=ffmpeg_color).pack(side="left", padx=(0, 6))
            
            # Install link
            install_link = ctk.CTkLabel(status_row, text="(Install required libraries)", 
                font=ctk.CTkFont(size=11), text_color="#f87171", cursor="hand2")
            install_link.pack(side="left")
            install_link.bind("<Button-1>", lambda e: self.show_page("lib_status"))
            
            self.lib_status_frame.pack(fill="x", padx=4, pady=(4, 0))
        
        # Update start button state
        self.update_start_button_state()
    
    def load_subtitles(self, url: str):
        """Fetch available subtitles for the video"""
        def fetch():
            try:
                # Show loading state
                self.after(0, lambda: self.show_subtitle_loading())
                
                # Import here to avoid circular dependency
                from clipper_core import AutoClipperCore
                
                # Get available subtitles (pass cookies_path)
                debug_log(f"Fetching subtitles for: {url}")
                debug_log(f"Cookies path: {self.cookies_path}")
                debug_log(f"Cookies exists: {self.cookies_path.exists()}")
                
                cookies_str = str(self.cookies_path) if self.cookies_path.exists() else None
                debug_log(f"Passing cookies_path: {cookies_str}")
                
                result = AutoClipperCore.get_available_subtitles(
                    url, 
                    self.ytdlp_path, 
                    cookies_path=cookies_str
                )
                debug_log(f"Subtitle fetch result: {result}")
                
                if result.get("error"):
                    debug_log(f"Subtitle error: {result['error']}")
                    self.after(0, lambda: self.on_subtitle_error(result["error"]))
                    return
                
                # Combine manual and auto-generated subtitles
                all_subs = []
                
                # Prioritize manual subtitles
                for sub in result.get("subtitles", []):
                    all_subs.append({
                        "code": sub["code"],
                        "name": sub["name"],
                        "type": "manual"
                    })
                
                # Add auto-generated subtitles
                for sub in result.get("automatic_captions", []):
                    all_subs.append({
                        "code": sub["code"],
                        "name": f"{sub['name']} (auto)",
                        "type": "auto"
                    })
                
                debug_log(f"Total subtitles found: {len(all_subs)}")
                
                if not all_subs:
                    # No subtitles — allow proceeding with AI transcription fallback
                    self.after(0, lambda: self.show_no_subtitle_fallback())
                    return
                
                self.after(0, lambda: self.show_subtitle_selector(all_subs))
                
            except Exception as e:
                debug_log(f"Exception in load_subtitles: {str(e)}")
                import traceback
                debug_log(traceback.format_exc())
                err_msg = str(e)
                self.after(0, lambda msg=err_msg: self.on_subtitle_error(msg))
        
        threading.Thread(target=fetch, daemon=True).start()
    
    def show_subtitle_loading(self):
        """Show loading state for subtitle selector"""
        # Keep dropdown visible but show loading indicator
        self.subtitle_dropdown.configure(state="disabled")
        self.subtitle_loading.pack(fill="x", padx=(4, 6), pady=(4, 0))
    
    def on_subtitle_error(self, error: str):
        """Handle subtitle fetch error"""
        debug_log(f"Subtitle fetch error: {error}")
        self.subtitle_loaded = False
        # Hide loading, keep dropdown disabled
        self.subtitle_loading.pack_forget()
        self.subtitle_dropdown.configure(state="disabled")
        # Show error to user
        messagebox.showerror("Subtitle Error", f"Failed to fetch subtitles:\n\n{error}")
        # Update button state
        self.update_start_button_state()
    
    def show_subtitle_selector(self, subtitles: list):
        """Show subtitle selector with available options"""
        # Hide loading
        self.subtitle_loading.pack_forget()
        
        # Create dropdown options
        options = [f"{sub['code']} - {sub['name']}" for sub in subtitles]
        
        # Set default to Indonesian if available, otherwise first option
        default_value = options[0]
        for opt in options:
            if opt.startswith("id "):
                default_value = opt
                break
        
        self.subtitle_var.set(default_value)
        self.subtitle_dropdown.configure(values=options, state="normal")
        
        # Mark subtitles as loaded
        self.subtitle_loaded = True
        
        # Update start button state (subtitles loaded successfully)
        self.update_start_button_state()
    
    def show_no_subtitle_fallback(self):
        """Handle case where no subtitles are available.
        
        Since the new flow requires subtitles (no video download for Whisper),
        we disable the Find Highlights button and inform the user.
        """
        # Hide loading
        self.subtitle_loading.pack_forget()
        
        # Set dropdown to show no subtitle message
        no_sub_option = "none - No subtitle available"
        self.subtitle_var.set(no_sub_option)
        self.subtitle_dropdown.configure(values=[no_sub_option], state="disabled")
        
        # Do NOT mark as loaded — this prevents Find Highlights button from enabling
        self.subtitle_loaded = False
        
        # Update start button state (will be disabled)
        self.update_start_button_state()
    
    def load_thumbnail(self, video_id: str):
        def fetch():
            try:
                import ssl
                import certifi
                
                # Try with certifi first, fallback to unverified SSL
                ssl_context = None
                try:
                    ssl_context = ssl.create_default_context(cafile=certifi.where())
                except Exception:
                    pass
                
                if ssl_context is None:
                    # Fallback to unverified SSL (for PyInstaller builds)
                    ssl_context = ssl.create_default_context()
                    ssl_context.check_hostname = False
                    ssl_context.verify_mode = ssl.CERT_NONE
                
                img = None
                for quality in ["maxresdefault", "hqdefault", "mqdefault"]:
                    try:
                        url = f"https://img.youtube.com/vi/{video_id}/{quality}.jpg"
                        with urllib.request.urlopen(url, timeout=5, context=ssl_context) as r:
                            data = r.read()
                        img = Image.open(io.BytesIO(data))
                        if img.size[0] > 120:
                            break
                    except Exception as e:
                        debug_log(f"Thumbnail fetch error ({quality}): {e}")
                        continue
                
                if img is None:
                    raise Exception("All thumbnail qualities failed")
                    
                # Resize to fit preview area in landscape (16:9 aspect ratio)
                # Frame is 400x225
                img.thumbnail((390, 220), Image.Resampling.LANCZOS)
                self.after(0, lambda: self.show_thumbnail(img))
            except Exception as e:
                debug_log(f"Thumbnail load failed: {e}")
                self.after(0, lambda: self.on_thumbnail_error())
        
        # Clear image reference properly before loading new one
        self.current_thumbnail = None
        
        # Show loading state
        for widget in self.thumb_frame.winfo_children():
            widget.destroy()
        
        loading_container = ctk.CTkFrame(self.thumb_frame, fg_color="transparent")
        loading_container.place(relx=0.5, rely=0.5, anchor="center")
        
        self.thumb_label = ctk.CTkLabel(loading_container, text="Loading...", 
            font=ctk.CTkFont(size=11), text_color="gray")
        self.thumb_label.pack()
        
        self.start_btn.configure(state="disabled", fg_color="gray", hover_color="gray")
        threading.Thread(target=fetch, daemon=True).start()
    
    def on_thumbnail_error(self):
        # Clear image reference properly before showing error
        self.current_thumbnail = None
        # Recreate placeholder with error message
        for widget in self.thumb_frame.winfo_children():
            widget.destroy()
        
        preview_container = ctk.CTkFrame(self.thumb_frame, fg_color="transparent")
        preview_container.place(relx=0.5, rely=0.5, anchor="center")
        
        self.thumb_label = ctk.CTkLabel(preview_container, 
            text="⚠️ Could not load thumbnail\nPlease check the URL", 
            font=ctk.CTkFont(size=11), text_color="gray", justify="center")
        self.thumb_label.pack()
        
        self.start_btn.configure(state="disabled", fg_color="gray", hover_color="gray")
    
    def show_thumbnail(self, img):
        try:
            # Clear the preview container and show thumbnail
            for widget in self.thumb_frame.winfo_children():
                widget.destroy()
            
            # Create image with proper size
            ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
            self.current_thumbnail = ctk_img
            
            # Show thumbnail centered
            self.thumb_label = ctk.CTkLabel(self.thumb_frame, image=ctk_img, text="")
            self.thumb_label.place(relx=0.5, rely=0.5, anchor="center")
            
            # Update start button state (checks both URL and cookies)
            self.update_start_button_state()
        except Exception as e:
            debug_log(f"Error showing thumbnail: {e}")
            # If thumbnail fails, still update button state
            self.update_start_button_state()

    def start_processing(self):
        # Disable button during validation
        self.start_btn.configure(state="disabled", text="Validating...")
        
        def validate_and_start():
            try:
                from openai import OpenAI
                
                # Validate Highlight Finder (required for all processing)
                ai_providers = self.config.get("ai_providers", {})
                hf_config = ai_providers.get("highlight_finder", {})
                hf_api_key = hf_config.get("api_key", "").strip()
                hf_base_url = hf_config.get("base_url", "https://api.openai.com/v1").strip()
                hf_model = hf_config.get("model", "").strip()
                
                if not hf_api_key or not hf_model:
                    self.after(0, lambda: self._on_validation_failed(
                        "Highlight Finder API is not configured!\n\n" +
                        "This is required to find viral moments in videos.\n\n" +
                        "Please configure it in Settings → AI API Settings → Highlight Finder"))
                    return
                
                # Test Highlight Finder API
                try:
                    hf_client = OpenAI(api_key=hf_api_key, base_url=hf_base_url)
                    
                    # Try to list models to verify API key and model availability
                    try:
                        hf_models = hf_client.models.list()
                        hf_available = [m.id for m in hf_models.data]
                        
                        if hf_model not in hf_available:
                            self.after(0, lambda: self._on_validation_failed(
                                f"Highlight Finder model '{hf_model}' is not available!\n\n" +
                                "Please check your configuration in:\n" +
                                "Settings → AI API Settings → Highlight Finder"))
                            return
                    except Exception as list_error:
                        # If models.list() fails, the API key might still be valid
                        # Some providers don't support models.list()
                        # Just verify the API key is not empty and continue
                        pass
                    
                except Exception as e:
                    self.after(0, lambda: self._on_validation_failed(
                        f"Highlight Finder API validation failed!\n\n" +
                        f"Error: {str(e)[:100]}\n\n" +
                        "Please check your configuration in:\n" +
                        "Settings → AI API Settings → Highlight Finder"))
                    return
                
                # All validations passed, proceed with processing
                self.after(0, self._start_processing_validated)
                
            except Exception as e:
                err_msg = f"Validation error: {str(e)[:100]}"
                self.after(0, lambda msg=err_msg: self._on_validation_failed(msg))
        
        threading.Thread(target=validate_and_start, daemon=True).start()
    
    def _on_validation_failed(self, error_msg):
        """Handle validation failure"""
        self.start_btn.configure(state="normal", text="Find Highlights")
        messagebox.showerror("Validation Failed", error_msg)
    
    def _start_processing_validated(self):
        """Start processing after validation passed"""
        self.start_btn.configure(state="normal", text="Find Highlights")
        
        # Legacy validation (backward compatibility)
        
        url = self.url_var.get().strip()
        if not extract_video_id(url):
            messagebox.showerror("Error", "Enter a valid YouTube URL!")
            return
        try:
            num_clips = int(self.clips_var.get())
            if not 1 <= num_clips <= 10:
                raise ValueError()
        except:
            messagebox.showerror("Error", "Clips must be 1-10!")
            return
        
        # Get selected subtitle language (extract code from "id - Indonesian" format)
        subtitle_selection = self.subtitle_var.get()
        subtitle_lang = subtitle_selection.split(" - ")[0] if " - " in subtitle_selection else "id"
        
        # Reset UI
        self.processing = True
        self.cancelled = False
        self.token_usage = {"gpt_input": 0, "gpt_output": 0, "whisper_seconds": 0, "tts_chars": 0}
        
        # Reset processing page UI
        self.pages["processing"].reset_ui()
        
        self.show_processing_embed()
        
        output_dir = self.config.get("output_dir", str(OUTPUT_DIR))
        model = self.config.get("model", "gpt-4.1")
        
        # NEW FLOW: Only download subtitle + find highlights (no video download)
        threading.Thread(target=self.run_find_highlights, 
                        args=(url, num_clips, output_dir, model, subtitle_lang), 
                        daemon=True).start()
    
    def run_processing(self, url, num_clips, output_dir, model, add_captions, add_hook, subtitle_lang="id"):
        try:
            from clipper_core import AutoClipperCore
            
            # Wrapper for log callback that also logs to console in debug mode
            def log_with_debug(msg):
                debug_log(msg)
                self.after(0, lambda: self.update_status(strip_ansi(msg)))
            
            # Get system prompt from config
            # Priority: ai_providers.highlight_finder.system_message > root system_prompt
            ai_providers = self.config.get("ai_providers", {})
            highlight_finder = ai_providers.get("highlight_finder", {})
            system_prompt = highlight_finder.get("system_message") or self.config.get("system_prompt", None)
            
            temperature = self.config.get("temperature", 1.0)
            tts_model = self.config.get("tts_model", "tts-1")
            watermark_settings = self.config.get("watermark", {"enabled": False})
            credit_watermark_settings = self.config.get("credit_watermark", {"enabled": False})
            hook_style_settings = self.config.get("hook_style", {})
            
            # Get face tracking mode from config (set in settings page)
            face_tracking_mode = self.config.get("face_tracking_mode", "opencv")
            portrait_mode = self.config.get("portrait_mode", "crop")
            subtitle_style = self.config.get("subtitle_style", "pop")
            aspect_ratio = self.config.get("aspect_ratio", "9:16")
            
            mediapipe_settings = self.config.get("mediapipe_settings", {
                "lip_activity_threshold": 0.15,
                "switch_threshold": 0.3,
                "min_shot_duration": 90,
                "center_weight": 0.3
            })
            
            core = AutoClipperCore(
                client=self.client,
                ffmpeg_path=get_ffmpeg_path(),
                ytdlp_path=get_ytdlp_path(),
                output_dir=output_dir,
                model=model,
                tts_model=tts_model,
                temperature=temperature,
                system_prompt=system_prompt,
                watermark_settings=watermark_settings,
                credit_watermark_settings=credit_watermark_settings,
                hook_style_settings=hook_style_settings,
                face_tracking_mode=face_tracking_mode,
                portrait_mode=portrait_mode,
                subtitle_style=subtitle_style,
                aspect_ratio=aspect_ratio,
                mediapipe_settings=mediapipe_settings,
                ai_providers=self.config.get("ai_providers"),
                subtitle_language=subtitle_lang,
                log_callback=log_with_debug,
                progress_callback=lambda s, p: self.after(0, lambda: self.update_progress(s, p)),
                token_callback=lambda a, b, c, d: self.after(0, lambda: self.update_tokens(a, b, c, d)),
                cancel_check=lambda: self.cancelled
            )
            
            # Enable GPU acceleration if configured
            gpu_settings = self.config.get("gpu_acceleration", {})
            if gpu_settings.get("enabled", False):
                core.enable_gpu_acceleration(True)
            
            core.process(url, num_clips, add_captions=add_captions, add_hook=add_hook)
            if not self.cancelled:
                self.after(0, self.on_complete)
        except Exception as e:
            error_msg = str(e)
            debug_log(f"ERROR: {error_msg}")
            
            # Log error to file with full traceback
            log_error(f"Processing failed for URL: {url}", e)
            
            if self.cancelled or "cancel" in error_msg.lower():
                self.after(0, self.on_cancelled)
            else:
                self.after(0, lambda: self.on_error(error_msg))

    def toggle_log_panel(self):
        """Expand/collapse the in-app log console (Ctrl+L or footer button)."""
        if hasattr(self, "log_panel"):
            self.log_panel.toggle()

    def update_status(self, msg):
        self.pages["processing"].update_status(msg)
    
    def update_progress(self, status, progress):
        print(f"[DEBUG] update_progress called: status='{status}', progress={progress}")
        self.pages["processing"].update_status(status)
        
        # Update step indicators based on status text
        status_lower = status.lower()
        
        # Parse progress percentage from status if available
        progress_match = re.search(r'\((\d+(?:\.\d+)?)%\)|(\d+(?:\.\d+)?)%', status)
        if progress_match:
            step_progress = float(progress_match.group(1) or progress_match.group(2)) / 100
        else:
            step_progress = None
        
        print(f"[DEBUG] Parsed step_progress: {step_progress}")
        
        num_steps = len(self.steps)
        
        if "download" in status_lower or "processing downloaded" in status_lower or "subtitle" in status_lower:
            if step_progress is None:
                step_progress = 0.0
            self.steps[0].set_active(status, step_progress)
            for s in self.steps[1:]:
                s.reset()
        elif "transcrib" in status_lower:
            # AI transcription step (3-step mode: step index 1)
            self.steps[0].set_done("Downloaded")
            if num_steps >= 3:
                # 3-step mode: transcription is step 2
                if step_progress is None:
                    step_progress = 0.0
                self.steps[1].set_active(status, step_progress)
                self.steps[2].reset()
            elif num_steps >= 2:
                # 2-step mode fallback
                if step_progress is None:
                    step_progress = 0.0
                self.steps[1].set_active(status, step_progress)
        elif "highlight" in status_lower or "finding" in status_lower:
            self.steps[0].set_done("Downloaded")
            if num_steps >= 3:
                # 3-step mode: highlights is step 3
                self.steps[1].set_done("Transcribed")
                self.steps[2].set_active(status, step_progress)
            elif num_steps >= 2:
                # 2-step mode: highlights is step 2
                self.steps[1].set_active(status, step_progress)
        elif "complete" in status_lower:
            for step in self.steps:
                step.set_done("Complete")
    
    def update_tokens(self, gpt_in, gpt_out, whisper, tts):
        self.token_usage["gpt_input"] += gpt_in
        self.token_usage["gpt_output"] += gpt_out
        self.token_usage["whisper_seconds"] += whisper
        self.token_usage["tts_chars"] += tts
        
        # Update processing page display
        gpt_total = self.token_usage['gpt_input'] + self.token_usage['gpt_output']
        whisper_minutes = self.token_usage['whisper_seconds'] / 60
        tts_chars = self.token_usage['tts_chars']
        self.pages["processing"].update_tokens(gpt_total, whisper_minutes, tts_chars)
    
    def run_find_highlights(self, url, num_clips, output_dir, model, subtitle_lang="id",
                            session_dir=None, title=None):
        """NEW: Phase 1 - Find highlights only (don't process yet)"""
        core = None
        try:
            from clipper_core import AutoClipperCore, SubtitleNotFoundError
            
            # Wrapper for log callback
            def log_with_debug(msg):
                debug_log(msg)
                self.after(0, lambda: self.update_status(strip_ansi(msg)))
            
            # Get system prompt from config
            ai_providers = self.config.get("ai_providers", {})
            highlight_finder = ai_providers.get("highlight_finder", {})
            system_prompt = highlight_finder.get("system_message") or self.config.get("system_prompt", None)
            
            temperature = self.config.get("temperature", 1.0)
            
            core = AutoClipperCore(
                client=self.client,
                ffmpeg_path=get_ffmpeg_path(),
                ytdlp_path=get_ytdlp_path(),
                output_dir=output_dir,
                model=model,
                temperature=temperature,
                system_prompt=system_prompt,
                ai_providers=self.config.get("ai_providers"),
                subtitle_language=subtitle_lang,
                log_callback=log_with_debug,
                progress_callback=lambda s, p: self.after(0, lambda: self.update_progress(s, p)),
                token_callback=lambda a, b, c, d: self.after(0, lambda: self.update_tokens(a, b, c, d)),
                cancel_check=lambda: self.cancelled
            )
            
            try:
                # Call find_highlights_only (returns session data - subtitle only, no video)
                video_title = title if title is not None else self.video_title_var.get().strip()
                # Remember parameters so the failed step can be retried in-place
                self._retry_context = (url, num_clips, output_dir, model, subtitle_lang, video_title, session_dir)
                result = core.find_highlights_only(url, num_clips, title=video_title, session_dir=session_dir)
            except SubtitleNotFoundError as snf:
                # No subtitle found - can't proceed without video for Whisper
                if self.cancelled:
                    self.after(0, self.on_cancelled)
                    return
                
                self.after(0, lambda: self.on_error(
                    f"No subtitle available for language: {subtitle_lang.upper()}\n\n"
                    "This video doesn't have the selected subtitle.\n\n"
                    "Tips:\n"
                    "1. Go back and select a different subtitle language\n"
                    "2. Try a video that has subtitles available"
                ))
                return
            
            if not self.cancelled and result:
                # Store session data for later processing
                self.session_data = result
                
                # Navigate to highlight selection page
                self.after(0, self.show_highlight_selection)
            elif self.cancelled:
                self.after(0, self.on_cancelled)
                
        except Exception as e:
            error_msg = str(e)
            debug_log(f"ERROR: {error_msg}")
            log_error(f"Find highlights failed for URL: {url}", e)
            
            if self.cancelled or "cancel" in error_msg.lower():
                self.after(0, self.on_cancelled)
            else:
                self.after(0, lambda: self.on_find_error(error_msg))
    
    def on_find_error(self, error_msg: str):
        """Handle find-highlights errors; offer Retry so the same step can be re-run."""
        # Re-enable navigation (Home button) after a failure
        self.processing = False
        if self._retry_context:
            self.pages["processing"].set_retryable_error(error_msg)
        else:
            self.on_error(error_msg)
    
    def retry_find_highlights(self):
        """Re-run the find-highlights step with the same parameters (same session dir)."""
        if not self._retry_context:
            return
        url, num_clips, output_dir, model, subtitle_lang, title, session_dir = self._retry_context
        self.processing = True
        self.cancelled = False
        self.pages["processing"].retry_btn.configure(state="disabled")
        self.pages["processing"].reset_ui()
        self.show_processing_embed()
        threading.Thread(
            target=self.run_find_highlights,
            args=(url, num_clips, output_dir, model, subtitle_lang),
            kwargs={"session_dir": session_dir, "title": title},
            daemon=True
        ).start()
    
    def _show_whisper_fallback_dialog(self, core, snf_error, num_clips: int):
        """Show dialog asking user if they want to use Whisper API for transcription.
        
        Called on the main thread when SubtitleNotFoundError is caught.
        """
        # Update processing page to show no subtitle found
        self.steps[0].set_done("Downloaded (no subtitle)")
        self.pages["processing"].update_status("No subtitle found for this video.")
        
        # Check if Caption Maker is configured
        ai_providers = self.config.get("ai_providers", {})
        cm_config = ai_providers.get("caption_maker", {})
        cm_api_key = cm_config.get("api_key", "").strip()
        
        if not cm_api_key:
            self.on_error(
                "No subtitle found for this video.\n\n"
                "You can use AI transcription (Whisper API) as a fallback,\n"
                "but Caption Maker is not configured yet.\n\n"
                "Please set it up in:\n"
                "Settings → AI API Settings → Caption Maker"
            )
            return
        
        # Bring window to front so dialog is visible
        self.lift()
        self.focus_force()
        
        # Show confirmation dialog
        result = messagebox.askyesno(
            "No Subtitle Found",
            "No subtitle available for this video.\n\n"
            "Would you like to use AI transcription (Whisper API) instead?\n\n"
            "This will use your Caption Maker API to transcribe the full video audio.\n"
            "Note: This may take a while and will consume Whisper API credits.",
            icon="question"
        )
        
        if result:
            # Switch processing page to 3-step transcription mode
            self.pages["processing"].switch_to_transcription_mode()
            # Refresh self.steps reference
            self.steps = self.pages["processing"].steps
            
            threading.Thread(
                target=self._run_whisper_transcription,
                args=(core, snf_error.video_path, snf_error.video_info, 
                      num_clips, snf_error.session_dir),
                daemon=True
            ).start()
        else:
            self.on_error(
                "No subtitle available for this video.\n\n"
                "Tips:\n"
                "1. Check available subtitles using 'Check Subtitles'\n"
                "2. Try a different subtitle language\n"
                "3. Use a video that has subtitles"
            )
    
    def _run_whisper_transcription(self, core, video_path: str, video_info: dict, 
                                    num_clips: int, session_dir: str):
        """Run Whisper transcription fallback in background thread."""
        try:
            result = core.find_highlights_with_transcription(
                video_path, video_info, num_clips, session_dir
            )
            
            if not self.cancelled and result:
                self.session_data = result
                self.after(0, self.show_highlight_selection)
            elif self.cancelled:
                self.after(0, self.on_cancelled)
                
        except Exception as e:
            error_msg = str(e)
            debug_log(f"ERROR (Whisper fallback): {error_msg}")
            log_error(f"Whisper transcription fallback failed", e)
            
            if self.cancelled or "cancel" in error_msg.lower():
                self.after(0, self.on_cancelled)
            else:
                self.after(0, lambda: self.on_error(error_msg))
    
    def show_highlight_selection(self):
        """Show highlight selection page with found highlights"""
        if not self.session_data:
            messagebox.showerror("Error", "No highlight data available")
            self.show_page("home")
            return
        
        # Set highlights in selection page (no video_path needed)
        self.pages["highlight_selection"].set_highlights(
            self.session_data["highlights"],
            self.session_data["session_dir"],
            self.session_data.get("url", "")
        )
        
        # Show the page
        self.show_page("highlight_selection")
        
        # Stop the loading spinner (find highlights finished)
        self.pages["processing"].stop_loading()
        
        # Reset processing flag
        self.processing = False
    
    def resume_session(self, session_data: dict):
        """Resume a previous session"""
        # Store session data
        self.session_data = session_data
        
        # Navigate to highlight selection page
        self.show_highlight_selection()
    
    def load_session_clips(self, clips_dir: Path):
        """Load clips from a session's clips folder and show results page"""
        # Change back button to go to session browser instead of processing
        self.pages["results"].set_back_callback(lambda: self.show_page("session_browser"))
        
        # Load clips from the specific directory
        self.pages["results"].load_clips(clips_dir)
        
        # Show results page
        self.pages["results"].show_results()
        self.show_page("results")
    
    def process_selected_highlights(self, selected_highlights: list, add_captions: bool = False, add_hook: bool = False, portrait_mode: str = "crop", subtitle_style: str = "pop", face_tracking_mode: str = "opencv", aspect_ratio: str = "9:16", resolution: str = "1080p", subtitle_sync_offset: float = -0.3, add_credit: bool = False):
        """NEW: Phase 2 - Process only selected highlights"""
        if not self.session_data:
            messagebox.showerror("Error", "No session data available")
            return
        
        # Store enhancement options
        self.add_captions = add_captions
        self.add_hook = add_hook
        self.add_credit = add_credit
        self.portrait_mode = portrait_mode
        self.subtitle_style = subtitle_style
        self.face_tracking_mode = face_tracking_mode
        self.aspect_ratio = aspect_ratio
        self.clip_resolution = resolution
        self.subtitle_sync_offset = subtitle_sync_offset
        # Persist choices for next time
        try:
            self.config.set("portrait_mode", portrait_mode)
            self.config.set("subtitle_style", subtitle_style)
            self.config.set("face_tracking_mode", face_tracking_mode)
            self.config.set("aspect_ratio", aspect_ratio)
            self.config.set("subtitle_sync_offset", float(subtitle_sync_offset))
        except Exception:
            pass
        
        # Check if session has URL (new flow) or video_path (old flow)
        has_url = bool(self.session_data.get("url", ""))
        has_video = bool(self.session_data.get("video_path", ""))
        
        if not has_url and not has_video:
            messagebox.showerror("Error", 
                "This session is missing both URL and video path.\n\n"
                "Please start a new session from the home page.")
            return
        
        if not has_url and has_video:
            # Old session format — check if video file still exists
            video_path = self.session_data["video_path"]
            if not Path(video_path).exists():
                messagebox.showerror("Error", 
                    "This is an old session and the video file no longer exists.\n\n"
                    "Please start a new session from the home page.")
                return
        
        # Reset UI for clipping
        self.processing = True
        self.cancelled = False
        
        # Reset clipping page UI
        self.pages["clipping"].reset_ui()
        self.show_page("clipping")
        
        # Start processing in background thread
        threading.Thread(
            target=self.run_process_selected,
            args=(selected_highlights,),
            daemon=True
        ).start()
    
    def run_process_selected(self, selected_highlights: list):
        """Process selected highlights in background thread"""
        try:
            from clipper_core import AutoClipperCore
            
            # Store total clips for progress tracking
            self.total_clips = len(selected_highlights)
            self.current_clip = 0
            
            # Wrapper for log callback with clipping progress
            def log_with_debug(msg):
                debug_log(msg)
                self.after(0, lambda: self.update_clipping_status(msg))
            
            # Get config
            ai_providers = self.config.get("ai_providers", {})
            highlight_finder = ai_providers.get("highlight_finder", {})
            system_prompt = highlight_finder.get("system_message") or self.config.get("system_prompt", None)
            
            temperature = self.config.get("temperature", 1.0)
            tts_model = self.config.get("tts_model", "tts-1")
            watermark_settings = self.config.get("watermark", {"enabled": False})
            credit_watermark_settings = self.config.get("credit_watermark", {"enabled": False})
            if getattr(self, "add_credit", False):
                credit_watermark_settings = dict(credit_watermark_settings)
                credit_watermark_settings["enabled"] = True
            hook_style_settings = self.config.get("hook_style", {})
            face_tracking_mode = getattr(self, "face_tracking_mode", self.config.get("face_tracking_mode", "opencv"))
            portrait_mode = self.config.get("portrait_mode", "crop")
            subtitle_style = self.config.get("subtitle_style", "pop")
            aspect_ratio = getattr(self, "aspect_ratio", self.config.get("aspect_ratio", "9:16"))
            subtitle_sync_offset = self.config.get("subtitle_sync_offset", -0.3)
            mediapipe_settings = self.config.get("mediapipe_settings", {
                "lip_activity_threshold": 0.15,
                "switch_threshold": 0.3,
                "min_shot_duration": 90,
                "center_weight": 0.3
            })            
            output_dir = self.config.get("output_dir", str(OUTPUT_DIR))
            model = self.config.get("model", "gpt-4.1")
            
            core = AutoClipperCore(
                client=self.client,
                ffmpeg_path=get_ffmpeg_path(),
                ytdlp_path=get_ytdlp_path(),
                output_dir=output_dir,
                model=model,
                tts_model=tts_model,
                temperature=temperature,
                system_prompt=system_prompt,
                watermark_settings=watermark_settings,
                credit_watermark_settings=credit_watermark_settings,
                hook_style_settings=hook_style_settings,
                face_tracking_mode=face_tracking_mode,
                portrait_mode=getattr(self, "portrait_mode", portrait_mode),
                subtitle_style=getattr(self, "subtitle_style", subtitle_style),
                aspect_ratio=aspect_ratio,
                mediapipe_settings=mediapipe_settings,
                ai_providers=self.config.get("ai_providers"),
                subtitle_language="id",  # Already downloaded
                subtitle_sync_offset=subtitle_sync_offset,
                log_callback=log_with_debug,
                progress_callback=lambda s, p: self.after(0, lambda: self.update_clipping_progress(s, p)),
                token_callback=lambda a, b, c, d: None,  # No token tracking for clipping
                cancel_check=lambda: self.cancelled
            )
            
            # Enable GPU acceleration if configured
            gpu_settings = self.config.get("gpu_acceleration", {})
            if gpu_settings.get("enabled", False):
                core.enable_gpu_acceleration(True)
            
            # Restore channel name from session data (needed for credit watermark)
            session_video_info = (self.session_data or {}).get("video_info") or {}
            core.channel_name = str(session_video_info.get("channel", "") or "").strip()
            if not core.channel_name:
                # Old sessions may have video_info: null in session_data.json.
                # Try to repair metadata from the URL so credit watermark works.
                session_url = self.session_data.get("url", "")
                if session_url:
                    try:
                        fetched = core.fetch_video_info(session_url)
                        if fetched.get("channel"):
                            core.channel_name = str(fetched.get("channel") or "").strip()
                            session_video_info.update(fetched)
                            self.session_data["video_info"] = session_video_info
                    except Exception as fetch_error:
                        debug_log(f"  ⚠ Could not fetch video info: {fetch_error}")
            if not core.channel_name:
                log_with_debug("  ⚠ No channel name in session, credit watermark will be skipped")
            
            # Process selected highlights
            # New flow: download sections per clip using URL
            # Old flow (backward compat): use existing video_path
            session_url = self.session_data.get("url", "")
            session_video_path = self.session_data.get("video_path", "")
            
            if session_url:
                # New flow: download video sections per clip
                core.process_selected_highlights(
                    session_url,
                    selected_highlights,
                    self.session_data["session_dir"],
                    add_captions=self.add_captions,
                    add_hook=self.add_hook,
                    resolution=getattr(self, "clip_resolution", "1080p")
                )
            elif session_video_path:
                # Old flow (backward compat): process from existing video
                # Use the old method signature with video_path
                self._process_old_session(core, session_video_path, selected_highlights)
            
            if not self.cancelled:
                self.after(0, self.on_clipping_complete)
                
        except Exception as e:
            error_msg = str(e)
            debug_log(f"ERROR: {error_msg}")
            log_error(f"Process selected highlights failed", e)
            
            if self.cancelled or "cancel" in error_msg.lower():
                self.after(0, self.on_clipping_cancelled)
            else:
                self.after(0, lambda: self.on_clipping_error(error_msg))
    
    def _process_old_session(self, core, video_path: str, selected_highlights: list):
        """Backward compatibility: process clips from an already-downloaded video (old session format)"""
        from pathlib import Path
        
        session_dir = Path(self.session_data["session_dir"])
        clips_dir = session_dir / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)
        
        total_clips = len(selected_highlights)
        for i, highlight in enumerate(selected_highlights, 1):
            if self.cancelled:
                return
            
            original_output_dir = core.output_dir
            core.output_dir = clips_dir
            
            try:
                core.process_clip(video_path, highlight, i, total_clips,
                                add_captions=self.add_captions, add_hook=self.add_hook,
                                pre_cut=False)
            finally:
                core.output_dir = original_output_dir
        
        core.set_progress("Cleaning up...", 0.95)
        core.cleanup()
        core.set_progress("Complete!", 1.0)
    
    def update_clipping_status(self, msg: str):
        """Update clipping page status"""
        self.pages["clipping"].update_status(msg)
    
    def update_clipping_progress(self, status: str, progress: float):
        """Update clipping progress from clipper_core"""
        # Parse status to extract clip number and title
        # Format: "Clip 1/3: Converting to portrait... (50%)"
        if "Clip " in status:
            try:
                # Extract clip number
                clip_part = status.split("Clip ")[1].split(":")[0]  # "1/3"
                current = int(clip_part.split("/")[0])
                total = int(clip_part.split("/")[1])
                
                # Extract title (everything after "Clip X/Y: " and before " (")
                title_part = status.split(": ", 1)[1]
                if " (" in title_part:
                    title = title_part.split(" (")[0]
                else:
                    title = title_part
                
                # Update UI
                self.pages["clipping"].update_progress(current, total, title)
                self.pages["clipping"].update_status(status)
            except:
                # Fallback: just update status
                self.pages["clipping"].update_status(status)
        else:
            # Not a clip progress message, just update status
            self.pages["clipping"].update_status(status)
    
    def cancel_processing(self):
        if messagebox.askyesno("Cancel", "Are you sure you want to cancel?"):
            self.cancelled = True
            # Update both pages
            if "processing" in self.pages:
                self.pages["processing"].update_status("⚠️ Cancelling... please wait")
                self.pages["processing"].cancel_btn.configure(state="disabled")
            if "clipping" in self.pages:
                self.pages["clipping"].update_status("⚠️ Cancelling... please wait")
                self.pages["clipping"].cancel_btn.configure(state="disabled")
    
    def on_cancelled(self):
        """Called when processing is cancelled"""
        self.processing = False
        self.pages["processing"].on_cancelled()
    
    def on_clipping_cancelled(self):
        """Called when clipping is cancelled"""
        self.processing = False
        self.pages["clipping"].on_cancelled()
    
    def on_complete(self):
        self.processing = False
        self.pages["processing"].on_complete()
        
        # Reset back button to default (processing page)
        self.pages["results"].set_back_callback(self.pages["results"].default_back_callback)
        
        # Load created clips in results page
        self.pages["results"].load_clips()
    
    def on_clipping_complete(self):
        """Called when clipping completes successfully"""
        self.processing = False
        self.pages["clipping"].on_complete()
    
    def on_clipping_error(self, error: str):
        """Called when clipping encounters an error"""
        self.processing = False
        self.pages["clipping"].on_error(error)
    
    def on_error(self, error):
        self.processing = False
        self.pages["processing"].on_error(error)
    
    def open_output(self):
        output_dir = self.config.get("output_dir", str(OUTPUT_DIR))
        if sys.platform == "win32":
            os.startfile(output_dir)
        else:
            subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", output_dir])
    
    def open_current_clips(self):
        """Show the current session's created clips on the results page"""
        try:
            if self.session_data and self.session_data.get("session_dir"):
                clips_dir = Path(self.session_data["session_dir"]) / "clips"
                if clips_dir.exists():
                    # Back button returns to session browser
                    self.pages["results"].set_back_callback(lambda: self.show_page("session_browser"))
                    # Load clips from the session's clips folder
                    self.pages["results"].load_clips(clips_dir)
                    # Show results page
                    self.pages["results"].show_results()
                    self.show_page("results")
                    return
            self.open_output()
        except Exception as e:
            self.log_panel.append(f"Error viewing clips: {e}")
    
    def open_discord(self):
        """Open Discord server invite link"""
        import webbrowser
        webbrowser.open("https://s.id/ytsdiscord")
    
    def open_github(self):
        """Open GitHub repository"""
        import webbrowser
        webbrowser.open("https://github.com/jipraks/yt-short-clipper")
    
    def check_update_silent(self):
        """Check for updates silently on startup"""
        try:
            # Get installation_id from config
            installation_id = self.config.get("installation_id", "unknown")
            url = f"{UPDATE_CHECK_URL}?installation_id={installation_id}&app_version={__version__}"
            
            req = urllib.request.Request(url, headers={'User-Agent': 'YT-Short-Clipper'})
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode())
                latest_version = data.get("version", "")
                download_url = data.get("download_url", "")
                changelog = data.get("changelog", "")
                
                if latest_version and self._compare_versions(latest_version, __version__) > 0:
                    # New version available
                    self.after(0, lambda: self._show_update_notification(latest_version, download_url, changelog))
        except Exception as e:
            debug_log(f"Update check failed: {e}")
    
    def check_update_manual(self):
        """Check for updates manually from settings page"""
        try:
            # Get installation_id from config
            installation_id = self.config.get("installation_id", "unknown")
            url = f"{UPDATE_CHECK_URL}?installation_id={installation_id}&app_version={__version__}"
            
            req = urllib.request.Request(url, headers={'User-Agent': 'YT-Short-Clipper'})
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode())
                latest_version = data.get("version", "")
                download_url = data.get("download_url", "")
                changelog = data.get("changelog", "")
                
                if not latest_version:
                    messagebox.showinfo("Update Check", "Could not retrieve version information.")
                    return
                
                comparison = self._compare_versions(latest_version, __version__)
                
                if comparison > 0:
                    # New version available
                    msg = f"New version available: {latest_version}\nCurrent version: {__version__}\n\n"
                    if changelog:
                        msg += f"Changelog:\n{changelog}\n\n"
                    msg += f"Download: {download_url}"
                    
                    if messagebox.askyesno("Update Available", msg + "\n\nOpen download page?"):
                        import webbrowser
                        webbrowser.open(download_url)
                elif comparison == 0:
                    messagebox.showinfo("Update Check", f"You are using the latest version ({__version__})")
                else:
                    messagebox.showinfo("Update Check", f"Your version ({__version__}) is newer than the latest release ({latest_version})")
        except Exception as e:
            messagebox.showerror("Update Check Failed", f"Could not check for updates:\n{str(e)}")
    
    def _compare_versions(self, v1: str, v2: str) -> int:
        """Compare two version strings. Returns: 1 if v1 > v2, -1 if v1 < v2, 0 if equal"""
        try:
            parts1 = [int(x) for x in v1.split('.')]
            parts2 = [int(x) for x in v2.split('.')]
            
            # Pad shorter version with zeros
            max_len = max(len(parts1), len(parts2))
            parts1 += [0] * (max_len - len(parts1))
            parts2 += [0] * (max_len - len(parts2))
            
            for p1, p2 in zip(parts1, parts2):
                if p1 > p2:
                    return 1
                elif p1 < p2:
                    return -1
            return 0
        except:
            return 0
    
    def _show_update_notification(self, latest_version: str, download_url: str, changelog: str = ""):
        """Show update notification popup"""
        msg = f"New version available: {latest_version}\nCurrent version: {__version__}\n\n"
        if changelog:
            msg += f"What's new:\n{changelog}\n\n"
        msg += "Would you like to download it?"
        
        if messagebox.askyesno("Update Available", msg):
            import webbrowser
            webbrowser.open(download_url)


def handle_exception(exc_type, exc_value, exc_traceback):
    """Global exception handler to log uncaught exceptions"""
    # Don't log KeyboardInterrupt
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    
    # Log the exception
    log_error("Uncaught exception", exc_value)
    
    # Show error dialog to user
    try:
        import tkinter.messagebox as mb
        error_log = get_error_log_path()
        msg = f"An unexpected error occurred:\n\n{exc_value}\n\n"
        if error_log:
            msg += f"Error details saved to:\n{error_log}\n\n"
        msg += "Please report this issue with the error.log file."
        mb.showerror("Unexpected Error", msg)
    except:
        pass
    
    # Call default handler
    sys.__excepthook__(exc_type, exc_value, exc_traceback)


def main():
    # Set global exception handler
    sys.excepthook = handle_exception
    
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    app = YTShortClipperApp()
    app.mainloop()


if __name__ == "__main__":
    main()
