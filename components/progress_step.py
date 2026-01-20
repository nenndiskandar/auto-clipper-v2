"""
Progress step component for showing processing status
"""

import customtkinter as ctk


class ProgressStep(ctk.CTkFrame):
    """A single step in the progress indicator"""
    
    def __init__(self, parent, step_num: int, title: str):
        super().__init__(parent, fg_color="transparent")
        self.step_num = step_num
        self.status = "pending"  # pending, active, done, error
        
        # Step indicator circle
        self.indicator = ctk.CTkLabel(
            self, 
            text=str(step_num), 
            width=35, 
            height=35,
            fg_color=("gray70", "gray30"), 
            corner_radius=17, 
            font=ctk.CTkFont(size=14, weight="bold")
        )
        self.indicator.pack(side="left", padx=(0, 10))
        
        # Step title and status
        text_frame = ctk.CTkFrame(self, fg_color="transparent")
        text_frame.pack(side="left", fill="x", expand=True)
        
        self.title_label = ctk.CTkLabel(
            text_frame, 
            text=title, 
            font=ctk.CTkFont(size=13), 
            anchor="w"
        )
        self.title_label.pack(fill="x")
        
        self.status_label = ctk.CTkLabel(
            text_frame, 
            text="Waiting...", 
            font=ctk.CTkFont(size=11), 
            text_color="gray", 
            anchor="w"
        )
        self.status_label.pack(fill="x")
        
        # Progress bar (hidden by default)
        self.progress_bar = ctk.CTkProgressBar(text_frame, height=8)
        self.progress_bar.set(0)
        self.progress_bar.pack_forget()  # Hidden initially

    def set_active(self, status_text: str = "Processing...", progress: float = None):
        """Set step to active state with optional progress"""
        self.status = "active"
        self.indicator.configure(fg_color=("#3498db", "#2980b9"), text="●")
        self.status_label.configure(text=status_text, text_color=("#3498db", "#5dade2"))
        
        # Always show progress bar when active, default to 0 if no progress provided
        if progress is None:
            progress = 0.0
        
        self.progress_bar.pack(fill="x", pady=(3, 0))
        self.progress_bar.set(progress)
    
    def set_done(self, status_text: str = "Complete"):
        """Set step to done state"""
        self.status = "done"
        self.indicator.configure(fg_color=("#27ae60", "#1e8449"), text="✓")
        self.status_label.configure(text=status_text, text_color=("#27ae60", "#2ecc71"))
        self.progress_bar.pack_forget()  # Hide progress bar when done
    
    def set_error(self, status_text: str = "Failed"):
        """Set step to error state"""
        self.status = "error"
        self.indicator.configure(fg_color=("#e74c3c", "#c0392b"), text="✗")
        self.status_label.configure(text=status_text, text_color=("#e74c3c", "#ec7063"))
        self.progress_bar.pack_forget()  # Hide progress bar on error
    
    def reset(self):
        """Reset step to initial pending state"""
        self.status = "pending"
        self.indicator.configure(fg_color=("gray70", "gray30"), text=str(self.step_num))
        self.status_label.configure(text="Waiting...", text_color="gray")
        self.progress_bar.pack_forget()
        self.progress_bar.set(0)
