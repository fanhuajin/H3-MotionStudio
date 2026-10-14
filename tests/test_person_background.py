import unittest

from backend.batch_ai import compose_image_prompt


class PersonBackgroundTests(unittest.TestCase):
    def test_all_person_prompts_exclude_beds(self):
        for kind, source in (("dance", "video"), ("singing", "video"), ("singing", "redesign")):
            with self.subTest(kind=kind, source=source):
                prompt = compose_image_prompt(kind, style_source=source)
                self.assertIn("不出现床、床垫、床头、枕头、被褥或卧室布置", prompt)
                self.assertIn("即使参考图有床也要移除", prompt)
