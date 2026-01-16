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
    APP_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).parent

CONFIG_FILE = APP_DIR / "config.json"
OUTPUT_DIR = APP_DIR / "output"
ASSETS_DIR = APP_DIR / "assets"
ICON_PATH = ASSETS_DIR / "icon.png"


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


class SettingsPage(ctk.CTkToplevel):
    def __init__(self, parent, config: ConfigManager, on_save_callback):
        super().__init__(parent)
        self.config = config
        self.on_save = on_save_callback
        self.models_list = []
        
        self.title("API Settings")
        self.geometry("500x350")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        
        main = ctk.CTkFrame(self)
        main.pack(fill="both", expand=True, padx=20, pady=20)
        
        ctk.CTkLabel(main, text="⚙️ API Settings", font=ctk.CTkFont(size=20, weight="bold")).pack(pady=(0, 20))
        
        ctk.CTkLabel(main, text="Base URL", anchor="w").pack(fill="x")
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
        self.load_config()
    
    def load_config(self):
        self.url_entry.insert(0, self.config.get("base_url", "https://api.openai.com/v1"))
        self.key_entry.insert(0, self.config.get("api_key", ""))
        self.model_var.set(self.config.get("model", "gpt-4.1"))
        if self.config.get("api_key"):
            self.validate_key()

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
        if not api_key or model == "Select model...":
            messagebox.showerror("Error", "Fill all fields")
            return
        self.config.set("api_key", api_key)
        self.config.set("base_url", base_url)
        self.config.set("model", model)
        self.on_save(api_key, base_url, model)
        self.destroy()


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
        
        self.show_page("home")
        self.load_config()
        
        # Store created clips info
        self.created_clips = []
    
    def set_app_icon(self):
        """Set window icon"""
        if not ICON_PATH.exists():
            return
        try:
            if sys.platform == "win32":
                ico_path = ASSETS_DIR / "icon.ico"
                if not ico_path.exists():
                    img = Image.open(ICON_PATH)
                    img.save(str(ico_path), format='ICO', sizes=[(16, 16), (32, 32), (48, 48)])
                self.iconbitmap(str(ico_path))
            else:
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
            command=self.open_settings).pack(side="right")
        self.api_indicator = ctk.CTkLabel(top, text="", font=ctk.CTkFont(size=11))
        self.api_indicator.pack(side="right", padx=10)
        
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
        
        # Start button
        self.start_btn = ctk.CTkButton(main, text="🚀 Start Processing", font=ctk.CTkFont(size=15, weight="bold"), 
            height=50, command=self.start_processing)
        self.start_btn.pack(fill="x", padx=15, pady=(0, 20))

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
    
    def load_config(self):
        api_key = self.config.get("api_key", "")
        base_url = self.config.get("base_url", "https://api.openai.com/v1")
        model = self.config.get("model", "")
        if api_key:
            try:
                self.client = OpenAI(api_key=api_key, base_url=base_url)
                self.api_indicator.configure(text=f"✓ {model}", text_color="green")
            except:
                self.api_indicator.configure(text="⚠️ API", text_color="yellow")
        else:
            self.api_indicator.configure(text="⚠️ API", text_color="yellow")
    
    def open_settings(self):
        SettingsPage(self, self.config, self.on_settings_saved)
    
    def on_settings_saved(self, api_key, base_url, model):
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.api_indicator.configure(text=f"✓ {model}", text_color="green")
    
    def on_url_change(self, *args):
        url = self.url_var.get().strip()
        video_id = extract_video_id(url)
        if video_id:
            self.load_thumbnail(video_id)
        else:
            self.thumb_label.configure(image=None, text="📺 Video thumbnail will appear here")
            self.current_thumbnail = None
    
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
                self.after(0, lambda: self.thumb_label.configure(text="⚠️ Could not load thumbnail"))
        self.thumb_label.configure(text="Loading...")
        threading.Thread(target=fetch, daemon=True).start()
    
    def show_thumbnail(self, img):
        ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
        self.current_thumbnail = ctk_img
        self.thumb_label.configure(image=ctk_img, text="")

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
            text_color="gray", anchor="w", wraplength=250).pack(fill="x")
        ctk.CTkLabel(info_frame, text=f"Duration: {clip['duration']:.0f}s", font=ctk.CTkFont(size=10), 
            text_color="gray", anchor="w").pack(fill="x")
        
        # Right: Buttons
        btn_frame = ctk.CTkFrame(card, fg_color="transparent")
        btn_frame.pack(side="right", padx=10, pady=10)
        
        ctk.CTkButton(btn_frame, text="▶️", width=40, height=35, 
            command=lambda v=clip["video"]: self.play_video(v)).pack(pady=2)
        ctk.CTkButton(btn_frame, text="📂", width=40, height=35, fg_color="gray",
            command=lambda f=clip["folder"]: self.open_folder(f)).pack(pady=2)
    
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
