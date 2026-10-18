import contextlib
import io
import json
import types
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from scripts import pick_hand_frame as picker


class CoverFrameScoringTests(unittest.TestCase):
    def setUp(self):
        self.sharp = np.random.default_rng(7).integers(70, 180, (360, 640), dtype=np.uint8)
        self.boxes = [[100, 100, 160, 160], [400, 100, 460, 160]]
        self.confs = [0.95, 0.95]

    def test_sharp_background_with_blurred_hands_loses_to_clear_no_hand_frame(self):
        blurry_hands = self.sharp.copy()
        for x1, y1, x2, y2 in self.boxes:
            blurry_hands[y1:y2, x1:x2] = cv2.GaussianBlur(
                blurry_hands[y1:y2, x1:x2], (21, 21), 6)
        hands_score, hands = picker._score_frame(blurry_hands, self.boxes, self.confs)
        no_hands_score, no_hands = picker._score_frame(self.sharp, [], [])
        self.assertEqual(len(hands), 2)
        self.assertEqual(no_hands, [])
        self.assertGreater(no_hands_score, hands_score)

    def test_hand_candidates_prioritize_the_clearest_hands(self):
        blurred = cv2.GaussianBlur(self.sharp, (21, 21), 6)
        sharp_score, _ = picker._score_frame(self.sharp, self.boxes, self.confs)
        blurry_score, _ = picker._score_frame(blurred, self.boxes, self.confs)
        self.assertGreater(sharp_score, blurry_score)

    def test_full_scan_without_hands_selects_clear_frame_successfully(self):
        blurred = cv2.GaussianBlur(self.sharp, (21, 21), 6)
        frames = [cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR) for gray in (blurred, self.sharp)]
        cap = Mock()
        cap.isOpened.return_value = True
        cap.get.return_value = 30.0
        cap.read.side_effect = [(True, frame) for frame in frames] + [(False, None)]
        model = Mock()
        model.predict.side_effect = lambda batch, **kwargs: [types.SimpleNamespace(boxes=None) for _ in batch]
        module = types.ModuleType("ultralytics")
        module.YOLO = Mock(return_value=model)
        output = io.StringIO()
        with patch.dict("sys.modules", {"ultralytics": module}), \
             patch("sys.argv", ["pick", "--video", "fake.mp4", "--output", "fake.png", "--model", "fake.pt"]), \
             patch.object(picker, "_resolve_device", return_value="cpu"), \
             patch.object(picker.cv2, "VideoCapture", return_value=cap), \
             patch.object(picker, "_grab", return_value=frames[1]) as grab, \
             patch.object(picker.cv2, "imwrite", return_value=True), \
             contextlib.redirect_stdout(output):
            self.assertEqual(picker.main(), 0)
        grab.assert_called_once_with(cap, 1)
        result = json.loads(output.getvalue())
        self.assertTrue(result["ok"])
        self.assertEqual(result["hands"], [])
        self.assertEqual(result["frames"], 2)
