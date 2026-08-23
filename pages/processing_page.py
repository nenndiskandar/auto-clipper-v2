"""
Processing page for video processing workflow
"""

import customtkinter as ctk
from components.progress_step import ProgressStep
from utils.logger import get_error_log_path


class ProcessingPage(ctk.CTkFrame):
    """Processing page - shows progress during video processing"""
    
    def __init__(self, parent, on_cancel_callback, on_back_callback, on_open_output_callback, on_browse_callback, on_retry_callback=None):
        super().__init__(parent)
        self.on_cancel = on_cancel_callback
        self.on_back = on_back_callback
        self.on_open_output = on_open_output_callback
        self.on_browse = on_browse_callback
        self.on_retry = on_retry_callback
        
        self.create_ui()
    
    def open_github(self):
        """Open GitHub repository"""
        import webbrowser
        webbrowser.open("https://github.com/jipraks/yt-short-clipper")
    
    def open_current_clips(self):
        """Open the current session's clips via the app callback."""
        if self.on_browse:
            self.on_browse()
    
    def open_discord(self):
        """Open Discord server"""
        import webbrowser
        webbrowser.open("https://s.id/ytsdiscord")
    
    def show_page(self, page_name: str):
        """Navigate to another page"""
        pass
    
    def create_ui(self):
        """Create the processing page UI"""
        from components.page_layout import PageHeader, PageFooter
        
        self.configure(fg_color=("#ffffff", "#0b0b0c"), corner_radius=5)
        
        # Header
        header = PageHeader(self, self, show_nav_buttons=False, show_back_button=False, page_title="🎬 Processing")
        header.pack(fill="x", padx=4, pady=(6, 6))
        
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.pack(fill="both", expand=True, padx=4, pady=(0, 8))
        
        # Progress steps - 2 cards horizontal (NEW FLOW)
        steps_frame = ctk.CTkFrame(main, fg_color=("gray90", "gray17"), border_width=1, border_color=("#2a2a30", "#2a2a30"), corner_radius=5)
        steps_frame.pack(fill="x", padx=4, pady=4)
        
        ctk.CTkLabel(steps_frame, text="Progress", font=ctk.CTkFont(size=11, weight="bold")).pack(anchor="w", padx=4, pady=(6, 6))
        
        cards_frame = ctk.CTkFrame(steps_frame, fg_color="transparent")
        cards_frame.pack(fill="x", padx=4, pady=(0, 8))
        cards_frame.grid_columnconfigure((0, 1), weight=1, uniform="step")
        self.cards_frame = cards_frame
        
        self.steps = []
        step_titles = [
            "Downloading Subtitles",
            "Finding Highlights with AI"
        ]
        
        for i, title in enumerate(step_titles):
            step = ProgressStep(cards_frame, i + 1, title)
            step.grid(row=0, column=i, padx=4, pady=4, sticky="nsew")
            self.steps.append(step)
        
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
        
        # Buttons - single row
        btn_frame = ctk.CTkFrame(main, fg_color="transparent")
        btn_frame.pack(fill="x", padx=4, pady=(0, 8))

        self.cancel_btn = ctk.CTkButton(btn_frame, font=ctk.CTkFont(size=11), text="Cancel", height=22, fg_color="#c0392b",
            hover_color="#e74c3c", command=self.on_cancel, text_color=("#FFFFFF", "#FFFFFF"))
        self.cancel_btn.pack(side="left", fill="x", expand=True, padx=(0, 4))

        self.retry_btn = ctk.CTkButton(btn_frame, font=ctk.CTkFont(size=11), text="Retry", height=22, state="disabled",
            fg_color="#00A878", hover_color="#008F66", command=self._do_retry, text_color=("#0B0B0C", "#0B0B0C"))
        self.retry_btn.pack(side="left", fill="x", expand=True, padx=(4, 4))

        self.open_btn = ctk.CTkButton(btn_frame, font=ctk.CTkFont(size=11), text="Open Output", height=22, state="disabled", command=self.on_open_output)
        self.open_btn.pack(side="left", fill="x", expand=True, padx=(4, 4))

        self.view_clips_btn = ctk.CTkButton(btn_frame, font=ctk.CTkFont(size=11), text="View Clip", height=22, state="disabled",
            fg_color="#00A878", hover_color="#008F66", text_color=("#FFFFFF", "#FFFFFF"), command=self.open_current_clips)
        self.view_clips_btn.pack(side="left", fill="x", expand=True, padx=(4, 0))
        
        # Footer
        footer = PageFooter(self, self)
        footer.pack(fill="x", padx=4, pady=(4, 8), side="bottom")
    
    def reset_ui(self):
        """Reset UI for new processing"""
        for step in self.steps:
            step.reset()
        
        self.status_label.configure(text="Initializing...")
        self.spinner.pack(side="left", padx=(0, 6))
        self.spinner.start()
        self.cancel_btn.configure(state="normal")
        self.open_btn.configure(state="disabled")
        self.view_clips_btn.configure(state="disabled")
        self.retry_btn.configure(state="disabled")
    
    def start_loading(self):
        """Start the animated loading spinner"""
        self.spinner.pack(side="left", padx=(0, 6))
        self.spinner.start()
    
    def stop_loading(self):
        """Stop the animated loading spinner"""
        self.spinner.stop()
        self.spinner.pack_forget()
    
    def _do_retry(self):
        """Trigger the retry callback if one is set"""
        if self.on_retry:
            self.on_retry()
    
    def set_retryable_error(self, error: str):
        """Show an error that can be retried (e.g. AI returned invalid JSON)"""
        self.stop_loading()
        self.status_label.configure(
            text=f"❌ {error}\n\n🔄 Click 'Retry' to try again")
        self.cancel_btn.configure(state="disabled")
        self.retry_btn.configure(state="normal")
        for step in self.steps:
            if step.status == "active":
                step.set_error("Failed")
    
    def switch_to_transcription_mode(self):
        """Rebuild step cards for 3-step AI transcription flow.
        
        Replaces the 2-step layout with:
        1. Download Video
        2. AI Transcription (Whisper)
        3. Finding Highlights with AI
        """
        # Destroy existing step widgets
        for step in self.steps:
            step.destroy()
        self.steps.clear()
        
        # Reconfigure grid for 3 columns
        self.cards_frame.grid_columnconfigure((0, 1, 2), weight=1, uniform="step")
        
        step_titles = [
            "Downloading Video",
            "AI Transcription",
            "Finding Highlights"
        ]
        
        for i, title in enumerate(step_titles):
            step = ProgressStep(self.cards_frame, i + 1, title)
            step.grid(row=0, column=i, padx=4, pady=4, sticky="nsew")
            self.steps.append(step)
        
        self.status_label.configure(text="Downloading video...")
    
    def update_status(self, msg: str):
        """Update status label"""
        self.status_label.configure(text=msg)
    
    def update_tokens(self, gpt_total: int, whisper_minutes: float, tts_chars: int):
        """Update token usage display (deprecated - kept for compatibility)"""
        pass  # No-op since we removed the UI
    
    def on_complete(self):
        """Called when processing completes successfully"""
        self.stop_loading()
        self.status_label.configure(text="✅ All clips created successfully!")
        self.cancel_btn.configure(state="disabled")
        self.open_btn.configure(state="normal")
        self.view_clips_btn.configure(state="normal")
        for step in self.steps:
            step.set_done("Complete")
    
    def on_cancelled(self):
        """Called when processing is cancelled"""
        self.stop_loading()
        self.status_label.configure(text="⚠️ Cancelled by user")
        self.cancel_btn.configure(state="disabled")
        for step in self.steps:
            if step.status == "active":
                step.set_error("Cancelled")
    
    def on_error(self, error: str):
        """Called when processing encounters an error"""
        self.stop_loading()
        error_log = get_error_log_path()
        
        if error_log:
            error_msg = f"❌ {error}\n\n📄 Error details saved to:\n{error_log}"
        else:
            error_msg = f"❌ {error}"
        
        self.status_label.configure(text=error_msg)
        self.cancel_btn.configure(state="disabled")
        self.retry_btn.configure(state="disabled")
        for step in self.steps:
            if step.status == "active":
                step.set_error("Failed")
