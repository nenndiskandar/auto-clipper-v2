"""
Settings page for YT Short Clipper
"""

import os
import sys
import subprocess
import threading
import customtkinter as ctk
from pathlib import Path
from tkinter import filedialog, messagebox
from openai import OpenAI

from dialogs.model_selector import SearchableModelDropdown
from version import __version__


class SettingsPage(ctk.CTkFrame):
    """Settings page - embedded in main window"""
    
    def __init__(self, parent, config, on_save_callback, on_back_callback, output_dir, check_update_callback=None):
        super().__init__(parent)
        self.config = config
        self.on_save = on_save_callback
        self.on_back = on_back_callback
        self.output_dir = output_dir
        self.check_update = check_update_callback
        self.models_list = []
        self.youtube_uploader = None
        
        self.create_ui()
        self.load_config()
    
    def create_ui(self):
        """Create the settings UI"""
        # Header with back button
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(15, 10))
        
        ctk.CTkButton(header, text="←", width=40, fg_color="transparent", 
            hover_color=("gray75", "gray25"), command=self.on_back).pack(side="left")
        ctk.CTkLabel(header, text="Settings", font=ctk.CTkFont(size=22, weight="bold")).pack(side="left", padx=10)
        
        # Main content with tabs
        main = ctk.CTkFrame(self)
        main.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        
        # Create tabview with custom styling
        self.tabview = ctk.CTkTabview(main, height=40, segmented_button_fg_color=("gray80", "gray20"),
            segmented_button_selected_color=("#3B8ED0", "#1F6AA5"),
            segmented_button_selected_hover_color=("#36719F", "#144870"),
            segmented_button_unselected_color=("gray85", "gray25"),
            segmented_button_unselected_hover_color=("gray75", "gray30"))
        self.tabview.pack(fill="both", expand=True, padx=10, pady=10)
        
        self.tabview.add("OpenAI API")
        self.tabview.add("Output")
        self.tabview.add("Watermark")
        self.tabview.add("YouTube")
        self.tabview.add("About")
        
        self.create_openai_tab()
        self.create_output_tab()
        self.create_watermark_tab()
        self.create_youtube_tab()
        self.create_about_tab()
    
    def create_openai_tab(self):
        """Create OpenAI API settings tab"""
        main = self.tabview.tab("OpenAI API")
        
        # Scrollable frame for all content
        scroll = ctk.CTkScrollableFrame(main)
        scroll.pack(fill="both", expand=True, padx=5, pady=5)
        
        ctk.CTkLabel(scroll, text="Base URL", anchor="w").pack(fill="x", pady=(10, 0))
        self.url_entry = ctk.CTkEntry(scroll, placeholder_text="https://api.openai.com/v1")
        self.url_entry.pack(fill="x", pady=(5, 15))
        
        key_frame = ctk.CTkFrame(scroll, fg_color="transparent")
        key_frame.pack(fill="x")
        ctk.CTkLabel(key_frame, text="API Key", anchor="w").pack(side="left")
        self.key_status = ctk.CTkLabel(key_frame, text="", font=ctk.CTkFont(size=11))
        self.key_status.pack(side="right")
        
        key_input = ctk.CTkFrame(scroll, fg_color="transparent")
        key_input.pack(fill="x", pady=(5, 15))
        self.key_entry = ctk.CTkEntry(key_input, placeholder_text="sk-...", show="•")
        self.key_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.validate_btn = ctk.CTkButton(key_input, text="Validate", width=80, command=self.validate_key)
        self.validate_btn.pack(side="right")
        
        ctk.CTkLabel(scroll, text="Model", anchor="w").pack(fill="x")
        model_frame = ctk.CTkFrame(scroll, fg_color="transparent")
        model_frame.pack(fill="x", pady=(5, 20))
        self.model_var = ctk.StringVar(value="Select model...")
        self.model_btn = ctk.CTkButton(model_frame, textvariable=self.model_var, anchor="w",
            fg_color=("gray75", "gray25"), hover_color=("gray70", "gray30"),
            text_color=("gray10", "gray90"), command=self.open_model_selector)
        self.model_btn.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.model_count = ctk.CTkLabel(model_frame, text="", text_color="gray", font=ctk.CTkFont(size=11))
        self.model_count.pack(side="right")
        
        # Temperature setting
        ctk.CTkLabel(scroll, text="Temperature", anchor="w").pack(fill="x", pady=(0, 0))
        ctk.CTkLabel(scroll, text="Control AI creativity (0.0 = consistent, 2.0 = creative). Some models only support specific values.", 
            anchor="w", font=ctk.CTkFont(size=11), text_color="gray", wraplength=450).pack(fill="x", pady=(0, 5))
        
        temp_frame = ctk.CTkFrame(scroll, fg_color="transparent")
        temp_frame.pack(fill="x", pady=(5, 20))
        
        self.temp_var = ctk.DoubleVar(value=1.0)
        self.temp_slider = ctk.CTkSlider(temp_frame, from_=0.0, to=2.0, variable=self.temp_var, 
            command=self.update_temp_label, number_of_steps=20)
        self.temp_slider.pack(side="left", fill="x", expand=True, padx=(0, 10))
        
        self.temp_label = ctk.CTkLabel(temp_frame, text="1.0", width=40, anchor="e")
        self.temp_label.pack(side="right")
        
        # TTS Model setting
        ctk.CTkLabel(scroll, text="TTS Model (Text-to-Speech)", anchor="w").pack(fill="x", pady=(0, 0))
        ctk.CTkLabel(scroll, text="Model for generating audio hooks. Examples: tts-1, tts-1-hd (OpenAI) or other models based on provider.", 
            anchor="w", font=ctk.CTkFont(size=11), text_color="gray", wraplength=450).pack(fill="x", pady=(0, 5))
        
        self.tts_model_entry = ctk.CTkEntry(scroll, placeholder_text="tts-1")
        self.tts_model_entry.pack(fill="x", pady=(5, 20))
        
        # System Prompt section
        ctk.CTkLabel(scroll, text="System Prompt", anchor="w", font=ctk.CTkFont(size=14, weight="bold")).pack(fill="x", pady=(20, 5))
        ctk.CTkLabel(scroll, text="Prompt for AI when finding highlights. Use {num_clips}, {video_context}, {transcript} as placeholders.", 
            anchor="w", font=ctk.CTkFont(size=11), text_color="gray", wraplength=450).pack(fill="x", pady=(0, 5))
        
        prompt_frame = ctk.CTkFrame(scroll, fg_color="transparent")
        prompt_frame.pack(fill="x", pady=(5, 10))
        
        self.prompt_text = ctk.CTkTextbox(prompt_frame, height=200, wrap="word")
        self.prompt_text.pack(fill="both", expand=True)
        
        # Buttons for prompt
        prompt_btn_frame = ctk.CTkFrame(scroll, fg_color="transparent")
        prompt_btn_frame.pack(fill="x", pady=(5, 15))
        
        ctk.CTkButton(prompt_btn_frame, text="Reset to Default", width=150, fg_color="gray",
            command=self.reset_prompt).pack(side="left", padx=(0, 5))
        
        self.prompt_char_count = ctk.CTkLabel(prompt_btn_frame, text="0 chars", text_color="gray", font=ctk.CTkFont(size=11))
        self.prompt_char_count.pack(side="right")
        
        # Bind text change to update char count
        self.prompt_text.bind("<KeyRelease>", self.update_prompt_char_count)
        
        ctk.CTkButton(scroll, text="Save Settings", height=40, command=self.save_settings).pack(fill="x", pady=(10, 0))
    
    def create_output_tab(self):
        """Create output folder settings tab"""
        main = self.tabview.tab("Output")
        
        ctk.CTkLabel(main, text="Output Folder", anchor="w", font=ctk.CTkFont(size=14, weight="bold")).pack(fill="x", pady=(15, 5))
        ctk.CTkLabel(main, text="Folder where video clips will be saved", anchor="w", 
            font=ctk.CTkFont(size=11), text_color="gray").pack(fill="x", pady=(0, 10))
        
        output_frame = ctk.CTkFrame(main, fg_color="transparent")
        output_frame.pack(fill="x", pady=(5, 15))
        self.output_var = ctk.StringVar(value=str(self.output_dir))
        self.output_entry = ctk.CTkEntry(output_frame, textvariable=self.output_var)
        self.output_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        ctk.CTkButton(output_frame, text="Browse", width=100, command=self.browse_output_folder).pack(side="right")
        
        # Open folder button
        ctk.CTkButton(main, text="Open Output Folder", height=40, fg_color="gray",
            command=lambda: self.open_folder(self.output_var.get())).pack(fill="x", pady=(0, 15))
        
        # Face Tracking Mode section
        ctk.CTkLabel(main, text="Face Tracking Mode", anchor="w", font=ctk.CTkFont(size=14, weight="bold")).pack(fill="x", pady=(15, 5))
        ctk.CTkLabel(main, text="Choose how the video crops to speakers", anchor="w", 
            font=ctk.CTkFont(size=11), text_color="gray").pack(fill="x", pady=(0, 10))
        
        # Radio buttons for face tracking mode
        self.face_tracking_var = ctk.StringVar(value="opencv")
        
        opencv_frame = ctk.CTkFrame(main, fg_color=("gray85", "gray20"))
        opencv_frame.pack(fill="x", pady=(5, 10))
        opencv_radio = ctk.CTkRadioButton(opencv_frame, text="OpenCV (Fast)", variable=self.face_tracking_var, 
            value="opencv", font=ctk.CTkFont(size=13, weight="bold"))
        opencv_radio.pack(anchor="w", padx=15, pady=(10, 5))
        ctk.CTkLabel(opencv_frame, text="• Crop to largest face", anchor="w", 
            font=ctk.CTkFont(size=11), text_color="gray").pack(anchor="w", padx=35, pady=0)
        ctk.CTkLabel(opencv_frame, text="• Faster processing", anchor="w", 
            font=ctk.CTkFont(size=11), text_color="gray").pack(anchor="w", padx=35, pady=0)
        ctk.CTkLabel(opencv_frame, text="• Recommended for most users", anchor="w", 
            font=ctk.CTkFont(size=11), text_color="gray").pack(anchor="w", padx=35, pady=(0, 10))
        
        mediapipe_frame = ctk.CTkFrame(main, fg_color=("gray85", "gray20"))
        mediapipe_frame.pack(fill="x", pady=(0, 10))
        mediapipe_radio = ctk.CTkRadioButton(mediapipe_frame, text="MediaPipe (Smart)", variable=self.face_tracking_var, 
            value="mediapipe", font=ctk.CTkFont(size=13, weight="bold"))
        mediapipe_radio.pack(anchor="w", padx=15, pady=(10, 5))
        ctk.CTkLabel(mediapipe_frame, text="• Crop to active speaker (lip movement)", anchor="w", 
            font=ctk.CTkFont(size=11), text_color="gray").pack(anchor="w", padx=35, pady=0)
        ctk.CTkLabel(mediapipe_frame, text="• More accurate speaker tracking", anchor="w", 
            font=ctk.CTkFont(size=11), text_color="gray").pack(anchor="w", padx=35, pady=0)
        ctk.CTkLabel(mediapipe_frame, text="⚠ Slower processing (2-3x)", anchor="w", 
            font=ctk.CTkFont(size=11), text_color="orange").pack(anchor="w", padx=35, pady=(0, 10))
        
        ctk.CTkButton(main, text="Save Settings", height=40, command=self.save_settings).pack(fill="x", pady=(10, 0))
    
    def create_watermark_tab(self):
        """Create watermark settings tab"""
        main = self.tabview.tab("Watermark")
        
        # Scrollable frame
        scroll = ctk.CTkScrollableFrame(main)
        scroll.pack(fill="both", expand=True, padx=5, pady=5)
        
        # Enable watermark toggle
        self.watermark_enabled = ctk.BooleanVar(value=False)
        enable_frame = ctk.CTkFrame(scroll, fg_color="transparent")
        enable_frame.pack(fill="x", pady=(10, 15))
        
        ctk.CTkSwitch(enable_frame, text="Enable Watermark", variable=self.watermark_enabled,
            font=ctk.CTkFont(size=14, weight="bold"), command=self.toggle_watermark).pack(side="left")
        
        # Watermark settings container
        self.watermark_settings_frame = ctk.CTkFrame(scroll, fg_color="transparent")
        self.watermark_settings_frame.pack(fill="both", expand=True)
        
        # Image selection
        ctk.CTkLabel(self.watermark_settings_frame, text="Watermark Image", anchor="w", 
            font=ctk.CTkFont(size=13, weight="bold")).pack(fill="x", pady=(5, 5))
        
        image_frame = ctk.CTkFrame(self.watermark_settings_frame, fg_color="transparent")
        image_frame.pack(fill="x", pady=(5, 15))
        
        self.watermark_path_var = ctk.StringVar(value="")
        self.watermark_path_entry = ctk.CTkEntry(image_frame, textvariable=self.watermark_path_var, 
            placeholder_text="Select PNG image...")
        self.watermark_path_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        ctk.CTkButton(image_frame, text="Browse", width=100, command=self.browse_watermark).pack(side="right")
        
        # Position simulator
        ctk.CTkLabel(self.watermark_settings_frame, text="Position", anchor="w", 
            font=ctk.CTkFont(size=13, weight="bold")).pack(fill="x", pady=(10, 5))
        ctk.CTkLabel(self.watermark_settings_frame, text="Drag the watermark to position it on the video", 
            anchor="w", font=ctk.CTkFont(size=11), text_color="gray").pack(fill="x", pady=(0, 10))
        
        # Canvas for 9:16 simulator
        self.canvas_frame = ctk.CTkFrame(self.watermark_settings_frame, fg_color=("gray85", "gray20"))
        self.canvas_frame.pack(fill="x", pady=(5, 15))
        
        import tkinter as tk
        self.canvas = tk.Canvas(self.canvas_frame, width=270, height=480, bg="#1a1a1a", 
            highlightthickness=1, highlightbackground="gray")
        self.canvas.pack(padx=10, pady=10)
        
        # Draw 9:16 frame
        self.canvas.create_rectangle(0, 0, 270, 480, outline="gray", width=2)
        self.canvas.create_text(135, 240, text="9:16 Video Preview", fill="gray50", 
            font=("Arial", 12))
        
        # Watermark placeholder
        self.watermark_item = None
        self.watermark_x = ctk.DoubleVar(value=0.85)
        self.watermark_y = ctk.DoubleVar(value=0.05)
        
        # Bind drag events
        self.canvas.bind("<Button-1>", self.on_watermark_click)
        self.canvas.bind("<B1-Motion>", self.on_watermark_drag)
        
        # Opacity slider
        ctk.CTkLabel(self.watermark_settings_frame, text="Opacity", anchor="w", 
            font=ctk.CTkFont(size=13, weight="bold")).pack(fill="x", pady=(10, 5))
        
        opacity_frame = ctk.CTkFrame(self.watermark_settings_frame, fg_color="transparent")
        opacity_frame.pack(fill="x", pady=(5, 15))
        
        self.watermark_opacity = ctk.DoubleVar(value=0.8)
        opacity_slider = ctk.CTkSlider(opacity_frame, from_=0.0, to=1.0, variable=self.watermark_opacity,
            command=self.update_watermark_preview, number_of_steps=20)
        opacity_slider.pack(side="left", fill="x", expand=True, padx=(0, 10))
        
        self.opacity_label = ctk.CTkLabel(opacity_frame, text="80%", width=50, anchor="e")
        self.opacity_label.pack(side="right")
        
        # Scale slider
        ctk.CTkLabel(self.watermark_settings_frame, text="Size", anchor="w", 
            font=ctk.CTkFont(size=13, weight="bold")).pack(fill="x", pady=(10, 5))
        
        scale_frame = ctk.CTkFrame(self.watermark_settings_frame, fg_color="transparent")
        scale_frame.pack(fill="x", pady=(5, 15))
        
        self.watermark_scale = ctk.DoubleVar(value=0.15)
        scale_slider = ctk.CTkSlider(scale_frame, from_=0.05, to=0.5, variable=self.watermark_scale,
            command=self.update_watermark_preview, number_of_steps=45)
        scale_slider.pack(side="left", fill="x", expand=True, padx=(0, 10))
        
        self.scale_label = ctk.CTkLabel(scale_frame, text="15%", width=50, anchor="e")
        self.scale_label.pack(side="right")
        
        # Save button
        ctk.CTkButton(scroll, text="Save Settings", height=40, command=self.save_settings).pack(fill="x", pady=(10, 0))
        
        # Initially disable watermark settings
        self.toggle_watermark()
    
    def toggle_watermark(self):
        """Toggle watermark settings visibility"""
        if self.watermark_enabled.get():
            # Enable all child widgets recursively
            self._set_children_state(self.watermark_settings_frame, "normal")
        else:
            # Disable all child widgets recursively
            self._set_children_state(self.watermark_settings_frame, "disabled")
    
    def _set_children_state(self, widget, state):
        """Recursively set state for all children widgets"""
        for child in widget.winfo_children():
            widget_type = child.winfo_class()
            
            # Skip frames and labels (they don't have state)
            if widget_type in ('Frame', 'Label', 'Canvas'):
                # Recursively process children of frames
                if widget_type == 'Frame':
                    self._set_children_state(child, state)
                continue
            
            # Try to set state for widgets that support it
            try:
                if hasattr(child, 'configure'):
                    child.configure(state=state)
            except Exception:
                # If widget doesn't support state, try its children
                try:
                    self._set_children_state(child, state)
                except:
                    pass
    
    def browse_watermark(self):
        """Browse for watermark image and copy to app folder"""
        from tkinter import filedialog
        import shutil
        
        file_path = filedialog.askopenfilename(
            title="Select Watermark Image",
            filetypes=[("PNG Images", "*.png"), ("All Images", "*.png *.jpg *.jpeg")]
        )
        if file_path:
            try:
                # Create watermarks folder if not exists
                watermarks_dir = Path("assets/watermarks")
                watermarks_dir.mkdir(parents=True, exist_ok=True)
                
                # Generate unique filename
                original_name = Path(file_path).stem
                extension = Path(file_path).suffix
                dest_filename = f"watermark_{original_name}{extension}"
                dest_path = watermarks_dir / dest_filename
                
                # If file already exists, add number suffix
                counter = 1
                while dest_path.exists():
                    dest_filename = f"watermark_{original_name}_{counter}{extension}"
                    dest_path = watermarks_dir / dest_filename
                    counter += 1
                
                # Copy file to app folder
                shutil.copy2(file_path, dest_path)
                
                # Save the new path
                self.watermark_path_var.set(str(dest_path))
                self.update_watermark_preview()
                
                self.log(f"Watermark copied to: {dest_path}")
                
            except Exception as e:
                messagebox.showerror("Error", f"Failed to copy watermark: {str(e)}")
    
    def log(self, message: str):
        """Log message (helper method)"""
        print(message)
    
    def update_watermark_preview(self, *args):
        """Update watermark preview on canvas"""
        # Update labels
        opacity_percent = int(self.watermark_opacity.get() * 100)
        self.opacity_label.configure(text=f"{opacity_percent}%")
        
        scale_percent = int(self.watermark_scale.get() * 100)
        self.scale_label.configure(text=f"{scale_percent}%")
        
        # Update canvas preview
        if self.watermark_item:
            self.canvas.delete(self.watermark_item)
            self.watermark_item = None
        
        watermark_path = self.watermark_path_var.get()
        if not watermark_path or not Path(watermark_path).exists():
            return
        
        try:
            from PIL import Image, ImageTk
            
            # Load and resize watermark
            img = Image.open(watermark_path)
            
            # Calculate size based on scale (relative to canvas width)
            canvas_width = 270
            watermark_width = int(canvas_width * self.watermark_scale.get())
            aspect_ratio = img.height / img.width
            watermark_height = int(watermark_width * aspect_ratio)
            
            img = img.resize((watermark_width, watermark_height), Image.Resampling.LANCZOS)
            
            # Apply opacity
            if img.mode != 'RGBA':
                img = img.convert('RGBA')
            
            alpha = img.split()[3]
            alpha = alpha.point(lambda p: int(p * self.watermark_opacity.get()))
            img.putalpha(alpha)
            
            # Keep reference to prevent garbage collection
            self.watermark_photo = ImageTk.PhotoImage(img)
            
            # Calculate position
            x = int(self.watermark_x.get() * 270)
            y = int(self.watermark_y.get() * 480)
            
            # Draw on canvas
            self.watermark_item = self.canvas.create_image(x, y, image=self.watermark_photo, anchor="nw")
            
        except Exception as e:
            print(f"Error loading watermark preview: {e}")
    
    def on_watermark_click(self, event):
        """Handle watermark click"""
        if not self.watermark_item:
            return
        
        # Check if click is on watermark
        bbox = self.canvas.bbox(self.watermark_item)
        if bbox and bbox[0] <= event.x <= bbox[2] and bbox[1] <= event.y <= bbox[3]:
            self.dragging = True
            self.drag_offset_x = event.x - bbox[0]
            self.drag_offset_y = event.y - bbox[1]
    
    def on_watermark_drag(self, event):
        """Handle watermark drag"""
        if not hasattr(self, 'dragging') or not self.dragging:
            return
        
        # Calculate new position (constrained to canvas)
        new_x = max(0, min(event.x - self.drag_offset_x, 270 - 50))
        new_y = max(0, min(event.y - self.drag_offset_y, 480 - 50))
        
        # Update position variables (as percentage)
        self.watermark_x.set(new_x / 270)
        self.watermark_y.set(new_y / 480)
        
        # Redraw
        self.update_watermark_preview()
    
    def create_youtube_tab(self):
        """Create YouTube settings tab"""
        main = self.tabview.tab("YouTube")
        
        # YouTube connection status
        status_frame = ctk.CTkFrame(main)
        status_frame.pack(fill="x", pady=15, padx=5)
        
        ctk.CTkLabel(status_frame, text="YouTube Channel", font=ctk.CTkFont(size=14, weight="bold")).pack(anchor="w", padx=10, pady=(10, 5))
        
        self.yt_status_frame = ctk.CTkFrame(status_frame, fg_color="transparent")
        self.yt_status_frame.pack(fill="x", padx=10, pady=(0, 10))
        
        self.yt_status_label = ctk.CTkLabel(self.yt_status_frame, text="Not connected", text_color="gray")
        self.yt_status_label.pack(side="left")
        
        self.yt_connect_btn = ctk.CTkButton(status_frame, text="Connect YouTube", height=40, 
            command=self.connect_youtube)
        self.yt_connect_btn.pack(fill="x", padx=10, pady=(0, 10))
        
        self.yt_disconnect_btn = ctk.CTkButton(status_frame, text="Disconnect", height=35,
            fg_color="gray", hover_color="#c0392b", command=self.disconnect_youtube)
        self.yt_disconnect_btn.pack(fill="x", padx=10, pady=(0, 10))
        self.yt_disconnect_btn.pack_forget()  # Hide initially
        
        # Info
        info_frame = ctk.CTkFrame(main, fg_color=("gray90", "gray17"))
        info_frame.pack(fill="x", pady=10, padx=5)
        
        info_text = """ℹ️ YouTube Upload Feature

To enable YouTube upload:
1. Set up Google Cloud project
2. Enable YouTube Data API v3
3. Create OAuth credentials
4. Place client_secret.json in app folder

See README for detailed setup guide."""
        
        ctk.CTkLabel(info_frame, text=info_text, justify="left", anchor="w",
            font=ctk.CTkFont(size=11), wraplength=400).pack(padx=15, pady=15)
        
        # Check YouTube status
        self.check_youtube_status()
    
    def check_youtube_status(self):
        """Check if YouTube is configured and connected"""
        try:
            from youtube_uploader import YouTubeUploader
            self.youtube_uploader = YouTubeUploader()
            
            if not self.youtube_uploader.is_configured():
                self.yt_status_label.configure(text="⚠️ client_secret.json not found", text_color="orange")
                self.yt_connect_btn.configure(state="disabled")
                return
            
            if self.youtube_uploader.is_authenticated():
                channel = self.youtube_uploader.get_channel_info()
                if channel:
                    self.yt_status_label.configure(
                        text=f"✓ Connected: {channel['title']}", 
                        text_color="green"
                    )
                    self.yt_connect_btn.pack_forget()
                    self.yt_disconnect_btn.pack(fill="x", padx=10, pady=(0, 10))
                    return
            
            self.yt_status_label.configure(text="Not connected", text_color="gray")
            self.yt_connect_btn.configure(state="normal")
            
        except ImportError:
            self.yt_status_label.configure(text="⚠️ YouTube module not available", text_color="orange")
            self.yt_connect_btn.configure(state="disabled")
        except Exception as e:
            self.yt_status_label.configure(text=f"Error: {str(e)[:30]}", text_color="red")
    
    def connect_youtube(self):
        """Start YouTube OAuth flow"""
        self.yt_connect_btn.configure(state="disabled", text="Connecting...")
        
        def do_connect():
            try:
                self.youtube_uploader.authenticate(callback=self.on_youtube_connected)
            except Exception as e:
                self.after(0, lambda: self.on_youtube_error(str(e)))
        
        threading.Thread(target=do_connect, daemon=True).start()
    
    def on_youtube_connected(self, success, data):
        """Callback when YouTube connection completes"""
        if success:
            self.after(0, lambda: self._update_youtube_connected(data))
        else:
            self.after(0, lambda: self.on_youtube_error(str(data)))
    
    def _update_youtube_connected(self, channel):
        """Update UI after YouTube connection"""
        if channel and channel.get('title'):
            self.yt_status_label.configure(
                text=f"✓ Connected: {channel['title']}", 
                text_color="green"
            )
            self.yt_connect_btn.pack_forget()
            self.yt_disconnect_btn.pack(fill="x", padx=10, pady=(0, 10))
            # Update main app status
            if hasattr(self.master, 'master') and hasattr(self.master.master, 'update_connection_status'):
                self.master.master.update_connection_status()
            messagebox.showinfo("Success", f"Connected to YouTube channel: {channel['title']}")
        else:
            # Channel info not available but auth succeeded
            self.yt_status_label.configure(
                text="✓ Connected", 
                text_color="green"
            )
            self.yt_connect_btn.pack_forget()
            self.yt_disconnect_btn.pack(fill="x", padx=10, pady=(0, 10))
            if hasattr(self.master, 'master') and hasattr(self.master.master, 'update_connection_status'):
                self.master.master.update_connection_status()
            messagebox.showinfo("Success", "Connected to YouTube!")
    
    def on_youtube_error(self, error):
        """Handle YouTube connection error"""
        self.yt_status_label.configure(text="Connection failed", text_color="red")
        self.yt_connect_btn.configure(state="normal", text="🔗 Connect YouTube")
        messagebox.showerror("Error", f"Failed to connect: {error}")
    
    def disconnect_youtube(self):
        """Disconnect YouTube account"""
        if messagebox.askyesno("Disconnect", "Are you sure you want to disconnect YouTube?"):
            if self.youtube_uploader:
                self.youtube_uploader.disconnect()
            self.yt_status_label.configure(text="Not connected", text_color="gray")
            self.yt_disconnect_btn.pack_forget()
            self.yt_connect_btn.configure(state="normal", text="🔗 Connect YouTube")
            self.yt_connect_btn.pack(fill="x", padx=10, pady=(0, 10))
            # Update main app status
            if hasattr(self.master, 'master') and hasattr(self.master.master, 'update_connection_status'):
                self.master.master.update_connection_status()
    
    def create_about_tab(self):
        """Create about tab"""
        main = self.tabview.tab("About")
        
        # App info
        info_frame = ctk.CTkFrame(main, fg_color="transparent")
        info_frame.pack(fill="x", pady=(20, 15))
        
        ctk.CTkLabel(info_frame, text="YT Short Clipper", font=ctk.CTkFont(size=20, weight="bold")).pack()
        ctk.CTkLabel(info_frame, text=f"v{__version__}", font=ctk.CTkFont(size=12), text_color="gray").pack(pady=(5, 0))
        
        # Check for updates button
        if self.check_update:
            ctk.CTkButton(info_frame, text="Check for Updates", height=35, width=150,
                fg_color="gray", hover_color=("gray70", "gray30"),
                command=self.check_update).pack(pady=(10, 0))
        
        # Description
        desc_frame = ctk.CTkFrame(main, fg_color=("gray90", "gray17"))
        desc_frame.pack(fill="x", pady=10, padx=5)
        
        desc_text = """Automated YouTube to Short-Form Content Pipeline

Transform long-form YouTube videos into engaging 
short-form content for TikTok, Instagram Reels, 
and YouTube Shorts."""
        
        ctk.CTkLabel(desc_frame, text=desc_text, justify="center", 
            font=ctk.CTkFont(size=11), wraplength=380).pack(padx=15, pady=15)
        
        # Credits
        credits_frame = ctk.CTkFrame(main, fg_color="transparent")
        credits_frame.pack(fill="x", pady=10)
        
        ctk.CTkLabel(credits_frame, text="Made with ☕ by", font=ctk.CTkFont(size=11), 
            text_color="gray").pack()
        ctk.CTkLabel(credits_frame, text="Aji Prakoso", font=ctk.CTkFont(size=13, weight="bold")).pack(pady=(5, 0))
        
        # Links
        links_frame = ctk.CTkFrame(main, fg_color="transparent")
        links_frame.pack(fill="x", pady=15)
        
        ctk.CTkButton(links_frame, text="⭐ GitHub Repository", height=40,
            fg_color=("#24292e", "#0d1117"), hover_color=("#2c3136", "#161b22"),
            command=lambda: self.open_url("https://github.com/jipraks/yt-short-clipper")).pack(fill="x", pady=2)
        
        ctk.CTkButton(links_frame, text="📸 @jipraks on Instagram", height=40,
            fg_color=("#E4405F", "#C13584"), hover_color=("#F56040", "#E1306C"),
            command=lambda: self.open_url("https://instagram.com/jipraks")).pack(fill="x", pady=2)
        
        ctk.CTkButton(links_frame, text="🎬 YouTube Channel", height=40,
            fg_color=("#c4302b", "#FF0000"), hover_color=("#ff0000", "#CC0000"),
            command=lambda: self.open_url("https://youtube.com/@jipraks")).pack(fill="x", pady=2)
        
        # Footer
        footer_frame = ctk.CTkFrame(main, fg_color="transparent")
        footer_frame.pack(side="bottom", fill="x", pady=(10, 5))
        
        ctk.CTkLabel(footer_frame, text="Open Source • MIT License", 
            font=ctk.CTkFont(size=10), text_color="gray").pack()
    
    def open_url(self, url: str):
        """Open URL in browser"""
        import webbrowser
        webbrowser.open(url)
    
    def open_folder(self, folder_path: str):
        """Open folder in file explorer"""
        if sys.platform == "win32":
            os.startfile(folder_path)
        elif sys.platform == "darwin":
            subprocess.run(["open", folder_path])
        else:
            subprocess.run(["xdg-open", folder_path])
    
    def load_config(self):
        """Load configuration into UI"""
        self.url_entry.insert(0, self.config.get("base_url", "https://api.openai.com/v1"))
        self.key_entry.insert(0, self.config.get("api_key", ""))
        self.model_var.set(self.config.get("model", "gpt-4.1"))
        self.output_var.set(self.config.get("output_dir", str(self.output_dir)) or str(self.output_dir))
        
        # Load temperature
        temperature = self.config.get("temperature", 1.0)
        self.temp_var.set(temperature)
        self.update_temp_label(temperature)
        
        # Load TTS model
        tts_model = self.config.get("tts_model", "tts-1")
        self.tts_model_entry.insert(0, tts_model)
        
        # Load face tracking mode
        face_tracking_mode = self.config.get("face_tracking_mode", "opencv")
        self.face_tracking_var.set(face_tracking_mode)
        
        # Load watermark settings
        watermark = self.config.get("watermark", {})
        self.watermark_enabled.set(watermark.get("enabled", False))
        self.watermark_path_var.set(watermark.get("image_path", ""))
        self.watermark_x.set(watermark.get("position_x", 0.85))
        self.watermark_y.set(watermark.get("position_y", 0.05))
        self.watermark_opacity.set(watermark.get("opacity", 0.8))
        self.watermark_scale.set(watermark.get("scale", 0.15))
        self.toggle_watermark()
        self.update_watermark_preview()
        
        # Load system prompt
        from clipper_core import AutoClipperCore
        system_prompt = self.config.get("system_prompt", AutoClipperCore.get_default_prompt())
        self.prompt_text.delete("1.0", "end")
        self.prompt_text.insert("1.0", system_prompt)
        self.update_prompt_char_count()
        
        if self.config.get("api_key"):
            self.validate_key()
    
    def browse_output_folder(self):
        """Browse for output folder"""
        folder = filedialog.askdirectory(initialdir=self.output_var.get())
        if folder:
            self.output_var.set(folder)

    def validate_key(self):
        """Validate OpenAI API key"""
        api_key = self.key_entry.get().strip()
        base_url = self.url_entry.get().strip() or "https://api.openai.com/v1"
        self.key_status.configure(text="Validating...", text_color="yellow")
        self.validate_btn.configure(state="disabled")
        
        def do_validate():
            try:
                client = OpenAI(api_key=api_key, base_url=base_url)
                models = sorted([m.id for m in client.models.list().data])
                self.models_list = models
                self.after(0, lambda: self._on_success(models))
            except:
                self.after(0, self._on_error)
        threading.Thread(target=do_validate, daemon=True).start()
    
    def _on_success(self, models):
        """Handle successful API key validation"""
        self.key_status.configure(text="✓ Valid", text_color="green")
        self.validate_btn.configure(state="normal")
        self.model_count.configure(text=f"{len(models)} models")
        if self.model_var.get() not in models:
            for p in ["gpt-4.1", "gpt-4o", "gpt-4o-mini"]:
                if p in models:
                    self.model_var.set(p)
                    break
    
    def _on_error(self):
        """Handle API key validation error"""
        self.key_status.configure(text="✗ Invalid", text_color="red")
        self.validate_btn.configure(state="normal")
        self.models_list = []
    
    def open_model_selector(self):
        """Open model selector dialog"""
        if not self.models_list:
            messagebox.showwarning("Warning", "Validate API key first")
            return
        SearchableModelDropdown(self, self.models_list, self.model_var.get(), lambda m: self.model_var.set(m))
    
    def save_settings(self):
        """Save settings"""
        api_key = self.key_entry.get().strip()
        base_url = self.url_entry.get().strip() or "https://api.openai.com/v1"
        model = self.model_var.get()
        output_dir = self.output_var.get().strip() or str(self.output_dir)
        system_prompt = self.prompt_text.get("1.0", "end-1c").strip()
        
        if not api_key or model == "Select model...":
            messagebox.showerror("Error", "Fill all fields")
            return
        
        if not system_prompt:
            messagebox.showerror("Error", "System prompt cannot be empty")
            return
        
        # Validate placeholders
        required_placeholders = ["{num_clips}", "{video_context}", "{transcript}"]
        missing = [p for p in required_placeholders if p not in system_prompt]
        if missing:
            messagebox.showwarning("Warning", f"System prompt missing placeholders: {', '.join(missing)}\n\nPrompt might not work correctly.")
        
        # Create output folder if not exists
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        
        self.config.set("api_key", api_key)
        self.config.set("base_url", base_url)
        self.config.set("model", model)
        self.config.set("output_dir", output_dir)
        self.config.set("temperature", self.temp_var.get())
        self.config.set("tts_model", self.tts_model_entry.get().strip() or "tts-1")
        self.config.set("system_prompt", system_prompt)
        self.config.set("face_tracking_mode", self.face_tracking_var.get())
        
        # Save watermark settings
        watermark_settings = {
            "enabled": self.watermark_enabled.get(),
            "image_path": self.watermark_path_var.get(),
            "position_x": self.watermark_x.get(),
            "position_y": self.watermark_y.get(),
            "opacity": self.watermark_opacity.get(),
            "scale": self.watermark_scale.get()
        }
        self.config.set("watermark", watermark_settings)
        
        self.on_save(api_key, base_url, model)
        self.on_back()
    
    def reset_prompt(self):
        """Reset system prompt to default"""
        if messagebox.askyesno("Reset Prompt", "Reset system prompt to default?"):
            from clipper_core import AutoClipperCore
            default_prompt = AutoClipperCore.get_default_prompt()
            self.prompt_text.delete("1.0", "end")
            self.prompt_text.insert("1.0", default_prompt)
            self.update_prompt_char_count()
    
    def update_prompt_char_count(self, event=None):
        """Update character count for system prompt"""
        text = self.prompt_text.get("1.0", "end-1c")
        char_count = len(text)
        self.prompt_char_count.configure(text=f"{char_count} chars")
    
    def update_temp_label(self, value):
        """Update temperature label"""
        self.temp_label.configure(text=f"{float(value):.1f}")
