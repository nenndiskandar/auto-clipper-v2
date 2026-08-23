"""
Highlight Selection Page - User selects which highlights to process
"""

import customtkinter as ctk
import threading
from pathlib import Path
from tkinter import messagebox


class HighlightSelectionPage(ctk.CTkFrame):
    """Page for selecting highlights to process"""
    
    def __init__(self, parent, on_back_callback, on_process_callback):
        super().__init__(parent)
        self.on_back = on_back_callback
        self.on_process = on_process_callback
        
        self.highlights = []
        self.session_dir = None
        self.checkboxes = []
        self.checkbox_vars = []
        
        self.create_ui()
    
    def create_ui(self):
        """Create the highlight selection UI"""
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
        ctk.CTkLabel(left_header, text="Select Highlights", 
            font=ctk.CTkFont(size=15, weight="bold")).pack(side="left", padx=4)
        
        # Instructions
        instructions_frame = ctk.CTkFrame(self, fg_color="transparent")
        instructions_frame.pack(fill="x", padx=4, pady=(0, 4))
        
        ctk.CTkLabel(instructions_frame, text="Select which highlights you want to process into short videos",
            font=ctk.CTkFont(size=11), text_color="gray").pack(anchor="w")
        
        # Virality score legend
        legend_frame = ctk.CTkFrame(instructions_frame, fg_color="transparent")
        legend_frame.pack(anchor="w", pady=(4, 0))
        
        ctk.CTkLabel(legend_frame, text="Virality Score:", 
            font=ctk.CTkFont(size=11), text_color="gray").pack(side="left", padx=(0, 6))
        ctk.CTkLabel(legend_frame, text="🔥 7-10 High", 
            font=ctk.CTkFont(size=11), text_color="#27ae60").pack(side="left", padx=(0, 6))
        ctk.CTkLabel(legend_frame, text="⚡ 5-6 Medium", 
            font=ctk.CTkFont(size=11), text_color="#f39c12").pack(side="left", padx=(0, 6))
        ctk.CTkLabel(legend_frame, text="💫 1-4 Low", 
            font=ctk.CTkFont(size=11), text_color="#e74c3c").pack(side="left")
        
        # Enhancement options (Caption & Hook) - 2-column grid
        options_frame = ctk.CTkFrame(self, fg_color=("#ffffff", "#17171b"), corner_radius=5, border_width=1, border_color=("#2a2a30", "#2a2a30"))
        options_frame.pack(fill="x", padx=4, pady=(0, 6))
        options_frame.grid_columnconfigure(0, weight=1)
        options_frame.grid_columnconfigure(1, weight=1)

        # Captions toggle (col 0)
        captions_cell = ctk.CTkFrame(options_frame, fg_color="transparent")
        captions_cell.grid(row=1, column=0, sticky="ew", padx=(4, 4), pady=(0, 4))
        ctk.CTkLabel(captions_cell, text="Add Captions", font=ctk.CTkFont(size=11), 
            anchor="w").pack(side="left", padx=(0, 8))
        self.caption_var = ctk.BooleanVar(value=False)
        self.caption_switch = ctk.CTkSwitch(captions_cell, text="OFF", variable=self.caption_var, 
            width=36, height=18, command=self.update_caption_switch_text)
        self.caption_switch.pack(side="right")

        # Hook toggle (col 1)
        hook_cell = ctk.CTkFrame(options_frame, fg_color="transparent")
        hook_cell.grid(row=1, column=1, sticky="ew", padx=(4, 4), pady=(0, 4))
        ctk.CTkLabel(hook_cell, text="Add Hook Text", font=ctk.CTkFont(size=11), 
            anchor="w").pack(side="left", padx=(0, 8))
        self.hook_var = ctk.BooleanVar(value=False)
        self.hook_switch = ctk.CTkSwitch(hook_cell, text="OFF", variable=self.hook_var, 
            width=36, height=18, command=self.update_hook_switch_text)
        self.hook_switch.pack(side="right")

        # Portrait mode (col 0)
        portrait_cell = ctk.CTkFrame(options_frame, fg_color="transparent")
        portrait_cell.grid(row=2, column=0, sticky="ew", padx=(4, 4), pady=(0, 4))
        ctk.CTkLabel(portrait_cell, text="Portrait Mode", font=ctk.CTkFont(size=11), 
            anchor="w").pack(side="left", padx=(0, 8))
        self.portrait_mode_var = ctk.StringVar(value="Smart Crop")
        self.portrait_mode_menu = ctk.CTkOptionMenu(portrait_cell, variable=self.portrait_mode_var,
            values=["Smart Crop", "Blurred Background (no crop)"], width=170, height=24, corner_radius=5,
            fg_color=("#ffffff", "#17171b"), button_color=("#e8eaee", "#2a2a30"),
            button_hover_color=("#d9dbe0", "#3a3a40"),
            dropdown_fg_color=("#ffffff", "#17171b"))
        self.portrait_mode_menu.pack(side="right")

        # Face tracking mode (col 1)
        tracking_cell = ctk.CTkFrame(options_frame, fg_color="transparent")
        tracking_cell.grid(row=2, column=1, sticky="ew", padx=(4, 4), pady=(0, 4))
        ctk.CTkLabel(tracking_cell, text="Face Tracking", font=ctk.CTkFont(size=11), 
            anchor="w").pack(side="left", padx=(0, 8))
        self.face_tracking_var = ctk.StringVar(value="OpenCV (Fast)")
        self.face_tracking_menu = ctk.CTkOptionMenu(tracking_cell, variable=self.face_tracking_var,
            values=["OpenCV (Fast)", "MediaPipe (Smart)"], width=170, height=24, corner_radius=5,
            fg_color=("#ffffff", "#17171b"), button_color=("#e8eaee", "#2a2a30"),
            button_hover_color=("#d9dbe0", "#3a3a40"),
            dropdown_fg_color=("#ffffff", "#17171b"))
        self.face_tracking_menu.pack(side="right")

        # Aspect ratio (col 0)
        ratio_cell = ctk.CTkFrame(options_frame, fg_color="transparent")
        ratio_cell.grid(row=3, column=0, sticky="ew", padx=(4, 4), pady=(0, 4))
        ctk.CTkLabel(ratio_cell, text="Aspect Ratio", font=ctk.CTkFont(size=11), 
            anchor="w").pack(side="left", padx=(0, 8))
        self.aspect_ratio_var = ctk.StringVar(value="9:16")
        self.aspect_ratio_menu = ctk.CTkOptionMenu(ratio_cell, variable=self.aspect_ratio_var,
            values=["9:16", "1:1", "4:5", "16:9"], width=170, height=24, corner_radius=5,
            fg_color=("#ffffff", "#17171b"), button_color=("#e8eaee", "#2a2a30"),
            button_hover_color=("#d9dbe0", "#3a3a40"),
            dropdown_fg_color=("#ffffff", "#17171b"))
        self.aspect_ratio_menu.pack(side="right")

        # Video resolution (col 1)
        resolution_cell = ctk.CTkFrame(options_frame, fg_color="transparent")
        resolution_cell.grid(row=3, column=1, sticky="ew", padx=(4, 4), pady=(0, 4))
        ctk.CTkLabel(resolution_cell, text="Video Resolution", font=ctk.CTkFont(size=11), 
            anchor="w").pack(side="left", padx=(0, 8))
        self.resolution_var = ctk.StringVar(value="Auto (Best)")
        self.resolution_menu = ctk.CTkOptionMenu(resolution_cell, variable=self.resolution_var,
            values=["Auto (Best)", "1080p", "720p", "480p", "360p", "144p"], width=170, height=24, corner_radius=5,
            fg_color=("#ffffff", "#17171b"), button_color=("#e8eaee", "#2a2a30"),
            button_hover_color=("#d9dbe0", "#3a3a40"),
            dropdown_fg_color=("#ffffff", "#17171b"))
        self.resolution_menu.pack(side="right")

        # Subtitle style (col 0, hidden when captions off)
        self.subtitle_cell = ctk.CTkFrame(options_frame, fg_color="transparent")
        self.subtitle_cell.grid(row=4, column=0, sticky="ew", padx=(4, 4), pady=(0, 4))
        ctk.CTkLabel(self.subtitle_cell, text="Subtitle Style", font=ctk.CTkFont(size=11), 
            anchor="w").pack(side="left", padx=(0, 8))
        self.subtitle_style_var = ctk.StringVar(value="Pop Highlight")
        self.subtitle_style_menu = ctk.CTkOptionMenu(self.subtitle_cell, variable=self.subtitle_style_var,
            values=["Pop Highlight", "Pop + Bounce", "Karaoke", "Bounce", "Bounce + Word-by-Word"], width=170, height=24, corner_radius=5,
            fg_color=("#ffffff", "#17171b"), button_color=("#e8eaee", "#2a2a30"),
            button_hover_color=("#d9dbe0", "#3a3a40"),
            dropdown_fg_color=("#ffffff", "#17171b"))
        self.subtitle_style_menu.pack(side="right")

        # Subtitle sync offset (col 1, hidden when captions off) - negative = earlier
        self.sync_cell = ctk.CTkFrame(options_frame, fg_color="transparent")
        self.sync_cell.grid(row=4, column=1, sticky="ew", padx=(4, 4), pady=(0, 4))
        ctk.CTkLabel(self.sync_cell, text="Subtitle Sync", font=ctk.CTkFont(size=11),
            anchor="w").pack(side="left", padx=(0, 8))
        self.sync_var = ctk.StringVar(value="-0.3")
        self.sync_menu = ctk.CTkOptionMenu(self.sync_cell, variable=self.sync_var,
            values=["-0.5", "-0.4", "-0.3", "-0.2", "-0.1", "0", "+0.1", "+0.2"], width=110, height=24, corner_radius=5,
            fg_color=("#ffffff", "#17171b"), button_color=("#e8eaee", "#2a2a30"),
            button_hover_color=("#d9dbe0", "#3a3a40"),
            dropdown_fg_color=("#ffffff", "#17171b"))
        self.sync_menu.pack(side="right")
        ctk.CTkLabel(self.sync_cell, text="negatif = lebih cepat", font=ctk.CTkFont(size=11),
            text_color="gray").pack(side="right", padx=(4, 0))

        # Credit toggle (col 0)
        credit_cell = ctk.CTkFrame(options_frame, fg_color="transparent")
        credit_cell.grid(row=5, column=0, sticky="ew", padx=(4, 4), pady=(0, 4))
        ctk.CTkLabel(credit_cell, text="Add Credit", font=ctk.CTkFont(size=11), 
            anchor="w").pack(side="left", padx=(0, 8))
        self.credit_var = ctk.BooleanVar(value=False)
        self.credit_switch = ctk.CTkSwitch(credit_cell, text="OFF", variable=self.credit_var, 
            width=36, height=18, command=self.update_credit_switch_text)
        self.credit_switch.pack(side="right")
        ctk.CTkLabel(credit_cell, text="channel name di Settings", font=ctk.CTkFont(size=11),
            text_color="gray").pack(side="right", padx=(4, 0))
        
        # Scrollable list of highlights (packed last so bottom buttons always stay visible)
        self.list_frame = ctk.CTkScrollableFrame(self, fg_color="transparent")

        # Bottom action buttons
        bottom_frame = ctk.CTkFrame(self, fg_color="transparent")
        bottom_frame.pack(fill="x", padx=4, pady=(0, 6))

        # Select all / Deselect all
        select_frame = ctk.CTkFrame(bottom_frame, fg_color="transparent")
        select_frame.pack(fill="x", pady=(0, 6))

        ctk.CTkButton(select_frame, text="Select All", height=18,
            fg_color=("#3a3a40", "#2a2a30"), hover_color=("#4a4a50", "#3a3a40"),
            font=ctk.CTkFont(size=11), command=self.select_all, border_width=1, border_color=("#3a3a40", "#2a2a30"), text_color=("#FFFFFF", "#FFFFFF")).pack(side="left", fill="x", expand=True, padx=(0, 4))

        ctk.CTkButton(select_frame, text="Deselect All", height=18,
            fg_color=("#3a3a40", "#2a2a30"), hover_color=("#4a4a50", "#3a3a40"),
            font=ctk.CTkFont(size=11), command=self.deselect_all, text_color=("#FFFFFF", "#FFFFFF")).pack(side="left", fill="x", expand=True, padx=(4, 0))

        # Process button
        self.process_btn = ctk.CTkButton(bottom_frame, text="Process Selected Clips", height=22,
            font=ctk.CTkFont(size=11, weight="bold"), command=self.process_selected,
            fg_color=("#00A878", "#00A878"), hover_color=("#008F66", "#008F66"), text_color=("#0B0B0C", "#0B0B0C"))
        self.process_btn.pack(fill="x")

        # Footer
        footer = PageFooter(self, self)
        footer.pack(fill="x", padx=4, pady=(4, 8))

        self.list_frame.pack(fill="both", expand=True, padx=4, pady=(0, 6))
        
        # Apply caption toggle state on startup (hide subtitle style if captions off)
        self.after(100, self.update_caption_switch_text)
    
    def set_highlights(self, highlights: list, session_dir, url: str = None):
        """Set highlights data and populate list"""
        # Sort highlights by virality_score descending
        self.highlights = sorted(highlights, key=lambda x: x.get("virality_score", 0), reverse=True)
        self.session_dir = session_dir
        self.populate_list()
        # Populate resolution dropdown from server offers
        if url:
            self._load_server_resolutions(url)
    
    def _load_server_resolutions(self, url: str):
        """Query the server for available video resolutions in a background
        thread and update the resolution dropdown once they are known."""
        def query():
            try:
                import yt_dlp as _yt
                from utils.helpers import get_app_dir, get_deno_path
                opts = {
                    'quiet': True,
                    'no_warnings': True,
                    'skip_download': True,
                    'noplaylist': True,
                }
                deno_path = get_deno_path()
                if deno_path and Path(deno_path).exists():
                    opts['js_runtimes'] = {'deno': {'path': deno_path}}
                    opts['remote_components'] = ['ejs:github']
                app_dir = get_app_dir()
                for loc in [Path("cookies.txt"), app_dir / "cookies.txt"]:
                    if loc.exists():
                        opts['cookiefile'] = str(loc)
                        break
                with _yt.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                heights = sorted({
                    f.get('height') for f in info.get('formats', [])
                    if f.get('height') and f.get('vcodec') != 'none'
                })
                if heights:
                    values = ["Auto (Best)"] + [f"{h}p" for h in heights]
                    self.after(0, lambda v=values: self._set_resolution_options(v))
            except Exception:
                pass  # keep default options if query fails

        threading.Thread(target=query, daemon=True).start()

    def _set_resolution_options(self, values: list):
        current = self.resolution_var.get()
        self.resolution_menu.configure(values=values)
        if current not in values:
            self.resolution_var.set("Auto (Best)")

    def populate_list(self):
        """Populate the highlights list"""
        # Clear existing
        for widget in self.list_frame.winfo_children():
            widget.destroy()
        self.checkboxes = []
        self.checkbox_vars = []
        
        if not self.highlights:
            ctk.CTkLabel(self.list_frame, text="No highlights found",
                font=ctk.CTkFont(size=11), text_color="gray").pack(pady=18)
            return
        
        # Create list items
        for i, highlight in enumerate(self.highlights, 1):
            # Card frame
            card = ctk.CTkFrame(self.list_frame, fg_color=("gray85", "gray20"), corner_radius=5, border_width=1, border_color=("#2a2a30", "#2a2a30"))
            card.pack(fill="x", pady=4, padx=4)
            
            # Main content
            content = ctk.CTkFrame(card, fg_color="transparent")
            content.pack(fill="x", padx=4, pady=4)
            
            # Top row: Checkbox + Title + Virality Score
            top_row = ctk.CTkFrame(content, fg_color="transparent")
            top_row.pack(fill="x", pady=(0, 4))
            
            # Checkbox
            var = ctk.BooleanVar(value=True)  # Default selected
            checkbox = ctk.CTkCheckBox(top_row, text="", variable=var, width=24, height=24)
            checkbox.pack(side="left", padx=(0, 6))
            self.checkboxes.append(checkbox)
            self.checkbox_vars.append(var)
            
            # Virality score badge
            virality = highlight.get("virality_score", 0)
            if virality >= 7:
                score_color = "#27ae60"
                score_emoji = "🔥"
            elif virality >= 5:
                score_color = "#f39c12"
                score_emoji = "⚡"
            elif virality > 0:
                score_color = "#e74c3c"
                score_emoji = "💫"
            else:
                score_color = "#95a5a6"
                score_emoji = "❓"
            
            ctk.CTkLabel(top_row, text=f"{score_emoji} {virality}",
                font=ctk.CTkFont(size=11, weight="bold"), text_color=score_color).pack(side="right", padx=(0, 0))
            
            # Title
            title = highlight.get("title", "Untitled")
            ctk.CTkLabel(top_row, text=f"#{i}. {title}", 
                font=ctk.CTkFont(size=11, weight="bold"), anchor="w").pack(side="left", fill="x", expand=True)
            
            # Hook text
            hook_text = highlight.get("hook_text", "")
            if hook_text:
                hook_frame = ctk.CTkFrame(content, fg_color="transparent")
                hook_frame.pack(fill="x", pady=(0, 4))
                ctk.CTkLabel(hook_frame, text=f"🪝 {hook_text}", font=ctk.CTkFont(size=11, weight="bold"),
                    text_color="#FFD166", anchor="w", wraplength=650, justify="left").pack(fill="x")
            
            # Description
            description = highlight.get("description", "")
            if description:
                ctk.CTkLabel(content, text=description, font=ctk.CTkFont(size=11),
                    text_color="gray", anchor="w", wraplength=650, justify="left").pack(fill="x", pady=(0, 4))
            
            # Transcript text (conversation content)
            transcript_text = highlight.get("transcript_text", "")
            if transcript_text:
                transcript_frame = ctk.CTkFrame(content, fg_color=("#222222", "#111114"), corner_radius=5, border_width=1, border_color=("#2a2a30", "#2a2a30"))
                transcript_frame.pack(fill="x", pady=(0, 4))
                
                ctk.CTkLabel(transcript_frame, text="💬 Isi Percakapan:", 
                    font=ctk.CTkFont(size=11, weight="bold"), text_color="#aaaaaa",
                    anchor="w").pack(fill="x", padx=4, pady=(4, 4))
                
                # Truncate long transcripts
                display_text = transcript_text[:300]
                if len(transcript_text) > 300:
                    display_text += "..."
                
                ctk.CTkLabel(transcript_frame, text=display_text, 
                    font=ctk.CTkFont(size=11), text_color="#cccccc",
                    anchor="w", wraplength=630, justify="left").pack(fill="x", padx=4, pady=(0, 6))
            
            # Bottom row: Timestamp + Duration
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
                font=ctk.CTkFont(size=11), text_color="gray", anchor="w").pack(side="left")
    
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
        add_credit = self.credit_var.get()
        portrait_mode = "blur" if self.portrait_mode_var.get().startswith("Blurred Background") else "crop"
        face_tracking_mode = "mediapipe" if self.face_tracking_var.get() == "MediaPipe (Smart)" else "opencv"
        aspect_ratio = self.aspect_ratio_var.get()
        subtitle_style = self.subtitle_style_var.get().lower()
        if subtitle_style == "pop highlight":
            subtitle_style = "pop"
        elif subtitle_style == "pop + bounce":
            subtitle_style = "pop_bounce"
        elif subtitle_style == "bounce":
            subtitle_style = "bounce"
        elif subtitle_style == "bounce + word-by-word":
            subtitle_style = "animated"
        else:
            subtitle_style = "karaoke"
        
        # Confirm with user
        count = len(selected)
        enhancements = []
        if add_captions:
            enhancements.append("Captions")
        if add_hook:
            enhancements.append("Hook Text")
        if add_credit:
            enhancements.append("Credit")
        enhancements.append("Blur Background (no crop)" if portrait_mode == "blur" else "Smart Crop")
        enhancements.append("MediaPipe" if face_tracking_mode == "mediapipe" else "OpenCV")
        enhancements.append(aspect_ratio)
        enhancements.append(self.resolution_var.get())
        if add_captions:
            style_label = {"pop": "Pop Highlight", "pop_bounce": "Pop + Bounce", "karaoke": "Karaoke",
                           "bounce": "Bounce", "animated": "Bounce + Word-by-Word"}.get(subtitle_style, "Pop Highlight")
            enhancements.append(style_label)
        
        enhancement_text = " + ".join(enhancements)
        
        if not messagebox.askyesno("Confirm Processing", 
            f"Process {count} selected clip{'s' if count > 1 else ''}?\n\n"
            f"Enhancements: {enhancement_text}\n\n"
            "Video sections will be downloaded individually for each clip."):
            return
        
        # Call process callback with selected highlights and options
        resolution = self.resolution_var.get()
        sync_offset = float(self.sync_var.get().replace("+", ""))
        self.on_process(selected, add_captions, add_hook, portrait_mode, subtitle_style, face_tracking_mode, aspect_ratio, resolution, sync_offset, add_credit)
    
    def update_caption_switch_text(self):
        """Update caption switch text and show/hide subtitle style based on state"""
        if self.caption_var.get():
            self.caption_switch.configure(text="ON")
            self.subtitle_cell.grid(row=4, column=0, sticky="ew", padx=(4, 4), pady=(0, 4))
            self.sync_cell.grid(row=4, column=1, sticky="ew", padx=(4, 4), pady=(0, 4))
        else:
            self.caption_switch.configure(text="OFF")
            self.subtitle_cell.grid_remove()
            self.sync_cell.grid_remove()
    
    def update_hook_switch_text(self):
        """Update hook switch text based on state"""
        if self.hook_var.get():
            self.hook_switch.configure(text="ON")
        else:
            self.hook_switch.configure(text="OFF")
    
    def update_credit_switch_text(self):
        """Update credit switch text based on state"""
        if self.credit_var.get():
            self.credit_switch.configure(text="ON")
        else:
            self.credit_switch.configure(text="OFF")
    
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
