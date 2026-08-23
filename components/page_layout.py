"""
Reusable page layout components (header and footer)
"""

import customtkinter as ctk
from pathlib import Path
from PIL import Image
from datetime import datetime


class PageHeader(ctk.CTkFrame):
    """Reusable header component with logo, title, and navigation buttons"""
    
    def __init__(self, parent, app_instance, show_nav_buttons=True, show_back_button=False, page_title=None, show_title=True):
        super().__init__(parent, fg_color="transparent")
        self.app = app_instance
        self.show_nav_buttons = show_nav_buttons
        self.show_back_button = show_back_button
        self.page_title = page_title
        self.show_title = show_title
        
        self.create_header()
    
    def create_header(self):
        """Create header with logo, title, and navigation"""
        # Back button mode (back + title)
        if self.show_back_button and self.page_title:
            left_frame = ctk.CTkFrame(self, fg_color="transparent")
            left_frame.pack(side="left")
            ctk.CTkButton(left_frame, font=ctk.CTkFont(size=11), text="Back", width=75, height=22,
                fg_color=("#17171b", "#17171b"), hover_color=("#2a2a30", "#2a2a30"),
                border_width=1, border_color=("#3a3a40", "#2a2a30"), corner_radius=5,
                command=self.app.on_back if hasattr(self.app, 'on_back') else lambda: self.app.show_page("home"), text_color=("#FFFFFF", "#FFFFFF")).pack(side="left")
            ctk.CTkLabel(left_frame, text=self.page_title, font=ctk.CTkFont(size=15, weight="bold")).pack(side="left", padx=4)
            right_frame = ctk.CTkFrame(self, fg_color="transparent")
            right_frame.pack(side="right")
            try:
                from utils.helpers import get_bundle_dir
                BUNDLE_DIR = get_bundle_dir()
                ASSETS_DIR = BUNDLE_DIR / "assets"
                ICON_PATH = ASSETS_DIR / "icon.png"
                if ICON_PATH.exists():
                    icon_img = Image.open(ICON_PATH)
                    icon_img.thumbnail((32, 32), Image.Resampling.LANCZOS)
                    header_icon = ctk.CTkImage(light_image=icon_img, dark_image=icon_img, size=(32, 32))
                    ctk.CTkLabel(right_frame, image=header_icon, text="").pack(side="left", padx=(0, 6))
                    self.header_icon = header_icon
            except:
                pass
            tagline_col = ctk.CTkFrame(right_frame, fg_color="transparent")
            tagline_col.pack(side="left")
            ctk.CTkLabel(tagline_col, text="YT Short Clipper", font=ctk.CTkFont(size=11, weight="bold")).pack(anchor="w")
            ctk.CTkLabel(tagline_col, text="Turn long YouTube videos into viral shorts — Powered by AI", font=ctk.CTkFont(size=11), text_color="gray").pack(anchor="w")
            return

        # Title only mode (no back button, no logo, no tagline — just the page title)
        if self.page_title:
            left_frame = ctk.CTkFrame(self, fg_color="transparent")
            left_frame.pack(side="left")
            ctk.CTkLabel(left_frame, text=self.page_title, font=ctk.CTkFont(size=15, weight="bold")).pack(side="left", padx=4)
            if self.show_nav_buttons:
                nav_frame = ctk.CTkFrame(self, fg_color="transparent")
                nav_frame.pack(side="right")
                self._create_nav_buttons(nav_frame)
            return

        # Normal mode: logo + title + tagline on left, nav buttons on right
        if not self.show_title:
            if self.show_nav_buttons:
                nav_frame = ctk.CTkFrame(self, fg_color="transparent")
                nav_frame.pack(side="right")
                self._create_nav_buttons(nav_frame)
            return

        title_frame = ctk.CTkFrame(self, fg_color="transparent")
        title_frame.pack(side="left")
        try:
            from utils.helpers import get_bundle_dir
            BUNDLE_DIR = get_bundle_dir()
            ASSETS_DIR = BUNDLE_DIR / "assets"
            ICON_PATH = ASSETS_DIR / "icon.png"
            if ICON_PATH.exists():
                icon_img = Image.open(ICON_PATH)
                icon_img.thumbnail((40, 40), Image.Resampling.LANCZOS)
                header_icon = ctk.CTkImage(light_image=icon_img, dark_image=icon_img, size=(40, 40))
                ctk.CTkLabel(title_frame, image=header_icon, text="").pack(side="left", padx=(0, 8))
                self.header_icon = header_icon
        except:
            pass
        title_col = ctk.CTkFrame(title_frame, fg_color="transparent")
        title_col.pack(side="left")
        ctk.CTkLabel(title_col, text="YT Short Clipper", font=ctk.CTkFont(size=15, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(title_col, text="Turn long YouTube videos into viral shorts — Powered by AI", font=ctk.CTkFont(size=11), text_color="gray").pack(anchor="w")
        if self.show_nav_buttons:
            nav_frame = ctk.CTkFrame(self, fg_color="transparent")
            nav_frame.pack(side="right")
            self._create_nav_buttons(nav_frame)
        return
    
    def _create_nav_buttons(self, nav_frame):
        """Create the Settings / API / Library navigation buttons."""
        ctk.CTkButton(nav_frame, text="Settings",
            width=90, height=22, font=ctk.CTkFont(size=11),
            fg_color=("#17171b", "#17171b"), hover_color=("#2a2a30", "#2a2a30"),
            border_width=1, border_color=("#3a3a40", "#2a2a30"), corner_radius=5,
            command=lambda: self.app.show_page("settings"), text_color=("#FFFFFF", "#FFFFFF")).pack(side="left", padx=4)
        
        ctk.CTkButton(nav_frame, text="API",
            width=70, height=22, font=ctk.CTkFont(size=11),
            fg_color=("#17171b", "#17171b"), hover_color=("#2a2a30", "#2a2a30"),
            border_width=1, border_color=("#3a3a40", "#2a2a30"), corner_radius=5,
            command=lambda: self.app.show_page("api_status"), text_color=("#FFFFFF", "#FFFFFF")).pack(side="left", padx=4)
        
        ctk.CTkButton(nav_frame, text="Library",
            width=85, height=22, font=ctk.CTkFont(size=11),
            fg_color=("#17171b", "#17171b"), hover_color=("#2a2a30", "#2a2a30"),
            border_width=1, border_color=("#3a3a40", "#2a2a30"), corner_radius=5,
            command=lambda: self.app.show_page("lib_status"), text_color=("#FFFFFF", "#FFFFFF")).pack(side="left", padx=4)


class PageFooter(ctk.CTkFrame):
    """Reusable footer component with copyright and links.

    Footer is hidden app-wide (pack is a no-op) — the log console panel
    occupies the bottom area instead.
    """

    def pack(self, *args, **kwargs):
        """Hide the footer: never place it in the layout."""
        pass
    
    def __init__(self, parent, app_instance):
        super().__init__(parent, fg_color="transparent", height=60)
        self.pack_propagate(False)
        self.app = app_instance
        
        self.create_footer()
    
    def create_footer(self):
        """Create footer with separator, copyright, and links"""
        # Separator line
        separator = ctk.CTkFrame(self, height=1, fg_color=("#3a3a40", "#2a2a30"), border_width=1, border_color=("#2a2a30", "#2a2a30"))
        separator.pack(fill="x", pady=(0, 8))
        
        # Footer content
        footer_content = ctk.CTkFrame(self, fg_color="transparent")
        footer_content.pack(fill="x")
        
        # Copyright text on left with dynamic year and version
        try:
            from version import __version__
            current_year = datetime.now().year
            copyright_text = f"© {current_year} YT Short Clipper • v{__version__}"
        except:
            copyright_text = "© 2026 YT Short Clipper"
        
        ctk.CTkLabel(footer_content, text=copyright_text, 
            font=ctk.CTkFont(size=11), text_color="gray", anchor="w").pack(side="left")
        
        # Links on right
        links_frame = ctk.CTkFrame(footer_content, fg_color="transparent")
        links_frame.pack(side="right")
        
        # GitHub link
        github_link = ctk.CTkLabel(links_frame, text="⭐ GitHub", 
            font=ctk.CTkFont(size=11), text_color="#ffffff", cursor="hand2")
        github_link.pack(side="left", padx=(0, 8))
        github_link.bind("<Button-1>", lambda e: self.app.open_github())

    def open_autoklip(self):
        """Open AutoKlip multi-platform link"""
        import webbrowser
        webbrowser.open("https://dub.sh/autoklip")
    
    def open_ai_api_key_page(self):
        """Open AI API Key page"""
        import webbrowser
        webbrowser.open("https://ai.ytclip.org")

