"""
Browse page for viewing existing videos
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


class BrowsePage(ctk.CTkFrame):
    """Browse page - view and manage existing videos"""
    
    def __init__(self, parent, config, client, on_back_callback, refresh_icon=None):
        super().__init__(parent)
        self.config = config
        self.client = client
        self.on_back = on_back_callback
        self.refresh_icon = refresh_icon
        
        self.selected_browse_video = None
        self.browse_thumbnails = []
        self.browse_list_items = {}
        
        self.create_ui()
    
    def create_ui(self):
        """Create the browse page UI"""
        # Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(15, 10))
        
        ctk.CTkButton(header, text="←", width=40, fg_color="transparent", 
            hover_color=("gray75", "gray25"), command=self.on_back).pack(side="left")
        ctk.CTkLabel(header, text="Browse Videos", font=ctk.CTkFont(size=22, weight="bold")).pack(side="left", padx=10)
        ctk.CTkButton(header, text="Refresh", image=self.refresh_icon, compound="left",
            height=35, width=110, command=self.refresh_list).pack(side="right")
        
        # Main content
        main = ctk.CTkFrame(self)
        main.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        
        # Video list (scrollable) - larger
        self.list_frame = ctk.CTkScrollableFrame(main, height=400)
        self.list_frame.pack(fill="both", expand=True, pady=(10, 10))
        
        # Selected video info - fixed height to prevent overlap
        self.info_frame = ctk.CTkFrame(main, fg_color=("gray90", "gray17"), height=120)
        self.info_frame.pack(fill="x", pady=(0, 10))
        self.info_frame.pack_propagate(False)  # Prevent frame from expanding
        
        self.info_label = ctk.CTkLabel(self.info_frame, text="Select a video to view details", 
            font=ctk.CTkFont(size=11), text_color="gray")
        self.info_label.pack(pady=12)
        
        # Action buttons - larger
        btn_frame = ctk.CTkFrame(main, fg_color="transparent")
        btn_frame.pack(fill="x", side="bottom")
        
        self.play_btn = ctk.CTkButton(btn_frame, text="▶ Play Video", height=45, state="disabled",
            font=ctk.CTkFont(size=14, weight="bold"), command=self.play_selected_video)
        self.play_btn.pack(fill="x", pady=(0, 5))
        
        btn_row = ctk.CTkFrame(btn_frame, fg_color="transparent")
        btn_row.pack(fill="x")
        
        self.upload_btn = ctk.CTkButton(btn_row, text="⬆️ Upload to YouTube", height=45, state="disabled",
            font=ctk.CTkFont(size=13), fg_color="#c4302b", hover_color="#ff0000",
            command=self.upload_video)
        self.upload_btn.pack(side="left", fill="x", expand=True, padx=(0, 5))
        
        self.folder_btn = ctk.CTkButton(btn_row, text="📂 Open Folder", height=45, state="disabled",
            font=ctk.CTkFont(size=13), fg_color="gray", command=self.open_selected_folder)
        self.folder_btn.pack(side="left", fill="x", expand=True, padx=(5, 0))
    
    def refresh_list(self):
        """Refresh the list of videos in output folder"""
        # Clear selection
        self.selected_browse_video = None
        
        # Clear existing list
        for widget in self.list_frame.winfo_children():
            widget.destroy()
        self.browse_thumbnails = []
        self.browse_list_items = {}
        
        # Clear info frame
        for widget in self.info_frame.winfo_children():
            widget.destroy()
        ctk.CTkLabel(self.info_frame, text="Select a video to view details", 
            font=ctk.CTkFont(size=11), text_color="gray").pack(pady=12)
        
        # Disable buttons
        self.play_btn.configure(state="disabled")
        self.folder_btn.configure(state="disabled")
        self.upload_btn.configure(state="disabled", text="⬆️ Upload to YouTube")
        
        output_dir = Path(self.config.get("output_dir", "output"))
        
        if not output_dir.exists():
            ctk.CTkLabel(self.list_frame, text="📂 Output folder not found", 
                font=ctk.CTkFont(size=13), text_color="gray").pack(pady=30)
            return
        
        # Find all clip folders
        clip_folders = sorted([d for d in output_dir.iterdir() if d.is_dir() and not d.name.startswith("_")], reverse=True)
        
        if not clip_folders:
            ctk.CTkLabel(self.list_frame, text="📹 No videos found\n\nProcess a video to see it here", 
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
                    item = ctk.CTkFrame(self.list_frame, fg_color=("gray85", "gray20"), corner_radius=10)
                    item.pack(fill="x", pady=5, padx=5)
                    
                    # Thumbnail on left
                    thumb_frame = ctk.CTkFrame(item, width=140, height=80, fg_color=("gray75", "gray30"), corner_radius=8)
                    thumb_frame.pack(side="left", padx=12, pady=12)
                    thumb_frame.pack_propagate(False)
                    
                    # Load thumbnail async
                    self.load_thumbnail(master_file, thumb_frame)
                    
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
                        return lambda e: self.select_video(v)
                    
                    item.bind("<Button-1>", make_click_handler(video_data))
                    for child in item.winfo_children():
                        child.bind("<Button-1>", make_click_handler(video_data))
                        for subchild in child.winfo_children():
                            subchild.bind("<Button-1>", make_click_handler(video_data))
                    
                except:
                    pass
    
    def load_thumbnail(self, video_path: Path, frame: ctk.CTkFrame):
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
                    pil_img.thumbnail((140, 80), Image.Resampling.LANCZOS)
                    self.after(0, lambda: self.show_thumb(frame, pil_img))
            except:
                pass
        
        threading.Thread(target=extract, daemon=True).start()
    
    def show_thumb(self, frame: ctk.CTkFrame, img: Image.Image):
        """Display thumbnail in frame"""
        ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
        self.browse_thumbnails.append(ctk_img)  # Keep reference
        
        for widget in frame.winfo_children():
            widget.destroy()
        ctk.CTkLabel(frame, image=ctk_img, text="").pack(expand=True)
    
    def select_video(self, video_data: dict):
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
        for widget in self.info_frame.winfo_children():
            widget.destroy()
        
        data = video_data["data"]
        
        # Create compact info display
        info_container = ctk.CTkFrame(self.info_frame, fg_color="transparent")
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
        self.play_btn.configure(state="normal")
        self.folder_btn.configure(state="normal")
        
        # Disable upload button if already uploaded
        if data.get('youtube_url'):
            self.upload_btn.configure(state="disabled", text="✅ Already Uploaded")
        else:
            self.upload_btn.configure(state="normal", text="⬆️ Upload to YouTube")
    
    def open_youtube_url(self, url: str):
        """Open YouTube URL in browser"""
        import webbrowser
        webbrowser.open(url)
    
    def play_selected_video(self):
        """Play video - open in external player"""
        if not self.selected_browse_video:
            return
        
        video_path = self.selected_browse_video["video"]
        
        if sys.platform == "win32":
            os.startfile(str(video_path))
        elif sys.platform == "darwin":
            subprocess.run(["open", str(video_path)])
        else:
            subprocess.run(["xdg-open", str(video_path)])
    
    def upload_video(self):
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
        YouTubeUploadDialog(self, clip_data, self.client, 
            self.config.get("model", "gpt-4.1"), 
            self.config.get("temperature", 1.0))
    
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
