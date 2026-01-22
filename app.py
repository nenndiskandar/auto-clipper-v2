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

# Import version info
from version import __version__, UPDATE_CHECK_URL

# Import utilities
from utils.helpers import get_app_dir, get_bundle_dir, get_ffmpeg_path, get_ytdlp_path, extract_video_id
from utils.logger import debug_log, setup_error_logging, log_error, get_error_log_path
from config.config_manager import ConfigManager
from dialogs.model_selector import SearchableModelDropdown
from dialogs.youtube_upload import YouTubeUploadDialog
from components.progress_step import ProgressStep
from pages.settings_page import SettingsPage
from pages.browse_page import BrowsePage
from pages.results_page import ResultsPage
from pages.status_pages import APIStatusPage, LibStatusPage
from pages.processing_page import ProcessingPage
from pages.contact_page import ContactPage

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
        
        self.title("YT Short Clipper")
        self.geometry("680x780")
        self.resizable(False, False)
        
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        
        # Set app icon after window is created
        self.after(200, self.set_app_icon)
        
        self.container = ctk.CTkFrame(self)
        self.container.pack(fill="both", expand=True)
        
        self.pages = {}
        self.create_home_page()
        self.create_processing_page()
        self.create_results_page()
        self.create_browse_page()
        self.create_settings_page()
        self.create_api_status_page()
        self.create_lib_status_page()
        self.create_contact_page()
        
        self.show_page("home")
        self.load_config()
        self.check_youtube_status()
        
        # Check for updates on startup
        threading.Thread(target=self.check_update_silent, daemon=True).start()
    
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
        for page in self.pages.values():
            page.pack_forget()
        self.pages[name].pack(fill="both", expand=True)
        
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
    
    def reset_home_page(self):
        """Reset home page to initial state"""
        # Clear URL input
        self.url_var.set("")
        
        # Reset thumbnail - recreate preview placeholder
        self.current_thumbnail = None
        self.create_preview_placeholder()
        
        # Reset clips input to default
        self.clips_var.set("5")
        
        # Reset toggles to default
        self.caption_var.set(True)
        self.hook_var.set(True)
        
        # Update switch texts
        self.caption_switch.configure(text="ON")
        self.hook_switch.configure(text="ON")
        
        # Disable start button
        self.start_btn.configure(state="disabled", fg_color="gray", hover_color="gray")

    def create_home_page(self):
        page = ctk.CTkFrame(self.container, fg_color=("#1a1a1a", "#0a0a0a"))
        self.pages["home"] = page
        
        # Top bar with icon and navigation
        top = ctk.CTkFrame(page, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(15, 10))
        
        # App icon + title on left
        title_frame = ctk.CTkFrame(top, fg_color="transparent")
        title_frame.pack(side="left")
        
        if ICON_PATH.exists():
            try:
                icon_img = Image.open(ICON_PATH)
                icon_img.thumbnail((40, 40), Image.Resampling.LANCZOS)
                self.header_icon = ctk.CTkImage(light_image=icon_img, dark_image=icon_img, size=(40, 40))
                ctk.CTkLabel(title_frame, image=self.header_icon, text="").pack(side="left", padx=(0, 12))
            except:
                pass
        
        title_col = ctk.CTkFrame(title_frame, fg_color="transparent")
        title_col.pack(side="left")
        ctk.CTkLabel(title_col, text="YT Short Clipper", font=ctk.CTkFont(size=20, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(title_col, text="Turn long videos into viral shorts", font=ctk.CTkFont(size=11), 
            text_color="gray").pack(anchor="w")
        
        # Right side navigation buttons with icons
        nav_frame = ctk.CTkFrame(top, fg_color="transparent")
        nav_frame.pack(side="right")
        
        # Load button icons
        try:
            settings_img = Image.open(ASSETS_DIR / "settings.png")
            settings_img.thumbnail((18, 18), Image.Resampling.LANCZOS)
            self.settings_icon = ctk.CTkImage(light_image=settings_img, dark_image=settings_img, size=(18, 18))
            
            api_img = Image.open(ASSETS_DIR / "api-status.png")
            api_img.thumbnail((18, 18), Image.Resampling.LANCZOS)
            self.api_icon = ctk.CTkImage(light_image=api_img, dark_image=api_img, size=(18, 18))
            
            lib_img = Image.open(ASSETS_DIR / "lib-status.png")
            lib_img.thumbnail((18, 18), Image.Resampling.LANCZOS)
            self.lib_icon = ctk.CTkImage(light_image=lib_img, dark_image=lib_img, size=(18, 18))
            
            # Load icons for main buttons
            play_img = Image.open(ASSETS_DIR / "play.png")
            play_img.thumbnail((20, 20), Image.Resampling.LANCZOS)
            self.play_icon = ctk.CTkImage(light_image=play_img, dark_image=play_img, size=(20, 20))
            
            browse_img = Image.open(ASSETS_DIR / "lib-status.png")
            browse_img.thumbnail((18, 18), Image.Resampling.LANCZOS)
            self.browse_icon = ctk.CTkImage(light_image=browse_img, dark_image=browse_img, size=(18, 18))
            
            # Load refresh icon for status pages
            refresh_img = Image.open(ASSETS_DIR / "refresh.png")
            refresh_img.thumbnail((20, 20), Image.Resampling.LANCZOS)
            self.refresh_icon = ctk.CTkImage(light_image=refresh_img, dark_image=refresh_img, size=(20, 20))
        except Exception as e:
            # Fallback to text if icons not found
            debug_log(f"Icon load error: {e}")
            self.settings_icon = None
            self.api_icon = None
            self.lib_icon = None
            self.play_icon = None
            self.browse_icon = None
            self.refresh_icon = None
        
        # Navigation buttons with rounded style
        ctk.CTkButton(nav_frame, text="Settings", image=self.settings_icon, compound="left",
            width=90, height=40, font=ctk.CTkFont(size=11),
            fg_color=("#2b2b2b", "#1a1a1a"), hover_color=("#3a3a3a", "#2a2a2a"), corner_radius=10,
            command=lambda: self.show_page("settings")).pack(side="left", padx=3)
        
        ctk.CTkButton(nav_frame, text="API", image=self.api_icon, compound="left",
            width=70, height=40, font=ctk.CTkFont(size=11),
            fg_color=("#2b2b2b", "#1a1a1a"), hover_color=("#3a3a3a", "#2a2a2a"), corner_radius=10,
            command=lambda: self.show_page("api_status")).pack(side="left", padx=3)
        
        ctk.CTkButton(nav_frame, text="Library", image=self.lib_icon, compound="left",
            width=85, height=40, font=ctk.CTkFont(size=11),
            fg_color=("#2b2b2b", "#1a1a1a"), hover_color=("#3a3a3a", "#2a2a2a"), corner_radius=10,
            command=lambda: self.show_page("lib_status")).pack(side="left", padx=3)
        
        # Main content area - two columns
        main = ctk.CTkFrame(page, fg_color="transparent")
        main.pack(fill="both", expand=True, padx=20, pady=(10, 20))
        
        # Left column - Configuration
        left_col = ctk.CTkFrame(main, fg_color="transparent")
        left_col.pack(side="left", fill="both", expand=True, padx=(0, 10))
        
        # YouTube URL input
        ctk.CTkLabel(left_col, text="YouTube URL", font=ctk.CTkFont(size=12, weight="bold"), 
            anchor="w").pack(fill="x", pady=(0, 5))
        
        url_frame = ctk.CTkFrame(left_col, fg_color=("#2b2b2b", "#1a1a1a"), corner_radius=8)
        url_frame.pack(fill="x", pady=(0, 15))
        
        # URL input with paste button
        url_input_container = ctk.CTkFrame(url_frame, fg_color="transparent")
        url_input_container.pack(fill="x", padx=8, pady=8)
        
        self.url_var = ctk.StringVar()
        self.url_var.trace("w", self.on_url_change)
        url_entry = ctk.CTkEntry(url_input_container, textvariable=self.url_var, 
            placeholder_text="Paste YouTube link here...", height=40, border_width=0,
            fg_color="transparent")
        url_entry.pack(side="left", fill="x", expand=True, padx=(4, 8))
        
        # Paste button
        paste_btn = ctk.CTkButton(url_input_container, text="📋 Paste", width=80, height=36,
            fg_color=("#3a3a3a", "#2a2a2a"), hover_color=("#4a4a4a", "#3a3a3a"),
            font=ctk.CTkFont(size=11), command=self.paste_url)
        paste_btn.pack(side="right")
        
        # Clip Configuration section
        config_frame = ctk.CTkFrame(left_col, fg_color=("#2b2b2b", "#1a1a1a"), corner_radius=10)
        config_frame.pack(fill="x", pady=(0, 15))
        
        ctk.CTkLabel(config_frame, text="Clip Configuration", font=ctk.CTkFont(size=12, weight="bold"), 
            anchor="w").pack(fill="x", padx=15, pady=(12, 8))
        
        # Clips Count
        clips_row = ctk.CTkFrame(config_frame, fg_color="transparent")
        clips_row.pack(fill="x", padx=15, pady=(0, 12))
        
        ctk.CTkLabel(clips_row, text="Clips Count", font=ctk.CTkFont(size=11), 
            anchor="w").pack(side="left", fill="x", expand=True)
        
        clips_input_frame = ctk.CTkFrame(clips_row, fg_color="transparent")
        clips_input_frame.pack(side="right")
        
        self.clips_var = ctk.StringVar(value="5")
        clips_entry = ctk.CTkEntry(clips_input_frame, textvariable=self.clips_var, width=80, height=32,
            fg_color=("#3a3a3a", "#2a2a2a"), border_width=0, justify="center")
        clips_entry.pack(side="left", padx=(0, 5))
        
        ctk.CTkLabel(clips_input_frame, text="(1-10)", font=ctk.CTkFont(size=10), 
            text_color="gray").pack(side="left")
        
        # Enhancements section
        enhance_frame = ctk.CTkFrame(left_col, fg_color=("#2b2b2b", "#1a1a1a"), corner_radius=10)
        enhance_frame.pack(fill="x", pady=(0, 15))
        
        ctk.CTkLabel(enhance_frame, text="Enhancements", font=ctk.CTkFont(size=12, weight="bold"), 
            anchor="w").pack(fill="x", padx=15, pady=(12, 8))
        
        # Captions toggle
        captions_row = ctk.CTkFrame(enhance_frame, fg_color="transparent")
        captions_row.pack(fill="x", padx=15, pady=(0, 8))
        
        captions_left = ctk.CTkFrame(captions_row, fg_color="transparent")
        captions_left.pack(side="left", fill="x", expand=True)
        
        ctk.CTkLabel(captions_left, text="💬 Captions", font=ctk.CTkFont(size=11, weight="bold"), 
            anchor="w").pack(anchor="w")
        
        self.caption_var = ctk.BooleanVar(value=True)
        caption_switch = ctk.CTkSwitch(captions_row, text="ON", variable=self.caption_var, 
            width=60, command=self.update_caption_switch_text)
        caption_switch.pack(side="right")
        self.caption_switch = caption_switch
        
        # Hook Text toggle
        hook_row = ctk.CTkFrame(enhance_frame, fg_color="transparent")
        hook_row.pack(fill="x", padx=15, pady=(0, 12))
        
        hook_left = ctk.CTkFrame(hook_row, fg_color="transparent")
        hook_left.pack(side="left", fill="x", expand=True)
        
        ctk.CTkLabel(hook_left, text="🪝 Hook Text", font=ctk.CTkFont(size=11, weight="bold"), 
            anchor="w").pack(anchor="w")
        
        self.hook_var = ctk.BooleanVar(value=True)
        hook_switch = ctk.CTkSwitch(hook_row, text="ON", variable=self.hook_var, 
            width=60, command=self.update_hook_switch_text)
        hook_switch.pack(side="right")
        self.hook_switch = hook_switch
        
        # Generate Shorts button
        self.start_btn = ctk.CTkButton(left_col, text="Generate Shorts", image=self.play_icon, 
            compound="left", font=ctk.CTkFont(size=15, weight="bold"), 
            height=50, command=self.start_processing, state="disabled", 
            fg_color="gray", hover_color="gray", corner_radius=10)
        self.start_btn.pack(fill="x", pady=(0, 8))
        
        # Browse Videos link
        browse_link = ctk.CTkLabel(left_col, text="📂 Browse Videos", 
            font=ctk.CTkFont(size=11), text_color=("#3B8ED0", "#1F6AA5"), cursor="hand2")
        browse_link.pack(pady=(0, 0))
        browse_link.bind("<Button-1>", lambda e: self.show_page("browse"))
        
        # Right column - Video Preview
        right_col = ctk.CTkFrame(main, fg_color="transparent")
        right_col.pack(side="right", fill="both", expand=True, padx=(10, 0))
        
        # Video preview frame with landscape aspect ratio for YouTube thumbnails
        self.thumb_frame = ctk.CTkFrame(right_col, width=400, height=520, 
            fg_color=("#2b2b2b", "#1a1a1a"), corner_radius=15)
        self.thumb_frame.pack(fill="both", expand=True)
        self.thumb_frame.pack_propagate(False)
        
        # Preview content container (will be recreated when showing thumbnail)
        self.create_preview_placeholder()
        
        # Footer - Contact links with separator line
        footer = ctk.CTkFrame(page, fg_color="transparent", height=60)
        footer.pack(fill="x", padx=20, pady=(10, 15), side="bottom")
        footer.pack_propagate(False)
        
        # Separator line
        separator = ctk.CTkFrame(footer, height=1, fg_color=("#3a3a3a", "#2a2a2a"))
        separator.pack(fill="x", pady=(0, 12))
        
        # Footer content
        footer_content = ctk.CTkFrame(footer, fg_color="transparent")
        footer_content.pack(fill="x")
        
        # Copyright text on left with dynamic year and version
        from datetime import datetime
        current_year = datetime.now().year
        ctk.CTkLabel(footer_content, text=f"© {current_year} YT Short Clipper • v{__version__}", 
            font=ctk.CTkFont(size=10), text_color="gray", anchor="w").pack(side="left")
        
        # Links on right
        links_frame = ctk.CTkFrame(footer_content, fg_color="transparent")
        links_frame.pack(side="right")
        
        # GitHub link
        github_link = ctk.CTkLabel(links_frame, text="⭐ GitHub", 
            font=ctk.CTkFont(size=11), text_color="#ffffff", cursor="hand2")
        github_link.pack(side="left", padx=(0, 15))
        github_link.bind("<Button-1>", lambda e: self.open_github())
        
        # Join Discord link (blurple)
        discord_link = ctk.CTkLabel(links_frame, text="💬 Join Discord Server", 
            font=ctk.CTkFont(size=11), text_color="#5865F2", cursor="hand2")
        discord_link.pack(side="left")
        discord_link.bind("<Button-1>", lambda e: self.open_discord())
    
    def create_preview_placeholder(self):
        """Create placeholder content for video preview"""
        # Clear existing content
        for widget in self.thumb_frame.winfo_children():
            widget.destroy()
        
        # Preview content container
        preview_container = ctk.CTkFrame(self.thumb_frame, fg_color="transparent")
        preview_container.place(relx=0.5, rely=0.5, anchor="center")
        
        # Play button icon (large)
        play_circle = ctk.CTkFrame(preview_container, width=80, height=80, 
            fg_color=("#3a3a3a", "#2a2a2a"), corner_radius=40)
        play_circle.pack(pady=(0, 15))
        play_circle.pack_propagate(False)
        
        if self.play_icon:
            play_label = ctk.CTkLabel(play_circle, image=self.play_icon, text="")
            play_label.place(relx=0.5, rely=0.5, anchor="center")
        
        # Placeholder text
        self.thumb_label = ctk.CTkLabel(preview_container, 
            text="Paste a YouTube link\nto preview a video", 
            font=ctk.CTkFont(size=13), text_color="gray", justify="center")
        self.thumb_label.pack()
    
    def paste_url(self):
        """Paste URL from clipboard"""
        try:
            # Get clipboard content
            clipboard_text = self.clipboard_get()
            if clipboard_text:
                self.url_var.set(clipboard_text.strip())
        except Exception as e:
            debug_log(f"Paste error: {e}")
            # If clipboard is empty or error, do nothing
            pass
    
    def update_caption_switch_text(self):
        """Update caption switch text based on state"""
        self.caption_switch.configure(text="ON" if self.caption_var.get() else "OFF")
    
    def update_hook_switch_text(self):
        """Update hook switch text based on state"""
        self.hook_switch.configure(text="ON" if self.hook_var.get() else "OFF")

    def create_processing_page(self):
        """Create processing page as embedded frame"""
        self.pages["processing"] = ProcessingPage(
            self.container,
            self.cancel_processing,
            lambda: self.show_page("home"),
            self.open_output,
            self.show_browse_after_complete
        )
        # Keep reference to steps for update_progress
        self.steps = self.pages["processing"].steps
    
    def create_results_page(self):
        """Create results page as embedded frame"""
        self.pages["results"] = ResultsPage(
            self.container,
            self.config,
            self.client,
            lambda: self.show_page("processing"),
            lambda: self.show_page("home"),
            self.open_output
        )
    
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
            self.refresh_icon
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
    
    def on_settings_saved(self, api_key, base_url, model):
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        # Update config will be reflected when user returns to home page
    
    def on_url_change(self, *args):
        url = self.url_var.get().strip()
        video_id = extract_video_id(url)
        if video_id:
            self.load_thumbnail(video_id)
        else:
            self.current_thumbnail = None
            # Recreate placeholder
            self.create_preview_placeholder()
            # Disable start button when URL is invalid
            self.start_btn.configure(state="disabled", fg_color="gray", hover_color="gray")
    
    def load_thumbnail(self, video_id: str):
        def fetch():
            try:
                for quality in ["maxresdefault", "hqdefault", "mqdefault"]:
                    try:
                        url = f"https://img.youtube.com/vi/{video_id}/{quality}.jpg"
                        with urllib.request.urlopen(url, timeout=5) as r:
                            data = r.read()
                        img = Image.open(io.BytesIO(data))
                        if img.size[0] > 120:
                            break
                    except:
                        continue
                # Resize to fit preview area in landscape (16:9 aspect ratio)
                # Max width 380px to fit in the frame with padding
                img.thumbnail((380, 214), Image.Resampling.LANCZOS)
                self.after(0, lambda: self.show_thumbnail(img))
            except:
                self.after(0, lambda: self.on_thumbnail_error())
        
        # Clear image reference properly before loading new one
        self.current_thumbnail = None
        
        # Show loading state
        for widget in self.thumb_frame.winfo_children():
            widget.destroy()
        
        loading_container = ctk.CTkFrame(self.thumb_frame, fg_color="transparent")
        loading_container.place(relx=0.5, rely=0.5, anchor="center")
        
        self.thumb_label = ctk.CTkLabel(loading_container, text="Loading...", 
            font=ctk.CTkFont(size=13), text_color="gray")
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
            font=ctk.CTkFont(size=13), text_color="gray", justify="center")
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
            
            # Enable start button when thumbnail loads successfully
            self.start_btn.configure(state="normal", fg_color=("#3B8ED0", "#1F6AA5"), 
                hover_color=("#36719F", "#144870"))
        except Exception as e:
            debug_log(f"Error showing thumbnail: {e}")
            # If thumbnail fails, just enable the button anyway
            self.start_btn.configure(state="normal", fg_color=("#3B8ED0", "#1F6AA5"), 
                hover_color=("#36719F", "#144870"))

    def start_processing(self):
        if not self.client:
            messagebox.showerror("Error", "Configure API settings first!\nClick ⚙️ button.")
            return
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
        
        # Get options
        add_captions = self.caption_var.get()
        add_hook = self.hook_var.get()
        
        # Reset UI
        self.processing = True
        self.cancelled = False
        self.token_usage = {"gpt_input": 0, "gpt_output": 0, "whisper_seconds": 0, "tts_chars": 0}
        
        # Reset processing page UI
        self.pages["processing"].reset_ui()
        
        self.show_page("processing")
        
        output_dir = self.config.get("output_dir", str(OUTPUT_DIR))
        model = self.config.get("model", "gpt-4.1")
        
        threading.Thread(target=self.run_processing, args=(url, num_clips, output_dir, model, add_captions, add_hook), daemon=True).start()
    
    def run_processing(self, url, num_clips, output_dir, model, add_captions, add_hook):
        try:
            from clipper_core import AutoClipperCore
            
            # Wrapper for log callback that also logs to console in debug mode
            def log_with_debug(msg):
                debug_log(msg)
                self.after(0, lambda: self.update_status(msg))
            
            # Get system prompt from config
            system_prompt = self.config.get("system_prompt", None)
            temperature = self.config.get("temperature", 1.0)
            tts_model = self.config.get("tts_model", "tts-1")
            watermark_settings = self.config.get("watermark", {"enabled": False})
            
            # Get face tracking mode from config (set in settings page)
            face_tracking_mode = self.config.get("face_tracking_mode", "opencv")
            
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
                face_tracking_mode=face_tracking_mode,
                mediapipe_settings=mediapipe_settings,
                log_callback=log_with_debug,
                progress_callback=lambda s, p: self.after(0, lambda: self.update_progress(s, p)),
                token_callback=lambda a, b, c, d: self.after(0, lambda: self.update_tokens(a, b, c, d)),
                cancel_check=lambda: self.cancelled
            )
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

    def update_status(self, msg):
        self.pages["processing"].update_status(msg)
    
    def update_progress(self, status, progress):
        print(f"[DEBUG] update_progress called: status='{status}', progress={progress}")
        self.pages["processing"].update_status(status)
        
        # Update step indicators based on status text
        status_lower = status.lower()
        
        # Parse progress percentage from status if available
        # Try multiple formats: (51%) or 51.2% or 51%
        progress_match = re.search(r'\((\d+(?:\.\d+)?)%\)|(\d+(?:\.\d+)?)%', status)
        if progress_match:
            # Get the first non-None group
            step_progress = float(progress_match.group(1) or progress_match.group(2)) / 100
        else:
            step_progress = None
        
        print(f"[DEBUG] Parsed step_progress: {step_progress}")
        
        if "download" in status_lower:
            if step_progress is None:
                step_progress = 0.0
            self.steps[0].set_active(status, step_progress)
            self.steps[1].reset()
            self.steps[2].reset()
            self.steps[3].reset()
        elif "highlight" in status_lower or "finding" in status_lower:
            self.steps[0].set_done("Downloaded")
            self.steps[1].set_active(status, step_progress)
            self.steps[2].reset()
            self.steps[3].reset()
        elif "clip" in status_lower:
            self.steps[0].set_done("Downloaded")
            self.steps[1].set_done("Found highlights")
            
            # Parse clip progress and sub-step progress
            if "cutting" in status_lower:
                # Show progress bar even if no percentage yet
                if step_progress is None:
                    step_progress = 0.0
                self.steps[2].set_active(status, step_progress)
                self.steps[3].reset()
            elif "portrait" in status_lower or "converting" in status_lower:
                if step_progress is None:
                    step_progress = 0.0
                self.steps[2].set_active(status, step_progress)
                self.steps[3].reset()
            elif "hook" in status_lower:
                if step_progress is None:
                    step_progress = 0.0
                self.steps[2].set_active(status, step_progress)
                self.steps[3].reset()
            elif "caption" in status_lower:
                if step_progress is None:
                    step_progress = 0.0
                # Only show progress in step 3 (Creating clips), not step 4
                self.steps[2].set_active(status, step_progress)
                self.steps[3].reset()
            elif "done" in status_lower:
                # Extract clip number to show progress
                match = re.search(r'Clip (\d+)/(\d+)', status)
                if match:
                    current, total = int(match.group(1)), int(match.group(2))
                    percent = current / total
                    self.steps[2].set_active(f"Clip {current}/{total} complete", percent)
                else:
                    self.steps[2].set_active(status, step_progress)
                self.steps[3].reset()
            else:
                self.steps[2].set_active(status, step_progress)
                self.steps[3].reset()
        elif "clean" in status_lower:
            self.steps[0].set_done("Downloaded")
            self.steps[1].set_done("Found highlights")
            self.steps[2].set_done("All clips created")
            self.steps[3].set_active("Cleaning up...", step_progress)
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
    
    def cancel_processing(self):
        if messagebox.askyesno("Cancel", "Are you sure you want to cancel?"):
            self.cancelled = True
            self.pages["processing"].update_status("⚠️ Cancelling... please wait")
            self.pages["processing"].cancel_btn.configure(state="disabled")
    
    def on_cancelled(self):
        """Called when processing is cancelled"""
        self.processing = False
        self.pages["processing"].on_cancelled()
    
    def on_complete(self):
        self.processing = False
        self.pages["processing"].on_complete()
        
        # Load created clips in results page
        self.pages["results"].load_clips()
    
    def show_browse_after_complete(self):
        """Show browse page after processing complete"""
        self.show_page("browse")
    
    def on_error(self, error):
        self.processing = False
        self.pages["processing"].on_error(error)
    
    def open_output(self):
        output_dir = self.config.get("output_dir", str(OUTPUT_DIR))
        if sys.platform == "win32":
            os.startfile(output_dir)
        else:
            subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", output_dir])
    
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
            url = f"{UPDATE_CHECK_URL}?installation_id={installation_id}"
            
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
            url = f"{UPDATE_CHECK_URL}?installation_id={installation_id}"
            
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
