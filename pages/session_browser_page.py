"""
Session Browser Page - Browse and resume previous sessions
"""

import os
import json
import customtkinter as ctk
from pathlib import Path
from tkinter import messagebox
from datetime import datetime


class SessionBrowserPage(ctk.CTkFrame):
    """Page for browsing and resuming previous sessions"""
    
    def __init__(self, parent, config, on_back_callback, on_resume_callback, app=None):
        super().__init__(parent)
        self.config = config
        self.on_back = on_back_callback
        self.on_resume = on_resume_callback
        self.app = app  # Store reference to main app
        
        self.create_ui()
    
    def create_ui(self):
        """Create the session browser UI"""
        from components.page_layout import PageFooter
        
        # Set background color
        self.configure(fg_color=("#ffffff", "#0b0b0c"), corner_radius=5, border_width=1, border_color=("#2a2a30", "#2a2a30"))
        
        # Header
        header_frame = ctk.CTkFrame(self, fg_color="transparent")
        header_frame.pack(fill="x", padx=4, pady=(6, 6))
        
        # Back button + title
        left_header = ctk.CTkFrame(header_frame, fg_color="transparent")
        left_header.pack(side="left")
        
        # Back button removed (sidebar navigation instead)
        ctk.CTkLabel(left_header, text="Browse Sessions", 
            font=ctk.CTkFont(size=15, weight="bold")).pack(side="left", padx=4)
        
        # Instructions
        ctk.CTkLabel(self, text="Resume a previous highlight detection session",
            font=ctk.CTkFont(size=11), text_color="gray").pack(padx=4, pady=(0, 6))
        
        # Scrollable list of sessions
        self.list_frame = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.list_frame.pack(fill="both", expand=True, padx=4, pady=(0, 6))
        
        # Refresh button
        refresh_btn = ctk.CTkButton(self, text="Refresh", height=18,
            fg_color=("#3a3a40", "#2a2a30"), hover_color=("#4a4a50", "#3a3a40"),
            font=ctk.CTkFont(size=11), command=self.refresh_list, border_width=1, border_color=("#3a3a40", "#2a2a30"), text_color=("#FFFFFF", "#FFFFFF"))
        refresh_btn.pack(fill="x", padx=4, pady=(0, 6))
        
        # Footer
        footer = PageFooter(self, self)
        footer.pack(fill="x", padx=4, pady=(4, 8))
        
        # Load sessions on init
        self.refresh_list()
    
    def refresh_list(self):
        """Refresh the list of sessions"""
        # Clear existing
        for widget in self.list_frame.winfo_children():
            widget.destroy()
        
        output_dir = Path(self.config.get("output_dir", "output"))
        sessions_dir = output_dir / "sessions"
        
        if not sessions_dir.exists():
            ctk.CTkLabel(self.list_frame, text="📂 No sessions found",
                font=ctk.CTkFont(size=11), text_color="gray").pack(pady=18)
            return
        
        # Find all session folders with session_data.json (sorted by modification date, newest first)
        sessions = []
        for session_folder in sorted(sessions_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if not session_folder.is_dir():
                continue
            
            session_file = session_folder / "session_data.json"
            if session_file.exists():
                try:
                    with open(session_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    sessions.append((session_folder, data))
                except:
                    pass
        
        if not sessions:
            ctk.CTkLabel(self.list_frame, text="📂 No sessions found",
                font=ctk.CTkFont(size=11), text_color="gray").pack(pady=18)
            return
        
        # Create list items
        for session_folder, data in sessions[:50]:  # Limit to 50
            # Card frame
            card = ctk.CTkFrame(self.list_frame, fg_color=("gray85", "gray20"), corner_radius=5, border_width=1, border_color=("#2a2a30", "#2a2a30"))
            card.pack(fill="x", pady=4, padx=4)
            
            # Main content
            content = ctk.CTkFrame(card, fg_color="transparent")
            content.pack(fill="x", padx=4, pady=4)
            
            # Top row: Video title + Status badge
            top_row = ctk.CTkFrame(content, fg_color="transparent")
            top_row.pack(fill="x", pady=(0, 4))
            
            # Video title
            video_info = data.get("video_info") or {}
            title = (video_info.get("title") or session_folder.name or "Unknown Video")[:60]
            ctk.CTkLabel(top_row, text=title, 
                font=ctk.CTkFont(size=11, weight="bold"), anchor="w").pack(side="left", fill="x", expand=True)
            
            # Status badge
            status = data.get("status", "unknown")
            status_colors = {
                "highlights_found": ("#f39c12", "Highlights Ready"),
                "completed": ("#27ae60", "Completed"),
                "processing": ("#00A878", "Processing"),
                "downloading": ("#00A878", "Downloading"),
                "analyzing": ("#00A878", "Analyzing"),
                "cancelled": ("#e67e22", "Cancelled"),
                "error": ("#e74c3c", "Failed"),
                "unknown": ("#95a5a6", "Unknown")
            }
            color, status_text = status_colors.get(status, ("#95a5a6", status.title()))
            ctk.CTkLabel(top_row, text=status_text,
                font=ctk.CTkFont(size=11, weight="bold"), text_color=color).pack(side="right", padx=(4, 0))
            
            # Middle row: Info
            info_row = ctk.CTkFrame(content, fg_color="transparent")
            info_row.pack(fill="x", pady=(0, 4))
            
            # Highlights count
            highlights_count = len(data.get("highlights") or [])
            ctk.CTkLabel(info_row, text=f"🎯 {highlights_count} highlights",
                font=ctk.CTkFont(size=11), text_color="gray", anchor="w").pack(side="left", padx=(0, 8))
            
            # Clips processed (if completed)
            clips_processed = data.get("clips_processed", 0)
            if clips_processed > 0:
                ctk.CTkLabel(info_row, text=f"🎬 {clips_processed} clips",
                    font=ctk.CTkFont(size=11), text_color="gray", anchor="w").pack(side="left", padx=(0, 8))
            
            # Created date
            created_at = data.get("created_at", "")
            if created_at:
                try:
                    dt = datetime.fromisoformat(created_at)
                    date_str = dt.strftime("%Y-%m-%d %H:%M")
                except:
                    date_str = created_at[:16]
            else:
                date_str = session_folder.name
            
            ctk.CTkLabel(info_row, text=f"📅 {date_str}",
                font=ctk.CTkFont(size=11), text_color="gray", anchor="w").pack(side="left")
            
            # Bottom row: Actions
            action_row = ctk.CTkFrame(content, fg_color="transparent")
            action_row.pack(fill="x")
            
            # Error message (if the session failed)
            error_msg = data.get("error", "")
            if error_msg and status == "error":
                ctk.CTkLabel(content, text=f"⚠️ {error_msg[:120]}",
                    font=ctk.CTkFont(size=11), text_color="#e74c3c", anchor="w",
                    wraplength=560, justify="left").pack(fill="x", pady=(0, 4))
            
            # Check if clips exist
            clips_dir = session_folder / "clips"
            has_clips = clips_dir.exists() and any(clips_dir.iterdir())
            
            # View Session button (always show if highlights exist)
            if highlights_count > 0:
                view_session_btn = ctk.CTkButton(action_row, text="View Session", height=22, width=140,
                    font=ctk.CTkFont(size=11), fg_color=("#00A878", "#00A878"),
                    command=lambda d=data: self.resume_session(d), text_color=("#0B0B0C", "#0B0B0C"))
                view_session_btn.pack(side="left", padx=(0, 4))
            
            # View clips button (if clips exist)
            if has_clips:
                clips_btn = ctk.CTkButton(action_row, text="View Clips", height=22, width=120,
                    font=ctk.CTkFont(size=11), fg_color=("#27ae60", "#2ecc71"),
                    command=lambda f=clips_dir: self.view_clips(f), text_color=("#FFFFFF", "#FFFFFF"))
                clips_btn.pack(side="left", padx=(0, 4))
            
            # Open folder button
            folder_btn = ctk.CTkButton(action_row, text="Open Folder", height=22, width=120,
                font=ctk.CTkFont(size=11), fg_color=("#3a3a40", "#2a2a30"),
                command=lambda f=session_folder: self.open_folder(f), text_color=("#FFFFFF", "#FFFFFF"))
            folder_btn.pack(side="left", padx=(0, 4))
            
            # Delete button
            delete_btn = ctk.CTkButton(action_row, text="Delete", height=22, width=85,
                font=ctk.CTkFont(size=11), fg_color=("#c0392b", "#e74c3c"),
                hover_color=("#e74c3c", "#c0392b"),
                command=lambda f=session_folder: self.delete_session(f), text_color=("#FFFFFF", "#FFFFFF"))
            delete_btn.pack(side="left")
    
    def resume_session(self, session_data: dict):
        """Resume a session (supports both old and new format)"""
        # Convert paths back to Path objects
        session_data["session_dir"] = Path(session_data["session_dir"])
        if session_data.get("srt_path"):
            session_data["srt_path"] = session_data["srt_path"]
        
        # Backward compatibility: old sessions have video_path, new ones have url
        if "video_path" in session_data:
            session_data["video_path"] = session_data["video_path"]
        
        # Call resume callback
        self.on_resume(session_data)
    
    def view_clips(self, clips_dir: Path):
        """View clips in results page UI"""
        # Call app's method to load clips and show results page
        if self.app and hasattr(self.app, 'load_session_clips'):
            self.app.load_session_clips(clips_dir)
        else:
            # Fallback to opening folder if method not available
            self.open_folder(clips_dir)
    
    def open_folder(self, folder: Path):
        """Open session folder in file explorer"""
        import sys
        import subprocess
        
        if folder.exists():
            if sys.platform == "win32":
                os.startfile(str(folder))
            elif sys.platform == "darwin":
                subprocess.run(["open", str(folder)])
            else:
                subprocess.run(["xdg-open", str(folder)])
        else:
            messagebox.showerror("Error", "Folder not found")
    
    def delete_session(self, folder: Path):
        """Delete a session"""
        if messagebox.askyesno("Confirm Delete", 
            f"Delete this session?\n\n{folder.name}\n\nThis will delete all files in this session folder."):
            try:
                import shutil
                shutil.rmtree(folder)
                self.refresh_list()
                messagebox.showinfo("Success", "Session deleted successfully")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to delete session:\n{str(e)}")
    
    def show_page(self, page_name: str):
        """Navigate to another page (for footer compatibility)"""
        pass
    
    def open_github(self):
        """Open GitHub repository"""
        import webbrowser
        webbrowser.open("https://github.com/jipraks/yt-short-clipper")
    
    def open_discord(self):
        """Open Discord server"""
        import webbrowser
        webbrowser.open("https://s.id/ytsdiscord")
