import re
import os

with open("clipper_core.py", "r", encoding="utf-8") as f:
    content = f.read()

# 1. Add import
if "from core.subtitle_generator import SubtitleGeneratorMixin" not in content:
    # Add it after "import os" or at the top
    content = re.sub(r'(import os\n)', r'\1from core.subtitle_generator import SubtitleGeneratorMixin\n', content, count=1)

# 2. Inherit from mixin
content = re.sub(r'class AutoClipperCore:', r'class AutoClipperCore(SubtitleGeneratorMixin):', content)

# 3. Remove old methods (they start around def create_ass_subtitle_karaoke and end after format_time)
# We will use regex to carefully match and remove them.
methods_to_remove = [
    r'    def create_ass_subtitle_karaoke\(self, transcript.*?    def ',
    r'    def create_ass_subtitle_bounce\(self, transcript.*?    def ',
    r'    def create_ass_subtitle_pop_bounce\(self, transcript.*?    def ',
    r'    def create_ass_subtitle_animated\(self, transcript.*?    def ',
    r'    def create_ass_subtitle_capcut\(self, transcript.*?    def ',
    r'    def format_time\(self, seconds: float\) -> str:.*?    RATIO_DIMENSIONS'
]

for pattern in methods_to_remove:
    # Use re.DOTALL so .* matches newlines
    # The lookahead `(?=    def |    RATIO_DIMENSIONS)` ensures we stop at the next method definition
    if 'format_time' in pattern:
        content = re.sub(r'    def format_time\(self, seconds: float\) -> str:.*?(?=    RATIO_DIMENSIONS)', '', content, flags=re.DOTALL)
    else:
        # Match until the next def
        content = re.sub(pattern[:-8] + r'.*?(?=    def )', '', content, flags=re.DOTALL)

with open("clipper_core.py", "w", encoding="utf-8") as f:
    f.write(content)
print("Patcher script finished")
