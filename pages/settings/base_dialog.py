"""
Base class for settings sub-pages (embedded in main window)
"""

import customtkinter as ctk


class BaseSettingsSubPage(ctk.CTkFrame):
    """Base class for settings sub-pages"""
    
    def __init__(self, parent, title, on_back_callback):
        super().__init__(parent, fg_color=("#ffffff", "#0b0b0c"))
        self.pack(fill="both", expand=True)
        
        self.title = title
        self.on_back = on_back_callback
        
        # Create layout structure
        self._create_layout()
    
    def _create_layout(self):
        """Create the basic layout structure"""
        from components.page_layout import PageHeader, PageFooter
        
        # Header (title only, no back button, no logo)
        self.header = PageHeader(self, self, show_nav_buttons=False, show_back_button=False, page_title=self.title)
        self.header.pack(fill="x", padx=4, pady=(6, 6))
        
        # Footer (pack first to stay at bottom)
        self.footer = PageFooter(self, self)
        self.footer.pack(fill="x", padx=4, pady=(0, 8), side="bottom")
        
        # Scrollable content area
        self.content = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.content.pack(fill="both", expand=True, padx=4, pady=(0, 6))
    
    def create_section(self, title):
        """Create a section with title"""
        section = ctk.CTkFrame(self.content, fg_color=("gray90", "gray17"), border_width=1, border_color=("#2a2a30", "#2a2a30"))
        section.pack(fill="x", pady=(0, 6))
        
        ctk.CTkLabel(section, text=title, font=ctk.CTkFont(size=11, weight="bold")).pack(
            anchor="w", padx=4, pady=(6, 6))
        
        return section
    
    def create_input_row(self, parent, label, placeholder="", show="", width=None):
        """Create a labeled input row"""
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=4, pady=(0, 6))
        
        ctk.CTkLabel(row, text=label, font=ctk.CTkFont(size=11)).pack(anchor="w")
        
        entry = ctk.CTkEntry(row, placeholder_text=placeholder, show=show, height=26, width=width)
        entry.pack(fill="x", pady=(4, 0))
        
        return entry
    
    def create_dropdown_row(self, parent, label, values, variable=None):
        """Create a labeled dropdown row"""
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=4, pady=(0, 6))
        
        ctk.CTkLabel(row, text=label, font=ctk.CTkFont(size=11)).pack(anchor="w")
        
        dropdown = ctk.CTkOptionMenu(row, values=values, variable=variable, height=26)
        dropdown.pack(fill="x", pady=(4, 0))
        
        return dropdown
    
    def create_save_button(self, command, text="Save Settings"):
        """Create save button at bottom of content"""
        ctk.CTkButton(self.content, text=text, height=22,
            font=ctk.CTkFont(size=11, weight="bold"),
            fg_color=("#27ae60", "#27ae60"), hover_color=("#229954", "#229954"),
            command=command, text_color=("#FFFFFF", "#FFFFFF")).pack(fill="x", pady=(4, 0))
    
    def open_github(self):
        """Open GitHub repository"""
        import webbrowser
        webbrowser.open("https://github.com/jipraks/yt-short-clipper")
    
    def open_discord(self):
        """Open Discord server"""
        import webbrowser
        webbrowser.open("https://s.id/ytsdiscord")
    
    def show_page(self, page_name):
        """Delegate to parent app's show_page method"""
        try:
            parent = self.master
            while parent and not hasattr(parent, 'show_page'):
                parent = parent.master
            if parent and hasattr(parent, 'show_page'):
                parent.show_page(page_name)
        except:
            pass
