"""
Social Kit Dialog - Generate and copy SEO metadata for social media platforms
"""

import sys
import json
import threading
import customtkinter as ctk
from tkinter import messagebox
from pathlib import Path
from PIL import Image


class SocialKitDialog(ctk.CTkToplevel):
    """Dialog for generating and copying social media metadata (Title, Description, Hashtags, AI Results)"""
    
    def __init__(self, parent, clip: dict, openai_client, model: str, temperature: float = 1.0):
        super().__init__(parent)
        self.clip = clip
        self.openai_client = openai_client
        self.model = model
        self.temperature = temperature
        
        self.title("Social Kit Generator")
        self.geometry("600x750")
        self.resizable(True, True)
        self.transient(parent)
        self.grab_set()
        
        # Center on parent but clamp to screen so the Close button stays visible
        self.update_idletasks()
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        w, h = 600, min(750, screen_h - 120)
        self.geometry(f"{w}x{h}")
        x = parent.winfo_x() + (parent.winfo_width() // 2) - (w // 2)
        y = parent.winfo_y() + (parent.winfo_height() // 2) - (h // 2)
        x = max(0, min(x, screen_w - w - 10))
        y = max(0, min(y, screen_h - h - 60))
        self.geometry(f"+{x}+{y}")
        self.minsize(560, 480)
        self.bind("<Escape>", lambda e: self.destroy())
        
        # Set icon
        self.set_dialog_icon()
        
        self.create_ui()
        self.generate_social_metadata()
    
    def set_dialog_icon(self):
        """Set dialog icon to match main window"""
        try:
            from utils.helpers import get_bundle_dir
            BUNDLE_DIR = get_bundle_dir()
            ASSETS_DIR = BUNDLE_DIR / "assets"
            ICON_PATH = ASSETS_DIR / "icon.png"
            ICON_ICO_PATH = ASSETS_DIR / "icon.ico"
            
            if sys.platform == "win32":
                if ICON_ICO_PATH.exists():
                    self.iconbitmap(str(ICON_ICO_PATH))
                elif ICON_PATH.exists():
                    img = Image.open(ICON_PATH)
                    ico_path = ASSETS_DIR / "icon.ico"
                    img.save(str(ico_path), format='ICO', sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
                    self.iconbitmap(str(ico_path))
            else:
                if ICON_PATH.exists():
                    from tkinter import PhotoImage
                    icon_img = Image.open(ICON_PATH)
                    photo = PhotoImage(icon_img)
                    self.iconphoto(True, photo)
                    self._icon_photo = photo
        except Exception as e:
            pass
    
    def create_ui(self):
        """Create the dialog UI"""
        main = ctk.CTkFrame(self, border_width=1, border_color=("#2a2a30", "#2a2a30"))
        main.pack(fill="both", expand=True, padx=4, pady=4)
        
        # Header
        ctk.CTkLabel(main, text="📱 Social Kit Generator", 
            font=ctk.CTkFont(size=15, weight="bold")).pack(pady=(0, 8))
        
        # Video info
        info_frame = ctk.CTkFrame(main, fg_color=("gray85", "gray20"), border_width=1, border_color=("#2a2a30", "#2a2a30"))
        info_frame.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(info_frame, text=f"📹 {self.clip['title'][:50]}", 
            anchor="w").pack(fill="x", padx=4, pady=4)
        
        # Scrollable content area (fill expand=True so it takes all available space)
        scroll_frame = ctk.CTkScrollableFrame(main)
        scroll_frame.pack(fill="both", expand=True, pady=(0, 6))
        
        # Title
        title_header = ctk.CTkFrame(scroll_frame, fg_color="transparent")
        title_header.pack(fill="x", pady=(4, 0))
        ctk.CTkLabel(title_header, text="Title", anchor="w", 
            font=ctk.CTkFont(weight="bold")).pack(side="left")
        ctk.CTkButton(title_header, text="Copy", width=50, height=18, font=ctk.CTkFont(size=11),
            command=lambda: self.copy_to_clipboard(self.title_entry.get())).pack(side="right")
            
        self.title_entry = ctk.CTkEntry(scroll_frame, height=24)
        self.title_entry.pack(fill="x", pady=(4, 0))
        
        # Description
        desc_header = ctk.CTkFrame(scroll_frame, fg_color="transparent")
        desc_header.pack(fill="x", pady=(6, 0))
        ctk.CTkLabel(desc_header, text="Description", anchor="w", 
            font=ctk.CTkFont(weight="bold")).pack(side="left")
        ctk.CTkButton(desc_header, text="Copy", width=50, height=18, font=ctk.CTkFont(size=11),
            command=lambda: self.copy_to_clipboard(self.desc_text.get("1.0", "end-1c"))).pack(side="right")
            
        self.desc_text = ctk.CTkTextbox(scroll_frame, height=100)
        self.desc_text.pack(fill="x", pady=(4, 0))
        
        # Hashtags
        hash_header = ctk.CTkFrame(scroll_frame, fg_color="transparent")
        hash_header.pack(fill="x", pady=(6, 0))
        ctk.CTkLabel(hash_header, text="Hashtags", anchor="w", 
            font=ctk.CTkFont(weight="bold")).pack(side="left")
        ctk.CTkButton(hash_header, text="Copy", width=50, height=18, font=ctk.CTkFont(size=11),
            command=lambda: self.copy_to_clipboard(self.hash_entry.get())).pack(side="right")
            
        self.hash_entry = ctk.CTkEntry(scroll_frame, height=24)
        self.hash_entry.pack(fill="x", pady=(4, 0))
        
        # AI Results (Full Summary / Analysis)
        ai_header = ctk.CTkFrame(scroll_frame, fg_color="transparent")
        ai_header.pack(fill="x", pady=(6, 0))
        ctk.CTkLabel(ai_header, text="AI Analysis & Hook Results", anchor="w", 
            font=ctk.CTkFont(weight="bold")).pack(side="left")
        ctk.CTkButton(ai_header, text="Copy", width=50, height=18, font=ctk.CTkFont(size=11),
            command=lambda: self.copy_to_clipboard(self.ai_text.get("1.0", "end-1c"))).pack(side="right")
            
        self.ai_text = ctk.CTkTextbox(scroll_frame, height=120)
        self.ai_text.pack(fill="x", pady=(4, 0))
        
        # Generate button
        self.generate_btn = ctk.CTkButton(scroll_frame, font=ctk.CTkFont(size=11), text="Regenerate Social Kit", 
            height=18, fg_color="gray", command=self.generate_social_metadata, text_color=("#FFFFFF", "#FFFFFF"))
        self.generate_btn.pack(fill="x", pady=(6, 6))
        
        # Buttons (outside scroll)
        btn_frame = ctk.CTkFrame(main, fg_color="transparent")
        btn_frame.pack(fill="x", pady=(4, 0))
        
        ctk.CTkButton(btn_frame, font=ctk.CTkFont(size=11), text="Close", height=22, fg_color="gray",
            command=self.destroy, text_color=("#FFFFFF", "#FFFFFF")).pack(fill="x")
            
    def copy_to_clipboard(self, text):
        """Copy text to clipboard"""
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()
        messagebox.showinfo("Copied", "Copied to clipboard!")
        
    def generate_social_metadata(self):
        """Generate SEO-optimized social media metadata using GPT or load from data.json if exists"""
        # Check if metadata is already saved in data.json
        try:
            data_file = self.clip['folder'] / "data.json"
            if data_file.exists():
                with open(data_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if "social_kit" in data:
                    self.set_metadata(data["social_kit"])
                    return
        except Exception as e:
            print(f"Error loading saved social kit: {e}")

        self.generate_btn.configure(state="disabled", text="Generating...")
        self.title_entry.delete(0, "end")
        self.title_entry.insert(0, "Generating...")
        self.desc_text.delete("1.0", "end")
        self.desc_text.insert("1.0", "Generating description...")
        self.hash_entry.delete(0, "end")
        self.hash_entry.insert(0, "Generating hashtags...")
        self.ai_text.delete("1.0", "end")
        self.ai_text.insert("1.0", "Generating AI analysis...")
        
        def do_generate():
            prompt = f"""Kamu adalah expert Social Media Manager untuk konten short-form (TikTok, Instagram Reels, YouTube Shorts).

Berdasarkan informasi clip berikut, buatkan:
1. Title yang catchy dan clickbait (max 100 karakter, include emoji)
2. Description yang engaging dan interaktif untuk memancing komentar penonton (max 300 karakter)
3. Hashtags yang relevan dan viral (minimal 5-8 hashtags dipisahkan spasi)
4. AI Analysis & Hook Results: Penjelasan singkat mengapa hook ini menarik, target audiensnya siapa, dan analisis singkat tentang konten ini.

Info Clip:
- Judul: {self.clip['title']}
- Hook: {self.clip['hook_text']}

Format response dalam JSON:
{{
    "title": "judul dengan emoji",
    "description": "deskripsi engaging",
    "hashtags": "#shorts #viral #fyp #tag1 #tag2",
    "ai_analysis": "Penjelasan hook, target audiens, dan analisis konten."
}}

PENTING:
- Gunakan bahasa Indonesia
- Return HANYA JSON, tanpa markdown code blocks atau text lain."""

            try:
                response = self.openai_client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=self.temperature
                )
                
                result = response.choices[0].message.content.strip()
                
                # Parse JSON
                if result.startswith("```"):
                    import re
                    result = re.sub(r"```json?\n?", "", result)
                    result = re.sub(r"```\n?", "", result)
                
                metadata = json.loads(result)
                self.after(0, lambda: self.set_metadata(metadata))
                
                # Save to data.json
                self.save_metadata_to_clip(metadata)
            except Exception as e:
                self.after(0, lambda: self.set_metadata({
                    'title': f"🔥 {self.clip['title']}"[:100],
                    'description': f"Tonton video ini sampai habis! {self.clip['hook_text']}",
                    'hashtags': "#shorts #viral #fyp #trending",
                    'ai_analysis': f"Hook: '{self.clip['hook_text']}' dirancang untuk menarik perhatian dalam 3 detik pertama dengan menyoroti poin paling menarik dari video."
                }))
        
        threading.Thread(target=do_generate, daemon=True).start()
    
    def save_metadata_to_clip(self, metadata: dict):
        """Save generated metadata to clip's data.json"""
        try:
            data_file = self.clip['folder'] / "data.json"
            if data_file.exists():
                with open(data_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            else:
                data = {}
            
            data['social_kit'] = metadata
            
            with open(data_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving metadata: {e}")

    def set_metadata(self, metadata: dict):
        """Set generated metadata in UI"""
        self.title_entry.delete(0, "end")
        self.title_entry.insert(0, metadata.get('title', ''))
        
        self.desc_text.delete("1.0", "end")
        self.desc_text.insert("1.0", metadata.get('description', ''))
        
        self.hash_entry.delete(0, "end")
        self.hash_entry.insert(0, metadata.get('hashtags', ''))
        
        self.ai_text.delete("1.0", "end")
        self.ai_text.insert("1.0", metadata.get('ai_analysis', ''))
        
        self.generate_btn.configure(state="normal", text="🔄 Regenerate Social Kit")
