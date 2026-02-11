"""
Highlight Selection Page - User selects which highlights to process
"""

import os
import sys
import subprocess
import customtkinter as ctk
from pathlib import Path
from tkinter import messagebox


class HighlightSelectionPage(ctk.CTkFrame):
    """Page for selecting highlights to process"""
    
    def __init__(self, parent, on_back_callback, on_process_callback):
        super().__init__(parent)
        self.on_back = on_back_callback
        self.on_process = on_process_callback
        
        self.highlights = []
        self.video_path = None
        self.session_dir = None
        self.checkboxes = []
        self.checkbox_vars = []
        
        self.create_ui()
    
    def create_ui(self):
        """Create the highlight selection UI"""
        from components.page_layout import PageFooter
        
        # Set background color
        self.configure(fg_color=("#1a1a1a", "#0a0a0a"))
        
        # Header
        header_frame = ctk.CTkFrame(self, fg_color="transparent")
        header_frame.pack(fill="x", padx=20, pady=(15, 10))
        
        # Back button + title
        left_header = ctk.CTkFrame(header_frame, fg_color="transparent")
        left_header.pack(side="left")
        
        ctk.CTkButton(left_header, text="←", width=40, fg_color="transparent",
            hover_color=("gray75", "gray25"), command=self.on_back).pack(side="left")
        ctk.CTkLabel(left_header, text="Select Highlights", 
            font=ctk.CTkFont(size=22, weight="bold")).pack(side="left", padx=10)
        
        # Instructions
        instructions_frame = ctk.CTkFrame(self, fg_color="transparent")
        instructions_frame.pack(fill="x", padx=20, pady=(0, 5))
        
        ctk.CTkLabel(instructions_frame, text="Select which highlights you want to process into short videos",
            font=ctk.CTkFont(size=12), text_color="gray").pack(anchor="w")
        
        # Virality score legend
        legend_frame = ctk.CTkFrame(instructions_frame, fg_color="transparent")
        legend_frame.pack(anchor="w", pady=(3, 0))
        
        ctk.CTkLabel(legend_frame, text="Virality Score:", 
            font=ctk.CTkFont(size=10), text_color="gray").pack(side="left", padx=(0, 8))
        ctk.CTkLabel(legend_frame, text="🔥 7-10 High", 
            font=ctk.CTkFont(size=9), text_color="#27ae60").pack(side="left", padx=(0, 8))
        ctk.CTkLabel(legend_frame, text="⚡ 5-6 Medium", 
            font=ctk.CTkFont(size=9), text_color="#f39c12").pack(side="left", padx=(0, 8))
        ctk.CTkLabel(legend_frame, text="💫 1-4 Low", 
            font=ctk.CTkFont(size=9), text_color="#e74c3c").pack(side="left")
        
        # Enhancement options (Caption & Hook)
        options_frame = ctk.CTkFrame(self, fg_color=("#2b2b2b", "#1a1a1a"), corner_radius=8)
        options_frame.pack(fill="x", padx=20, pady=(0, 10))
        
        ctk.CTkLabel(options_frame, text="Enhancements", 
            font=ctk.CTkFont(size=11, weight="bold"), anchor="w").pack(fill="x", padx=12, pady=(10, 5))
        
        # Captions toggle
        captions_row = ctk.CTkFrame(options_frame, fg_color="transparent")
        captions_row.pack(fill="x", padx=12, pady=(0, 3))
        
        ctk.CTkLabel(captions_row, text="💬 Add Captions", font=ctk.CTkFont(size=10), 
            anchor="w").pack(side="left")
        
        self.caption_var = ctk.BooleanVar(value=False)
        self.caption_switch = ctk.CTkSwitch(captions_row, text="OFF", variable=self.caption_var, 
            width=36, height=18, command=self.update_caption_switch_text)
        self.caption_switch.pack(side="right")
        
        # Hook toggle
        hook_row = ctk.CTkFrame(options_frame, fg_color="transparent")
        hook_row.pack(fill="x", padx=12, pady=(0, 10))
        
        ctk.CTkLabel(hook_row, text="🪝 Add Hook Text", font=ctk.CTkFont(size=10), 
            anchor="w").pack(side="left")
        
        self.hook_var = ctk.BooleanVar(value=False)
        self.hook_switch = ctk.CTkSwitch(hook_row, text="OFF", variable=self.hook_var, 
            width=36, height=18, command=self.update_hook_switch_text)
        self.hook_switch.pack(side="right")
        
        # Scrollable list of highlights
        self.list_frame = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.list_frame.pack(fill="both", expand=True, padx=20, pady=(0, 10))
        
        # Bottom action buttons
        bottom_frame = ctk.CTkFrame(self, fg_color="transparent")
        bottom_frame.pack(fill="x", padx=20, pady=(0, 10))
        
        # Select all / Deselect all
        select_frame = ctk.CTkFrame(bottom_frame, fg_color="transparent")
        select_frame.pack(fill="x", pady=(0, 10))
        
        ctk.CTkButton(select_frame, text="✓ Select All", height=35,
            fg_color=("#3a3a3a", "#2a2a2a"), hover_color=("#4a4a4a", "#3a3a3a"),
            font=ctk.CTkFont(size=11), command=self.select_all).pack(side="left", fill="x", expand=True, padx=(0, 5))
        
        ctk.CTkButton(select_frame, text="✗ Deselect All", height=35,
            fg_color=("#3a3a3a", "#2a2a2a"), hover_color=("#4a4a4a", "#3a3a3a"),
            font=ctk.CTkFont(size=11), command=self.deselect_all).pack(side="left", fill="x", expand=True, padx=(5, 0))
        
        # Process button
        self.process_btn = ctk.CTkButton(bottom_frame, text="🎬 Process Selected Clips", height=45,
            font=ctk.CTkFont(size=14, weight="bold"), command=self.process_selected,
            fg_color=("#3B8ED0", "#1F6AA5"), hover_color=("#2E7AB8", "#16527D"))
        self.process_btn.pack(fill="x")
        
        # Footer
        footer = PageFooter(self, self)
        footer.pack(fill="x", padx=20, pady=(10, 15))
    
    def set_highlights(self, highlights: list, video_path: str, session_dir: Path):
        """Set highlights data and populate list"""
        self.highlights = highlights
        self.video_path = video_path
        self.session_dir = session_dir
        self.populate_list()
    
    def is_ffplay_available(self) -> bool:
        """Check if ffplay is available"""
        try:
            from utils.helpers import get_ffmpeg_path
            ffmpeg_path = get_ffmpeg_path()
            
            if ffmpeg_path and Path(ffmpeg_path).exists():
                ffmpeg_dir = Path(ffmpeg_path).parent
                # Check for ffplay.exe or ffplay
                for name in ["ffplay.exe", "ffplay"]:
                    if (ffmpeg_dir / name).exists():
                        return True
            
            # Try system PATH
            import shutil
            return shutil.which("ffplay") is not None
        except:
            return False
    
    def populate_list(self):
        """Populate the highlights list"""
        # Clear existing
        for widget in self.list_frame.winfo_children():
            widget.destroy()
        self.checkboxes = []
        self.checkbox_vars = []
        
        if not self.highlights:
            ctk.CTkLabel(self.list_frame, text="No highlights found",
                font=ctk.CTkFont(size=13), text_color="gray").pack(pady=30)
            return
        
        # Create list items
        for i, highlight in enumerate(self.highlights, 1):
            # Card frame
            card = ctk.CTkFrame(self.list_frame, fg_color=("gray85", "gray20"), corner_radius=10)
            card.pack(fill="x", pady=5, padx=5)
            
            # Main content
            content = ctk.CTkFrame(card, fg_color="transparent")
            content.pack(fill="x", padx=15, pady=12)
            
            # Top row: Checkbox + Title + Virality Score
            top_row = ctk.CTkFrame(content, fg_color="transparent")
            top_row.pack(fill="x", pady=(0, 5))
            
            # Checkbox
            var = ctk.BooleanVar(value=True)  # Default selected
            checkbox = ctk.CTkCheckBox(top_row, text="", variable=var, width=24, height=24)
            checkbox.pack(side="left", padx=(0, 10))
            self.checkboxes.append(checkbox)
            self.checkbox_vars.append(var)
            
            # Title
            title = highlight.get("title", "Untitled")
            ctk.CTkLabel(top_row, text=f"#{i}. {title}", 
                font=ctk.CTkFont(size=13, weight="bold"), anchor="w").pack(side="left", fill="x", expand=True)
            
            # Virality score badge (always show)
            virality = highlight.get("virality_score", 0)
            if virality >= 7:
                score_color = "#27ae60"  # Green - High viral potential
                score_emoji = "🔥"
            elif virality >= 5:
                score_color = "#f39c12"  # Orange - Medium viral potential
                score_emoji = "⚡"
            elif virality > 0:
                score_color = "#e74c3c"  # Red - Low viral potential
                score_emoji = "💫"
            else:
                score_color = "#95a5a6"  # Gray - No score
                score_emoji = "❓"
            
            ctk.CTkLabel(top_row, text=f"{score_emoji} {virality}/10",
                font=ctk.CTkFont(size=11, weight="bold"), text_color=score_color).pack(side="right", padx=(10, 0))
            
            # Description
            description = highlight.get("description", "")
            if description:
                ctk.CTkLabel(content, text=description, font=ctk.CTkFont(size=11),
                    text_color="gray", anchor="w", wraplength=650, justify="left").pack(fill="x", pady=(0, 5))
            
            # Bottom row: Timestamp + Duration + Preview button (if ffplay available)
            bottom_row = ctk.CTkFrame(content, fg_color="transparent")
            bottom_row.pack(fill="x")
            
            # Timestamp and duration
            start_time = highlight.get("start_time", "00:00:00,000")
            end_time = highlight.get("end_time", "00:00:00,000")
            duration = highlight.get("duration_seconds", 0)
            
            # Format timestamps (remove milliseconds for display)
            start_display = start_time.split(',')[0]
            end_display = end_time.split(',')[0]
            
            ctk.CTkLabel(bottom_row, text=f"⏱️ {start_display} → {end_display} ({duration:.0f}s)",
                font=ctk.CTkFont(size=10), text_color="gray", anchor="w").pack(side="left")
            
            # Preview button (only if ffplay is available)
            if self.is_ffplay_available():
                preview_btn = ctk.CTkButton(bottom_row, text="▶ Preview", height=28, width=100,
                    font=ctk.CTkFont(size=10), fg_color=("#3B8ED0", "#1F6AA5"),
                    command=lambda h=highlight: self.preview_highlight(h))
                preview_btn.pack(side="right")
    
    def preview_highlight(self, highlight: dict):
        """Preview a highlight using ffplay"""
        if not self.video_path or not Path(self.video_path).exists():
            messagebox.showerror("Error", "Video file not found")
            return
        
        try:
            # Get ffplay path (should be in same dir as ffmpeg)
            from utils.helpers import get_ffmpeg_path
            ffmpeg_path = get_ffmpeg_path()
            
            if ffmpeg_path and Path(ffmpeg_path).exists():
                ffmpeg_dir = Path(ffmpeg_path).parent
                # Try different possible names
                possible_names = ["ffplay.exe", "ffplay"]
                ffplay_path = None
                
                for name in possible_names:
                    candidate = ffmpeg_dir / name
                    if candidate.exists():
                        ffplay_path = str(candidate)
                        break
                
                if not ffplay_path:
                    # ffplay not found in ffmpeg directory
                    messagebox.showerror("Preview Error", 
                        "ffplay.exe not found!\n\n"
                        "ffplay is required for video preview.\n"
                        "Please ensure ffplay.exe is in the same folder as ffmpeg.exe")
                    return
            else:
                # Try system PATH
                ffplay_path = "ffplay"
            
            # Parse timestamps
            start_time = highlight.get("start_time", "00:00:00,000").replace(",", ".")
            duration = highlight.get("duration_seconds", 60)
            
            # Launch ffplay with start time and duration
            cmd = [
                ffplay_path,
                "-ss", start_time,
                "-t", str(duration),
                "-autoexit",  # Close when done
                "-window_title", f"Preview: {highlight.get('title', 'Highlight')}",
                self.video_path
            ]
            
            # Launch in background (don't wait)
            if sys.platform == "win32":
                subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
        except FileNotFoundError as e:
            messagebox.showerror("Preview Error", 
                f"Failed to launch ffplay:\n\n"
                f"ffplay.exe not found in system PATH or ffmpeg directory.\n\n"
                f"Please ensure ffplay.exe is available.")
        except Exception as e:
            messagebox.showerror("Preview Error", f"Failed to preview video:\n{str(e)}")
    
    def select_all(self):
        """Select all checkboxes"""
        for var in self.checkbox_vars:
            var.set(True)
    
    def deselect_all(self):
        """Deselect all checkboxes"""
        for var in self.checkbox_vars:
            var.set(False)
    
    def process_selected(self):
        """Process selected highlights"""
        # Get selected highlights
        selected = []
        for i, var in enumerate(self.checkbox_vars):
            if var.get():
                selected.append(self.highlights[i])
        
        if not selected:
            messagebox.showwarning("No Selection", "Please select at least one highlight to process")
            return
        
        # Get enhancement options
        add_captions = self.caption_var.get()
        add_hook = self.hook_var.get()
        
        # Confirm with user
        count = len(selected)
        enhancements = []
        if add_captions:
            enhancements.append("Captions")
        if add_hook:
            enhancements.append("Hook Text")
        
        enhancement_text = " + ".join(enhancements) if enhancements else "No enhancements"
        
        if not messagebox.askyesno("Confirm Processing", 
            f"Process {count} selected clip{'s' if count > 1 else ''}?\n\n"
            f"Enhancements: {enhancement_text}\n\n"
            "This will create short videos with portrait conversion."):
            return
        
        # Call process callback with selected highlights and options
        self.on_process(selected, add_captions, add_hook)
    
    def update_caption_switch_text(self):
        """Update caption switch text based on state"""
        if self.caption_var.get():
            self.caption_switch.configure(text="ON")
        else:
            self.caption_switch.configure(text="OFF")
    
    def update_hook_switch_text(self):
        """Update hook switch text based on state"""
        if self.hook_var.get():
            self.hook_switch.configure(text="ON")
        else:
            self.hook_switch.configure(text="OFF")
    
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
