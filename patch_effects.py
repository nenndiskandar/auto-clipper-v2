import re

with open("clipper_core.py", "r", encoding="utf-8") as f:
    content = f.read()

# 1. Add import
if "from core.effects import EffectsMixin" not in content:
    content = re.sub(
        r'from core\.subtitle_generator import SubtitleGeneratorMixin',
        'from core.subtitle_generator import SubtitleGeneratorMixin\nfrom core.effects import EffectsMixin',
        content,
        count=1
    )

# 2. Inherit EffectsMixin
content = re.sub(
    r'class AutoClipperCore\(SubtitleGeneratorMixin\):',
    'class AutoClipperCore(SubtitleGeneratorMixin, EffectsMixin):',
    content
)

# 3. Remove duplicate effect methods from clipper_core.py
# apply_color_grade through apply_ken_burns_with_progress
content = re.sub(
    r'    def apply_color_grade\(self, input_path: str.*?(?=    def _get_duration)',
    '',
    content,
    flags=re.DOTALL
)

with open("clipper_core.py", "w", encoding="utf-8") as f:
    f.write(content)
print("Effects patch finished")
