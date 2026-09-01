import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import media


class FfmpegIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _ffmpeg_available() -> bool:
        return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))

    async def test_generated_h264_mp4_passes_real_ffprobe(self):
        if not self._ffmpeg_available():
            self.skipTest("ffmpeg/ffprobe are not installed outside the Docker image")

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "tiny.mp4"
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=64x64:r=30:d=1",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-an",
                    "-movflags",
                    "+faststart",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(media.detect_file_format(path), "mp4")
            await media.probe_and_validate_mp4(path)

    async def test_real_four_to_one_display_aspect_is_rejected(self):
        if not self._ffmpeg_available():
            self.skipTest("ffmpeg/ffprobe are not installed outside the Docker image")

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "wide-display.mp4"
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=64x64:r=30:d=1",
                    "-vf",
                    "setsar=4/1",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-an",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            with self.assertRaisesRegex(
                media.MediaValidationError,
                "effective display aspect ratio",
            ):
                await media.probe_and_validate_mp4(path)


if __name__ == "__main__":
    unittest.main()
