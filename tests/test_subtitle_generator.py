import unittest
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.subtitle_generator import SubtitleGeneratorMixin

class DummyGenerator(SubtitleGeneratorMixin):
    pass

class TestSubtitleGenerator(unittest.TestCase):
    def setUp(self):
        self.gen = DummyGenerator()

    def test_format_time(self):
        self.assertEqual(self.gen.format_time(0), "0:00:00.00")
        self.assertEqual(self.gen.format_time(3661.5), "1:01:01.50")

    def test_create_ass_capcut(self):
        # Create a mock transcript with words
        w1 = SimpleNamespace(start=0.0, end=0.5, word="Halo")
        w2 = SimpleNamespace(start=0.5, end=1.0, word="Dunia")
        transcript = SimpleNamespace(words=[w1, w2], segments=[])

        with tempfile.NamedTemporaryFile(suffix=".ass", delete=False) as f:
            out_path = f.name

        try:
            self.gen.create_ass_subtitle_capcut(transcript, out_path)
            self.assertTrue(os.path.exists(out_path))
            with open(out_path, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("[Script Info]", content)
            self.assertIn("HALO", content)
            self.assertIn("DUNIA", content)
        finally:
            if os.path.exists(out_path):
                os.remove(out_path)

if __name__ == "__main__":
    unittest.main()
