"""
Results page for viewing created clips
"""

import os
import sys
import json
import threading
import subprocess
import customtkinter as ctk
from pathlib import Path
from tkinter import messagebox
from PIL import Image
import cv2

from dialogs.youtube_upload import YouTubeUploadDialog


class ResultsPage(ctk.CTkFrame):
    """Results page - view clips created in current session"""
    
    def __init__(self, parent, config, client, on_back_callback, on_home_callback, open_output_callback, get_youtube_client=None):
        super().__init__(parent)
        self.config = config
        self.client = client
        self.get_youtube_client = get_youtube_client or (lambda: client)
        self.on_back = on_back_callback
        self.on_home = on_home_callback
        self.open_output = open_output_callback
        self.default_back_callback = on_back_callback  # Store default
        
        self.created_clips = []
        self._thumb_refs = []
        
        self.create_ui()
    
    def set_back_callback(self, callback):
        """Change the back button callback dynamically"""
        self.on_back = callback
    
    def create_ui(self):
        """Create the results page UI"""
        # Configure page container
        self.configure(fg_color=("#ffffff", "#0b0b0c"), corner_radius=5, border_width=1, border_color=("#2a2a30", "#2a2a30"))
        
        # Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=4, pady=(6, 6))
        ctk.CTkLabel(header, text="📋 Results", font=ctk.CTkFont(size=15, weight="bold")).pack(side="left")
        
        # Clips list (scrollable)
        self.clips_frame = ctk.CTkScrollableFrame(self, height=450)
        self.clips_frame.pack(fill="both", expand=True, padx=4, pady=(0, 6))
        
        # Buttons
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=4, pady=(0, 8))
        
        ctk.CTkButton(btn_frame, font=ctk.CTkFont(size=11), text="Open Folder", height=22, command=self.open_output).pack(side="left", fill="x", expand=True, padx=(0, 4))
        ctk.CTkButton(btn_frame, font=ctk.CTkFont(size=11), text="New Clip", height=22, fg_color="#27ae60", hover_color="#2ecc71", command=self.on_home, text_color=("#FFFFFF", "#FFFFFF")).pack(side="left", fill="x", expand=True, padx=(4, 0))
    
    # Stage files produced by the pipeline - never used as the playable clip
    _STAGE_MP4 = {"landscape.mp4", "portrait.mp4", "hook.mp4", "captioned.mp4",
                  "watermark.mp4", "credit.mp4"}

    @classmethod
    def _find_video_file(cls, folder: Path):
        """Find the clip video file.

        Priority:
        1. Final clip-named mp4 (e.g. "<clip title>.mp4" from highlight selection)
        2. master.mp4 (legacy sessions)
        3. captioned.mp4 (fallback if no final-named file exists)
        4. First other mp4 in the folder
        """
        # 1. Final named file: any mp4 that is not a pipeline stage file
        final = [v for v in folder.glob("*.mp4") if v.name not in cls._STAGE_MP4]
        if final:
            return sorted(final)[0]
        # 2. Legacy master.mp4
        master = folder / "master.mp4"
        if master.exists():
            return master
        # 3. Captioned version
        captioned = folder / "captioned.mp4"
        if captioned.exists():
            return captioned
        # 4. Any remaining mp4
        videos = sorted(folder.glob("*.mp4"))
        return videos[0] if videos else None

    def load_clips(self, clips_dir: Path = None):
        """Load info about created clips from output directory or specific clips folder"""
        folders = []

        if clips_dir is None:
            # Default behavior: load from output directory
            output_dir = Path(self.config.get("output_dir", "output"))
            self.created_clips = []
            
            # Find all clip folders (sorted by modification date, newest first)
            folders = sorted(
                [d for d in output_dir.iterdir() if d.is_dir() and not d.name.startswith("_")],
                key=lambda x: x.stat().st_mtime,
                reverse=True
            )
            folders = folders[:20]  # Limit to 20 most recent
        else:
            # Load from specific clips directory (session-based)
            self.created_clips = []
            if not clips_dir.exists():
                return
            folders = sorted(
                [d for d in clips_dir.iterdir() if d.is_dir()],
                key=lambda x: x.stat().st_mtime,
                reverse=True
            )

        for folder in folders:
            data_file = folder / "data.json"
            master_file = self._find_video_file(folder)

            if data_file.exists() and master_file:
                try:
                    with open(data_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    self.created_clips.append({
                        "folder": folder,
                        "video": master_file,
                        "title": data.get("title", "Untitled"),
                        "hook_text": data.get("hook_text", ""),
                        "duration": data.get("duration_seconds", 0),
                        "has_hook": data.get("has_hook", False),
                        "has_captions": data.get("has_captions", False),
                        "has_watermark": data.get("has_watermark", False),
                        "has_credit": data.get("has_credit", False),
                        "channel_name": data.get("channel_name", ""),
                        "data_aspect_ratio": data.get("aspect_ratio", ""),
                        "file_size": self._file_size(master_file),
                        "created": self._file_date(master_file),
                        "aspect_ratio": data.get("aspect_ratio", ""),
                        "resolution": self._video_resolution(master_file),
                    })
                except:
                    pass

    @staticmethod
    def _file_size(video_path: Path) -> str:
        """Return human-readable file size, or empty string."""
        try:
            size = video_path.stat().st_size
            if size >= 1 << 30:
                return f"{size / (1 << 30):.2f} GB"
            if size >= 1 << 20:
                return f"{size / (1 << 20):.1f} MB"
            if size >= 1 << 10:
                return f"{size / (1 << 10):.0f} KB"
            return f"{size} B"
        except Exception:
            return ""

    @staticmethod
    def _file_date(video_path: Path) -> str:
        """Return human-readable modification date of the file."""
        try:
            import datetime
            ts = video_path.stat().st_mtime
            return datetime.datetime.fromtimestamp(ts).strftime("%d %b %Y")
        except Exception:
            return ""

    @staticmethod
    def _video_resolution(video_path: Path):
        """Return (width, height, orientation, aspect_ratio) or (None, ...)."""
        try:
            cap = cv2.VideoCapture(str(video_path))
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            cap.release()
            if w <= 0 or h <= 0:
                return None
            orientation = "Vertical" if h > w else "Horizontal"
            ratio = w / h
            if ratio > 1.7:
                ar = "16:9"
            elif ratio > 1.2:
                ar = "4:3"
            elif ratio > 0.9:
                ar = "1:1"
            elif ratio > 0.65:
                ar = "4:5"
            else:
                ar = "9:16"
            return {"width": w, "height": h, "orientation": orientation, "aspect_ratio": ar}
        except Exception:
            return None

    
    def show_results(self):
        """Show results page with clip list"""
        # Clear existing clips
        for widget in self.clips_frame.winfo_children():
            widget.destroy()
        
        # Clear thumbnail references
        self._thumb_refs = []
        
        if not self.created_clips:
            ctk.CTkLabel(self.clips_frame, text="No clips found", text_color="gray").pack(pady=28)
        else:
            for i, clip in enumerate(self.created_clips):
                self.create_clip_card(clip, i)
    
    def create_clip_card(self, clip: dict, index: int):
        """Create a card for a single clip with detailed metadata and stacked action buttons."""
        card = ctk.CTkFrame(self.clips_frame, fg_color=("gray85", "gray20"), corner_radius=5, border_width=1, border_color=("#2a2a30", "#2a2a30"))
        card.pack(fill="x", pady=6, padx=6)

        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(10, 0))

        # Left: Thumbnail (extract from video)
        thumb_frame = ctk.CTkFrame(top, width=200, height=120, fg_color=("gray75", "gray30"), corner_radius=5, border_width=1, border_color=("#2a2a30", "#2a2a30"))
        thumb_frame.pack(side="left", padx=(0, 12))
        thumb_frame.pack_propagate(False)

        # Try to load thumbnail
        self.load_video_thumbnail(clip["video"], thumb_frame)

        # Middle: Info
        info_frame = ctk.CTkFrame(top, fg_color="transparent")
        info_frame.pack(side="left", fill="both", expand=True)

        # Title + channel
        ctk.CTkLabel(info_frame, text=clip["title"][:45], font=ctk.CTkFont(size=12, weight="bold"), anchor="w").pack(fill="x", pady=(0, 2))
        if clip.get("channel_name"):
            ctk.CTkLabel(info_frame, text=f"📺 {clip['channel_name'][:40]}", font=ctk.CTkFont(size=11),
                text_color="gray", anchor="w").pack(fill="x", pady=(1, 0))

        # Duration + file size + date + resolution
        detail_items = []
        if clip.get("duration"):
            detail_items.append(f"⏱ {clip['duration']:.0f}s")
        if clip.get("file_size"):
            detail_items.append(f"💾 {clip['file_size']}")
        if clip.get("created"):
            detail_items.append(f"📅 {clip['created']}")
        if clip.get("resolution"):
            res = clip["resolution"]
            detail_items.append(f"🖥 {res['width']}×{res['height']}")
        if detail_items:
            ctk.CTkLabel(info_frame, text="  ·  ".join(detail_items), font=ctk.CTkFont(size=11),
                text_color="gray", anchor="w").pack(fill="x", pady=(2, 0))

        # Orientation + aspect ratio
        orient_parts = []
        if clip.get("resolution"):
            res = clip["resolution"]
            orient_parts.append(f"🧭 {res['orientation']}")
            data_ar = clip.get("aspect_ratio")
            computed_ar = res["aspect_ratio"]
            ar_display = data_ar if data_ar else computed_ar
            if ar_display:
                orient_parts.append(f"⚖ {ar_display}")
        if orient_parts:
            ctk.CTkLabel(info_frame, text="  ·  ".join(orient_parts), font=ctk.CTkFont(size=11),
                text_color="gray", anchor="w").pack(fill="x", pady=(2, 0))

        # Feature flags (has_hook / captions / watermark / credit)
        flags = []
        flag_defs = [
            ("has_hook", "Hook"),
            ("has_captions", "Captions"),
            ("has_watermark", "Watermark"),
            ("has_credit", "Credit"),
        ]
        for key, label in flag_defs:
            if clip.get(key):
                flags.append(label)
        if flags:
            ctk.CTkLabel(info_frame, text="✅ " + "  ·  ".join(flags), font=ctk.CTkFont(size=11),
                text_color="#27ae60", anchor="w").pack(fill="x", pady=(2, 0))

        # Hook text preview
        if clip.get("hook_text"):
            ctk.CTkLabel(info_frame, text=f"\"{clip['hook_text'][:55]}...\"", font=ctk.CTkFont(size=11),
                text_color="gray", anchor="w", wraplength=380, justify="left").pack(fill="x", pady=(2, 0))

        # Bottom: Action buttons (Play / Open / Repliz / YT / Social Kit) in stacked rows
        action_frame = ctk.CTkFrame(card, fg_color="transparent")
        action_frame.pack(fill="x", padx=10, pady=(10, 10))

        row1 = ctk.CTkFrame(action_frame, fg_color="transparent")
        row1.pack(fill="x", pady=(0, 6))
        ctk.CTkButton(row1, font=ctk.CTkFont(size=11), text="▶ Play", height=26, 
            command=lambda v=clip["video"]: self.play_video(v)).pack(side="left", fill="x", expand=True, padx=(0, 4))
        ctk.CTkButton(row1, font=ctk.CTkFont(size=11), text="📂 Open", height=26, fg_color="gray",
            command=lambda f=clip["folder"]: self.open_folder(f), text_color=("#FFFFFF", "#FFFFFF")).pack(side="left", fill="x", expand=True, padx=(4, 0))

        row2 = ctk.CTkFrame(action_frame, fg_color="transparent")
        row2.pack(fill="x")
        ctk.CTkButton(row2, font=ctk.CTkFont(size=11), text="Repliz", height=26,
            fg_color="#9b59b6", hover_color="#8e44ad",
            command=lambda c=clip: self.upload_to_repliz(c), text_color=("#FFFFFF", "#FFFFFF")).pack(side="left", fill="x", expand=True, padx=(0, 4))
        ctk.CTkButton(row2, font=ctk.CTkFont(size=11), text="YouTube", height=26,
            fg_color="#c4302b", hover_color="#ff0000",
            command=lambda c=clip: self.upload_to_youtube(c), text_color=("#FFFFFF", "#FFFFFF")).pack(side="left", fill="x", expand=True, padx=(4, 0))
        ctk.CTkButton(row2, font=ctk.CTkFont(size=11), text="Social Kit", height=26,
            fg_color="#2ecc71", hover_color="#27ae60",
            command=lambda c=clip: self.open_social_kit(c), text_color=("#FFFFFF", "#FFFFFF")).pack(side="left", fill="x", expand=True, padx=(4, 0))

    
    def open_social_kit(self, clip: dict):
        """Open Social Kit dialog for a clip"""
        try:
            from dialogs.social_kit import SocialKitDialog
            
            # Get client and config
            yt_client = self.get_youtube_client()
            ai_providers = self.config.get("ai_providers", {})
            yt_config = ai_providers.get("youtube_title_maker", {})
            model = yt_config.get("model", self.config.get("model", "gpt-4.1"))
            
            # Open Social Kit dialog
            SocialKitDialog(self, clip, yt_client, model, 
                self.config.get("temperature", 1.0))
            
        except Exception as e:
            messagebox.showerror("Error", f"Social Kit error: {str(e)}")
    
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
            
            # Get YouTube-specific client and config
            yt_client = self.get_youtube_client()
            ai_providers = self.config.get("ai_providers", {})
            yt_config = ai_providers.get("youtube_title_maker", {})
            model = yt_config.get("model", self.config.get("model", "gpt-4.1"))
            
            # Open upload dialog
            YouTubeUploadDialog(self, clip, yt_client, model, 
                self.config.get("temperature", 1.0))
            
        except ImportError:
            messagebox.showerror("Error", "YouTube upload module not available.\nInstall: pip install google-api-python-client google-auth-oauthlib")
        except Exception as e:
            messagebox.showerror("Error", f"Upload error: {str(e)}")
    
    def upload_to_repliz(self, clip: dict):
        """Open Repliz upload dialog for a clip"""
        try:
            # Check if Repliz is configured
            repliz_config = self.config.get("repliz", {})
            access_key = repliz_config.get("access_key", "")
            secret_key = repliz_config.get("secret_key", "")
            
            if not access_key or not secret_key:
                messagebox.showerror("Repliz Not Configured", 
                    "Please configure Repliz API keys in Settings → Repliz tab first.")
                return
            
            # Get OpenAI client and config for metadata generation
            yt_client = self.get_youtube_client()
            ai_providers = self.config.get("ai_providers", {})
            yt_config = ai_providers.get("youtube_title_maker", {})
            model = yt_config.get("model", self.config.get("model", "gpt-4.1"))
            
            # Open Repliz account selection dialog
            from dialogs.repliz_upload import ReplizUploadDialog
            ReplizUploadDialog(self, clip, access_key, secret_key, 
                yt_client, model, self.config.get("temperature", 1.0))
            
        except ImportError:
            messagebox.showerror("Error", "Repliz upload module not available.")
        except Exception as e:
            messagebox.showerror("Error", f"Upload error: {str(e)}")
    
    def load_video_thumbnail(self, video_path: Path, frame: ctk.CTkFrame):
        """Load thumbnail from video file"""
        def extract():
            try:
                cap = cv2.VideoCapture(str(video_path))
                cap.set(cv2.CAP_PROP_POS_FRAMES, 30)  # Get frame at ~1 second
                ret, img = cap.read()
                cap.release()
                
                if ret:
                    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                    pil_img = Image.fromarray(img)
                    pil_img.thumbnail((200, 120), Image.Resampling.LANCZOS)
                    self.after(0, lambda: self.show_video_thumb(frame, pil_img))
            except:
                pass
        
        threading.Thread(target=extract, daemon=True).start()
    
    def show_video_thumb(self, frame: ctk.CTkFrame, img: Image.Image):
        """Display thumbnail in frame"""
        ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
        self._thumb_refs.append(ctk_img)  # Store reference to prevent garbage collection
        
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
