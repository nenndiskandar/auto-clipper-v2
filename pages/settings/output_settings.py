"""
Output Settings Sub-Page
"""

import customtkinter as ctk
from tkinter import filedialog, messagebox
from pathlib import Path

from pages.settings.base_dialog import BaseSettingsSubPage


class OutputSettingsSubPage(BaseSettingsSubPage):
    """Sub-page for configuring output settings"""
    
    def __init__(self, parent, config, output_dir, on_save_callback, on_back_callback):
        self.config = config
        self.output_dir = output_dir
        self.on_save_callback = on_save_callback
        
        super().__init__(parent, "Output Settings", on_back_callback)
        
        self.create_content()
        self.load_config()
    
    def create_content(self):
        """Create page content"""
        # Output Folder Section
        folder_section = self.create_section("Output Folder")
        
        folder_frame = ctk.CTkFrame(folder_section, fg_color="transparent")
        folder_frame.pack(fill="x", padx=4, pady=(0, 8))
        
        ctk.CTkLabel(folder_frame, text="Folder where video clips will be saved", 
            font=ctk.CTkFont(size=11), text_color="gray").pack(anchor="w", pady=(0, 4))
        
        path_row = ctk.CTkFrame(folder_frame, fg_color="transparent")
        path_row.pack(fill="x")
        
        self.output_var = ctk.StringVar(value=str(self.output_dir))
        self.output_entry = ctk.CTkEntry(path_row, textvariable=self.output_var, height=26)
        self.output_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        
        ctk.CTkButton(path_row, font=ctk.CTkFont(size=11), text="Browse", width=100, height=22,
            command=self.browse_output_folder).pack(side="right")
        
        # Open folder button
        ctk.CTkButton(folder_frame, font=ctk.CTkFont(size=11), text="Open Output Folder", height=22, fg_color="gray",
            command=self.open_output_folder, text_color=("#FFFFFF", "#FFFFFF")).pack(fill="x", pady=(4, 0))
        
        # Save button
        self.create_save_button(self.save_settings)
    
    def browse_output_folder(self):
        """Browse for output folder"""
        dir_path = filedialog.askdirectory(initialdir=self.output_var.get())
        if dir_path:
            self.output_var.set(dir_path)
    
    def open_output_folder(self):
        """Open output folder in file explorer"""
        import subprocess
        import sys
        
        folder = self.output_var.get()
        if not folder or not Path(folder).exists():
            messagebox.showwarning("Warning", "Output folder does not exist")
            return
        
        try:
            if sys.platform == "win32":
                subprocess.run(["explorer", folder])
            elif sys.platform == "darwin":
                subprocess.run(["open", folder])
            else:
                subprocess.run(["xdg-open", folder])
        except Exception as e:
            messagebox.showerror("Error", f"Failed to open folder: {str(e)}")
    
    def load_config(self):
        """Load config into UI"""
        # Handle both ConfigManager and dict
        if hasattr(self.config, 'config'):
            config_dict = self.config.config
        else:
            config_dict = self.config
        
        # Output directory
        output_dir = config_dict.get("output_dir", str(self.output_dir))
        self.output_var.set(output_dir)
    
    def save_settings(self):
        """Save settings"""
        output_dir = self.output_var.get().strip()
        
        if not output_dir:
            messagebox.showerror("Error", "Output directory is required")
            return
        
        # Create directory if it doesn't exist
        try:
            Path(output_dir).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            messagebox.showerror("Error", f"Cannot create directory:\n{str(e)}")
            return
        
        # Handle both ConfigManager and dict
        if hasattr(self.config, 'config'):
            config_dict = self.config.config
        else:
            config_dict = self.config
        
        # Update config
        config_dict["output_dir"] = output_dir
        
        if self.on_save_callback:
            self.on_save_callback(config_dict)
        
        messagebox.showinfo("Success", "Output settings saved!")
        self.on_back()
