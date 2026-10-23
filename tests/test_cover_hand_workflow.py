import asyncio
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch
from PIL import Image
from backend import batch_ai, batch_image, batch_worker as worker, cover_review, image_workbench as wb


def picture(color="red"):
    output = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(output, "PNG")
    return output.getvalue()


class CoverHandWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_decision_uses_only_original_person_and_correct_framing(self):
        for status in ("reasonable", "no_hands", "unreasonable", "uncertain", "error"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as raw:
                folder = Path(raw)
                person = folder / wb.PERSON_NAME
                person.write_bytes(picture())
                (folder / "封面人物图.png").write_bytes(b"obsolete")
                seen = []
                async def judge(image):
                    if image.name == "reference.png":
                        if status == "error":
                            raise RuntimeError("offline")
                        return {"status": status, "reason": "输入判断", "fingersCutByFrame": "no"}
                    return {"status": "no_hands", "reason": "封面无手"}
                async def generate(**kw):
                    seen.append(kw)
                    self.assertEqual(kw["scene_image"].read_bytes(), picture())
                    self.assertIsNone(kw["identity_image"])
                    self.assertFalse(kw["upscale"])
                    kw["output_path"].write_bytes(picture("blue"))
                with patch.object(batch_ai, "judge_cover_hands", side_effect=judge) as check, patch.object(batch_image, "generate_candidate_image", side_effect=generate), patch.object(wb, "save_upload"):
                    await wb._generate_cover(folder, "bilibili", "dance")
                    await wb._generate_cover(folder, "douyin", "dance")
                self.assertEqual(check.await_count, 1, "两种比例只共享一次生成前检查，成图不再检查")
                for kw in seen:
                    if status in ("reasonable", "no_hands"):
                        self.assertIn("不重画已有手部", kw["prompt"])
                        self.assertIn("不补画原图画外的手掌或手指", kw["prompt"])
                        self.assertIn("不得出现空袖口", kw["prompt"])
                        self.assertIn("不为露出双手改变姿态", kw["prompt"])
                        self.assertNotIn("只扩展必要背景", kw["prompt"])
                        self.assertNotIn("适度拉远的半身构图", kw["prompt"])
                    else:
                        self.assertIn("胸部以上近景构图" if kw["ratio"] == "4:3" else "适度拉远的半身构图", kw["prompt"])
                        self.assertIn("胸前、肩旁或脸旁", kw["prompt"])
                        if kw["ratio"] == "4:3":
                            self.assertIn("脸部突出", kw["prompt"])
                            self.assertNotIn("适度拉远的半身构图", kw["prompt"])
                            self.assertNotIn("最多不超过45%", kw["prompt"])
                        else:
                            self.assertIn("完整保留双肩", kw["prompt"])
                            self.assertIn("最多不超过六成", kw["prompt"])
                            self.assertIn("不放大头脸", kw["prompt"])
                        self.assertIn("不得仅靠裁切", kw["prompt"])
                        self.assertIn("双臂调整为自然下垂", kw["prompt"])
                        self.assertIn("自然补全原先被手或手臂遮挡", kw["prompt"])
                self.assertEqual(person.read_bytes(), picture())
                self.assertEqual((folder / "封面人物图.png").read_bytes(), b"obsolete")

    async def test_cut_fingers_use_fallback_once_for_both_ratios(self):
        for cut in ("yes", "uncertain", "no"):
            with self.subTest(cut=cut), tempfile.TemporaryDirectory() as raw:
                folder = Path(raw)
                (folder / wb.PERSON_NAME).write_bytes(picture())
                seen = []
                async def generate(**kw):
                    seen.append(kw)
                    snapshot = json.loads((kw["output_path"].parent / "request.json").read_text(encoding="utf-8"))
                    self.assertEqual(bool(snapshot.get("failureFallback")), cut != "no")
                    kw["output_path"].write_bytes(picture("blue"))
                report = {"status": "reasonable", "reason": "边缘判断", "fingersCutByFrame": cut}
                with patch.object(batch_ai, "judge_cover_hands", new_callable=AsyncMock, return_value=report) as judge, patch.object(batch_image, "generate_candidate_image", side_effect=generate), patch.object(wb, "save_upload"):
                    for stage in ("bilibili", "douyin"):
                        await wb._generate_cover(folder, stage, "dance")
                self.assertEqual(judge.await_count, 1)
                self.assertEqual(len(seen), 2)
                for call in seen:
                    crop = "胸部以上近景构图" if call["ratio"] == "4:3" else "适度拉远的半身构图"
                    self.assertEqual(crop in call["prompt"], cut != "no")
                    if cut != "no":
                        self.assertIn("脸部突出" if call["ratio"] == "4:3" else "最多不超过六成", call["prompt"])

    async def test_old_review_cache_is_rechecked(self):
        with tempfile.TemporaryDirectory() as raw:
            image, report = Path(raw) / "p.png", Path(raw) / "review.json"
            image.write_bytes(picture())
            report.write_text(json.dumps({"version": 1, "sha256": cover_review.fingerprint(image), "status": "reasonable", "reason": "旧规则"}), encoding="utf-8")
            with patch.object(batch_ai, "judge_cover_hands", new_callable=AsyncMock, return_value={"status": "reasonable", "reason": "截指", "fingersCutByFrame": "yes"}) as judge:
                result = await cover_review.review(image, report)
            judge.assert_awaited_once()
            self.assertTrue(cover_review.hide_hands(result))

    async def test_concurrent_review_is_cached_and_invalidates_after_image_changes(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            image, report = folder / "p.png", folder / "review.json"
            image.write_bytes(picture())
            async def judge(_):
                await asyncio.sleep(0)
                return {"status": "reasonable", "reason": "自然", "fingersCutByFrame": "no"}
            with patch.object(batch_ai, "judge_cover_hands", side_effect=judge) as check:
                await asyncio.gather(*(cover_review.review(image, report) for _ in range(3)))
                self.assertEqual(check.await_count, 1)
                image.write_bytes(picture("blue"))
                self.assertIsNone(cover_review.read_report(image, report))
                await cover_review.review(image, report)
                self.assertEqual(check.await_count, 2)

    async def test_downloaded_cover_is_saved_without_any_visual_call(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            (folder / "最终成片.mp4").write_bytes(b"v")
            work = folder / ".h3-bilibili-work"
            work.mkdir()
            (work / "bilibili.png").write_bytes(picture("blue"))
            # Already downloaded paid result must be saved directly, even if old inputs are gone.
            with patch.object(batch_ai, "judge_cover_hands", new_callable=AsyncMock) as judge, patch.object(batch_image, "generate_candidate_image", new_callable=AsyncMock) as generate, patch.object(worker, "mark_publish_copy_locked"), patch.object(worker, "resume_failed_item_with_complete_images", return_value=None):
                await wb._generate_cover(folder, "bilibili", "dance")
            judge.assert_not_awaited()
            generate.assert_not_awaited()
            self.assertEqual((folder / wb.COVER_NAMES["bilibili"]).read_bytes(), picture("blue"))
            self.assertFalse((folder / ".h3-bilibili-review.json").exists())

    async def test_no_video_allows_batch_api_entry_point(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            (folder / wb.PERSON_NAME).write_bytes(picture())
            (folder / "发布文案.txt").write_text("标题：测试", encoding="utf-8")
            with patch.object(worker, "_item", return_value={}), patch.object(
                worker, "_set_item"
            ), patch.object(worker.batch_store, "add_item_log"), patch.object(
                wb, "_generate_cover", new_callable=AsyncMock
            ) as generate:
                self.assertEqual(await worker._generate_covers_via_api("b", "i", folder), [])
                self.assertEqual(generate.await_count, 2)

    async def test_background_has_no_frame_or_gpu_step(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(worker, "full_auto_enabled", return_value=True), patch.object(worker, "cover_blocking", return_value=False), patch.object(worker, "spawn_item_covers") as spawn:
            await worker._finish_covers_after_delivery("b", "i", Path(raw))
            spawn.assert_called_once()
        self.assertFalse(hasattr(worker, "ensure_cover_person"))
        self.assertFalse(hasattr(worker, "extract_cover_person"))

    async def test_legacy_paid_dual_reference_resumes_without_new_review_or_references(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            (folder / "最终成片.mp4").write_bytes(b"v")
            work = folder / ".h3-bilibili-work"
            work.mkdir()
            (work / "old.png").write_bytes(picture())
            (work / "identity.png").write_bytes(picture("green"))
            (work / "request.json").write_text(json.dumps({"reference": "old.png", "identity": "identity.png", "prompt": "old paid prompt", "ratio": "4:3"}))
            batch_image._task_checkpoint(work / "bilibili.png").write_text("{}")
            (folder / ".h3-bilibili-request-pending").write_text("paid")
            async def generate(**kw):
                self.assertEqual(kw["prompt"], "old paid prompt")
                self.assertEqual(kw["identity_image"].read_bytes(), picture("green"))
                kw["output_path"].write_bytes(picture("blue"))
            with patch.object(batch_ai, "judge_cover_hands", new_callable=AsyncMock, return_value={"status": "no_hands", "reason": "无手"}) as judge, patch.object(batch_image, "generate_candidate_image", side_effect=generate), patch.object(wb, "save_upload"):
                await wb._generate_cover(folder, "bilibili", "dance")
            judge.assert_not_awaited()

    async def test_cover_material_bundle_is_single_reference_without_blurring(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            (folder / wb.PERSON_NAME).write_bytes(picture())
            (folder / "发布文案.txt").write_text("标题：测试", encoding="utf-8")
            with patch.object(batch_image, "prepare_defocused_reference") as blur:
                zipped = wb.material_zip(folder, "bilibili", "dance")
            with zipfile.ZipFile(io.BytesIO(zipped)) as archive:
                self.assertEqual(set(archive.namelist()), {"提示词.txt", "人物图.png", "发布文案.txt"})
                self.assertIn("胸部以上近景构图", archive.read("提示词.txt").decode())
            blur.assert_not_called()

    async def test_cover_failure_never_reverts_completed_video(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            (folder / wb.PERSON_NAME).write_bytes(picture())
            (folder / "发布文案.txt").write_text("标题：测试", encoding="utf-8")
            with patch.object(worker, "full_auto_enabled", return_value=True), patch.object(worker, "_item", return_value={"status": "completed"}), patch.object(worker, "_covers_ready", return_value=False), patch.object(worker, "_generate_covers_via_api", side_effect=RuntimeError("relay offline")), patch.object(worker, "_set_item") as update, patch.object(worker.batch_store, "add_item_log") as log:
                await worker._finish_item_covers("b", "i", folder)
                update.assert_not_called()
                self.assertIn("视频不受影响", str(log.call_args))

    async def test_legacy_prep_record_does_not_start_covers(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            (folder / wb.PERSON_NAME).write_bytes(picture())
            (folder / "发布文案.txt").write_text("标题：测试", encoding="utf-8")
            with patch.object(worker, "full_auto_enabled", return_value=True), patch.object(worker, "_item", return_value={}), patch.object(worker, "_covers_ready", return_value=False), patch.object(worker, "_recorded_publish_folder", return_value=folder), patch.object(worker, "spawn_item_covers") as generate:
                await worker._finish_prep_covers("b", "i")
                generate.assert_not_called()

    async def test_model_prompt_and_validation(self):
        with tempfile.TemporaryDirectory() as raw:
            image = Path(raw) / "p.png"
            image.write_bytes(picture())
            with patch.object(batch_ai, "_chat_structured", new_callable=AsyncMock, return_value={"status": "reasonable", "reason": "自然", "fingersCutByFrame": "no"}) as chat:
                await batch_ai.judge_cover_hands(image)
                text = chat.call_args.kwargs["content"][0]["text"]
                self.assertIn("肩、肘", text)
                self.assertIn("遮挡", text)
                chat.return_value = {"status": "yes", "reason": "bad"}
                with self.assertRaises(ValueError):
                    await batch_ai.judge_cover_hands(image)
