"""
Performance Settings Sub-Page with GPU Detection
"""

import threading
import customtkinter as ctk
from tkinter import messagebox

from pages.settings.base_dialog import BaseSettingsSubPage


class PerformanceSettingsSubPage(BaseSettingsSubPage):
    """Sub-page for configuring performance settings with GPU detection"""
    
    def __init__(self, parent, config, on_save_callback, on_back_callback):
        self.config = config
        self.on_save_callback = on_save_callback
        
        super().__init__(parent, "Performance Settings", on_back_callback)
        
        self.create_content()
        self.load_config()
        
        # Auto-detect GPU on load
        self.after(500, self.detect_gpu)
    
    def create_content(self):
        """Create page content"""
        # GPU Detection Section
        detection_section = self.create_section("GPU Detection")
        
        detection_frame = ctk.CTkFrame(detection_section, fg_color="transparent")
        detection_frame.pack(fill="x", padx=4, pady=(0, 8))
        
        # GPU info display
        self.gpu_info_frame = ctk.CTkFrame(detection_frame, fg_color=("gray90", "gray15"), corner_radius=5, border_width=1, border_color=("#2a2a30", "#2a2a30"))
        self.gpu_info_frame.pack(fill="x", pady=(0, 6))
        
        self.gpu_status_label = ctk.CTkLabel(self.gpu_info_frame, text="Detecting GPU...", 
            font=ctk.CTkFont(size=11), anchor="w", justify="left")
        self.gpu_status_label.pack(fill="x", padx=4, pady=4)
        
        # Detect button
        self.detect_gpu_btn = ctk.CTkButton(detection_frame, font=ctk.CTkFont(size=11), text="Detect GPU", height=22,
            fg_color=("#00A878", "#00A878"), command=self.detect_gpu, text_color=("#0B0B0C", "#0B0B0C"))
        self.detect_gpu_btn.pack(fill="x")
        
        # GPU Acceleration Section
        accel_section = self.create_section("GPU Acceleration")
        
        accel_frame = ctk.CTkFrame(accel_section, fg_color="transparent")
        accel_frame.pack(fill="x", padx=4, pady=(0, 8))
        
        self.gpu_enabled_var = ctk.BooleanVar(value=False)
        self.gpu_switch = ctk.CTkSwitch(accel_frame, text="Enable GPU Acceleration", 
            variable=self.gpu_enabled_var, font=ctk.CTkFont(size=11),
            command=self.toggle_gpu_acceleration, state="disabled")
        self.gpu_switch.pack(anchor="w", pady=(0, 6))
        
        ctk.CTkLabel(accel_frame, 
            text="GPU encoding is 3-5x faster than CPU. Requires compatible hardware.",
            font=ctk.CTkFont(size=11), text_color="gray", anchor="w", justify="left").pack(fill="x")
        
        # Technical Details Section
        details_section = self.create_section("Technical Details")
        
        details_frame = ctk.CTkFrame(details_section, fg_color="transparent")
        details_frame.pack(fill="x", padx=4, pady=(0, 8))
        
        self.encoder_info_label = ctk.CTkLabel(details_frame, 
            text="Encoder: Not detected\nPreset: N/A\nStatus: Click 'Detect GPU' to check",
            font=ctk.CTkFont(size=11), text_color="gray", anchor="w", justify="left")
        self.encoder_info_label.pack(fill="x")
        
        # Face Tracking Section
        face_section = self.create_section("Face Tracking (MediaPipe)")
        
        face_frame = ctk.CTkFrame(face_section, fg_color="transparent")
        face_frame.pack(fill="x", padx=4, pady=(0, 8))
        
        self.smooth_follow_var = ctk.BooleanVar(value=True)
        ctk.CTkSwitch(face_frame, text="Smooth Follow (Camera pans with face movement)", 
            variable=self.smooth_follow_var, font=ctk.CTkFont(size=11)).pack(anchor="w", pady=(0, 6))
        
        self.pan_speed_var = ctk.DoubleVar(value=2.5)
        speed_row = ctk.CTkFrame(face_frame, fg_color="transparent")
        speed_row.pack(fill="x", pady=(0, 4))
        ctk.CTkLabel(speed_row, text="Pan Speed Limit (px/frame):", 
            font=ctk.CTkFont(size=11)).pack(side="left")
        self.pan_speed_entry = ctk.CTkEntry(speed_row, textvariable=self.pan_speed_var, width=70, height=26)
        self.pan_speed_entry.pack(side="right")
        
        ctk.CTkLabel(face_frame, 
            text="Smooth Follow ON: crop window glides continuously behind the speaker's face.\n"
                 "Smooth Follow OFF: crop locks per shot (fixed frame with quick cuts).",
            font=ctk.CTkFont(size=11), text_color="gray", anchor="w", justify="left").pack(fill="x", pady=(4, 0))
        
        # Save button
        self.create_save_button(self.save_settings)
    
    def detect_gpu(self):
        """Detect GPU and update UI"""
        self.detect_gpu_btn.configure(state="disabled", text="Detecting...")
        
        def do_detect():
            try:
                from utils.gpu_detector import GPUDetector
                detector = GPUDetector()
                
                gpu_info = detector.detect_gpu()
                recommendation = detector.get_recommended_encoder()
                
                self.after(0, lambda g=gpu_info, r=recommendation: self._on_gpu_detected(g, r))
            except Exception as e:
                error_msg = str(e)
                self.after(0, lambda err=error_msg: self._on_gpu_detect_error(err))
        
        threading.Thread(target=do_detect, daemon=True).start()
    
    def _on_gpu_detected(self, gpu_info, recommendation):
        """Handle GPU detection result"""
        self.detect_gpu_btn.configure(state="normal", text="🔄 Detect GPU")
        
        if gpu_info['available']:
            gpu_type_emoji = {'nvidia': '🟢', 'amd': '🔴', 'intel': '🔵'}
            emoji = gpu_type_emoji.get(gpu_info['type'], '⚪')
            
            status_text = f"{emoji} GPU Detected\n"
            status_text += f"Name: {gpu_info['name']}\n"
            status_text += f"Type: {gpu_info['type'].upper()}"
            
            self.gpu_status_label.configure(text=status_text, text_color=("green", "lightgreen"))
            
            if recommendation['available']:
                encoder_text = f"Encoder: {recommendation['encoder']}\n"
                encoder_text += f"Preset: {recommendation['preset']}\n"
                encoder_text += f"Status: ✓ Ready to use"
                self.encoder_info_label.configure(text=encoder_text, text_color=("green", "lightgreen"))
                self.gpu_switch.configure(state="normal")
            else:
                encoder_text = f"Encoder: Not available\n"
                encoder_text += f"Reason: {recommendation.get('reason', 'Unknown')}"
                self.encoder_info_label.configure(text=encoder_text, text_color=("orange", "yellow"))
                self.gpu_switch.configure(state="disabled")
                self.gpu_enabled_var.set(False)
        else:
            status_text = "⚪ No GPU Detected\n"
            status_text += "Video processing will use CPU."
            
            self.gpu_status_label.configure(text=status_text, text_color="gray")
            
            encoder_text = "Encoder: libx264 (CPU)\n"
            encoder_text += "Preset: fast\n"
            encoder_text += "Status: Using CPU encoding"
            self.encoder_info_label.configure(text=encoder_text, text_color="gray")
            
            self.gpu_switch.configure(state="disabled")
            self.gpu_enabled_var.set(False)
    
    def _on_gpu_detect_error(self, error):
        """Handle GPU detection error"""
        self.detect_gpu_btn.configure(state="normal", text="🔄 Detect GPU")
        
        status_text = f"❌ Detection Error\nError: {error}"
        self.gpu_status_label.configure(text=status_text, text_color=("red", "orange"))
        
        self.gpu_switch.configure(state="disabled")
        self.gpu_enabled_var.set(False)
    
    def toggle_gpu_acceleration(self):
        """Handle GPU acceleration toggle"""
        if self.gpu_enabled_var.get():
            messagebox.showinfo("GPU Enabled", 
                "GPU acceleration enabled.\nDon't forget to save settings.")
        else:
            messagebox.showinfo("GPU Disabled", 
                "GPU acceleration disabled.\nDon't forget to save settings.")
    
    def load_config(self):
        """Load config into UI"""
        # Handle both ConfigManager and dict
        if hasattr(self.config, 'config'):
            config_dict = self.config.config
        else:
            config_dict = self.config
            
        gpu_config = config_dict.get("gpu_acceleration", {})
        self.gpu_enabled_var.set(gpu_config.get("enabled", False))
        
        mp_settings = config_dict.get("mediapipe_settings", {})
        self.smooth_follow_var.set(mp_settings.get("smooth_follow", True))
        self.pan_speed_var.set(mp_settings.get("pan_speed_limit", 2.5))
    
    def save_settings(self):
        """Save settings"""
        # Handle both ConfigManager and dict
        if hasattr(self.config, 'config'):
            config_dict = self.config.config
        else:
            config_dict = self.config
        
        config_dict["gpu_acceleration"] = {
            "enabled": self.gpu_enabled_var.get()
        }
        
        mp_settings = config_dict.get("mediapipe_settings", {})
        mp_settings["smooth_follow"] = self.smooth_follow_var.get()
        try:
            mp_settings["pan_speed_limit"] = max(0.5, min(10.0, float(self.pan_speed_var.get())))
        except Exception:
            pass
        config_dict["mediapipe_settings"] = mp_settings
        
        if self.on_save_callback:
            self.on_save_callback(config_dict)
        
        messagebox.showinfo("Success", "Performance settings saved!")
        self.on_back()
