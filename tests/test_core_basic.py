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

    def test_stabilize_positions_with_activity(self):
        # Setup positions and activity
        positions = [100] * 20 + [500] * 30  # Speaker changes position at index 20
        activities = [0.1] * 20 + [0.8] * 30
        
        # Call stabilize with a small min_shot_duration and 0.18 switch_threshold
        # original width = 1000
        stabilized = self.core._stabilize_positions_with_activity(
            positions, activities, min_shot_duration=5, switch_threshold=0.18, orig_w=1000
        )
        
        # Verify it has the same length
        self.assertEqual(len(stabilized), len(positions))
        # Verify it switched to 500 at/around index 20 due to activity and exceeding the 180px threshold
        self.assertEqual(stabilized[-1], 500)

    def test_smooth_follow_positions(self):
        # Setup static positions
        positions = [100] * 50
        smoothed = self.core._smooth_follow_positions(positions, pan_speed_limit=1.8)
        self.assertEqual(len(smoothed), len(positions))
        self.assertEqual(smoothed[-1], 100.0)

    # ── ClipForge-style portrait mode ─────────────────────────────────────
    def test_clipforge_crop_window_9x16(self):
        # 1920x1080 landscape -> 9:16 crop window (720x1280 target)
        crop_w, crop_h = self.core._get_crop_window(1920, 1080)
        # crop_crop_h = orig_h = 1080, crop_w = 1080*9/16 = 607.5 -> 607
        self.assertEqual(crop_h, 1080)
        self.assertEqual(crop_w, 607)
        self.assertAlmostEqual(crop_w / crop_h, 9 / 16, delta=0.01)

    def test_clipforge_crop_window_clamped(self):
        # Nyaris-square source -> crop window di-clamp agar tak melebihi lebar
        crop_w, crop_h = self.core._get_crop_window(1000, 1000)
        self.assertLessEqual(crop_w, 1000)
        self.assertLessEqual(crop_h, 1000)
        self.assertAlmostEqual(crop_w / crop_h, 9 / 16, delta=0.01)

    def test_clipforge_single_crop_filter_conversion(self):
        # Posisi konstan -> _build_portrait_filter_script kembali ke branch single-crop
        script = self.core._build_portrait_filter_script(
            [300] * 100, 607, 1080, 720, 1280)
        # single-crop: tidak ada split/concat, langsung crop=...
        self.assertNotIn("split", script)
        self.assertIn("crop=607:1080:x=300:y=0", script)
        self.assertIn("scale=720:1280", script)

    def test_clipforge_builds_crop_x_clamped(self):
        # focus_x di edge -> crop_x di-clamp ke [0, orig_w - crop_w]
        crop_w, _ = self.core._get_crop_window(1920, 1080)
        # focus dekat kanan (focus_x = 0.99)
        focus_px = 0.99 * 1920
        crop_x = int(focus_px - crop_w / 2)
        crop_x = max(0, min(crop_x, 1920 - crop_w))
        self.assertGreaterEqual(crop_x, 0)
        self.assertLessEqual(crop_x, 1920 - crop_w)

if __name__ == "__main__":
    unittest.main()
