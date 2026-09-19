"""固化 2026-09-16 与用户一起验证通过的「剪映歌词字幕」流程的关键约定。

守住的都是**实测踩出来的**行为，改动前请先读 `docs/手工剪映歌词字幕流程.md`：
  1. 每次新建工程（不复用/不点旧草稿）；
  2. 文件框用完整路径选文件 + 导入后校验素材路径（同名文件太多）；
  3. 点击必须真实鼠标 + 强切前台校验（投递消息对自绘控件无效）；
  4. 这条链路不等 pipeline_lock；
  5. 跑完不关剪映（除非显式 H3_CLOSE_JIANYING_AFTER=1）；
  6. 最后一格里程碑要收尾（否则一直挂 running）。
"""
from __future__ import annotations

import ast
import inspect
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def code_only(func) -> str:
    """只取**可执行代码**（剥掉 docstring 与注释）。

    踩过：直接 `inspect.getsource` 会把"说明为什么不再用 pipeline_lock"这类注释/文档
    也算成行为，于是测试自己误报。断言行为就必须只看代码。
    """
    tree = ast.parse(inspect.getsource(func))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


class SubtitleFlowHardeningTests(unittest.TestCase):
    """源码级断言：这些约定一旦被改回去，立刻红。"""

    def _src(self, rel: str) -> str:
        return (ROOT / rel).read_text(encoding="utf-8")

    def test_every_run_starts_a_new_project(self):
        """流程必须从「开始创作」新建工程，而不是克隆模板/点已有草稿。"""
        from backend import jianying_recognize as jr

        self.assertTrue(hasattr(jr, "start_new_project_and_import"),
                        "必须保留「开始创作 → 导入」这条入口")
        source = code_only(jr.recognize_video)
        self.assertIn("start_new_project_and_import", source)
        self.assertNotIn("clone_draft", source, "不允许再克隆模板草稿")
        self.assertNotIn("open_draft", source, "不允许再去点草稿列表")
        self.assertIn("HomePageStartProjectDesp", code_only(jr.start_new_project_and_import),
                      "新工程必须从首页「开始创作」进入")

    def test_file_dialog_uses_full_path_and_verifies_it(self):
        """文件框要填完整路径（对话框会记住上次目录），导入后必须校验素材路径。"""
        from backend import jianying_recognize as jr

        picker = code_only(jr._pick_file_in_dialog)
        self.assertIn("resolve", picker, "必须用绝对路径填文件名框")
        self.assertIn("SetValue", picker, "优先用 ValuePattern 写入完整路径")
        self.assertNotIn("ListItemControl", picker,
                         "不要再靠点缩略图选文件（会点到别的目录里的同名文件）")
        self.assertIn("{ENTER}", picker,
                      "完整路径写入后应直接确认，不能把按钮文字当成唯一入口")
        self.assertIn("%o", picker, "Enter 无效时应使用 Windows 打开助记键兜底")
        self.assertIn("wait_closed", picker, "必须验证文件框确实关闭后才能继续")
        self.assertNotIn('raise RuntimeError("文件框里找不到「打开」按钮")', picker,
                         "按钮枚举失败不能直接终止导入")
        starter = inspect.getsource(jr.start_new_project_and_import)
        self.assertIn("导入的视频不是目标文件", starter, "导入后必须比对草稿里的素材路径")

    def test_clicks_are_real_mouse_with_foreground_check(self):
        """自绘控件只认真实鼠标，且点前必须确认剪映在前台。"""
        from backend import jianying_desktop as jd
        from backend import jianying_recognize as jr

        self.assertTrue(hasattr(jd, "force_foreground_hard"),
                        "必须有可用的抢前台实现（force_foreground 会假成功）")
        clicker = code_only(jr._real_click)
        self.assertIn("force_foreground_hard", clicker)
        self.assertIn("拒绝点击", clicker, "抢不到前台必须拒绝点击，而不是照点")
        recognize_src = code_only(jr.recognize)
        self.assertIn("_real_click", recognize_src)
        self.assertNotIn("post_click", recognize_src, "识别三步不能用投递消息点击")

    def test_subtitle_does_not_wait_for_the_pipeline_lock(self):
        """这条链路只用剪映 + ffmpeg，不等全局流水线锁（用户 2026-09-16 明确要求）。"""
        from backend import subtitle_step

        source = code_only(subtitle_step.run_subtitle_job)
        self.assertNotIn("pipeline_lock", source)
        self.assertFalse(hasattr(subtitle_step, "pipeline_lock"),
                         "连 import 都要去掉，避免又有人拿它去串行化字幕")

    def test_jianying_is_not_closed_after_a_run(self):
        """跑完不关剪映（用户要求：「自动运行的时候剪映不要关掉」）。"""
        from backend import jianying_recognize as jr
        from backend import subtitle_step

        closer = code_only(subtitle_step.close_jianying_quietly)
        self.assertIn("H3_CLOSE_JIANYING_AFTER", closer)
        self.assertIn("return False", closer, "默认必须是不关")
        self.assertIn("H3_KEEP_JIANYING", code_only(jr.recognize_video))

    def test_last_milestone_gets_closed(self):
        """烧录结束要有 on_done 收尾，否则第三格永远挂在 running。"""
        from backend import subtitle_step

        burn_src = inspect.getsource(subtitle_step.burn_for_video)
        self.assertIn("on_done: StageHook | None = None", burn_src, "批量链路不传时行为不变")
        self.assertIn("on_done(", code_only(subtitle_step.burn_for_video),
                      "烧录后必须回调收尾")
        self.assertIn("on_done=", code_only(subtitle_step._run_subtitle_once))
        done = code_only(subtitle_step._done_hook)
        self.assertIn("set_milestone", done)
        self.assertIn("completed", done)

    def test_cli_entry_exists_and_uses_the_backend(self):
        """固化后的正式入口必须存在，且直接复用 backend 的实现（不另抄一份流程）。"""
        cli = ROOT / "scripts" / "add_subtitles.py"
        self.assertTrue(cli.is_file(), "缺少 scripts/add_subtitles.py")
        source = cli.read_text(encoding="utf-8")
        self.assertIn("start_new_project_and_import", source)
        self.assertIn("burn_for_video", source)
        self.assertIn("最终成片_字幕版.mp4", source, "交付文件名必须与两条链路一致")


class PublishBatchScanTests(unittest.TestCase):
    """一键批量：扫发布根只挑「缺字幕版的唱歌视频」（已存在字幕版/跳舞/无成片都跳过）。"""

    def _make_root(self):
        import tempfile
        return Path(tempfile.mkdtemp(prefix="h3_subtitle_batch_"))

    def test_scans_only_singing_folders_missing_subtitles(self):
        """只挑：目录名带「唱歌」+ 有无字幕成片 + 还没有字幕版。"""
        from backend import subtitle_step

        root = self._make_root()
        try:
            # 唱歌 + 有成片 + 无字幕版 → 要生成
            singing = root / "001_唱歌_夜空中最亮的星_7686514674456574953"
            singing.mkdir()
            (singing / "最终成片.mp4").write_bytes(b"v")
            # 唱歌 + 已存在字幕版 → 跳过
            done = root / "002_唱歌_开始懂了_7685337147331345894"
            done.mkdir()
            (done / "最终成片.mp4").write_bytes(b"v")
            (done / "最终成片_字幕版.mp4").write_bytes(b"s")
            # 跳舞 → 跳过
            dance = root / "003_跳舞_随性摇摆_1234567890123456789"
            dance.mkdir()
            (dance / "最终成片.mp4").write_bytes(b"v")
            # 唱歌但没有无字幕成片 → 跳过
            no_clean = root / "004_唱歌_只有字幕版_9876543210987654321"
            no_clean.mkdir()
            (no_clean / "最终成片_字幕版.mp4").write_bytes(b"s")

            jobs = subtitle_step.scan_singing_publish_videos(root=root)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["folder"], "001_唱歌_夜空中最亮的星_7686514674456574953")
            self.assertEqual(jobs[0]["out"], str(singing / "最终成片_字幕版.mp4"))
            # 歌名从目录名「唱歌标签后、作品号前」抽出
            self.assertEqual(jobs[0]["song"], "夜空中最亮的星")
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_empty_or_missing_root(self):
        """发布根不存在 → 空列表，不报错。"""
        from backend import subtitle_step
        root = self._make_root()
        try:
            self.assertEqual(subtitle_step.scan_singing_publish_videos(root=root), [])
            self.assertEqual(subtitle_step.scan_singing_publish_videos(root=root / "nope"), [])
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_batch_endpoint_and_pause_resume_are_wired(self):
        """批量端点、暂停、继续都已注册到 FastAPI。"""
        import backend.app as app

        paths = [route.path for route in app.app.routes]
        self.assertIn("/api/jobs/subtitle/publish-batch", paths)
        self.assertIn("/api/jobs/{job_id}/pause", paths)
        self.assertIn("/api/jobs/{job_id}/resume", paths)
        from backend import subtitle_step
        self.assertTrue(hasattr(subtitle_step, "run_publish_subtitle_batch"))
        self.assertTrue(hasattr(subtitle_step, "request_publish_batch_pause"))
        self.assertTrue(hasattr(subtitle_step, "resume_publish_batch"))


if __name__ == "__main__":
    unittest.main()
