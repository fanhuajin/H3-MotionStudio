import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from backend import douyin_direct

class EmptyDescriptionTests(unittest.TestCase):
    def test_fetch_downloads_empty_and_normal_descriptions(self):
        for description in ('', None, '正常标题\n第二行'):
            with self.subTest(description=description), tempfile.TemporaryDirectory() as directory:
                payload = {'videoId': 'video', 'desc': description, 'tags': [], 'nickname': '', 'durationMs': 0}
                def download(client, cdn, destination, **kwargs):
                    destination.write_bytes(b'0' * 12000)
                    return 12000
                with patch.object(douyin_direct, 'load_cookies', return_value={}), patch.object(douyin_direct, 'resolve_aweme_id', return_value='123'), patch.object(douyin_direct, 'share_payload', return_value=payload), patch.object(douyin_direct, 'play_url', return_value=('https://example.com/video', '720p')), patch.object(douyin_direct, 'download_cdn_file', side_effect=download), patch.object(douyin_direct, 'duration_mismatch_reason', return_value=None):
                    destination, metadata = douyin_direct.fetch('https://www.douyin.com/video/123', Path(directory))
                self.assertTrue(destination.is_file())
                self.assertEqual(destination.name, '正常标题_123.mp4' if description else 'douyin_123.mp4')
                self.assertEqual(metadata['awemeId'], '123')
