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

if getattr(sys, 'frozen', False):
    # Running as compiled exe
    APP_DIR = Path(sys.executable).parent
    # For bundled resources, use sys._MEIPASS
    BUNDLE_DIR = Path(sys._MEIPASS) if hasattr(sys, '_MEIPASS') else APP_DIR
else:
    # Running as script
    APP_DIR = Path(__file__).parent
    BUNDLE_DIR = APP_DIR

CONFIG_FILE = APP_DIR / "config.json"
OUTPUT_DIR = APP_DIR / "output"
ASSETS_DIR = BUNDLE_DIR / "assets"
ICON_PATH = ASSETS_DIR / "icon.png"
ICON_ICO_PATH = ASSETS_DIR / "icon.ico"


def get_ffmpeg_path():
    if getattr(sys, 'frozen', False):
        bundled = APP_DIR / "ffmpeg" / "ffmpeg.exe"
        if bundled.exists():
            return str(bundled)
    return "ffmpeg"


def get_ytdlp_path():
    if getattr(sys, 'frozen', False):
        bundled = APP_DIR / "yt-dlp.exe"
        if bundled.exists():
            return str(bundled)
    return "yt-dlp"


def extract_video_id(url: str) -> str:
    patterns = [r'(?:v=|\/)([0-9A-Za-z_-]{11}).*', r'(?:youtu\.be\/)([0-9A-Za-z_-]{11})']
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


class ConfigManager:
    def __init__(self):
        self.config = self.load()
    
    def load(self):
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        return {"api_key": "", "base_url": "https://api.openai.com/v1", "model": "gpt-4.1", "output_dir": str(OUTPUT_DIR)}

    def save(self):
        with open(CONFIG_FILE, "w") as f:
            json.dump(self.config, f, indent=2)
    
    def get(self, key, default=None):
        return self.config.get(key, default)
    
    def set(self, key, value):
        self.config[key] = value
        self.save()


class SearchableModelDropdown(ctk.CTkToplevel):
    def __init__(self, parent, models: list, current_value: str, callback):
        super().__init__(parent)
        self.callback = callback
        self.models = models
        self.filtered_models = models.copy()
        
        self.title("Select Model")
        self.geometry("400x500")
        self.transient(parent)
        self.grab_set()
        
        self.search_var = ctk.StringVar()
        self.search_var.trace("w", self.filter_models)
        
        search_entry = ctk.CTkEntry(self, textvariable=self.search_var, placeholder_text="🔍 Search models...", height=40)
        search_entry.pack(fill="x", padx=10, pady=10)
        search_entry.focus()
        
        self.list_frame = ctk.CTkScrollableFrame(self, height=400)
        self.list_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        
        self.model_buttons = []
        self.current_value = current_value
        self.render_models()
    
    def render_models(self):
        for btn in self.model_buttons:
            btn.destroy()
        self.model_buttons.clear()
        for model in self.filtered_models:
            is_selected = model == self.current_value
            btn = ctk.CTkButton(self.list_frame, text=model, anchor="w",
                fg_color=("gray75", "gray25") if is_selected else "transparent",
                hover_color=("gray70", "gray30"), text_color=("gray10", "gray90"),
                command=lambda m=model: self.select_model(m))
            btn.pack(fill="x", pady=1)
            self.model_buttons.append(btn)
    
    def filter_models(self, *args):
        search = self.search_var.get().lower()
        self.filtered_models = [m for m in self.models if search in m.lower()] if search else self.models.copy()
        self.render_models()
    
    def select_model(self, model: str):
        self.callback(model)
        self.destroy()


class YouTubeUploadDialog(ctk.CTkToplevel):
    """Dialog for uploading video to YouTube with SEO metadata"""
    
    def __init__(self, parent, clip: dict, openai_client, model: str):
        super().__init__(parent)
        self.clip = clip
        self.openai_client = openai_client
        self.model = model
        self.uploading = False
        
        self.title("Upload to YouTube")
        self.geometry("550x700")  # Increased height for schedule inputs
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        
        self.create_ui()
        self.generate_seo_metadata()
    
    def create_ui(self):
        main = ctk.CTkFrame(self)
        main.pack(fill="both", expand=True, padx=20, pady=20)
        
        # Header
        ctk.CTkLabel(main, text="📤 Upload to YouTube", font=ctk.CTkFont(size=18, weight="bold")).pack(pady=(0, 15))
        
        # Video info
        info_frame = ctk.CTkFrame(main, fg_color=("gray85", "gray20"))
        info_frame.pack(fill="x", pady=(0, 15))
        ctk.CTkLabel(info_frame, text=f"📹 {self.clip['title'][:50]}", anchor="w").pack(fill="x", padx=10, pady=10)
        
        # Scrollable content area
        scroll_frame = ctk.CTkScrollableFrame(main, height=400)
        scroll_frame.pack(fill="both", expand=True, pady=(0, 10))
        
        # Title
        ctk.CTkLabel(scroll_frame, text="Title (max 100 chars)", anchor="w", font=ctk.CTkFont(weight="bold")).pack(fill="x", pady=(5, 0))
        self.title_entry = ctk.CTkEntry(scroll_frame, height=40)
        self.title_entry.pack(fill="x", pady=(5, 0))
        self.title_count = ctk.CTkLabel(scroll_frame, text="0/100", text_color="gray", anchor="e")
        self.title_count.pack(fill="x")
        self.title_entry.bind("<KeyRelease>", self.update_title_count)
        
        # Description
        ctk.CTkLabel(scroll_frame, text="Description", anchor="w", font=ctk.CTkFont(weight="bold")).pack(fill="x", pady=(10, 0))
        self.desc_text = ctk.CTkTextbox(scroll_frame, height=120)
        self.desc_text.pack(fill="x", pady=(5, 0))
        self.desc_count = ctk.CTkLabel(scroll_frame, text="0/5000", text_color="gray", anchor="e")
        self.desc_count.pack(fill="x")
        self.desc_text.bind("<KeyRelease>", self.update_desc_count)
        
        # Privacy
        privacy_frame = ctk.CTkFrame(scroll_frame, fg_color="transparent")
        privacy_frame.pack(fill="x", pady=(15, 10))
        ctk.CTkLabel(privacy_frame, text="Privacy:", font=ctk.CTkFont(weight="bold")).pack(side="left")
        self.privacy_var = ctk.StringVar(value="private")
        for val, text in [("private", "Private"), ("unlisted", "Unlisted"), ("public", "Public")]:
            ctk.CTkRadioButton(privacy_frame, text=text, variable=self.privacy_var, value=val).pack(side="left", padx=10)
        
        # Schedule option
        schedule_frame = ctk.CTkFrame(scroll_frame, fg_color="transparent")
        schedule_frame.pack(fill="x", pady=(0, 10))
        
        self.schedule_var = ctk.BooleanVar(value=False)
        self.schedule_check = ctk.CTkCheckBox(schedule_frame, text="Schedule publish", 
            variable=self.schedule_var, command=self.toggle_schedule)
        self.schedule_check.pack(side="left")
        
        # Schedule datetime inputs (hidden by default)
        self.schedule_inputs = ctk.CTkFrame(scroll_frame, fg_color=("gray90", "gray17"))
        
        schedule_inner = ctk.CTkFrame(self.schedule_inputs, fg_color="transparent")
        schedule_inner.pack(fill="x", padx=10, pady=10)
        
        # Date
        date_frame = ctk.CTkFrame(schedule_inner, fg_color="transparent")
        date_frame.pack(side="left", fill="x", expand=True, padx=(0, 5))
        ctk.CTkLabel(date_frame, text="Date (YYYY-MM-DD)", font=ctk.CTkFont(size=10)).pack(anchor="w")
        self.date_entry = ctk.CTkEntry(date_frame, placeholder_text="2026-01-20", height=35)
        self.date_entry.pack(fill="x")
        
        # Time
        time_frame = ctk.CTkFrame(schedule_inner, fg_color="transparent")
        time_frame.pack(side="left", fill="x", expand=True, padx=(5, 0))
        ctk.CTkLabel(time_frame, text="Time (HH:MM)", font=ctk.CTkFont(size=10)).pack(anchor="w")
        self.time_entry = ctk.CTkEntry(time_frame, placeholder_text="14:00", height=35)
        self.time_entry.pack(fill="x")
        
        ctk.CTkLabel(self.schedule_inputs, text="⚠️ Time in UTC timezone", 
            font=ctk.CTkFont(size=10), text_color="orange").pack(pady=(0, 5))
        
        # Generate button
        self.generate_btn = ctk.CTkButton(scroll_frame, text="🔄 Regenerate SEO", height=35, fg_color="gray",
            command=self.generate_seo_metadata)
        self.generate_btn.pack(fill="x", pady=(10, 0))
        
        # Progress (outside scroll)
        self.progress_frame = ctk.CTkFrame(main, fg_color="transparent")
        self.progress_label = ctk.CTkLabel(self.progress_frame, text="", text_color="gray")
        self.progress_label.pack()
        self.progress_bar = ctk.CTkProgressBar(self.progress_frame)
        self.progress_bar.pack(fill="x", pady=5)
        self.progress_bar.set(0)
        
        # Buttons (outside scroll)
        btn_frame = ctk.CTkFrame(main, fg_color="transparent")
        btn_frame.pack(fill="x", pady=(10, 0))
        
        ctk.CTkButton(btn_frame, text="Cancel", height=45, fg_color="gray",
            command=self.destroy).pack(side="left", fill="x", expand=True, padx=(0, 5))
        
        self.upload_btn = ctk.CTkButton(btn_frame, text="⬆️ Upload", height=45, 
            fg_color="#c4302b", hover_color="#ff0000", command=self.start_upload)
        self.upload_btn.pack(side="left", fill="x", expand=True, padx=(5, 0))
    
    def toggle_schedule(self):
        """Show/hide schedule inputs"""
        if self.schedule_var.get():
            self.schedule_inputs.pack(fill="x", pady=(0, 10))
            # Set default to tomorrow at current time
            from datetime import datetime, timedelta
            tomorrow = datetime.utcnow() + timedelta(days=1)
            self.date_entry.delete(0, "end")
            self.date_entry.insert(0, tomorrow.strftime("%Y-%m-%d"))
            self.time_entry.delete(0, "end")
            self.time_entry.insert(0, tomorrow.strftime("%H:%M"))
        else:
            self.schedule_inputs.pack_forget()
    
    def update_title_count(self, event=None):
        count = len(self.title_entry.get())
        color = "red" if count > 100 else "gray"
        self.title_count.configure(text=f"{count}/100", text_color=color)
    
    def update_desc_count(self, event=None):
        count = len(self.desc_text.get("1.0", "end-1c"))
        color = "red" if count > 5000 else "gray"
        self.desc_count.configure(text=f"{count}/5000", text_color=color)
    
    def generate_seo_metadata(self):
        """Generate SEO-optimized title and description using GPT"""
        self.generate_btn.configure(state="disabled", text="Generating...")
        self.title_entry.delete(0, "end")
        self.title_entry.insert(0, "Generating...")
        self.desc_text.delete("1.0", "end")
        self.desc_text.insert("1.0", "Generating SEO metadata...")
        
        def do_generate():
            try:
                from youtube_uploader import generate_seo_metadata
                metadata = generate_seo_metadata(
                    self.openai_client,
                    self.clip['title'],
                    self.clip['hook_text'],
                    self.model
                )
                self.after(0, lambda: self.set_metadata(metadata))
            except Exception as e:
                self.after(0, lambda: self.set_metadata({
                    'title': f"🔥 {self.clip['title']}"[:100],
                    'description': f"{self.clip['hook_text']}\n\n#shorts #viral #fyp",
                    'tags': ['shorts', 'viral']
                }))
        
        threading.Thread(target=do_generate, daemon=True).start()
    
    def set_metadata(self, metadata: dict):
        self.title_entry.delete(0, "end")
        self.title_entry.insert(0, metadata.get('title', ''))
        self.desc_text.delete("1.0", "end")
        self.desc_text.insert("1.0", metadata.get('description', ''))
        self.generate_btn.configure(state="normal", text="🔄 Regenerate SEO")
        self.update_title_count()
        self.update_desc_count()
        
        # Save to data.json
        self.save_metadata_to_clip(metadata)
    
    def save_metadata_to_clip(self, metadata: dict):
        """Save generated metadata to clip's data.json"""
        try:
            data_file = self.clip['folder'] / "data.json"
            if data_file.exists():
                with open(data_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            else:
                data = {}
            
            data['youtube_title'] = metadata.get('title', '')
            data['youtube_description'] = metadata.get('description', '')
            data['youtube_tags'] = metadata.get('tags', [])
            
            with open(data_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving metadata: {e}")
    
    def start_upload(self):
        if self.uploading:
            return
        
        title = self.title_entry.get().strip()
        description = self.desc_text.get("1.0", "end-1c").strip()
        
        if not title:
            messagebox.showerror("Error", "Title is required")
            return
        
        if len(title) > 100:
            messagebox.showerror("Error", "Title must be under 100 characters")
            return
        
        # Validate schedule if enabled
        publish_at = None
        if self.schedule_var.get():
            date_str = self.date_entry.get().strip()
            time_str = self.time_entry.get().strip()
            
            if not date_str or not time_str:
                messagebox.showerror("Error", "Please enter both date and time for scheduled upload")
                return
            
            try:
                from datetime import datetime
                # Parse and validate datetime
                datetime_str = f"{date_str}T{time_str}:00Z"
                publish_dt = datetime.fromisoformat(datetime_str.replace('Z', '+00:00'))
                
                # Check if in future
                if publish_dt <= datetime.utcnow().replace(tzinfo=publish_dt.tzinfo):
                    messagebox.showerror("Error", "Scheduled time must be in the future")
                    return
                
                publish_at = datetime_str
            except ValueError:
                messagebox.showerror("Error", "Invalid date/time format. Use YYYY-MM-DD and HH:MM")
                return
        
        self.uploading = True
        self.upload_btn.configure(state="disabled", text="Uploading...")
        self.generate_btn.configure(state="disabled")
        self.schedule_check.configure(state="disabled")
        self.progress_frame.pack(fill="x", pady=5)
        self.progress_label.configure(text="Starting upload...")
        
        def do_upload():
            try:
                from youtube_uploader import YouTubeUploader
                uploader = YouTubeUploader(status_callback=lambda m: self.after(0, lambda: self.progress_label.configure(text=m)))
                
                result = uploader.upload_video(
                    video_path=str(self.clip['video']),
                    title=title,
                    description=description,
                    privacy_status=self.privacy_var.get(),
                    publish_at=publish_at,
                    progress_callback=lambda p: self.after(0, lambda: self.update_upload_progress(p))
                )
                
                self.after(0, lambda: self.on_upload_complete(result))
                
            except Exception as e:
                self.after(0, lambda: self.on_upload_error(str(e)))
        
        threading.Thread(target=do_upload, daemon=True).start()
    
    def update_upload_progress(self, progress: int):
        self.progress_bar.set(progress / 100)
        self.progress_label.configure(text=f"Uploading... {progress}%")
    
    def on_upload_complete(self, result: dict):
        self.uploading = False
        
        if result.get('success'):
            video_url = result.get('url', '')
            messagebox.showinfo("Success", f"Video uploaded successfully!\n\n{video_url}")
            
            # Save YouTube URL to data.json
            try:
                data_file = self.clip['folder'] / "data.json"
                if data_file.exists():
                    with open(data_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    data['youtube_url'] = video_url
                    data['youtube_video_id'] = result.get('video_id', '')
                    with open(data_file, 'w', encoding='utf-8') as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
            except:
                pass
            
            self.destroy()
        else:
            self.on_upload_error(result.get('error', 'Unknown error'))
    
    def on_upload_error(self, error: str):
        self.uploading = False
        self.upload_btn.configure(state="normal", text="⬆️ Upload")
        self.generate_btn.configure(state="normal")
        self.progress_label.configure(text=f"Error: {error[:50]}")
        messagebox.showerror("Upload Failed", error)


class SettingsPage(ctk.CTkFrame):
    """Settings page - embedded in main window"""
    def __init__(self, parent, config: ConfigManager, on_save_callback, on_back_callback):
        super().__init__(parent)
        self.config = config
        self.on_save = on_save_callback
        self.on_back = on_back_callback
        self.models_list = []
        self.youtube_uploader = None
        
        self.create_ui()
        self.load_config()
    
    def create_ui(self):
        # Header with back button
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(15, 10))
        
        ctk.CTkButton(header, text="←", width=40, fg_color="transparent", 
            hover_color=("gray75", "gray25"), command=self.on_back).pack(side="left")
        ctk.CTkLabel(header, text="Settings", font=ctk.CTkFont(size=22, weight="bold")).pack(side="left", padx=10)
        
        # Main content with tabs
        main = ctk.CTkFrame(self)
        main.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        
        self.tabview = ctk.CTkTabview(main)
        self.tabview.pack(fill="both", expand=True, padx=10, pady=10)
        
        self.tabview.add("OpenAI API")
        self.tabview.add("Output")
        self.tabview.add("YouTube")
        self.tabview.add("About")
        
        self.create_openai_tab()
        self.create_output_tab()
        self.create_youtube_tab()
        self.create_about_tab()
    
    def create_openai_tab(self):
        main = self.tabview.tab("OpenAI API")
        
        ctk.CTkLabel(main, text="Base URL", anchor="w").pack(fill="x", pady=(10, 0))
        self.url_entry = ctk.CTkEntry(main, placeholder_text="https://api.openai.com/v1")
        self.url_entry.pack(fill="x", pady=(5, 15))
        
        key_frame = ctk.CTkFrame(main, fg_color="transparent")
        key_frame.pack(fill="x")
        ctk.CTkLabel(key_frame, text="API Key", anchor="w").pack(side="left")
        self.key_status = ctk.CTkLabel(key_frame, text="", font=ctk.CTkFont(size=11))
        self.key_status.pack(side="right")
        
        key_input = ctk.CTkFrame(main, fg_color="transparent")
        key_input.pack(fill="x", pady=(5, 15))
        self.key_entry = ctk.CTkEntry(key_input, placeholder_text="sk-...", show="•")
        self.key_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.validate_btn = ctk.CTkButton(key_input, text="Validate", width=80, command=self.validate_key)
        self.validate_btn.pack(side="right")
        
        ctk.CTkLabel(main, text="Model", anchor="w").pack(fill="x")
        model_frame = ctk.CTkFrame(main, fg_color="transparent")
        model_frame.pack(fill="x", pady=(5, 20))
        self.model_var = ctk.StringVar(value="Select model...")
        self.model_btn = ctk.CTkButton(model_frame, textvariable=self.model_var, anchor="w",
            fg_color=("gray75", "gray25"), hover_color=("gray70", "gray30"),
            text_color=("gray10", "gray90"), command=self.open_model_selector)
        self.model_btn.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.model_count = ctk.CTkLabel(model_frame, text="", text_color="gray", font=ctk.CTkFont(size=11))
        self.model_count.pack(side="right")
        
        ctk.CTkButton(main, text="💾 Save Settings", height=40, command=self.save_settings).pack(fill="x", pady=(10, 0))
    
    def create_output_tab(self):
        main = self.tabview.tab("Output")
        
        ctk.CTkLabel(main, text="Output Folder", anchor="w", font=ctk.CTkFont(size=14, weight="bold")).pack(fill="x", pady=(15, 5))
        ctk.CTkLabel(main, text="Folder tempat menyimpan hasil clip video", anchor="w", 
            font=ctk.CTkFont(size=11), text_color="gray").pack(fill="x", pady=(0, 10))
        
        output_frame = ctk.CTkFrame(main, fg_color="transparent")
        output_frame.pack(fill="x", pady=(5, 15))
        self.output_var = ctk.StringVar(value=str(OUTPUT_DIR))
        self.output_entry = ctk.CTkEntry(output_frame, textvariable=self.output_var)
        self.output_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        ctk.CTkButton(output_frame, text="📂 Browse", width=100, command=self.browse_output_folder).pack(side="right")
        
        # Open folder button
        ctk.CTkButton(main, text="📂 Open Output Folder", height=40, fg_color="gray",
            command=lambda: self.open_folder(self.output_var.get())).pack(fill="x", pady=(0, 15))
        
        ctk.CTkButton(main, text="💾 Save Settings", height=40, command=self.save_settings).pack(fill="x", pady=(10, 0))
    
    def create_youtube_tab(self):
        main = self.tabview.tab("YouTube")
        
        # YouTube connection status
        status_frame = ctk.CTkFrame(main)
        status_frame.pack(fill="x", pady=15, padx=5)
        
        ctk.CTkLabel(status_frame, text="YouTube Channel", font=ctk.CTkFont(size=14, weight="bold")).pack(anchor="w", padx=10, pady=(10, 5))
        
        self.yt_status_frame = ctk.CTkFrame(status_frame, fg_color="transparent")
        self.yt_status_frame.pack(fill="x", padx=10, pady=(0, 10))
        
        self.yt_status_label = ctk.CTkLabel(self.yt_status_frame, text="Not connected", text_color="gray")
        self.yt_status_label.pack(side="left")
        
        self.yt_connect_btn = ctk.CTkButton(status_frame, text="🔗 Connect YouTube", height=40, 
            command=self.connect_youtube)
        self.yt_connect_btn.pack(fill="x", padx=10, pady=(0, 10))
        
        self.yt_disconnect_btn = ctk.CTkButton(status_frame, text="Disconnect", height=35,
            fg_color="gray", hover_color="#c0392b", command=self.disconnect_youtube)
        self.yt_disconnect_btn.pack(fill="x", padx=10, pady=(0, 10))
        self.yt_disconnect_btn.pack_forget()  # Hide initially
        
        # Info
        info_frame = ctk.CTkFrame(main, fg_color=("gray90", "gray17"))
        info_frame.pack(fill="x", pady=10, padx=5)
        
        info_text = """ℹ️ YouTube Upload Feature

To enable YouTube upload:
1. Set up Google Cloud project
2. Enable YouTube Data API v3
3. Create OAuth credentials
4. Place client_secret.json in app folder

See README for detailed setup guide."""
        
        ctk.CTkLabel(info_frame, text=info_text, justify="left", anchor="w",
            font=ctk.CTkFont(size=11), wraplength=400).pack(padx=15, pady=15)
        
        # Check YouTube status
        self.check_youtube_status()
    
    def check_youtube_status(self):
        """Check if YouTube is configured and connected"""
        try:
            from youtube_uploader import YouTubeUploader
            self.youtube_uploader = YouTubeUploader()
            
            if not self.youtube_uploader.is_configured():
                self.yt_status_label.configure(text="⚠️ client_secret.json not found", text_color="orange")
                self.yt_connect_btn.configure(state="disabled")
                return
            
            if self.youtube_uploader.is_authenticated():
                channel = self.youtube_uploader.get_channel_info()
                if channel:
                    self.yt_status_label.configure(
                        text=f"✓ Connected: {channel['title']}", 
                        text_color="green"
                    )
                    self.yt_connect_btn.pack_forget()
                    self.yt_disconnect_btn.pack(fill="x", padx=10, pady=(0, 10))
                    return
            
            self.yt_status_label.configure(text="Not connected", text_color="gray")
            self.yt_connect_btn.configure(state="normal")
            
        except ImportError:
            self.yt_status_label.configure(text="⚠️ YouTube module not available", text_color="orange")
            self.yt_connect_btn.configure(state="disabled")
        except Exception as e:
            self.yt_status_label.configure(text=f"Error: {str(e)[:30]}", text_color="red")
    
    def connect_youtube(self):
        """Start YouTube OAuth flow"""
        self.yt_connect_btn.configure(state="disabled", text="Connecting...")
        
        def do_connect():
            try:
                self.youtube_uploader.authenticate(callback=self.on_youtube_connected)
            except Exception as e:
                self.after(0, lambda: self.on_youtube_error(str(e)))
        
        threading.Thread(target=do_connect, daemon=True).start()
    
    def on_youtube_connected(self, success, data):
        """Callback when YouTube connection completes"""
        if success:
            self.after(0, lambda: self._update_youtube_connected(data))
        else:
            self.after(0, lambda: self.on_youtube_error(str(data)))
    
    def _update_youtube_connected(self, channel):
        if channel and channel.get('title'):
            self.yt_status_label.configure(
                text=f"✓ Connected: {channel['title']}", 
                text_color="green"
            )
            self.yt_connect_btn.pack_forget()
            self.yt_disconnect_btn.pack(fill="x", padx=10, pady=(0, 10))
            # Update main app status
            if hasattr(self.master, 'master') and hasattr(self.master.master, 'update_connection_status'):
                self.master.master.update_connection_status()
            messagebox.showinfo("Success", f"Connected to YouTube channel: {channel['title']}")
        else:
            # Channel info not available but auth succeeded
            self.yt_status_label.configure(
                text="✓ Connected", 
                text_color="green"
            )
            self.yt_connect_btn.pack_forget()
            self.yt_disconnect_btn.pack(fill="x", padx=10, pady=(0, 10))
            if hasattr(self.master, 'master') and hasattr(self.master.master, 'update_connection_status'):
                self.master.master.update_connection_status()
            messagebox.showinfo("Success", "Connected to YouTube!")
    
    def on_youtube_error(self, error):
        self.yt_status_label.configure(text="Connection failed", text_color="red")
        self.yt_connect_btn.configure(state="normal", text="🔗 Connect YouTube")
        messagebox.showerror("Error", f"Failed to connect: {error}")
    
    def disconnect_youtube(self):
        """Disconnect YouTube account"""
        if messagebox.askyesno("Disconnect", "Are you sure you want to disconnect YouTube?"):
            if self.youtube_uploader:
                self.youtube_uploader.disconnect()
            self.yt_status_label.configure(text="Not connected", text_color="gray")
            self.yt_disconnect_btn.pack_forget()
            self.yt_connect_btn.configure(state="normal", text="🔗 Connect YouTube")
            self.yt_connect_btn.pack(fill="x", padx=10, pady=(0, 10))
            # Update main app status
            if hasattr(self.master, 'master') and hasattr(self.master.master, 'update_connection_status'):
                self.master.master.update_connection_status()
    
    def create_about_tab(self):
        main = self.tabview.tab("About")
        
        # App info
        info_frame = ctk.CTkFrame(main, fg_color="transparent")
        info_frame.pack(fill="x", pady=(20, 15))
        
        ctk.CTkLabel(info_frame, text="YT Short Clipper", font=ctk.CTkFont(size=20, weight="bold")).pack()
        ctk.CTkLabel(info_frame, text="v0.0.2", font=ctk.CTkFont(size=12), text_color="gray").pack(pady=(5, 0))
        
        # Description
        desc_frame = ctk.CTkFrame(main, fg_color=("gray90", "gray17"))
        desc_frame.pack(fill="x", pady=10, padx=5)
        
        desc_text = """Automated YouTube to Short-Form Content Pipeline

Transform long-form YouTube videos into engaging 
short-form content for TikTok, Instagram Reels, 
and YouTube Shorts."""
        
        ctk.CTkLabel(desc_frame, text=desc_text, justify="center", 
            font=ctk.CTkFont(size=11), wraplength=380).pack(padx=15, pady=15)
        
        # Credits
        credits_frame = ctk.CTkFrame(main, fg_color="transparent")
        credits_frame.pack(fill="x", pady=10)
        
        ctk.CTkLabel(credits_frame, text="Made with ☕ by", font=ctk.CTkFont(size=11), 
            text_color="gray").pack()
        ctk.CTkLabel(credits_frame, text="Aji Prakoso", font=ctk.CTkFont(size=13, weight="bold")).pack(pady=(5, 0))
        
        # Links
        links_frame = ctk.CTkFrame(main, fg_color="transparent")
        links_frame.pack(fill="x", pady=15)
        
        ctk.CTkButton(links_frame, text="⭐ GitHub Repository", height=40,
            fg_color=("#24292e", "#0d1117"), hover_color=("#2c3136", "#161b22"),
            command=lambda: self.open_url("https://github.com/jipraks/yt-short-clipper")).pack(fill="x", pady=2)
        
        ctk.CTkButton(links_frame, text="📸 @jipraks on Instagram", height=40,
            fg_color=("#E4405F", "#C13584"), hover_color=("#F56040", "#E1306C"),
            command=lambda: self.open_url("https://instagram.com/jipraks")).pack(fill="x", pady=2)
        
        ctk.CTkButton(links_frame, text="🎬 YouTube Channel", height=40,
            fg_color=("#c4302b", "#FF0000"), hover_color=("#ff0000", "#CC0000"),
            command=lambda: self.open_url("https://youtube.com/@jipraks")).pack(fill="x", pady=2)
        
        # Footer
        footer_frame = ctk.CTkFrame(main, fg_color="transparent")
        footer_frame.pack(side="bottom", fill="x", pady=(10, 5))
        
        ctk.CTkLabel(footer_frame, text="Open Source • MIT License", 
            font=ctk.CTkFont(size=10), text_color="gray").pack()
    
    def open_url(self, url: str):
        """Open URL in browser"""
        import webbrowser
        webbrowser.open(url)
    
    def open_folder(self, folder_path: str):
        """Open folder in file explorer"""
        if sys.platform == "win32":
            os.startfile(folder_path)
        elif sys.platform == "darwin":
            subprocess.run(["open", folder_path])
        else:
            subprocess.run(["xdg-open", folder_path])
    
    def load_config(self):
        self.url_entry.insert(0, self.config.get("base_url", "https://api.openai.com/v1"))
        self.key_entry.insert(0, self.config.get("api_key", ""))
        self.model_var.set(self.config.get("model", "gpt-4.1"))
        self.output_var.set(self.config.get("output_dir", str(OUTPUT_DIR)) or str(OUTPUT_DIR))
        if self.config.get("api_key"):
            self.validate_key()
    
    def browse_output_folder(self):
        folder = filedialog.askdirectory(initialdir=self.output_var.get())
        if folder:
            self.output_var.set(folder)

    def validate_key(self):
        api_key = self.key_entry.get().strip()
        base_url = self.url_entry.get().strip() or "https://api.openai.com/v1"
        self.key_status.configure(text="Validating...", text_color="yellow")
        self.validate_btn.configure(state="disabled")
        
        def do_validate():
            try:
                client = OpenAI(api_key=api_key, base_url=base_url)
                models = sorted([m.id for m in client.models.list().data])
                self.models_list = models
                self.after(0, lambda: self._on_success(models))
            except:
                self.after(0, self._on_error)
        threading.Thread(target=do_validate, daemon=True).start()
    
    def _on_success(self, models):
        self.key_status.configure(text="✓ Valid", text_color="green")
        self.validate_btn.configure(state="normal")
        self.model_count.configure(text=f"{len(models)} models")
        if self.model_var.get() not in models:
            for p in ["gpt-4.1", "gpt-4o", "gpt-4o-mini"]:
                if p in models:
                    self.model_var.set(p)
                    break
    
    def _on_error(self):
        self.key_status.configure(text="✗ Invalid", text_color="red")
        self.validate_btn.configure(state="normal")
        self.models_list = []
    
    def open_model_selector(self):
        if not self.models_list:
            messagebox.showwarning("Warning", "Validate API key first")
            return
        SearchableModelDropdown(self, self.models_list, self.model_var.get(), lambda m: self.model_var.set(m))
    
    def save_settings(self):
        api_key = self.key_entry.get().strip()
        base_url = self.url_entry.get().strip() or "https://api.openai.com/v1"
        model = self.model_var.get()
        output_dir = self.output_var.get().strip() or str(OUTPUT_DIR)
        
        if not api_key or model == "Select model...":
            messagebox.showerror("Error", "Fill all fields")
            return
        
        # Create output folder if not exists
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        
        self.config.set("api_key", api_key)
        self.config.set("base_url", base_url)
        self.config.set("model", model)
        self.config.set("output_dir", output_dir)
        self.on_save(api_key, base_url, model)
        self.on_back()


class ProgressStep(ctk.CTkFrame):
    """A single step in the progress indicator"""
    def __init__(self, parent, step_num: int, title: str):
        super().__init__(parent, fg_color="transparent")
        self.step_num = step_num
        self.status = "pending"  # pending, active, done, error
        
        # Step indicator circle
        self.indicator = ctk.CTkLabel(self, text=str(step_num), width=35, height=35,
            fg_color=("gray70", "gray30"), corner_radius=17, font=ctk.CTkFont(size=14, weight="bold"))
        self.indicator.pack(side="left", padx=(0, 10))
        
        # Step title and status
        text_frame = ctk.CTkFrame(self, fg_color="transparent")
        text_frame.pack(side="left", fill="x", expand=True)
        
        self.title_label = ctk.CTkLabel(text_frame, text=title, font=ctk.CTkFont(size=13), anchor="w")
        self.title_label.pack(fill="x")
        
        self.status_label = ctk.CTkLabel(text_frame, text="Waiting...", font=ctk.CTkFont(size=11), 
            text_color="gray", anchor="w")
        self.status_label.pack(fill="x")

    def set_active(self, status_text: str = "Processing..."):
        self.status = "active"
        self.indicator.configure(fg_color=("#3498db", "#2980b9"), text="●")
        self.status_label.configure(text=status_text, text_color=("#3498db", "#5dade2"))
    
    def set_done(self, status_text: str = "Complete"):
        self.status = "done"
        self.indicator.configure(fg_color=("#27ae60", "#1e8449"), text="✓")
        self.status_label.configure(text=status_text, text_color=("#27ae60", "#2ecc71"))
    
    def set_error(self, status_text: str = "Failed"):
        self.status = "error"
        self.indicator.configure(fg_color=("#e74c3c", "#c0392b"), text="✗")
        self.status_label.configure(text=status_text, text_color=("#e74c3c", "#ec7063"))
    
    def reset(self):
        self.status = "pending"
        self.indicator.configure(fg_color=("gray70", "gray30"), text=str(self.step_num))
        self.status_label.configure(text="Waiting...", text_color="gray")


class YTShortClipperApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        
        self.config = ConfigManager()
        self.client = None
        self.current_thumbnail = None
        self.processing = False
        self.cancelled = False
        self.token_usage = {"gpt_input": 0, "gpt_output": 0, "whisper_seconds": 0, "tts_chars": 0}
        self.youtube_connected = False
        self.youtube_channel = None
        
        self.title("YT Short Clipper")
        self.geometry("550x720")
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
        
        self.show_page("home")
        self.load_config()
        self.check_youtube_status()
        
        # Store created clips info
        self.created_clips = []
    
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
            self.refresh_browse_list()

    def create_home_page(self):
        page = ctk.CTkFrame(self.container)
        self.pages["home"] = page
        
        # Top bar with icon
        top = ctk.CTkFrame(page, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(15, 10))
        
        # App icon + title
        title_frame = ctk.CTkFrame(top, fg_color="transparent")
        title_frame.pack(side="left")
        
        if ICON_PATH.exists():
            try:
                icon_img = Image.open(ICON_PATH)
                icon_img.thumbnail((32, 32), Image.Resampling.LANCZOS)
                self.header_icon = ctk.CTkImage(light_image=icon_img, dark_image=icon_img, size=(32, 32))
                ctk.CTkLabel(title_frame, image=self.header_icon, text="").pack(side="left", padx=(0, 10))
            except:
                pass
        
        ctk.CTkLabel(title_frame, text="YT Short Clipper", font=ctk.CTkFont(size=22, weight="bold")).pack(side="left")
        
        ctk.CTkButton(top, text="⚙️", width=40, fg_color="transparent", hover_color=("gray75", "gray25"), 
            command=lambda: self.show_page("settings")).pack(side="right")
        
        # Connection status cards
        status_frame = ctk.CTkFrame(page, fg_color="transparent")
        status_frame.pack(fill="x", padx=20, pady=(0, 10))
        
        # API Card
        self.api_card = ctk.CTkFrame(status_frame, fg_color=("gray85", "gray20"), corner_radius=10)
        self.api_card.pack(side="left", fill="x", expand=True, padx=(0, 5))
        
        api_inner = ctk.CTkFrame(self.api_card, fg_color="transparent")
        api_inner.pack(fill="x", padx=12, pady=10)
        
        self.api_dot = ctk.CTkLabel(api_inner, text="●", font=ctk.CTkFont(size=14), text_color="#e74c3c")
        self.api_dot.pack(side="left")
        ctk.CTkLabel(api_inner, text="API", font=ctk.CTkFont(size=12)).pack(side="left", padx=(5, 0))
        self.api_status_label = ctk.CTkLabel(api_inner, text="Not configured", font=ctk.CTkFont(size=10), text_color="gray")
        self.api_status_label.pack(side="right")
        
        # YouTube Card
        self.yt_card = ctk.CTkFrame(status_frame, fg_color=("gray85", "gray20"), corner_radius=10)
        self.yt_card.pack(side="left", fill="x", expand=True, padx=(5, 0))
        
        yt_inner = ctk.CTkFrame(self.yt_card, fg_color="transparent")
        yt_inner.pack(fill="x", padx=12, pady=10)
        
        self.yt_dot = ctk.CTkLabel(yt_inner, text="●", font=ctk.CTkFont(size=14), text_color="#e74c3c")
        self.yt_dot.pack(side="left")
        
        yt_text_frame = ctk.CTkFrame(yt_inner, fg_color="transparent")
        yt_text_frame.pack(side="left", fill="x", expand=True, padx=(5, 0))
        
        ctk.CTkLabel(yt_text_frame, text="YouTube", font=ctk.CTkFont(size=12)).pack(anchor="w")
        self.yt_status_label_home = ctk.CTkLabel(yt_text_frame, text="Not connected", 
            font=ctk.CTkFont(size=10), text_color="gray")
        self.yt_status_label_home.pack(anchor="w")
        
        # Main content
        main = ctk.CTkFrame(page)
        main.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        
        # URL input
        ctk.CTkLabel(main, text="YouTube URL", font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w", padx=15, pady=(15, 5))
        self.url_var = ctk.StringVar()
        self.url_var.trace("w", self.on_url_change)
        ctk.CTkEntry(main, textvariable=self.url_var, placeholder_text="https://youtube.com/watch?v=...", height=40).pack(fill="x", padx=15)
        
        # Thumbnail
        self.thumb_frame = ctk.CTkFrame(main, height=200, fg_color=("gray85", "gray20"))
        self.thumb_frame.pack(fill="x", padx=15, pady=15)
        self.thumb_frame.pack_propagate(False)
        self.thumb_label = ctk.CTkLabel(self.thumb_frame, text="📺 Video thumbnail will appear here", text_color="gray")
        self.thumb_label.pack(expand=True)
        
        # Clips input
        clips_frame = ctk.CTkFrame(main, fg_color="transparent")
        clips_frame.pack(fill="x", padx=15, pady=(0, 20))
        ctk.CTkLabel(clips_frame, text="Jumlah Clips:", font=ctk.CTkFont(size=13)).pack(side="left")
        self.clips_var = ctk.StringVar(value="5")
        ctk.CTkEntry(clips_frame, textvariable=self.clips_var, width=60, height=35).pack(side="left", padx=10)
        ctk.CTkLabel(clips_frame, text="(1-10)", text_color="gray").pack(side="left")
        
        # Buttons
        btn_frame = ctk.CTkFrame(main, fg_color="transparent")
        btn_frame.pack(fill="x", padx=15, pady=(0, 20))
        
        # Start button (disabled by default until valid URL)
        self.start_btn = ctk.CTkButton(btn_frame, text="🚀 Start Processing", font=ctk.CTkFont(size=15, weight="bold"), 
            height=50, command=self.start_processing, state="disabled", fg_color="gray")
        self.start_btn.pack(fill="x", pady=(0, 5))
        
        # Browse button
        ctk.CTkButton(btn_frame, text="📂 Browse Videos", font=ctk.CTkFont(size=13), 
            height=40, fg_color="gray", hover_color=("gray60", "gray40"),
            command=lambda: self.show_page("browse")).pack(fill="x")

    def create_processing_page(self):
        page = ctk.CTkFrame(self.container)
        self.pages["processing"] = page
        
        # Header
        header = ctk.CTkFrame(page, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(15, 10))
        ctk.CTkLabel(header, text="🎬 Processing", font=ctk.CTkFont(size=22, weight="bold")).pack(side="left")
        
        main = ctk.CTkFrame(page)
        main.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        
        # Progress steps
        steps_frame = ctk.CTkFrame(main)
        steps_frame.pack(fill="x", padx=15, pady=15)
        
        self.steps = []
        step_titles = [
            ("Download", "Downloading video & subtitles"),
            ("Analyze", "Finding highlights with AI"),
            ("Process", "Creating clips"),
            ("Finalize", "Adding captions & hooks")
        ]
        
        for i, (name, title) in enumerate(step_titles, 1):
            step = ProgressStep(steps_frame, i, title)
            step.pack(fill="x", pady=8, padx=10)
            self.steps.append(step)
        
        # Current status
        self.status_frame = ctk.CTkFrame(main)
        self.status_frame.pack(fill="x", padx=15, pady=(0, 15))
        
        self.status_label = ctk.CTkLabel(self.status_frame, text="Initializing...", 
            font=ctk.CTkFont(size=14), wraplength=480)
        self.status_label.pack(pady=15)
        
        # Token usage (compact)
        token_frame = ctk.CTkFrame(main)
        token_frame.pack(fill="x", padx=15, pady=(0, 15))
        
        ctk.CTkLabel(token_frame, text="API Usage", font=ctk.CTkFont(size=11, weight="bold")).pack(anchor="w", padx=10, pady=(8, 5))
        stats = ctk.CTkFrame(token_frame, fg_color="transparent")
        stats.pack(fill="x", padx=10, pady=(0, 8))
        
        for label, attr in [("GPT", "gpt_label"), ("Whisper", "whisper_label"), ("TTS", "tts_label")]:
            f = ctk.CTkFrame(stats, fg_color=("gray80", "gray25"), corner_radius=8)
            f.pack(side="left", fill="x", expand=True, padx=2)
            ctk.CTkLabel(f, text=label, font=ctk.CTkFont(size=10), text_color="gray").pack(side="left", padx=(8, 5), pady=5)
            lbl = ctk.CTkLabel(f, text="0", font=ctk.CTkFont(size=12, weight="bold"))
            lbl.pack(side="right", padx=(5, 8), pady=5)
            setattr(self, attr, lbl)

        # Buttons - reorganize layout
        btn_frame = ctk.CTkFrame(main, fg_color="transparent")
        btn_frame.pack(fill="x", padx=15, pady=(0, 15))
        
        # Row 1: Cancel and Back
        row1 = ctk.CTkFrame(btn_frame, fg_color="transparent")
        row1.pack(fill="x", pady=(0, 5))
        
        self.cancel_btn = ctk.CTkButton(row1, text="❌ Cancel", height=45, fg_color="#c0392b", 
            hover_color="#e74c3c", command=self.cancel_processing)
        self.cancel_btn.pack(side="left", fill="x", expand=True, padx=(0, 5))
        
        self.back_btn = ctk.CTkButton(row1, text="← Back", height=45, state="disabled", command=lambda: self.show_page("home"))
        self.back_btn.pack(side="left", fill="x", expand=True, padx=(5, 0))
        
        # Row 2: Open Output and View Results
        row2 = ctk.CTkFrame(btn_frame, fg_color="transparent")
        row2.pack(fill="x")
        
        self.open_btn = ctk.CTkButton(row2, text="📂 Open Output", height=45, state="disabled", command=self.open_output)
        self.open_btn.pack(side="left", fill="x", expand=True, padx=(0, 5))
        
        self.results_btn = ctk.CTkButton(row2, text="📋 View Results", height=45, state="disabled", 
            fg_color="#27ae60", hover_color="#2ecc71", command=self.show_results)
        self.results_btn.pack(side="left", fill="x", expand=True, padx=(5, 0))
    
    def create_results_page(self):
        """Create results page showing all created clips"""
        page = ctk.CTkFrame(self.container)
        self.pages["results"] = page
        
        # Header
        header = ctk.CTkFrame(page, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(15, 10))
        ctk.CTkLabel(header, text="📋 Results", font=ctk.CTkFont(size=22, weight="bold")).pack(side="left")
        
        # Clips list (scrollable)
        self.clips_frame = ctk.CTkScrollableFrame(page, height=450)
        self.clips_frame.pack(fill="both", expand=True, padx=20, pady=(0, 10))
        
        # Buttons
        btn_frame = ctk.CTkFrame(page, fg_color="transparent")
        btn_frame.pack(fill="x", padx=20, pady=(0, 20))
        
        ctk.CTkButton(btn_frame, text="← Back", height=45, command=lambda: self.show_page("processing")).pack(side="left", fill="x", expand=True, padx=(0, 5))
        ctk.CTkButton(btn_frame, text="📂 Open Folder", height=45, command=self.open_output).pack(side="left", fill="x", expand=True, padx=(5, 5))
        ctk.CTkButton(btn_frame, text="🏠 New Clip", height=45, fg_color="#27ae60", hover_color="#2ecc71", command=lambda: self.show_page("home")).pack(side="left", fill="x", expand=True, padx=(5, 0))
    
    def create_settings_page(self):
        """Create settings page as embedded frame"""
        self.pages["settings"] = SettingsPage(
            self.container, 
            self.config, 
            self.on_settings_saved,
            lambda: self.show_page("home")
        )
    
    def create_browse_page(self):
        """Create browse page for viewing existing videos"""
        page = ctk.CTkFrame(self.container)
        self.pages["browse"] = page
        
        # Header
        header = ctk.CTkFrame(page, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(15, 10))
        
        ctk.CTkButton(header, text="←", width=40, fg_color="transparent", 
            hover_color=("gray75", "gray25"), command=lambda: self.show_page("home")).pack(side="left")
        ctk.CTkLabel(header, text="Browse Videos", font=ctk.CTkFont(size=22, weight="bold")).pack(side="left", padx=10)
        ctk.CTkButton(header, text="🔄 Refresh", height=35, width=100,
            command=self.refresh_browse_list).pack(side="right")
        
        # Main content
        main = ctk.CTkFrame(page)
        main.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        
        # Video list (scrollable) - larger
        self.browse_list_frame = ctk.CTkScrollableFrame(main, height=400)
        self.browse_list_frame.pack(fill="both", expand=True, pady=(10, 10))
        
        # Selected video info - more compact
        self.browse_info_frame = ctk.CTkFrame(main, fg_color=("gray90", "gray17"))
        self.browse_info_frame.pack(fill="x", pady=(0, 10))
        
        self.browse_info_label = ctk.CTkLabel(self.browse_info_frame, text="Select a video to view details", 
            font=ctk.CTkFont(size=11), text_color="gray")
        self.browse_info_label.pack(pady=12)
        
        # Action buttons - larger
        btn_frame = ctk.CTkFrame(main, fg_color="transparent")
        btn_frame.pack(fill="x")
        
        self.browse_play_btn = ctk.CTkButton(btn_frame, text="▶ Play Video", height=45, state="disabled",
            font=ctk.CTkFont(size=14, weight="bold"), command=self.play_selected_video_internal)
        self.browse_play_btn.pack(fill="x", pady=(0, 5))
        
        btn_row = ctk.CTkFrame(btn_frame, fg_color="transparent")
        btn_row.pack(fill="x")
        
        self.browse_upload_btn = ctk.CTkButton(btn_row, text="⬆️ Upload to YouTube", height=45, state="disabled",
            font=ctk.CTkFont(size=13), fg_color="#c4302b", hover_color="#ff0000",
            command=self.upload_browse_video)
        self.browse_upload_btn.pack(side="left", fill="x", expand=True, padx=(0, 5))
        
        self.browse_folder_btn = ctk.CTkButton(btn_row, text="📂 Open Folder", height=45, state="disabled",
            font=ctk.CTkFont(size=13), fg_color="gray", command=self.open_selected_folder)
        self.browse_folder_btn.pack(side="left", fill="x", expand=True, padx=(5, 0))
        
        # Initialize
        self.selected_browse_video = None
        self.browse_thumbnails = []  # Store thumbnail references
        self.browse_list_items = {}  # Store items for selection highlight
    
    def refresh_browse_list(self):
        """Refresh the list of videos in output folder"""
        # Clear selection
        self.selected_browse_video = None
        
        # Clear existing list
        for widget in self.browse_list_frame.winfo_children():
            widget.destroy()
        self.browse_thumbnails = []
        self.browse_list_items = {}  # Store items for selection highlight
        
        # Clear info frame
        for widget in self.browse_info_frame.winfo_children():
            widget.destroy()
        ctk.CTkLabel(self.browse_info_frame, text="Select a video to view details", 
            font=ctk.CTkFont(size=11), text_color="gray").pack(pady=12)
        
        # Disable buttons
        self.browse_play_btn.configure(state="disabled")
        self.browse_folder_btn.configure(state="disabled")
        self.browse_upload_btn.configure(state="disabled", text="⬆️ Upload to YouTube")
        
        output_dir = Path(self.config.get("output_dir", str(OUTPUT_DIR)))
        
        if not output_dir.exists():
            ctk.CTkLabel(self.browse_list_frame, text="📂 Output folder not found", 
                font=ctk.CTkFont(size=13), text_color="gray").pack(pady=30)
            return
        
        # Find all clip folders
        clip_folders = sorted([d for d in output_dir.iterdir() if d.is_dir() and not d.name.startswith("_")], reverse=True)
        
        if not clip_folders:
            ctk.CTkLabel(self.browse_list_frame, text="📹 No videos found\n\nProcess a video to see it here", 
                font=ctk.CTkFont(size=13), text_color="gray", justify="center").pack(pady=30)
            return
        
        # Create list items with thumbnails
        for folder in clip_folders[:50]:  # Limit to 50
            data_file = folder / "data.json"
            master_file = folder / "master.mp4"
            
            if data_file.exists() and master_file.exists():
                try:
                    with open(data_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    
                    # Create list item with better layout
                    item = ctk.CTkFrame(self.browse_list_frame, fg_color=("gray85", "gray20"), corner_radius=10)
                    item.pack(fill="x", pady=5, padx=5)
                    
                    # Thumbnail on left
                    thumb_frame = ctk.CTkFrame(item, width=140, height=80, fg_color=("gray75", "gray30"), corner_radius=8)
                    thumb_frame.pack(side="left", padx=12, pady=12)
                    thumb_frame.pack_propagate(False)
                    
                    # Load thumbnail async
                    self.load_browse_thumbnail(master_file, thumb_frame)
                    
                    # Info on right
                    info = ctk.CTkFrame(item, fg_color="transparent")
                    info.pack(side="left", fill="both", expand=True, padx=(0, 12), pady=12)
                    
                    # Title with YouTube badge if uploaded
                    title_frame = ctk.CTkFrame(info, fg_color="transparent")
                    title_frame.pack(fill="x")
                    
                    title = data.get("title", "Untitled")[:45]
                    title_label = ctk.CTkLabel(title_frame, text=title, font=ctk.CTkFont(size=13, weight="bold"), 
                        anchor="w")
                    title_label.pack(side="left", fill="x", expand=True)
                    
                    # YouTube badge if uploaded
                    if data.get("youtube_url"):
                        yt_badge = ctk.CTkLabel(title_frame, text="▶️", font=ctk.CTkFont(size=12), 
                            text_color="#c4302b", cursor="hand2")
                        yt_badge.pack(side="right", padx=(5, 0))
                        # Make badge clickable to open YouTube
                        yt_url = data.get("youtube_url")
                        yt_badge.bind("<Button-1>", lambda e, url=yt_url: self.open_youtube_url(url))
                    
                    duration = data.get("duration_seconds", 0)
                    hook = data.get("hook_text", "")[:35]
                    subtitle_label = ctk.CTkLabel(info, text=f"⏱️ {duration:.0f}s • {hook}...", 
                        font=ctk.CTkFont(size=11), text_color="gray", anchor="w")
                    subtitle_label.pack(fill="x", pady=(3, 0))
                    
                    date_label = ctk.CTkLabel(info, text=f"📅 {folder.name}", 
                        font=ctk.CTkFont(size=10), text_color="gray", anchor="w")
                    date_label.pack(fill="x", pady=(2, 0))
                    
                    # Make clickable
                    video_data = {
                        "folder": folder,
                        "video": master_file,
                        "data": data,
                        "item_widget": item,
                        "title_label": title_label,
                        "subtitle_label": subtitle_label,
                        "date_label": date_label
                    }
                    
                    # Store item reference
                    self.browse_list_items[str(master_file)] = item
                    
                    # Bind click events
                    def make_click_handler(v):
                        return lambda e: self.select_browse_video(v)
                    
                    item.bind("<Button-1>", make_click_handler(video_data))
                    for child in item.winfo_children():
                        child.bind("<Button-1>", make_click_handler(video_data))
                        for subchild in child.winfo_children():
                            subchild.bind("<Button-1>", make_click_handler(video_data))
                    
                except:
                    pass
    
    def load_browse_thumbnail(self, video_path: Path, frame: ctk.CTkFrame):
        """Load thumbnail from video file"""
        def extract():
            try:
                import cv2
                cap = cv2.VideoCapture(str(video_path))
                cap.set(cv2.CAP_PROP_POS_FRAMES, 30)  # Get frame at ~1 second
                ret, img = cap.read()
                cap.release()
                
                if ret:
                    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                    pil_img = Image.fromarray(img)
                    pil_img.thumbnail((140, 80), Image.Resampling.LANCZOS)
                    self.after(0, lambda: self.show_browse_thumb(frame, pil_img))
            except:
                pass
        
        threading.Thread(target=extract, daemon=True).start()
    
    def show_browse_thumb(self, frame: ctk.CTkFrame, img: Image.Image):
        """Display thumbnail in frame"""
        ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
        self.browse_thumbnails.append(ctk_img)  # Keep reference
        
        for widget in frame.winfo_children():
            widget.destroy()
        ctk.CTkLabel(frame, image=ctk_img, text="").pack(expand=True)
    
    def select_browse_video(self, video_data: dict):
        """Select a video and show its details"""
        # Unhighlight previous selection
        if self.selected_browse_video:
            prev_key = str(self.selected_browse_video["video"])
            if prev_key in self.browse_list_items:
                prev_item = self.browse_list_items[prev_key]
                try:
                    prev_item.configure(fg_color=("gray85", "gray20"))
                except:
                    pass  # Widget destroyed
            
            # Reset text colors (check if widget still exists)
            try:
                if "title_label" in self.selected_browse_video and self.selected_browse_video["title_label"].winfo_exists():
                    self.selected_browse_video["title_label"].configure(text_color=("gray10", "gray90"))
                if "subtitle_label" in self.selected_browse_video and self.selected_browse_video["subtitle_label"].winfo_exists():
                    self.selected_browse_video["subtitle_label"].configure(text_color="gray")
                if "date_label" in self.selected_browse_video and self.selected_browse_video["date_label"].winfo_exists():
                    self.selected_browse_video["date_label"].configure(text_color="gray")
            except:
                pass  # Widget destroyed
        
        self.selected_browse_video = video_data
        
        # Highlight current selection
        if str(video_data["video"]) in self.browse_list_items:
            current_item = self.browse_list_items[str(video_data["video"])]
            current_item.configure(fg_color=("#3B8ED0", "#1F6AA5"))  # Blue highlight
        
        # Change text colors to white for better contrast
        if "title_label" in video_data:
            video_data["title_label"].configure(text_color="white")
        if "subtitle_label" in video_data:
            video_data["subtitle_label"].configure(text_color=("gray95", "gray95"))
        if "date_label" in video_data:
            video_data["date_label"].configure(text_color=("gray95", "gray95"))
        
        # Update info display - more compact
        for widget in self.browse_info_frame.winfo_children():
            widget.destroy()
        
        data = video_data["data"]
        
        # Create compact info display
        info_container = ctk.CTkFrame(self.browse_info_frame, fg_color="transparent")
        info_container.pack(fill="x", padx=15, pady=10)
        
        # Title
        ctk.CTkLabel(info_container, text=f"📹 {data.get('title', 'Untitled')}", 
            font=ctk.CTkFont(size=12, weight="bold"), anchor="w").pack(fill="x")
        
        # Hook
        ctk.CTkLabel(info_container, text=f"🪝 {data.get('hook_text', 'N/A')}", 
            font=ctk.CTkFont(size=11), text_color="gray", anchor="w", wraplength=480).pack(fill="x", pady=(3, 0))
        
        # Stats in one line
        stats = f"⏱️ {data.get('duration_seconds', 0):.0f}s  •  📅 {video_data['folder'].name}"
        ctk.CTkLabel(info_container, text=stats, 
            font=ctk.CTkFont(size=10), text_color="gray", anchor="w").pack(fill="x", pady=(3, 0))
        
        # YouTube info if uploaded
        if data.get('youtube_url'):
            yt_info = ctk.CTkFrame(info_container, fg_color=("gray90", "gray17"), corner_radius=8)
            yt_info.pack(fill="x", pady=(8, 0))
            
            yt_label = ctk.CTkLabel(yt_info, text=f"✅ Uploaded to YouTube", 
                font=ctk.CTkFont(size=11, weight="bold"), text_color="#27ae60", anchor="w")
            yt_label.pack(fill="x", padx=10, pady=(8, 3))
            
            yt_link = ctk.CTkLabel(yt_info, text=data.get('youtube_url'), 
                font=ctk.CTkFont(size=10), text_color="#3B8ED0", anchor="w", cursor="hand2")
            yt_link.pack(fill="x", padx=10, pady=(0, 8))
            yt_link.bind("<Button-1>", lambda e: self.open_youtube_url(data.get('youtube_url')))
        
        # Enable buttons
        self.browse_play_btn.configure(state="normal")
        self.browse_folder_btn.configure(state="normal")
        
        # Disable upload button if already uploaded
        if data.get('youtube_url'):
            self.browse_upload_btn.configure(state="disabled", text="✅ Already Uploaded")
        else:
            self.browse_upload_btn.configure(state="normal", text="⬆️ Upload to YouTube")
    
    def open_youtube_url(self, url: str):
        """Open YouTube URL in browser"""
        import webbrowser
        webbrowser.open(url)
    
    def play_selected_video_internal(self):
        """Play video - open in external player (simpler and more reliable)"""
        if not self.selected_browse_video:
            return
        
        video_path = self.selected_browse_video["video"]
        
        if sys.platform == "win32":
            os.startfile(str(video_path))
        elif sys.platform == "darwin":
            subprocess.run(["open", str(video_path)])
        else:
            subprocess.run(["xdg-open", str(video_path)])
    
    def upload_browse_video(self):
        """Upload selected video to YouTube"""
        if not self.selected_browse_video:
            return
        
        # Reformat data to match YouTubeUploadDialog expected format
        clip_data = {
            "folder": self.selected_browse_video["folder"],
            "video": self.selected_browse_video["video"],
            "title": self.selected_browse_video["data"].get("title", "Untitled"),
            "hook_text": self.selected_browse_video["data"].get("hook_text", ""),
            "duration": self.selected_browse_video["data"].get("duration_seconds", 0)
        }
        
        # Open YouTube upload dialog
        YouTubeUploadDialog(self, clip_data, self.client, self.config.get("model", "gpt-4.1"))
    
    def open_selected_folder(self):
        """Open the selected video's folder"""
        if self.selected_browse_video:
            folder_path = self.selected_browse_video["folder"]
            if sys.platform == "win32":
                os.startfile(str(folder_path))
            elif sys.platform == "darwin":
                subprocess.run(["open", str(folder_path)])
            else:
                subprocess.run(["xdg-open", str(folder_path)])
    
    def load_config(self):
        api_key = self.config.get("api_key", "")
        base_url = self.config.get("base_url", "https://api.openai.com/v1")
        model = self.config.get("model", "")
        if api_key:
            try:
                self.client = OpenAI(api_key=api_key, base_url=base_url)
                self.api_dot.configure(text_color="#27ae60")  # Green
                self.api_status_label.configure(text=model[:15] if model else "Connected")
            except:
                self.api_dot.configure(text_color="#e74c3c")  # Red
                self.api_status_label.configure(text="Invalid key")
        else:
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
                    self.yt_dot.configure(text_color="#27ae60")  # Green
                    
                    # Show channel name
                    channel_name = channel['title']
                    self.yt_status_label_home.configure(text=f"{channel_name[:20]}")
                    return
            
            self.youtube_connected = False
            self.yt_dot.configure(text_color="#e74c3c")  # Red
            self.yt_status_label_home.configure(text="Not connected")
        except:
            self.youtube_connected = False
            self.yt_dot.configure(text_color="#e74c3c")  # Red
            self.yt_status_label_home.configure(text="Not available")
    
    def update_connection_status(self):
        """Update connection status cards (called after settings change)"""
        self.load_config()
        self.check_youtube_status()
    
    def on_settings_saved(self, api_key, base_url, model):
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.api_dot.configure(text_color="#27ae60")  # Green
        self.api_status_label.configure(text=model[:15] if model else "Connected")
    
    def on_url_change(self, *args):
        url = self.url_var.get().strip()
        video_id = extract_video_id(url)
        if video_id:
            self.load_thumbnail(video_id)
        else:
            self.thumb_label.configure(image=None, text="📺 Video thumbnail will appear here")
            self.current_thumbnail = None
            # Disable start button when URL is invalid
            self.start_btn.configure(state="disabled", fg_color="gray")
    
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
                img.thumbnail((480, 190), Image.Resampling.LANCZOS)
                self.after(0, lambda: self.show_thumbnail(img))
            except:
                self.after(0, lambda: self.on_thumbnail_error())
        self.thumb_label.configure(text="Loading...")
        self.start_btn.configure(state="disabled", fg_color="gray")
        threading.Thread(target=fetch, daemon=True).start()
    
    def on_thumbnail_error(self):
        self.thumb_label.configure(text="⚠️ Could not load thumbnail")
        self.start_btn.configure(state="disabled", fg_color="gray")
    
    def show_thumbnail(self, img):
        ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
        self.current_thumbnail = ctk_img
        self.thumb_label.configure(image=ctk_img, text="")
        # Enable start button when thumbnail loads successfully
        self.start_btn.configure(state="normal", fg_color=("#3B8ED0", "#1F6AA5"))

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
        
        # Reset UI
        self.processing = True
        self.cancelled = False
        self.token_usage = {"gpt_input": 0, "gpt_output": 0, "whisper_seconds": 0, "tts_chars": 0}
        
        for step in self.steps:
            step.reset()
        
        self.status_label.configure(text="Initializing...")
        self.gpt_label.configure(text="0")
        self.whisper_label.configure(text="0")
        self.tts_label.configure(text="0")
        self.cancel_btn.configure(state="normal")
        self.open_btn.configure(state="disabled")
        self.back_btn.configure(state="disabled")
        
        self.show_page("processing")
        
        output_dir = self.config.get("output_dir", str(OUTPUT_DIR))
        model = self.config.get("model", "gpt-4.1")
        
        threading.Thread(target=self.run_processing, args=(url, num_clips, output_dir, model), daemon=True).start()
    
    def run_processing(self, url, num_clips, output_dir, model):
        try:
            from clipper_core import AutoClipperCore
            core = AutoClipperCore(
                client=self.client,
                ffmpeg_path=get_ffmpeg_path(),
                ytdlp_path=get_ytdlp_path(),
                output_dir=output_dir,
                model=model,
                log_callback=lambda m: self.after(0, lambda: self.update_status(m)),
                progress_callback=lambda s, p: self.after(0, lambda: self.update_progress(s, p)),
                token_callback=lambda a, b, c, d: self.after(0, lambda: self.update_tokens(a, b, c, d)),
                cancel_check=lambda: self.cancelled
            )
            core.process(url, num_clips)
            if not self.cancelled:
                self.after(0, self.on_complete)
        except Exception as e:
            error_msg = str(e)
            if self.cancelled or "cancel" in error_msg.lower():
                self.after(0, self.on_cancelled)
            else:
                self.after(0, lambda: self.on_error(error_msg))

    def update_status(self, msg):
        self.status_label.configure(text=msg)
    
    def update_progress(self, status, progress):
        self.status_label.configure(text=status)
        
        # Update step indicators based on status text
        status_lower = status.lower()
        
        if "download" in status_lower:
            self.steps[0].set_active(status)
            self.steps[1].reset()
            self.steps[2].reset()
            self.steps[3].reset()
        elif "highlight" in status_lower or "finding" in status_lower:
            self.steps[0].set_done("Downloaded")
            self.steps[1].set_active(status)
            self.steps[2].reset()
            self.steps[3].reset()
        elif "clip" in status_lower:
            self.steps[0].set_done("Downloaded")
            self.steps[1].set_done("Found highlights")
            
            # Parse clip progress: "Clip 2/5: Adding captions..."
            # Show detailed sub-step in step 3
            if "cutting" in status_lower:
                self.steps[2].set_active(f"{status} (25%)")
                self.steps[3].reset()
            elif "portrait" in status_lower:
                self.steps[2].set_active(f"{status} (50%)")
                self.steps[3].reset()
            elif "hook" in status_lower:
                self.steps[2].set_active(f"{status} (75%)")
                self.steps[3].reset()
            elif "caption" in status_lower:
                self.steps[2].set_active(f"{status} (90%)")
                self.steps[3].set_active("Adding captions...")
            elif "done" in status_lower:
                # Extract clip number to show progress
                import re
                match = re.search(r'Clip (\d+)/(\d+)', status)
                if match:
                    current, total = int(match.group(1)), int(match.group(2))
                    percent = int(100 * current / total)
                    self.steps[2].set_active(f"Clip {current}/{total} complete ({percent}%)")
                else:
                    self.steps[2].set_active(status)
                self.steps[3].reset()
            else:
                self.steps[2].set_active(status)
                self.steps[3].reset()
        elif "clean" in status_lower:
            self.steps[0].set_done("Downloaded")
            self.steps[1].set_done("Found highlights")
            self.steps[2].set_done("All clips created")
            self.steps[3].set_active("Cleaning up...")
        elif "complete" in status_lower:
            for step in self.steps:
                step.set_done("Complete")
    
    def update_tokens(self, gpt_in, gpt_out, whisper, tts):
        self.token_usage["gpt_input"] += gpt_in
        self.token_usage["gpt_output"] += gpt_out
        self.token_usage["whisper_seconds"] += whisper
        self.token_usage["tts_chars"] += tts
        self.gpt_label.configure(text=f"{self.token_usage['gpt_input'] + self.token_usage['gpt_output']:,}")
        self.whisper_label.configure(text=f"{self.token_usage['whisper_seconds']/60:.1f}m")
        self.tts_label.configure(text=f"{self.token_usage['tts_chars']:,}")
    
    def cancel_processing(self):
        if messagebox.askyesno("Cancel", "Are you sure you want to cancel?"):
            self.cancelled = True
            self.status_label.configure(text="⚠️ Cancelling... please wait")
            self.cancel_btn.configure(state="disabled")
    
    def on_cancelled(self):
        """Called when processing is cancelled"""
        self.processing = False
        self.status_label.configure(text="⚠️ Cancelled by user")
        self.cancel_btn.configure(state="disabled")
        self.back_btn.configure(state="normal")
        for step in self.steps:
            if step.status == "active":
                step.set_error("Cancelled")
    
    def on_complete(self):
        self.processing = False
        self.status_label.configure(text="✅ All clips created successfully!")
        self.cancel_btn.configure(state="disabled")
        self.open_btn.configure(state="normal")
        self.back_btn.configure(state="normal")
        self.results_btn.configure(state="normal")
        for step in self.steps:
            step.set_done("Complete")
        
        # Load created clips
        self.load_created_clips()
    
    def load_created_clips(self):
        """Load info about created clips from output directory"""
        output_dir = Path(self.config.get("output_dir", str(OUTPUT_DIR)))
        self.created_clips = []
        
        # Find all clip folders (sorted by name = creation time)
        clip_folders = sorted([d for d in output_dir.iterdir() if d.is_dir() and not d.name.startswith("_")], reverse=True)
        
        for folder in clip_folders[:20]:  # Limit to 20 most recent
            data_file = folder / "data.json"
            master_file = folder / "master.mp4"
            
            if data_file.exists() and master_file.exists():
                try:
                    with open(data_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    self.created_clips.append({
                        "folder": folder,
                        "video": master_file,
                        "title": data.get("title", "Untitled"),
                        "hook_text": data.get("hook_text", ""),
                        "duration": data.get("duration_seconds", 0)
                    })
                except:
                    pass
    
    def show_results(self):
        """Show results page with clip list"""
        # Clear existing clips
        for widget in self.clips_frame.winfo_children():
            widget.destroy()
        
        if not self.created_clips:
            ctk.CTkLabel(self.clips_frame, text="No clips found", text_color="gray").pack(pady=50)
        else:
            for i, clip in enumerate(self.created_clips):
                self.create_clip_card(clip, i)
        
        self.show_page("results")
    
    def create_clip_card(self, clip: dict, index: int):
        """Create a card for a single clip"""
        card = ctk.CTkFrame(self.clips_frame, fg_color=("gray85", "gray20"), corner_radius=10)
        card.pack(fill="x", pady=5, padx=5)
        
        # Left: Thumbnail (extract from video)
        thumb_frame = ctk.CTkFrame(card, width=120, height=80, fg_color=("gray75", "gray30"), corner_radius=8)
        thumb_frame.pack(side="left", padx=10, pady=10)
        thumb_frame.pack_propagate(False)
        
        # Try to load thumbnail
        self.load_video_thumbnail(clip["video"], thumb_frame)
        
        # Middle: Info
        info_frame = ctk.CTkFrame(card, fg_color="transparent")
        info_frame.pack(side="left", fill="both", expand=True, pady=10)
        
        ctk.CTkLabel(info_frame, text=clip["title"][:40], font=ctk.CTkFont(size=13, weight="bold"), anchor="w").pack(fill="x")
        ctk.CTkLabel(info_frame, text=f"Hook: {clip['hook_text'][:50]}...", font=ctk.CTkFont(size=11), 
            text_color="gray", anchor="w", wraplength=200).pack(fill="x")
        ctk.CTkLabel(info_frame, text=f"Duration: {clip['duration']:.0f}s", font=ctk.CTkFont(size=10), 
            text_color="gray", anchor="w").pack(fill="x")
        
        # Right: Buttons
        btn_frame = ctk.CTkFrame(card, fg_color="transparent")
        btn_frame.pack(side="right", padx=10, pady=10)
        
        ctk.CTkButton(btn_frame, text="▶", width=35, height=30, 
            command=lambda v=clip["video"]: self.play_video(v)).pack(side="left", padx=2)
        ctk.CTkButton(btn_frame, text="📂", width=35, height=30, fg_color="gray",
            command=lambda f=clip["folder"]: self.open_folder(f)).pack(side="left", padx=2)
        
        # YouTube upload button
        upload_btn = ctk.CTkButton(btn_frame, text="⬆️ YT", width=50, height=30, 
            fg_color="#c4302b", hover_color="#ff0000",
            command=lambda c=clip: self.upload_to_youtube(c))
        upload_btn.pack(side="left", padx=2)
    
    def upload_to_youtube(self, clip: dict):
        """Open YouTube upload dialog for a clip"""
        try:
            from youtube_uploader import YouTubeUploader
            uploader = YouTubeUploader()
            
            if not uploader.is_configured():
                messagebox.showerror("Error", "YouTube not configured.\nPlease add client_secret.json to app folder.\nSee README for setup guide.")
                return
            
            if not uploader.is_authenticated():
                messagebox.showinfo("Connect YouTube", "Please connect your YouTube account first.\nGo to Settings → YouTube tab.")
                return
            
            # Open upload dialog
            YouTubeUploadDialog(self, clip, self.client, self.config.get("model", "gpt-4.1"))
            
        except ImportError:
            messagebox.showerror("Error", "YouTube upload module not available.\nInstall: pip install google-api-python-client google-auth-oauthlib")
        except Exception as e:
            messagebox.showerror("Error", f"Upload error: {str(e)}")
    
    def load_video_thumbnail(self, video_path: Path, frame: ctk.CTkFrame):
        """Load thumbnail from video file"""
        def extract():
            try:
                import cv2
                cap = cv2.VideoCapture(str(video_path))
                cap.set(cv2.CAP_PROP_POS_FRAMES, 30)  # Get frame at ~1 second
                ret, img = cap.read()
                cap.release()
                
                if ret:
                    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                    pil_img = Image.fromarray(img)
                    pil_img.thumbnail((120, 80), Image.Resampling.LANCZOS)
                    self.after(0, lambda: self.show_video_thumb(frame, pil_img))
            except:
                pass
        
        threading.Thread(target=extract, daemon=True).start()
    
    def show_video_thumb(self, frame: ctk.CTkFrame, img: Image.Image):
        """Display thumbnail in frame"""
        ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
        # Store reference to prevent garbage collection
        if not hasattr(self, '_thumb_refs'):
            self._thumb_refs = []
        self._thumb_refs.append(ctk_img)
        
        for widget in frame.winfo_children():
            widget.destroy()
        ctk.CTkLabel(frame, image=ctk_img, text="").pack(expand=True)
    
    def play_video(self, video_path: Path):
        """Open video in default player"""
        if sys.platform == "win32":
            os.startfile(str(video_path))
        elif sys.platform == "darwin":
            subprocess.run(["open", str(video_path)])
        else:
            subprocess.run(["xdg-open", str(video_path)])
    
    def open_folder(self, folder_path: Path):
        """Open folder in file explorer"""
        if sys.platform == "win32":
            os.startfile(str(folder_path))
        elif sys.platform == "darwin":
            subprocess.run(["open", str(folder_path)])
        else:
            subprocess.run(["xdg-open", str(folder_path)])
    
    def on_error(self, error):
        self.processing = False
        self.status_label.configure(text=f"❌ {error}")
        self.cancel_btn.configure(state="disabled")
        self.back_btn.configure(state="normal")
        for step in self.steps:
            if step.status == "active":
                step.set_error("Failed")
    
    def open_output(self):
        output_dir = self.config.get("output_dir", str(OUTPUT_DIR))
        if sys.platform == "win32":
            os.startfile(output_dir)
        else:
            subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", output_dir])


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    app = YTShortClipperApp()
    app.mainloop()


if __name__ == "__main__":
    main()
