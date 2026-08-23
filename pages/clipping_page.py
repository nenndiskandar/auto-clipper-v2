"""
Clipping page for video clipping workflow
"""

import customtkinter as ctk
from utils.logger import get_error_log_path


class ClippingPage(ctk.CTkFrame):
    """Clipping page - shows progress during video clipping"""
    
    def __init__(self, parent, on_cancel_callback, on_back_callback, on_open_output_callback, on_browse_callback, on_view_clips_callback=None):
        super().__init__(parent)
        self.on_cancel = on_cancel_callback
        self.on_back = on_back_callback
        self.on_open_output = on_open_output_callback
        self.on_browse = on_browse_callback
        self.on_view_clips = on_view_clips_callback or on_open_output_callback
        
        self.create_ui()
    
    def open_github(self):
        """Open GitHub repository"""
        import webbrowser
        webbrowser.open("https://github.com/jipraks/yt-short-clipper")
    
    def open_discord(self):
        """Open Discord server"""
        import webbrowser
        webbrowser.open("https://s.id/ytsdiscord")
    
    def show_page(self, page_name: str):
        """Navigate to another page"""
        pass
    
    def create_ui(self):
        """Create the clipping page UI"""
        from components.page_layout import PageHeader, PageFooter
        
        self.configure(fg_color=("#ffffff", "#0b0b0c"), corner_radius=5, border_width=1, border_color=("#2a2a30", "#2a2a30"))
        
        # Header
        header = PageHeader(self, self, show_nav_buttons=False, show_back_button=False, page_title="✂️ Clipping Videos")
        header.pack(fill="x", padx=4, pady=(6, 6))        
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.pack(fill="both", expand=True, padx=4, pady=(0, 8))
        
        # Progress section
        progress_frame = ctk.CTkFrame(main, fg_color=("gray90", "gray17"), border_width=1, border_color=("#2a2a30", "#2a2a30"), corner_radius=5)
        progress_frame.pack(fill="x", padx=4, pady=4)
        
        ctk.CTkLabel(progress_frame, text="Clipping Progress", font=ctk.CTkFont(size=11, weight="bold")).pack(anchor="w", padx=4, pady=(6, 6))
        
        # Progress info
        info_frame = ctk.CTkFrame(progress_frame, fg_color="transparent")
        info_frame.pack(fill="x", padx=4, pady=(0, 8))
        
        # Current clip info
        self.current_clip_label = ctk.CTkLabel(info_frame, text="Preparing...", 
            font=ctk.CTkFont(size=11, weight="bold"))
        self.current_clip_label.pack(anchor="w", pady=(0, 4))
        
        # Progress bar
        self.progress_bar = ctk.CTkProgressBar(info_frame, height=20)
        self.progress_bar.pack(fill="x", pady=(0, 4))
        self.progress_bar.set(0)
        
        # Progress text (X of Y clips)
        self.progress_text = ctk.CTkLabel(info_frame, text="0 / 0 clips processed", 
            font=ctk.CTkFont(size=11), text_color="gray")
        self.progress_text.pack(anchor="w")
        
        # Current status
        self.status_frame = ctk.CTkFrame(main, fg_color=("gray85", "gray20"), border_width=1, border_color=("#2a2a30", "#2a2a30"), corner_radius=5)
        self.status_frame.pack(fill="x", padx=4, pady=(0, 8))
        
        from components.loading_spinner import LoadingSpinner
        status_row = ctk.CTkFrame(self.status_frame, fg_color="transparent")
        status_row.pack(fill="x", padx=4, pady=4)
        
        self.spinner = LoadingSpinner(status_row, size=16)
        self.spinner.pack(side="left", padx=(0, 6))
        self.spinner.pack_forget()
        
        self.status_label = ctk.CTkLabel(status_row, text="Initializing...", 
            font=ctk.CTkFont(size=11), wraplength=440, anchor="w", justify="left")
        self.status_label.pack(side="left", fill="x", expand=True)
        
        # Buttons
        btn_frame = ctk.CTkFrame(main, fg_color="transparent")
        btn_frame.pack(fill="x", padx=4, pady=(0, 8))
        
        row1 = ctk.CTkFrame(btn_frame, fg_color="transparent")
        row1.pack(fill="x", pady=(0, 4))
        
        self.cancel_btn = ctk.CTkButton(row1, font=ctk.CTkFont(size=11), text="Cancel", height=22, fg_color="#c0392b", 
            hover_color="#e74c3c", command=self.on_cancel, text_color=("#FFFFFF", "#FFFFFF"))
        self.cancel_btn.pack(side="left", fill="x", expand=True, padx=(0, 4))
        
        row2 = ctk.CTkFrame(btn_frame, fg_color="transparent")
        row2.pack(fill="x")
        
        self.open_btn = ctk.CTkButton(row2, font=ctk.CTkFont(size=11), text="Open Output", height=22, state="disabled", command=self.on_open_output)
        self.open_btn.pack(side="left", fill="x", expand=True, padx=(0, 4))
        
        self.view_clips_btn = ctk.CTkButton(row2, font=ctk.CTkFont(size=11), text="View Clips", height=22, state="disabled", 
            fg_color="#00A878", hover_color="#008F66", text_color="#0B0B0C", command=self.on_view_clips)
        self.view_clips_btn.pack(side="left", fill="x", expand=True, padx=(4, 4))
        
        self.results_btn = ctk.CTkButton(row2, font=ctk.CTkFont(size=11), text="Browse Sessions", height=22, state="disabled", 
            fg_color="#27ae60", hover_color="#2ecc71", command=self.on_browse, text_color=("#FFFFFF", "#FFFFFF"))
        self.results_btn.pack(side="left", fill="x", expand=True, padx=(4, 0))
        
        # Footer
        footer = PageFooter(self, self)
        footer.pack(fill="x", padx=4, pady=(4, 8), side="bottom")
    
    def reset_ui(self):
        """Reset UI for new clipping"""
        self.progress_bar.set(0)
        self.current_clip_label.configure(text="Preparing...")
        self.progress_text.configure(text="0 / 0 clips processed")
        self.status_label.configure(text="Initializing...")
        self.spinner.pack(side="left", padx=(0, 6))
        self.spinner.start()
        self.cancel_btn.configure(state="normal")
        self.open_btn.configure(state="disabled")
        self.view_clips_btn.configure(state="disabled")
        self.results_btn.configure(state="disabled")
    
    def start_loading(self):
        """Start the animated loading spinner"""
        self.spinner.pack(side="left", padx=(0, 6))
        self.spinner.start()
    
    def stop_loading(self):
        """Stop the animated loading spinner"""
        self.spinner.stop()
        self.spinner.pack_forget()
    
    def update_progress(self, current: int, total: int, clip_title: str = ""):
        """Update progress bar and text"""
        if total > 0:
            progress = current / total
            self.progress_bar.set(progress)
        
        self.progress_text.configure(text=f"{current} / {total} clips processed")
        
        if clip_title:
            self.current_clip_label.configure(text=f"Processing: {clip_title[:50]}")
    
    def update_status(self, msg: str):
        """Update status label"""
        self.status_label.configure(text=msg)
    
    def on_complete(self):
        """Called when clipping completes successfully"""
        self.stop_loading()
        self.status_label.configure(text="✅ All clips created successfully!")
        self.cancel_btn.configure(state="disabled")
        self.open_btn.configure(state="normal")
        self.view_clips_btn.configure(state="normal")
        self.results_btn.configure(state="normal")
        self.current_clip_label.configure(text="✓ Complete")
    
    def on_cancelled(self):
        """Called when clipping is cancelled"""
        self.stop_loading()
        self.status_label.configure(text="⚠️ Cancelled by user")
        self.cancel_btn.configure(state="disabled")
        self.current_clip_label.configure(text="⚠ Cancelled")
    
    def on_error(self, error: str):
        """Called when clipping encounters an error"""
        self.stop_loading()
        error_log = get_error_log_path()
        
        if error_log:
            error_msg = f"❌ {error}\n\n📄 Error details saved to:\n{error_log}"
        else:
            error_msg = f"❌ {error}"
        
        self.status_label.configure(text=error_msg)
        self.cancel_btn.configure(state="disabled")
        self.current_clip_label.configure(text="✗ Failed")
