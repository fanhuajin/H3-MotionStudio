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
from backend import chatgpt_image
from backend import image_workbench as workbench

from automation import cover_flow
from scripts import cover_common


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

    def test_singing_and_dance_folders_are_lists_from_their_subfolders(self) -> None:
        """`/images` 的作品列表要认发布根下 `singing/` `dance/` 两层（2026-09-20 新结构）。"""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for sub, name in (("singing", "001_唱歌_测试_111"), ("dance", "001_跳舞_测试_222")):
                folder = root / sub / name
                folder.mkdir(parents=True)
                (folder / "发布文案.txt").write_text("标题：测试\n简介：测试", encoding="utf-8")
                Image.new("RGB", (1200, 900)).save(folder / "人物图.png")
            with patch.object(workbench, "BATCH_OUTPUT_ROOT", root):
                rows = workbench.list_items()
                names = [row["folderName"] for row in rows]
                # 排序：先唱歌、再跳舞；两层都要出现在列表里
                self.assertEqual(names, ["001_唱歌_测试_111", "001_跳舞_测试_222"])
                # 子目录里的作品目录仍能被 resolve_folder 打开（编号只编码目录名）
                folder_id = rows[0]["id"]
                self.assertEqual(workbench.resolve_folder(folder_id).name, "001_唱歌_测试_111")
                # 真正往外跑的编号（`../`）必须被拒
                outside = root.parent / f"外面_{root.name}"
                outside.mkdir(exist_ok=True)
                with self.assertRaises(FileNotFoundError):
                    workbench.resolve_folder((f"../{outside.name}").encode("utf-8").hex())

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

    def test_person_image_low_score_does_not_auto_regenerate(self) -> None:
        """低分只提示，默认不能暗中再出一张并产生第二笔图片费用。"""
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            Image.new("RGB", (1080, 1920)).save(folder / "抽帧.jpg")
            attempts: list[int] = []

            async def fake_generate(*, scene_image, identity_image, prompt, ratio, output_path, **_: object):
                attempts.append(len(attempts) + 1)
                Path(output_path).write_bytes(png_shade(1152, 2048, 10 * len(attempts)))
                return Path(output_path)

            async def fake_judge(identity, candidate, **_):
                return {"same_person": False, "similarity": 50, "differences": [],
                        "compliant": True, "issues": [], "verdict": "low"}

            with patch.object(workbench.batch_image, "defocus_reference_face", fake_defocus), \
                 patch.object(workbench.batch_image, "generate_candidate_image", fake_generate), \
                 patch.object(workbench.batch_ai, "judge_person_image", fake_judge), \
                 patch.object(workbench, "person_prompt", lambda folder, kind: "提示词"):
                score = asyncio.run(workbench._generate_person(folder, "dance"))

            self.assertEqual(len(attempts), 1)
            self.assertEqual(score, 50)
            self.assertEqual(shade_of(folder / workbench.PERSON_NAME), 10)
            self.assertFalse(hasattr(workbench.batch_ai, "judge_hands"))

    def test_person_image_noncompliance_does_not_auto_regenerate(self) -> None:
        """不合规只提示并保留本次结果，是否再花钱由用户明确决定。"""
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            Image.new("RGB", (1080, 1920)).save(folder / "抽帧.jpg")
            attempts: list[int] = []

            async def fake_generate(*, scene_image, identity_image, prompt, ratio, output_path, **_: object):
                attempts.append(len(attempts) + 1)
                Path(output_path).write_bytes(png_shade(1152, 2048, 10 * len(attempts)))
                return Path(output_path)

            async def fake_judge(identity, candidate, **_):
                return {"same_person": True, "similarity": 80, "differences": [],
                        "compliant": False, "issues": ["露出事业线"], "verdict": "不合规"}

            with patch.object(workbench.batch_image, "defocus_reference_face", fake_defocus), \
                 patch.object(workbench.batch_image, "generate_candidate_image", fake_generate), \
                 patch.object(workbench.batch_ai, "judge_person_image", fake_judge), \
                 patch.object(workbench, "person_prompt", lambda folder, kind: "提示词"):
                score = asyncio.run(workbench._generate_person(folder, "dance"))

            self.assertEqual(len(attempts), 1)
            self.assertEqual(score, 80)
            self.assertEqual(shade_of(folder / workbench.PERSON_NAME), 10)

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

            async def boom(identity, candidate, **_):
                raise RuntimeError("视觉端点不可用")

            with patch.object(workbench.batch_image, "defocus_reference_face", fake_defocus), \
                 patch.object(workbench.batch_image, "generate_candidate_image", fake_generate), \
                 patch.object(workbench.batch_ai, "judge_person_image", boom), \
                 patch.object(workbench, "person_prompt", lambda folder, kind: "提示词"):
                score = asyncio.run(workbench._generate_person(folder, "dance"))

            self.assertEqual(len(attempts), 1)  # 不因为自检挂了就重做
            self.assertEqual(score, -1)
            self.assertTrue((folder / workbench.PERSON_NAME).is_file())

    def test_compliance_note_surfaces_the_reason_in_the_status_message(self) -> None:
        """合规没过时页面要看到原因（而不是只显示一个相似度）。"""
        self.assertEqual(workbench._compliance_note({}), "")
        self.assertIn("已通过", workbench._compliance_note({"compliant": True, "issues": []}))
        note = workbench._compliance_note({"compliant": False, "issues": ["吊带背心", "网袜"]})
        self.assertIn("未通过", note)
        self.assertIn("吊带背心", note)

    def test_uncertain_cover_request_is_not_automatically_repeated(self) -> None:
        """进程中断后留下 pending：批量自动链路必须停住，不能再扣一笔。"""
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            (folder / ".h3-bilibili-request-pending").write_text("submitted", encoding="utf-8")
            calls = 0

            async def fake_generate(**kwargs):
                nonlocal calls
                calls += 1

            with patch.object(workbench.batch_image, "generate_candidate_image", fake_generate):
                with self.assertRaisesRegex(RuntimeError, "避免重复扣费"):
                    asyncio.run(workbench._generate_cover(folder, "bilibili", "singing"))
            self.assertEqual(calls, 0)

    def test_definite_policy_rejection_allows_later_manual_retry(self) -> None:
        """明确的内容政策 400 不能永久留下结果未知标记。"""
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            marker = folder / ".h3-bilibili-request-pending"

            async def rejected(**kwargs):
                raise workbench.batch_image.PolicyRejected("内容政策 400")

            with patch.object(workbench.batch_image, "generate_candidate_image", rejected), \
                 patch.object(workbench, "asset_path", return_value=folder / "人物图.png"), \
                 patch.object(workbench, "prompt", return_value="封面提示词"):
                with self.assertRaises(workbench.batch_image.PolicyRejected):
                    asyncio.run(workbench._generate_cover(folder, "bilibili", "singing"))
            self.assertFalse(marker.exists())

    def test_interrupted_person_request_is_not_automatically_repeated(self) -> None:
        """人物图 attempt 留有请求标记时，不再调用图片接口。"""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            output = root / "person.png"
            (root / "person_attempt1.png.request-pending").write_text("submitted", encoding="utf-8")
            calls = 0

            async def fake_generate(**kwargs):
                nonlocal calls
                calls += 1

            with patch.object(workbench.batch_image, "generate_candidate_image", fake_generate):
                with self.assertRaisesRegex(RuntimeError, "结果未确认"):
                    asyncio.run(workbench.batch_image.generate_person_image(
                        scene_image=root / "scene.png", identity_image=root / "identity.png",
                        prompt="p", ratio="4:3", output_path=output,
                    ))
            self.assertEqual(calls, 0)

    def test_workbench_keeps_person_request_marker_across_restart(self) -> None:
        """图片工作台的人物图检查点必须落在作品目录，不能随临时目录消失。"""
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            Image.new("RGB", (1080, 1920)).save(folder / "抽帧.jpg")
            checkpoint = folder / ".h3-person-work"
            checkpoint.mkdir()
            marker = checkpoint / "person_attempt1.png.request-pending"
            marker.write_text("submitted", encoding="utf-8")
            calls = 0

            async def fake_generate(**_kwargs):
                nonlocal calls
                calls += 1

            with patch.object(workbench.batch_image, "defocus_reference_face", fake_defocus), \
                 patch.object(workbench.batch_image, "generate_candidate_image", fake_generate), \
                 patch.object(workbench, "person_prompt", lambda folder, kind: "提示词"):
                with self.assertRaisesRegex(RuntimeError, "结果未确认"):
                    asyncio.run(workbench._generate_person(folder, "dance"))
            self.assertEqual(calls, 0)
            self.assertTrue(marker.is_file(), "失败后的检查点要保留给重启后的 runner")

    def test_completed_person_checkpoint_is_reused_without_another_image_call(self) -> None:
        """出图完成但尚未登记时，恢复流程直接采用落盘结果。"""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            output = root / "person.png"
            output.write_bytes(png_shade(1152, 2048, 44))
            (root / "person_attempt1.png.judge.json").write_text(
                '{"similarity": 88, "compliant": true, "issues": []}', encoding="utf-8",
            )
            calls = 0

            async def fake_generate(**_kwargs):
                nonlocal calls
                calls += 1

            report: dict[str, object] = {}
            with patch.object(workbench.batch_image, "generate_candidate_image", fake_generate):
                path, score = asyncio.run(workbench.batch_image.generate_person_image(
                    scene_image=root / "scene.png", identity_image=root / "identity.png",
                    prompt="p", ratio="4:3", output_path=output, compliance=report,
                ))
            self.assertEqual(calls, 0)
            self.assertEqual(path, output)
            self.assertEqual(score, 88)
            self.assertIs(report.get("compliant"), True)

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

    def test_saving_a_cover_marks_the_copy_locked(self) -> None:
        """封面落盘时要把这条作品的文案**持久锁死**（删封面重出不再解锁，2026-09-20）。

        `locked_publish_copy()` 以前只看"发布目录里还有没有封面文件"，而这次要做的事
        恰好是「把封面删掉重出」—— 没有这个标记，重出封面时会带上新写的标题。
        """
        from backend import batch_worker

        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            calls: list[Path] = []
            with patch.object(
                batch_worker, "mark_publish_copy_locked",
                lambda target: (calls.append(Path(target)), True)[1],
            ):
                workbench.save_upload(folder, "bilibili", png(1448, 1086))
                workbench.save_upload(folder, "person", png(1152, 2048), "dance")
        self.assertEqual(calls, [folder], "只有封面需要锁文案；人物图不该触发")

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

    def test_prompts_forbid_blurry_hands(self) -> None:
        """2026-09-20 用户：「生成的图片有的手会模糊 禁止模糊」＋「复制提示词也是复制你的」。

        手工复制与自动出图共用精简人物图提示词；封面也须保留清晰度要求。
        ⚠️ 用户 2026-09-20 追加要求**复制出去的那份和自动出图那份是同一段**：所以这里
        直接断言 `batch_ai.SHARPNESS_BLOCK` 原文出现，而不是"含某个词"——
        以后改口径只改 `batch_ai` 一处，四个入口（自动出图 / 复制提示词 / 素材包 / 封面）一起变。
        """
        block = batch_ai.SHARPNESS_BLOCK.strip()
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            (folder / "发布文案.txt").write_text("标题：测试标题\n简介：测试简介", encoding="utf-8")
            person = workbench.prompt(folder, "person", "dance")
            cover = workbench.prompt(folder, "douyin", "dance")
            # 素材包里的 提示词.txt 就是 `prompt()` 的产物，这里只验它带同一段
            zipped = workbench.material_zip(folder, "person", "dance")
        self.assertIn(block, person, "复制出去的人物图提示词必须是同一段禁止模糊口径")
        self.assertIn(block, cover, "封面提示词也要带同一段")
        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(zipped)) as archive:
            self.assertIn(block, archive.read("提示词.txt").decode("utf-8"))
        # 封面这条 CDP 链路（桌面端出封面）也要带
        self.assertIn("禁止模糊", chatgpt_image.COVER_SHARPNESS_RULE)
        for _tag, instruction, _name in chatgpt_image.COVER_TASKS:
            self.assertIn(chatgpt_image.COVER_SHARPNESS_RULE, instruction)

    def test_prompts_clean_the_background(self) -> None:
        """2026-09-21：抽帧背景里的其他人一律去掉、杂乱背景清干净；封面**只加**「不要第二个人」。

        ⚠️ 封面**不写**背景清理整段 —— 用户 2026-09-19：「封面图不需要出现 背景规则」。
        背景跟着人物图走即可（人物图那一步已经清成单人 + 干净背景，封面是拿它当唯一参考图）。
        手工入口读的是**用户桌面那份长提示词**，所以人物图这条口径也只能写在代码里。
        """
        block = batch_ai.BACKGROUND_CLEAN_BLOCK.strip()
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            (folder / "发布文案.txt").write_text("标题：测试标题\n简介：测试简介", encoding="utf-8")
            person = workbench.prompt(folder, "person", "dance")
            cover = workbench.prompt(folder, "bilibili", "dance")
            zipped = workbench.material_zip(folder, "person", "dance")
        self.assertIn(block, person, "人物图提示词必须带背景清理整段")
        self.assertIn("只保留主体一人", person)
        self.assertIn("不用强虚化", person)
        self.assertIn("画面里只能有参考图里这一个人", cover)
        self.assertNotIn(block, cover, "封面不写背景风格规则（用户 2026-09-19）")

        import zipfile

        with zipfile.ZipFile(io.BytesIO(zipped)) as archive:
            self.assertIn(block, archive.read("提示词.txt").decode("utf-8"))

        # 三条封面链路（桌面 CDP / 老 automation / 脚本出图）都只加那一句主体约束
        for _tag, instruction, _name in chatgpt_image.COVER_TASKS:
            self.assertIn("只能有参考人物图里这一个人", instruction)
        for instruction in cover_flow.COVER_TASKS.values():
            self.assertIn("只能有参考人物图里这一个人", instruction)
        self.assertIn("只能有参考图里这一个人", cover_common.PROMPT_TEMPLATE)

    def test_manual_prompts_carry_the_outfit_compliance_rule(self) -> None:
        """手工入口（复制提示词 / 素材包）也必须带**过度裸露**那三条，且按平台原文豁免吊带/露肩。

        `prompt()` 的人物图分支现在与批量备料共用短提示词。
        口径 = 小红书规则页原文（① 大面积裸露 ② 透视/裸色 ③ 紧身到凸点轮廓突出）。
        """
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw)
            (folder / "发布文案.txt").write_text("标题：测试标题\n简介：测试简介", encoding="utf-8")
            person = workbench.prompt(folder, "person", "dance")
            cover = workbench.prompt(folder, "douyin", "dance")
        self.assertIn("【服装】", person)
        self.assertIn("胸前有衣物覆盖", person)
        self.assertIn("自然精致淡妆", person)
        self.assertLess(len(person), 900)
        self.assertNotIn("姿态不得有性暗示", person)  # 动作/姿态不在本次口径内
        self.assertIn("【服装】", cover)
        self.assertIn("面料不透明", cover)


if __name__ == "__main__":
    unittest.main()
