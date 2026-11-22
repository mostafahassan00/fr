"""Tests of reading photos (no AI models needed)."""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from facesorter import engine  # noqa: E402

DATA = Path(__file__).resolve().parent / "data"


class PhotoTest(unittest.TestCase):
    @unittest.skipIf(engine.pi_heif is None, "pi-heif not installed")
    def test_iphone_photo(self) -> None:
        sha1, img, date = engine.read_photo(DATA / "astronaut.heic")
        self.assertEqual(img.shape, (384, 384, 3))
        self.assertEqual(date, "2019-07-04")
        self.assertTrue(sha1)

    @unittest.skipIf(engine.pi_heif is None, "pi-heif not installed")
    def test_iphone_photos_are_found_in_folders(self) -> None:
        d = Path(tempfile.mkdtemp())
        try:
            shutil.copy2(DATA / "astronaut.heic", d / "IMG_0001.HEIC")
            self.assertEqual([p.name for p in engine.find_photos(d)], ["IMG_0001.HEIC"])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_broken_photo_does_not_crash(self) -> None:
        d = Path(tempfile.mkdtemp())
        try:
            for name in ("bad.jpg", "bad.heic"):
                (d / name).write_bytes(b"not a photo")
                sha1, img, date = engine.read_photo(d / name)
                self.assertIsNone(img)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
