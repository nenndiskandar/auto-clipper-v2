"""
Status pages for API and Library checking
"""

import threading
import subprocess
import customtkinter as ctk
from tkinter import messagebox

from utils.helpers import get_ffmpeg_path, get_ytdlp_path


class APIStatusPage(ctk.CTkFrame):
    """API Status page - check OpenAI and YouTube API status"""
    
    def __init__(self, parent, get_client_callback, get_config_callback, get_youtube_status_callback, on_back_callback, refresh_icon=None):
        super().__init__(parent)
        self.get_client = get_client_callback
        self.get_config = get_config_callback
        self.get_youtube_status = get_youtube_status_callback
        self.on_back = on_back_callback
        self.refresh_icon = refresh_icon
        
        self.create_ui()
    
    def create_ui(self):
        """Create the API status page UI"""
        # Header with back button
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(15, 10))
        
        ctk.CTkButton(header, text="←", width=40, fg_color="transparent", 
            hover_color=("gray75", "gray25"), command=self.on_back).pack(side="left")
        ctk.CTkLabel(header, text="API Status", font=ctk.CTkFont(size=22, weight="bold")).pack(side="left", padx=10)
        
        # Main content
        main = ctk.CTkFrame(self)
        main.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        
        # OpenAI API Status
        openai_frame = ctk.CTkFrame(main, fg_color=("gray90", "gray17"))
        openai_frame.pack(fill="x", pady=(15, 10))
        
        openai_header = ctk.CTkFrame(openai_frame, fg_color="transparent")
        openai_header.pack(fill="x", padx=15, pady=(15, 10))
        
        ctk.CTkLabel(openai_header, text="OpenAI API", font=ctk.CTkFont(size=16, weight="bold")).pack(side="left")
        
        self.api_status_label = ctk.CTkLabel(openai_header, text="Checking...", font=ctk.CTkFont(size=13), text_color="gray")
        self.api_status_label.pack(side="right")
        
        self.api_info_label = ctk.CTkLabel(openai_frame, text="", font=ctk.CTkFont(size=12), text_color="gray", anchor="w")
        self.api_info_label.pack(fill="x", padx=15, pady=(0, 15))
        
        # YouTube API Status
        yt_frame = ctk.CTkFrame(main, fg_color=("gray90", "gray17"))
        yt_frame.pack(fill="x", pady=(0, 10))
        
        yt_header = ctk.CTkFrame(yt_frame, fg_color="transparent")
        yt_header.pack(fill="x", padx=15, pady=(15, 10))
        
        ctk.CTkLabel(yt_header, text="YouTube API", font=ctk.CTkFont(size=16, weight="bold")).pack(side="left")
        
        self.yt_status_label = ctk.CTkLabel(yt_header, text="Checking...", font=ctk.CTkFont(size=13), text_color="gray")
        self.yt_status_label.pack(side="right")
        
        self.yt_info_label = ctk.CTkLabel(yt_frame, text="", font=ctk.CTkFont(size=12), text_color="gray", anchor="w")
        self.yt_info_label.pack(fill="x", padx=15, pady=(0, 15))
        
        # Refresh button
        ctk.CTkButton(main, text="Refresh Status", image=self.refresh_icon, compound="left",
            height=45, command=self.refresh_status).pack(fill="x", pady=(10, 0))
    
    def update_status(self, youtube_connected, youtube_channel):
        """Update YouTube connection status (deprecated - now uses callback)"""
        pass
    
    def refresh_status(self):
        """Refresh API status"""
        # Reset to checking state
        self.api_status_label.configure(text="Checking...", text_color="gray")
        self.api_info_label.configure(text="")
        self.yt_status_label.configure(text="Checking...", text_color="gray")
        self.yt_info_label.configure(text="")
        
        def check_status():
            # Get current client and config from parent
            client = self.get_client()
            config = self.get_config()
            youtube_connected, youtube_channel = self.get_youtube_status()
            
            # Check OpenAI status
            if client:
                try:
                    # Try to list models to verify connection
                    models = client.models.list()
                    model_name = config.get("model", "N/A")
                    self.after(0, lambda: self.api_status_label.configure(text="✓ Connected", text_color="green"))
                    self.after(0, lambda: self.api_info_label.configure(text=f"Model: {model_name}"))
                except Exception as e:
                    self.after(0, lambda: self.api_status_label.configure(text="✗ Error", text_color="red"))
                    self.after(0, lambda: self.api_info_label.configure(text=f"Error: {str(e)[:60]}"))
            else:
                self.after(0, lambda: self.api_status_label.configure(text="✗ Not configured", text_color="orange"))
                self.after(0, lambda: self.api_info_label.configure(text="Please configure API key in Settings"))
            
            # Check YouTube status
            if youtube_connected and youtube_channel:
                self.after(0, lambda: self.yt_status_label.configure(text="✓ Connected", text_color="green"))
                self.after(0, lambda: self.yt_info_label.configure(text=f"Channel: {youtube_channel['title']}"))
            else:
                try:
                    from youtube_uploader import YouTubeUploader
                    uploader = YouTubeUploader()
                    if not uploader.is_configured():
                        self.after(0, lambda: self.yt_status_label.configure(text="✗ Not configured", text_color="orange"))
                        self.after(0, lambda: self.yt_info_label.configure(text="client_secret.json not found"))
                    else:
                        self.after(0, lambda: self.yt_status_label.configure(text="✗ Not connected", text_color="orange"))
                        self.after(0, lambda: self.yt_info_label.configure(text="Connect in Settings → YouTube tab"))
                except Exception as e:
                    self.after(0, lambda: self.yt_status_label.configure(text="✗ Error", text_color="red"))
                    self.after(0, lambda: self.yt_info_label.configure(text=f"Error: {str(e)[:60]}"))
        
        threading.Thread(target=check_status, daemon=True).start()


class LibStatusPage(ctk.CTkFrame):
    """Library Status page - check FFmpeg and yt-dlp"""
    
    def __init__(self, parent, on_back_callback, refresh_icon=None):
        super().__init__(parent)
        self.on_back = on_back_callback
        self.refresh_icon = refresh_icon
        
        self.create_ui()
    
    def create_ui(self):
        """Create the library status page UI"""
        # Header with back button
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(15, 10))
        
        ctk.CTkButton(header, text="←", width=40, fg_color="transparent", 
            hover_color=("gray75", "gray25"), command=self.on_back).pack(side="left")
        ctk.CTkLabel(header, text="Library Status", font=ctk.CTkFont(size=22, weight="bold")).pack(side="left", padx=10)
        
        # Main content
        main = ctk.CTkFrame(self)
        main.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        
        # yt-dlp Status
        ytdlp_frame = ctk.CTkFrame(main, fg_color=("gray90", "gray17"))
        ytdlp_frame.pack(fill="x", pady=(15, 10))
        
        ytdlp_header = ctk.CTkFrame(ytdlp_frame, fg_color="transparent")
        ytdlp_header.pack(fill="x", padx=15, pady=(15, 10))
        
        ctk.CTkLabel(ytdlp_header, text="yt-dlp", font=ctk.CTkFont(size=16, weight="bold")).pack(side="left")
        
        self.ytdlp_status_label = ctk.CTkLabel(ytdlp_header, text="Checking...", font=ctk.CTkFont(size=13), text_color="gray")
        self.ytdlp_status_label.pack(side="right")
        
        self.ytdlp_info_label = ctk.CTkLabel(ytdlp_frame, text="", font=ctk.CTkFont(size=12), text_color="gray", anchor="w")
        self.ytdlp_info_label.pack(fill="x", padx=15, pady=(0, 15))
        
        # FFmpeg Status
        ffmpeg_frame = ctk.CTkFrame(main, fg_color=("gray90", "gray17"))
        ffmpeg_frame.pack(fill="x", pady=(0, 10))
        
        ffmpeg_header = ctk.CTkFrame(ffmpeg_frame, fg_color="transparent")
        ffmpeg_header.pack(fill="x", padx=15, pady=(15, 10))
        
        ctk.CTkLabel(ffmpeg_header, text="FFmpeg", font=ctk.CTkFont(size=16, weight="bold")).pack(side="left")
        
        self.ffmpeg_status_label = ctk.CTkLabel(ffmpeg_header, text="Checking...", font=ctk.CTkFont(size=13), text_color="gray")
        self.ffmpeg_status_label.pack(side="right")
        
        self.ffmpeg_info_label = ctk.CTkLabel(ffmpeg_frame, text="", font=ctk.CTkFont(size=12), text_color="gray", anchor="w")
        self.ffmpeg_info_label.pack(fill="x", padx=15, pady=(0, 15))
        
        # Refresh button
        ctk.CTkButton(main, text="Check Libraries", image=self.refresh_icon, compound="left",
            height=45, command=self.refresh_status).pack(fill="x", pady=(10, 0))
    
    def refresh_status(self):
        """Refresh library status"""
        # Reset to checking state
        self.ytdlp_status_label.configure(text="Checking...", text_color="gray")
        self.ytdlp_info_label.configure(text="")
        self.ffmpeg_status_label.configure(text="Checking...", text_color="gray")
        self.ffmpeg_info_label.configure(text="")
        
        def check_libs():
            # Check yt-dlp
            ytdlp_path = get_ytdlp_path()
            try:
                result = subprocess.run([ytdlp_path, "--version"], capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    version = result.stdout.strip()
                    self.after(0, lambda: self.ytdlp_status_label.configure(text="✓ Installed", text_color="green"))
                    self.after(0, lambda: self.ytdlp_info_label.configure(text=f"Version: {version}"))
                else:
                    self.after(0, lambda: self.ytdlp_status_label.configure(text="✗ Error", text_color="red"))
                    self.after(0, lambda: self.ytdlp_info_label.configure(text="Failed to get version"))
            except FileNotFoundError:
                self.after(0, lambda: self.ytdlp_status_label.configure(text="✗ Not found", text_color="red"))
                self.after(0, lambda: self.ytdlp_info_label.configure(text="yt-dlp not installed or not in PATH"))
            except Exception as e:
                self.after(0, lambda: self.ytdlp_status_label.configure(text="✗ Error", text_color="red"))
                self.after(0, lambda: self.ytdlp_info_label.configure(text=f"Error: {str(e)[:50]}"))
            
            # Check FFmpeg
            ffmpeg_path = get_ffmpeg_path()
            try:
                result = subprocess.run([ffmpeg_path, "-version"], capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    # Extract version from first line
                    version_line = result.stdout.split('\n')[0]
                    version = version_line.split('version')[1].split()[0] if 'version' in version_line else "Unknown"
                    self.after(0, lambda: self.ffmpeg_status_label.configure(text="✓ Installed", text_color="green"))
                    self.after(0, lambda: self.ffmpeg_info_label.configure(text=f"Version: {version}"))
                else:
                    self.after(0, lambda: self.ffmpeg_status_label.configure(text="✗ Error", text_color="red"))
                    self.after(0, lambda: self.ffmpeg_info_label.configure(text="Failed to get version"))
            except FileNotFoundError:
                self.after(0, lambda: self.ffmpeg_status_label.configure(text="✗ Not found", text_color="red"))
                self.after(0, lambda: self.ffmpeg_info_label.configure(text="FFmpeg not installed or not in PATH"))
            except Exception as e:
                self.after(0, lambda: self.ffmpeg_status_label.configure(text="✗ Error", text_color="red"))
                self.after(0, lambda: self.ffmpeg_info_label.configure(text=f"Error: {str(e)[:50]}"))
        
        threading.Thread(target=check_libs, daemon=True).start()
