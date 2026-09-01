import unittest
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from utils.helpers import extract_video_id, get_ffmpeg_path, get_ytdlp_path  # noqa: E402


class TestHelpers(unittest.TestCase):
    def test_extract_video_id(self):
        self.assertEqual(extract_video_id("https://youtu.be/dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(
            extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
            "dQw4w9WgXcQ",
        )
        self.assertEqual(
            extract_video_id("https://youtube.com/shorts/dQw4w9WgXcQ"),
            "dQw4w9WgXcQ",
        )
        # Bukan URL YouTube -> kembalikan input apa adanya
        self.assertEqual(extract_video_id("bukan_url"), "bukan_url")

    def test_get_ffmpeg_path_returns_nonempty(self):
        p = get_ffmpeg_path()
        self.assertIsInstance(p, str)
        self.assertTrue(len(p) > 0)

    def test_get_ytdlp_path_returns_nonempty(self):
        p = get_ytdlp_path()
        self.assertIsInstance(p, str)
        self.assertTrue(len(p) > 0)


if __name__ == "__main__":
    unittest.main()