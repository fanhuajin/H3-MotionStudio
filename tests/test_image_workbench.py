from __future__ import annotations

import asyncio
import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from backend import batch_ai
from backend import image_workbench as workbench


def png(width: int, height: int) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), (42, 56, 78)).save(output, "PNG")
    return output.getvalue()


def png_shade(width: int, height: int, shade: int) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), (shade, shade, shade)).save(output, "PNG")
    return output.getvalue()


def shade_of(path: Path) -> int:
    with Image.open(path) as image:
        return image.convert("RGB").getpixel((5, 5))[0]


async def fake_defocus(frame: Path, work: Path) -> tuple[Path, bool]:
    """糊脸那一步在单测里不做真事（它要调用视觉端点定位人脸）。"""
    return frame, True


class ImageWorkbenchTests(unittest.TestCase):
    def test_scan_recognizes_existing_outputs_without_regenerating(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            folder = root / "001_测试歌曲_123"
            folder.mkdir()
            (folder / "发布文案.txt").write_text("标题：测试歌曲\n简介：测试", encoding="utf-8")
            Image.new("RGB", (1200, 900)).save(folder / "人物图.png")
            Image.new("RGB", (1448, 1086)).save(folder / "B站4x3.png")
            with patch.object(workbench, "BATCH_OUTPUT_ROOT", root):
                rows = workbench.list_items()
            self.assertEqual(rows[0]["title"], "测试歌曲")
            self.assertIsNotNone(rows[0]["assets"]["person"])
            self.assertIsNotNone(rows[0]["assets"]["bilibili"])
            self.assertIsNone(rows[0]["assets"]["douyin"])

    def test_cover_is_normalized_to_exact_publish_size(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            saved = workbench.save_upload(folder, "douyin", png(900, 1200))
            self.assertEqual((saved["width"], saved["height"]), (1086, 1448))
            self.assertTrue((folder / "抖音3x4.png").is_file())

    def test_wrong_person_ratio_is_rejected_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            original = png(1200, 900)
            (folder / "人物图.png").write_bytes(original)
            with self.assertRaisesRegex(ValueError, "4:3"):
                workbench.save_upload(folder, "person", png(900, 1600), "singing")
            self.assertEqual((folder / "人物图.png").read_bytes(), original)

    def test_folder_id_cannot_escape_publish_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with patch.object(workbench, "BATCH_OUTPUT_ROOT", Path(raw)):
                with self.assertRaises((ValueError, FileNotFoundError)):
                    workbench.resolve_folder("2e2e2f2e2e")

    def test_person_image_keeps_the_highest_similarity_and_never_judges_hands(self) -> None:
        """人物图只按相似度重做；**手部没有自动判据**（三模型标定都不可靠，见 AGENTS.md）。"""
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            Image.new("RGB", (1080, 1920)).save(folder / "抽帧.jpg")
            attempts: list[int] = []

            async def fake_generate(*, scene_image, identity_image, prompt, ratio, output_path, **_: object):
                attempts.append(len(attempts) + 1)
                Path(output_path).write_bytes(png_shade(1152, 2048, 10 * len(attempts)))
                return Path(output_path)

            async def fake_identity(identity, candidate):
                # 第一张 50 分（不够 70）→ 重做；第二张 80 分（够）→ 留下第二张
                return {"same_person": True, "similarity": 50 if len(attempts) == 1 else 80,
                        "differences": [], "verdict": "ok"}

            async def fake_compliance(candidate):
                return True, []

            with patch.object(workbench.batch_image, "defocus_reference_face", fake_defocus), \
                 patch.object(workbench.batch_image, "generate_candidate_image", fake_generate), \
                 patch.object(workbench.batch_ai, "judge_identity", fake_identity), \
                 patch.object(workbench.batch_image, "judge_compliance_safe", fake_compliance), \
                 patch.object(workbench, "person_prompt", lambda folder, kind: "提示词"):
                score = asyncio.run(workbench._generate_person(folder, "dance"))

            self.assertEqual(len(attempts), 2)
            self.assertEqual(score, 80)
            self.assertEqual(shade_of(folder / workbench.PERSON_NAME), 20)
            self.assertFalse(hasattr(workbench.batch_ai, "judge_hands"))

    def test_person_image_prefers_a_compliant_take_over_a_higher_score(self) -> None:
        """着装自检：第一张 80 分但衣服暴露、第二张 75 分合规 → 必须留合规那张。"""
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            Image.new("RGB", (1080, 1920)).save(folder / "抽帧.jpg")
            attempts: list[int] = []

            async def fake_generate(*, scene_image, identity_image, prompt, ratio, output_path, **_: object):
                attempts.append(len(attempts) + 1)
                Path(output_path).write_bytes(png_shade(1152, 2048, 10 * len(attempts)))
                return Path(output_path)

            async def fake_identity(identity, candidate):
                return {"same_person": True, "similarity": 80 if len(attempts) == 1 else 75,
                        "differences": [], "verdict": "ok"}

            async def fake_compliance(candidate):
                if len(attempts) == 1:
                    return False, ["吊带背心", "露出事业线"]
                return True, []

            with patch.object(workbench.batch_image, "defocus_reference_face", fake_defocus), \
                 patch.object(workbench.batch_image, "generate_candidate_image", fake_generate), \
                 patch.object(workbench.batch_ai, "judge_identity", fake_identity), \
                 patch.object(workbench.batch_image, "judge_compliance_safe", fake_compliance), \
                 patch.object(workbench, "person_prompt", lambda folder, kind: "提示词"):
                score = asyncio.run(workbench._generate_person(folder, "dance"))

            self.assertEqual(len(attempts), 2)
            self.assertEqual(score, 75)
            self.assertEqual(shade_of(folder / workbench.PERSON_NAME), 20)  # 第二张（合规那张）

    def test_person_image_never_blocks_when_the_compliance_judge_fails(self) -> None:
        """判据本身失败必须按合规放行：自检是质量门槛，不是出片前提。"""
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            Image.new("RGB", (1080, 1920)).save(folder / "抽帧.jpg")
            attempts: list[int] = []

            async def fake_generate(*, scene_image, identity_image, prompt, ratio, output_path, **_: object):
                attempts.append(len(attempts) + 1)
                Path(output_path).write_bytes(png_shade(1152, 2048, 90))
                return Path(output_path)

            async def fake_identity(identity, candidate):
                return {"same_person": True, "similarity": 88, "differences": [], "verdict": "ok"}

            async def boom(candidate):
                raise RuntimeError("视觉端点不可用")

            with patch.object(workbench.batch_image, "defocus_reference_face", fake_defocus), \
                 patch.object(workbench.batch_image, "generate_candidate_image", fake_generate), \
                 patch.object(workbench.batch_ai, "judge_identity", fake_identity), \
                 patch.object(workbench.batch_ai, "judge_compliance", boom), \
                 patch.object(workbench, "person_prompt", lambda folder, kind: "提示词"):
                score = asyncio.run(workbench._generate_person(folder, "dance"))

            self.assertEqual(len(attempts), 1)  # 不因为自检挂了就重做
            self.assertEqual(score, 88)
            self.assertTrue((folder / workbench.PERSON_NAME).is_file())

    def test_compliance_note_surfaces_the_reason_in_the_status_message(self) -> None:
        """合规没过时页面要看到原因（而不是只显示一个相似度）。"""
        self.assertEqual(workbench._compliance_note({}), "")
        self.assertIn("已通过", workbench._compliance_note({"compliant": True, "issues": []}))
        note = workbench._compliance_note({"compliant": False, "issues": ["吊带背心", "网袜"]})
        self.assertIn("未通过", note)
        self.assertIn("吊带背心", note)

    def test_automation_started_at_does_not_restart_on_every_status_update(self) -> None:
        """「已用 N 秒」用后端记的开始时间算 —— 页面切走再切回来不许从 0 重新数。"""
        with patch.dict(workbench.AUTOMATIONS, {}, clear=True):
            workbench._set_auto("work", status="queued", message="已加入自动生成队列")
            started = workbench.AUTOMATIONS["work"]["startedAt"]
            time.sleep(0.02)
            workbench._set_auto("work", status="running", message="正在生成人物图")
            self.assertEqual(workbench.AUTOMATIONS["work"]["startedAt"], started)
            time.sleep(0.02)
            workbench._set_auto("work", status="completed", message="已完成生成")
            # 接着重新点一次生成 → 这是新的一轮，必须换一个开始时间
            workbench._set_auto("work", status="queued", message="已加入自动生成队列")
            self.assertGreater(workbench.AUTOMATIONS["work"]["startedAt"], started)

    def test_refreshing_publish_materials_never_deletes_a_person_image_it_cannot_replace(self) -> None:
        """2026-09-19 事故：没有候选图时 `_write_publish_image` 会把已有的人物图删掉且不补回来。"""
        from backend import batch_worker

        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            (folder / "人物图.png").write_bytes(png(1152, 2048))
            self.assertIsNone(batch_worker._write_publish_image(folder, None))
            self.assertTrue((folder / "人物图.png").is_file())

    def test_refreshing_publish_materials_swaps_the_image_and_clears_old_suffixes(self) -> None:
        from backend import batch_worker

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            folder = root / "发布"
            folder.mkdir()
            (folder / "人物图.jpg").write_bytes(png(1152, 2048))
            fresh = root / "新图.png"
            fresh.write_bytes(png_shade(1152, 2048, 200))

            written = batch_worker._write_publish_image(folder, fresh)

            self.assertEqual(written, folder / "人物图.png")
            self.assertEqual(shade_of(folder / "人物图.png"), 200)
            self.assertFalse((folder / "人物图.jpg").is_file())

    def test_prompts_forbid_broken_hands(self) -> None:
        """手部只靠提示词约束：不许自创手势、不许画崩（用户 2026-09-19）。"""
        self.assertIn("手**可以入镜**", batch_ai.VIDEO_READY_BLOCK)
        self.assertIn("禁止手指比心、剪刀手、指尖相触", batch_ai.VIDEO_READY_BLOCK)
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            (folder / "发布文案.txt").write_text("标题：测试标题\n简介：测试简介", encoding="utf-8")
            cover = workbench.prompt(folder, "bilibili", "dance")
        self.assertIn("手部硬要求", cover)
        self.assertIn("五指分明", cover)
        self.assertIn("不要自行设计新手势", cover)

    def test_manual_prompts_carry_the_outfit_compliance_rule(self) -> None:
        """手工入口（复制提示词 / 素材包）也必须带**着装**约束，且只管衣服。

        `prompt()` 走的是**用户桌面上的 4比3图片.txt / 9比16图片.txt**（存在就优先），
        所以着装约束只能写在代码里，不能只改仓库里的提示词文件。
        口径 = 用户 2026-09-20：「只要衣服不暴露就可以了；动作都不用处理，流程也不要动」。
        """
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            (folder / "发布文案.txt").write_text("标题：测试标题\n简介：测试简介", encoding="utf-8")
            person = workbench.prompt(folder, "person", "dance")
            cover = workbench.prompt(folder, "douyin", "dance")
        self.assertIn("着装必须端庄合规", person)
        self.assertIn("禁止吊带、抹胸、露肩", person)
        self.assertNotIn("姿态不得有性暗示", person)  # 动作/姿态不在本次口径内
        self.assertIn("着装合规 · 发布级硬要求", cover)
        self.assertIn("不要改得更暴露", cover)


if __name__ == "__main__":
    unittest.main()
