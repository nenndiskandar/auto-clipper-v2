"""
Caption Maker Settings Page
"""

import customtkinter as ctk
from tkinter import messagebox

from pages.settings.ai_providers.base_provider import BaseProviderSettingsPage

WHISPER_MODEL_SIZES = ["tiny", "base", "small", "medium", "large"]

class CaptionMakerSettingsPage(BaseProviderSettingsPage):
    """Settings page for Caption Maker AI provider"""
    
    # Use manual input instead of dropdown
    DEFAULT_MODEL = "whisper-1"
    
    def __init__(self, parent, config, on_save_callback, on_back_callback):
        super().__init__(
            parent=parent,
            title="Caption Maker",
            provider_key="caption_maker",
            config=config,
            on_save_callback=on_save_callback,
            on_back_callback=on_back_callback
        )
    
    def create_provider_content(self):
        """Create provider settings content with additional info"""
        # Info box
        info_frame = ctk.CTkFrame(self.content, fg_color=("gray85", "gray20"), corner_radius=5, border_width=1, border_color=("#2a2a30", "#2a2a30"))
        info_frame.pack(fill="x", pady=(0, 6))
        
        ctk.CTkLabel(info_frame, text="📝 About Caption Maker", 
            font=ctk.CTkFont(size=11, weight="bold")).pack(anchor="w", padx=4, pady=(4, 4))
        ctk.CTkLabel(info_frame, 
            text="Uses Whisper API to transcribe audio and generate\nword-by-word captions with precise timing.", 
            font=ctk.CTkFont(size=11), text_color="gray", justify="left").pack(anchor="w", padx=4, pady=(0, 6))
        
        # Initialize base class attributes
        self.url_entry = None
        self.key_entry = None
        self.model_entry = None
        self.provider_type_var = ctk.StringVar(value="custom")
        self.model_dropdown = None
        self.model_var = None
        self.load_btn = None
        self.system_message_textbox = None
        
        # Faster-Whisper Section
        self._create_faster_whisper_section()
        
        # Save Button at the bottom
        self.create_save_button(self.save_settings)
    
    def _create_faster_whisper_section(self):
        """Create section for local faster-whisper model configuration"""
        # Separator label
        sep = ctk.CTkFrame(self.content, fg_color=("gray75", "gray15"), corner_radius=5, border_width=1, border_color=("#2a2a30", "#2a2a30"))
        sep.pack(fill="x", pady=(4, 4))
        
        ctk.CTkLabel(sep, text="⚡ Faster-Whisper (Local / Offline)", 
            font=ctk.CTkFont(size=11, weight="bold")).pack(anchor="w", padx=4, pady=(4, 4))

        # Model size dropdown
        self.model_size_label = ctk.CTkLabel(sep, text="Model Size:", font=ctk.CTkFont(size=11))
        self.model_size_label.pack(anchor="w", padx=4, pady=(4, 0))
        
        self.model_size_var = ctk.StringVar(value="small")
        self.model_size_dropdown = ctk.CTkOptionMenu(sep,
            values=WHISPER_MODEL_SIZES,
            variable=self.model_size_var, height=26,
            command=self._on_model_size_changed)
        self.model_size_dropdown.pack(fill="x", padx=4, pady=(0, 4))
        
        # Status label (will be updated)
        self.fw_status_label = ctk.CTkLabel(sep, text="", font=ctk.CTkFont(size=11))
        self.fw_status_label.pack(anchor="w", padx=4, pady=(0, 4))
        
        # Download button
        self.fw_download_btn = ctk.CTkButton(sep, font=ctk.CTkFont(size=11), text="Download Model", height=22,
            fg_color=("#00A878", "#00A878"), hover_color=("#008F66", "#008F66"),
            command=self._download_faster_whisper_model, text_color=("#0B0B0C", "#0B0B0C"))
        self.fw_download_btn.pack(fill="x", padx=4, pady=(0, 6))
        
        # Initial status update
        self._update_fw_status()
    
    def _on_model_size_changed(self, value):
        """Handle model size change"""
        self._update_fw_status()
    
    def _update_fw_status(self):
        """Update the status label for faster-whisper model"""
        from utils.dependency_manager import get_faster_whisper_model_dir
        from utils.helpers import get_app_dir
        
        size = self.model_size_var.get()
        app_dir = get_app_dir()
        model_dir = get_faster_whisper_model_dir(app_dir, size)
        model_bin = model_dir / "model.bin"
        config_json = model_dir / "config.json"
        
        if model_bin.exists() and config_json.exists():
            self.fw_status_label.configure(
                text=f"✅ Model '{size}' is downloaded and ready",
                text_color="green")
            self.fw_download_btn.configure(state="normal", text="Download Model")
        else:
            self.fw_status_label.configure(
                text=f"⚠ Model '{size}' not downloaded yet",
                text_color="orange")
            self.fw_download_btn.configure(state="normal", text="Download Model")
    
    def _download_faster_whisper_model(self):
        """Download the selected faster-whisper model"""
        from utils.dependency_manager import setup_faster_whisper_model, get_faster_whisper_model_dir
        from utils.helpers import get_app_dir
        import threading
        
        size = self.model_size_var.get()
        app_dir = get_app_dir()
        model_dir = get_faster_whisper_model_dir(app_dir, size)
        
        if model_dir.exists() and (model_dir / "model.bin").exists():
            self.fw_status_label.configure(
                text=f"✅ Model '{size}' already downloaded",
                text_color="green")
            return
        
        self.fw_download_btn.configure(state="disabled", text="Downloading...")
        self.fw_status_label.configure(text="Downloading model...", text_color="blue")
        
        def do_download():
            success = setup_faster_whisper_model(app_dir, size)
            self.after(0, lambda: self._on_download_complete(success, size))
        
        threading.Thread(target=do_download, daemon=True).start()
    
    def _on_download_complete(self, success, size):
        """Handle download completion"""
        if success:
            self.fw_status_label.configure(
                text=f"✅ Model '{size}' downloaded successfully!",
                text_color="green")
            self.fw_download_btn.configure(text="⬇ Download Model", state="normal")
        else:
            self.fw_status_label.configure(
                text=f"❌ Failed to download model '{size}'",
                text_color="red")
            self.fw_download_btn.configure(text="⬇ Download Model", state="normal")
    
    def get_base_url(self):
        """Get base URL for caption maker"""
        return "https://api.openai.com/v1"
    
    def load_config(self):
        """Load config into UI, including faster-whisper settings"""
        # Handle both ConfigManager and dict
        if hasattr(self.config, 'config'):
            config_dict = self.config.config
        else:
            config_dict = self.config
        
        ai_providers = config_dict.get("ai_providers", {})
        provider = ai_providers.get(self.provider_key, {})
        
        # Load faster-whisper settings
        fw_settings = provider.get("faster_whisper", {})
        self.model_size_var.set(fw_settings.get("model_size", "small"))
        
        # Update status after loading config
        self.after(100, self._update_fw_status)
    
    def save_settings(self):
        """Save settings including faster-whisper config"""
        model = self.DEFAULT_MODEL
        url = self.get_base_url()
        
        # Handle both ConfigManager and dict
        if hasattr(self.config, 'config'):
            config_dict = self.config.config
        else:
            config_dict = self.config
        
        # Update config
        if "ai_providers" not in config_dict:
            config_dict["ai_providers"] = {}
        
        provider_config = {
            "base_url": url,
            "api_key": "",
            "model": model,
            "faster_whisper": {
                "model_size": self.model_size_var.get()
            }
        }
        
        # Save system message if textbox exists
        if self.system_message_textbox:
            system_message = self.system_message_textbox.get("1.0", "end").strip()
            if system_message:
                provider_config["system_message"] = system_message
        
        config_dict["ai_providers"][self.provider_key] = provider_config
        
        # Call save callback with the full config dict (not just ai_providers)
        if self.on_save_callback:
            self.on_save_callback(config_dict)
        
        messagebox.showinfo("Success", f"{self.title} settings saved!")
        self.on_back()
