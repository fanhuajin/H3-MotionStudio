from __future__ import annotations

import asyncio
import base64
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image
from backend import batch_ai, batch_image, image_workbench, source_appearance as appearance


class SourceAppearanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.frame = self.root / "抽帧.jpg"
        Image.new("RGB", (100, 100), "red").save(self.frame)
        cache = patch.object(appearance, "CACHE_ROOT", self.root / "cache")
        cache.start()
        self.addCleanup(cache.stop)
        self.answer = {
            "hair": "黑色长发，高马尾", "glasses": "黑色细框透明眼镜", "accessories": "蓝色发夹",
            "makeup": "双眼下方淡蜜桃色腮红，浅棕下眼影，水润豆沙色唇妆",
        }
        model = patch.object(batch_ai, "_chat_structured", AsyncMock(return_value=self.answer))
        self.model = model.start()
        self.addCleanup(model.stop)

    async def test_original_frame_is_read_and_same_content_only_billed_once(self):
        first = await appearance.review(self.frame)
        copy = self.root / "copy.jpg"
        copy.write_bytes(self.frame.read_bytes())
        second = await appearance.review(copy)
        self.assertEqual(first, second)
        self.model.assert_awaited_once()
        content = self.model.call_args.kwargs["content"]
        data = content[1]["image_url"]["url"].split(",", 1)[1]
        self.assertEqual(base64.b64decode(data), self.frame.read_bytes())
        self.assertIn("不描述脸型、五官", content[0]["text"])
        self.assertIn("两眼下方的淡色晕染", content[0]["text"])
        self.assertIn("不要把眼袋、黑眼圈", content[0]["text"])
        self.assertEqual(first["makeup"], self.answer["makeup"])

    async def test_changed_frame_gets_new_observation(self):
        await appearance.review(self.frame)
        Image.new("RGB", (100, 100), "blue").save(self.frame)
        self.assertIsNone(appearance.cached(self.frame))
        await appearance.review(self.frame)
        self.assertEqual(self.model.await_count, 2)

    async def test_parallel_reads_share_one_request(self):
        await asyncio.gather(*(appearance.review(self.frame) for _ in range(4)))
        self.model.assert_awaited_once()

    async def test_failure_is_persisted_without_retry_or_invented_description(self):
        self.model.side_effect = RuntimeError("timeout")
        report = await appearance.review(self.frame)
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(appearance.enrich("原提示词", report), "原提示词")
        await appearance.review(self.frame)
        self.model.assert_awaited_once()

    async def test_cancelled_request_is_not_resubmitted(self):
        self.model.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await appearance.review(self.frame)
        report = await appearance.review(self.frame)
        self.assertEqual(report["status"], "unknown")
        self.model.assert_awaited_once()

    async def test_invalid_description_is_not_used(self):
        self.model.return_value = {"hair": [], "glasses": "眼镜", "accessories": ""}
        self.assertEqual((await appearance.review(self.frame))["status"], "unavailable")

    async def test_new_prompt_contains_observation_before_identity(self):
        base = batch_ai.compose_image_prompt("dance")
        output = self.root / "person.png"
        prompt = await appearance.prepare_prompt(self.frame, base, output)
        self.assertIn("黑色细框透明眼镜", prompt)
        self.assertIn("妆容：" + self.answer["makeup"], prompt)
        self.assertNotIn("自然精致淡妆", prompt)
        self.assertEqual(prompt.count("【妆容】"), 1)
        self.assertNotIn("JSON", prompt)
        self.assertNotIn('"hair":', prompt)
        self.assertIn("发型：黑色长发，高马尾；眼镜：黑色细框透明眼镜；配饰：蓝色发夹", prompt)
        self.assertLess(prompt.index("【糊脸前的造型记录】"), prompt.index("【身份】"))
        self.assertTrue(prompt.endswith(batch_ai.IDENTITY_PRIORITY_BLOCK.strip()))
        candidate = output.with_name("person_attempt1.png")
        candidate.with_name(candidate.name + ".request-pending").write_text("{}", encoding="utf-8")
        resumed = await appearance.prepare_prompt(self.frame, "修改后的提示词", output)
        self.assertEqual(resumed, prompt)
        self.model.assert_awaited_once()

    async def test_new_request_replaces_old_json_record_once(self):
        await appearance.review(self.frame)
        base = batch_ai.compose_image_prompt("dance")
        head, separator, identity = base.rpartition("\n\n【身份】")
        old = head + '\n【糊脸前的造型记录】以下 JSON：{"hair":"黑色长发"}' + separator + identity
        prompt = await appearance.prepare_prompt(self.frame, old, self.root / "new.png")
        self.assertNotIn("JSON", prompt)
        self.assertNotIn('"hair":', prompt)
        self.assertEqual(prompt.count("【糊脸前的造型记录】"), 1)
        self.assertEqual(appearance.enrich(prompt, await appearance.review(self.frame)), prompt)
        self.model.assert_awaited_once()

    async def test_no_recognizable_makeup_keeps_default_without_inventing_details(self):
        self.answer["makeup"] = ""
        prompt = await appearance.prepare_prompt(
            self.frame, batch_ai.compose_image_prompt("dance"), self.root / "person.png"
        )
        self.assertIn("自然精致淡妆", prompt)
        self.assertNotIn("妆容：", prompt)
        self.assertNotIn("蜜桃色", prompt)

    async def test_old_cache_without_makeup_is_reused_without_another_paid_review(self):
        record = await appearance.review(self.frame)
        del record["makeup"]
        appearance._write(appearance._path(appearance._key(self.frame)), record)
        self.model.reset_mock()
        prompt = await appearance.prepare_prompt(
            self.frame, batch_ai.compose_image_prompt("dance"), self.root / "person.png"
        )
        self.assertIn("黑色细框透明眼镜", prompt)
        self.assertIn("自然精致淡妆", prompt)
        self.assertNotIn("妆容：", prompt)
        self.model.assert_not_awaited()

    async def test_old_paid_snapshot_is_preserved_without_inserting_makeup(self):
        output = self.root / "person.png"
        (self.root / "person_attempt1.png.relay-task.json").write_text("{}", encoding="utf-8")
        appearance._write(output.with_name(output.name + ".source-prompt.json"), {"prompt": "旧付费提示词"})
        prompt = await appearance.prepare_prompt(self.frame, batch_ai.compose_image_prompt("dance"), output)
        self.assertEqual(prompt, "旧付费提示词")
        self.model.assert_not_awaited()

    async def test_invalid_makeup_description_is_not_used(self):
        self.answer["makeup"] = []
        self.assertEqual((await appearance.review(self.frame))["status"], "unavailable")

    async def test_legacy_pending_task_keeps_original_prompt_without_recognition(self):
        output = self.root / "person.png"
        (self.root / "person_attempt1.png.relay-task.json").write_text("{}", encoding="utf-8")
        result = await appearance.prepare_prompt(self.frame, "新模板", output, legacy_prompt="旧付费提示词")
        self.assertEqual(result, "旧付费提示词")
        self.model.assert_not_awaited()

    async def test_existing_image_does_not_trigger_recognition(self):
        output = self.root / "person.png"
        output.write_bytes(b"existing")
        self.assertEqual(await appearance.prepare_prompt(self.frame, "提示词", output), "提示词")
        self.model.assert_not_awaited()

    async def test_copy_and_materials_use_cached_record_without_new_request(self):
        await appearance.review(self.frame)
        identity = self.root / "identity.png"
        Image.new("RGB", (100, 100)).save(identity)
        with patch.object(image_workbench.batch_store, "items_for_aweme", return_value=[]), \
             patch.object(image_workbench, "asset_path", side_effect=lambda folder, key: self.frame if key == "frame" else identity):
            prompt = image_workbench.person_prompt(self.root, "dance")
            payload = image_workbench.material_zip(self.root, "person", "dance")
        self.assertIn("黑色细框透明眼镜", prompt)
        self.assertIn("妆容：" + self.answer["makeup"], prompt)
        self.assertNotIn("JSON", prompt)
        self.assertNotIn('"hair":', prompt)
        self.assertIn("发型：黑色长发，高马尾；眼镜：黑色细框透明眼镜；配饰：蓝色发夹", prompt)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertEqual(archive.read("提示词.txt").decode(), prompt)
        self.model.assert_awaited_once()

    async def test_workbench_recognizes_before_blur_and_sends_only_guarded_frame(self):
        guarded = self.root / "guard.png"
        Image.new("RGB", (100, 100), "gray").save(guarded)
        order = []
        async def identify(**kwargs):
            order.append("recognize")
            return self.answer
        self.model.side_effect = identify
        async def blur(frame, work):
            order.append("blur")
            self.assertEqual(frame, self.frame)
            return guarded, True
        async def generate(**kwargs):
            order.append("generate")
            self.assertEqual(kwargs["scene_image"], guarded)
            self.assertIn("黑色细框透明眼镜", kwargs["prompt"])
            self.assertIn("妆容：" + self.answer["makeup"], kwargs["prompt"])
            self.assertNotIn("自然精致淡妆", kwargs["prompt"])
            Image.new("RGB", (100, 100)).save(kwargs["output_path"])
            return kwargs["output_path"], -1
        with patch.object(batch_image, "defocus_reference_face", blur), \
             patch.object(batch_image, "generate_person_image", generate), \
             patch.object(image_workbench.batch_store, "items_for_aweme", return_value=[]), \
             patch.object(image_workbench, "save_upload"):
            await image_workbench._generate_person(self.root, "dance")
        self.assertEqual(order, ["recognize", "blur", "generate"])


if __name__ == "__main__":
    unittest.main()
