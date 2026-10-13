import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from backend.duration_policy import validate_dance_duration
from backend import batch_worker as worker

class DanceDurationTests(unittest.TestCase):
    def test_boundary(self):
        for value in (0.1, 39.99, 40.0):
            validate_dance_duration(value)
        for value in (40.001, 49.852993, None, 0, float('nan')):
            with self.assertRaises(ValueError):
                validate_dance_duration(value)

    def test_existing_download_rejected_without_downloading_again(self):
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / 'video.mp4'
            source.touch()
            with patch.object(worker, '_item', return_value={'kind':'dance','sourcePath':str(source)}), patch.object(worker, '_park_truncated_source', return_value=None), patch.object(worker, '_video_duration_seconds', return_value=49.85), patch.object(worker, '_fetch_source_direct') as paid:
                with self.assertRaisesRegex(ValueError, '不能超过 40 秒'):
                    asyncio.run(worker._download('batch','item'))
                paid.assert_not_called()

    def test_existing_singing_video_unaffected(self):
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / 'video.mp4'
            source.touch()
            with patch.object(worker, '_item', return_value={'kind':'singing','sourcePath':str(source)}), patch.object(worker, '_park_truncated_source', return_value=None), patch.object(worker, '_video_duration_seconds') as duration:
                self.assertEqual(asyncio.run(worker._download('batch','item')),source)
                duration.assert_not_called()

    def test_local_replacement_rejected_before_state_change(self):
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / 'video.mp4'
            source.touch()
            with patch.object(worker, '_item', return_value={'kind':'dance','status':'confirmed'}), patch.object(worker, '_video_duration_seconds', return_value=40.01), patch.object(worker.batch_store, 'mutate_item') as mutation:
                with self.assertRaises(ValueError):
                    worker.replace_item_source_file('batch','item',source)
                mutation.assert_not_called()
