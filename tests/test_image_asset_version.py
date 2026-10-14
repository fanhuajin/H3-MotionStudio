import os
import tempfile
import unittest
from pathlib import Path
from PIL import Image
from backend.image_workbench import _meta


class ImageAssetVersionTests(unittest.TestCase):
    def test_same_size_replacement_changes_version(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'person.png'
            Image.new('RGB', (16, 16), 'red').save(path)
            before = _meta(path)
            stat = path.stat()
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
            after = _meta(path)
            self.assertEqual(before['bytes'], after['bytes'])
            self.assertNotEqual(before['version'], after['version'])

    def test_missing_asset_has_no_metadata(self):
        self.assertIsNone(_meta(None))


if __name__ == '__main__':
    unittest.main()
