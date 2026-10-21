import json
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch
from fill_helper import read_keyboard_state, keyboard_helper_ready, KEYBOARD_PROTOCOL
from bilibili_auto import BilibiliDraft


class KeyboardResultTests(unittest.TestCase):
    def test_legacy_helper_is_not_silently_reused(self):
        with self.assertRaisesRegex(RuntimeError,"F9"):
            keyboard_helper_ready({"pid":123,"protocol":1})
        self.assertTrue(keyboard_helper_ready({"pid":123,"protocol":KEYBOARD_PROTOCOL}))
        self.assertFalse(keyboard_helper_ready(None))
    def test_locked_state_can_be_read_on_next_poll(self):
        with patch.object(Path,"read_text",side_effect=[PermissionError(),'{"id":"current","ok":true}']):
            self.assertIsNone(read_keyboard_state(Path("result.json")))
            self.assertTrue(read_keyboard_state(Path("result.json"))["ok"])

    def test_invalid_json_is_not_treated_as_success(self):
        with patch.object(Path,"read_text",return_value="broken"):
            with self.assertRaises(json.JSONDecodeError):
                read_keyboard_state(Path("result.json"))

    def test_locked_or_old_result_does_not_resend_f6(self):
        draft=object.__new__(BilibiliDraft)
        draft.window=SimpleNamespace(NativeWindowHandle=123)
        draft.copy_file=Path("copy.txt"); draft.check=Mock(); draft.keys=Mock()
        draft.now=Mock(side_effect=range(10))
        with patch("bilibili_auto.uuid.uuid4",return_value=SimpleNamespace(hex="current")), \
             patch("bilibili_auto.write_keyboard_state"),patch("bilibili_auto.time.sleep"), \
             patch("bilibili_auto.read_keyboard_state",side_effect=[None,{"id":"old","ok":True},{"id":"current","ok":True}]), \
             patch.object(Path,"unlink"):
            draft.keyboard_field("description")
        draft.keys.assert_called_once_with("{F6}")

    def test_long_description_can_complete_after_thirty_seconds(self):
        draft=object.__new__(BilibiliDraft)
        draft.window=SimpleNamespace(NativeWindowHandle=123)
        draft.copy_file=Path("copy.txt"); draft.check=Mock(); draft.keys=Mock()
        draft.now=Mock(side_effect=[0,31,40])
        with patch("bilibili_auto.uuid.uuid4",return_value=SimpleNamespace(hex="current")), \
             patch("bilibili_auto.write_keyboard_state"),patch("bilibili_auto.time.sleep"), \
             patch("bilibili_auto.read_keyboard_state",side_effect=[None,{"id":"current","ok":True}]), \
             patch.object(Path,"unlink"):
            draft.keyboard_field("description")
        draft.keys.assert_called_once_with("{F6}")


if __name__=="__main__":
    unittest.main()
