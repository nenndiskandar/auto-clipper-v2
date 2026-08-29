import unittest
import os
import sys
from unittest.mock import MagicMock

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from clipper_core import AutoClipperCore, _hex_to_rgb

class TestCoreBasic(unittest.TestCase):
    def setUp(self):
        mock_client = MagicMock()
        self.core = AutoClipperCore(client=mock_client)

    def test_hex_to_rgb(self):
        self.assertEqual(_hex_to_rgb("#ffffff"), (255, 255, 255))
        self.assertEqual(_hex_to_rgb("000000"), (0, 0, 0))
        self.assertEqual(_hex_to_rgb("#FF0000"), (255, 0, 0))

    def test_format_time(self):
        self.assertEqual(self.core.format_time(0), "0:00:00.00")
        self.assertEqual(self.core.format_time(65.5), "0:01:05.50")

    def test_parse_timestamp(self):
        self.assertEqual(self.core.parse_timestamp("00:01:05.500"), 65.5)
        self.assertEqual(self.core.parse_timestamp("01:05"), 65.0)

    def test_repair_json_text(self):
        raw = '```json\n[{"start": "00:10", "end": "00:40", "hook": "Test Hook"}]\n```'
        repaired = self.core._repair_json_text(raw)
        self.assertTrue(repaired.startswith("[") and repaired.endswith("]"))

    def test_parse_srt(self):
        srt = (
            "1\n00:00:01,000 --> 00:00:03,500\nHello world\n\n"
            "2\n00:00:04,000 --> 00:00:06,000\nSecond line\n"
        )
        import tempfile, os
        with tempfile.NamedTemporaryFile(mode="w", suffix=".srt", delete=False, encoding="utf-8") as tf:
            tf.write(srt)
            path = tf.name
        try:
            result = self.core.parse_srt(path)
            self.assertIn("[00:00:01,000 - 00:00:03,500] Hello world", result)
            self.assertIn("[00:00:04,000 - 00:00:06,000] Second line", result)
        finally:
            os.unlink(path)

if __name__ == "__main__":
    unittest.main()
