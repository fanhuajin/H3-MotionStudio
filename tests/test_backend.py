from contextlib import contextmanager
from pathlib import Path
import asyncio
import base64
import json
import os
import re
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

from backend.app import douyin_job_payload
from backend import batch_ai, batch_image, settings
from backend.batch_portrait import prepare_portrait_workflow
from backend.batch_worker import (
    _image_provider,
    default_action_plan,
    new_batch_state,
    unique_urls,
)
from backend.douyin_preview import _convert_download_sync
from backend.douyin_service import (
    DOUYIN_URL,
    DouyinServiceManager,
    _cookie_ready,
    _extract_aweme_id,
    is_douyin_url,
)
from backend.settings import (
    CLEAN_WORKFLOW,
    MIGRATE_WORKFLOW,
    SINGING_WORKFLOW,
    UPSCALE_WORKFLOW,
    canvas_params,
    required_paths,
    singing_canvas_params,
)
from backend.store import format_elapsed, migrate_milestones, now_iso
from backend.workflows import (
    graph_to_api_prompt,
    node_by_id,
    patch_h3_lyrics_canvas,
    patch_wan_chunk_feedforward,
    prepare_clean_workflow,
    prepare_migrate_workflow,
    prepare_singing_workflow,
    prepare_upscale_workflow,
)


# 测试绝不许写到真实发布目录（`E:\AI_Exports\H3-MotionStudio\发布成品`）：2026-09-13
# 实测 `test_batch_reuses_previous_prep_for_the_same_aweme` 忘了换发布根，真的在用户
# 的发布目录里留下一个 `001_上一次的标题_<作品号>` 目录（标题/简介都是测试假数据）。
# 所以整个测试模块统一把发布根钉到临时目录，单个用例要断言目录结构时再自行覆盖。
_TEST_PUBLISH_ROOT = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
_PUBLISH_ROOT_PATCH = patch(
    "backend.batch_worker.BATCH_OUTPUT_ROOT", Path(_TEST_PUBLISH_ROOT.name) / "发布成品"
)

# 测试**绝不许真的关机**（2026-09-15 新加的「全部完成后自动关机」）：整个测试模块把
# 「调用系统关机 / 撤销关机」换成记录桩，任何用例都不可能碰到真正的 `shutdown.exe`；
# 想断言命令行的那条用例用下面保存的真实实现 + patch(subprocess.Popen)（依旧不会真关）。
from backend import batch_worker as _batch_worker_module  # noqa: E402

_REAL_LAUNCH_SHUTDOWN = _batch_worker_module.launch_shutdown
_REAL_ABORT_SHUTDOWN = _batch_worker_module.abort_shutdown
_SHUTDOWN_CALLS: list[int] = []
_ABORT_CALLS: list[str] = []


def _stub_launch_shutdown(seconds: int) -> bool:
    _SHUTDOWN_CALLS.append(int(seconds))
    return True


def _stub_abort_shutdown() -> bool:
    _ABORT_CALLS.append("abort")
    return True


_SHUTDOWN_SAFETY_PATCHES = (
    patch.object(_batch_worker_module, "launch_shutdown", _stub_launch_shutdown),
    patch.object(_batch_worker_module, "abort_shutdown", _stub_abort_shutdown),
)


def setUpModule() -> None:
    _PUBLISH_ROOT_PATCH.start()
    for patcher in _SHUTDOWN_SAFETY_PATCHES:
        patcher.start()
    _install_stopall_guard()


def tearDownModule() -> None:
    _remove_stopall_guard()
    for patcher in reversed(_SHUTDOWN_SAFETY_PATCHES):
        patcher.stop()
    _PUBLISH_ROOT_PATCH.stop()
    _TEST_PUBLISH_ROOT.cleanup()


# ⚠️ 2026-09-20 实测事故：`DirectDownloadResumeTests.setUp` 里的 `self.addCleanup(patch.stopall)`
# 会把**整个模块**的安全补丁一起停掉。而测试类按类名字母序执行，`WorkflowPreparationTests`（W）
# 与 `SubtitleStepTests`（S）都在它后面 —— 于是那两处真的写进了用户的发布根
# `E:\AI_Exports\H3-MotionStudio\发布成品`，实测留下 12 个空的
# `001/003_唱歌_等待处理_<条目id>` 目录 + 2 个只含 3 字节假成片的 `字幕_*_subtitle`
# （14 个，全部已删）。修法两层：① 去掉那处 stopall；② 这里给 `patch.stopall` 装一道保险，
# 停完立刻把发布根与关机桩重新装上 —— 以后谁再写一句 stopall 也不会漏到真实目录。
_GUARD_ACTIVE = False
_REAL_STOPALL = patch.stopall


def _safe_stopall() -> None:
    _REAL_STOPALL()
    if _GUARD_ACTIVE:
        _PUBLISH_ROOT_PATCH.start()
        for patcher in _SHUTDOWN_SAFETY_PATCHES:
            patcher.start()


def _install_stopall_guard() -> None:
    global _GUARD_ACTIVE
    patch.stopall = _safe_stopall
    _GUARD_ACTIVE = True


def _remove_stopall_guard() -> None:
    global _GUARD_ACTIVE
    _GUARD_ACTIVE = False
    patch.stopall = _REAL_STOPALL


@contextmanager
def process_env_only():
    """让环境读取只认进程环境（屏蔽 Windows 用户级变量，本机确实配了一堆）。

    两层都要换，否则不生效：
      · `settings.env_value` 本体（各模块 `from .settings import env_value` 的用法）；
      · 各模块里的 `settings` 引用（`settings.env_value(...)` 的用法，如 `chatgpt_image`）。
    """
    from types import SimpleNamespace

    def fake(name: str, default: str = "") -> str:
        # 三个图片自动化开关是**这一轮专门要关掉**的东西：进程环境与用户级变量里
        # 都可能还留着历史值 `1`（实测本机两处都是 1），所以这里**无条件**按「没配」处理，
        # 断言验的就是代码默认值 —— 否则测试结果会随机器状态飘。
        if name in {"H3_AUTO_CHATGPT_IMAGE", "H3_AUTO_CHATGPT_COVER", "H3_AUTO_CONFIRM"}:
            return "0"
        return (os.environ.get(name) or "").strip() or default

    stub = SimpleNamespace(env_value=fake)
    with patch("backend.settings.env_value", fake), patch(
        "backend.batch_image.env_value", fake
    ), patch("backend.batch_worker.env_value", fake), patch(
        "backend.batch_ai.env_value", fake
    ), patch("backend.chatgpt_image.settings", stub), patch(
        "backend.chatgpt_cdp.settings", stub, create=True
    ):
        yield


class _BoxStore:
    """跟着一个 `{"state": ...}` 盒子走的批量状态存储（runner 单测用）。

    与 run_batch 交互所需的最小面：get / update / mutate / mutate_item /
    set_item_milestone / add_item_log。
    """

    def __init__(self, box: dict) -> None:
        self.box = box

    def get(self, _batch_id):
        return self.box["state"]

    def update(self, _batch_id, **changes):
        self.box["state"].update(changes)
        return self.box["state"]

    def mutate(self, _batch_id, mutator):
        mutator(self.box["state"])
        return self.box["state"]

    def mutate_item(self, _batch_id, item_id, mutator):
        for item in self.box["state"]["items"]:
            if item["id"] == item_id:
                mutator(item)
        return self.box["state"]

    def set_item_milestone(self, _batch_id, item_id, milestone_id, **changes):
        for item in self.box["state"]["items"]:
            if item["id"] == item_id:
                for milestone in item["milestones"]:
                    if milestone["id"] == milestone_id:
                        milestone.update(changes)
        return self.box["state"]

    def add_item_log(self, _batch_id, item_id, message):
        for item in self.box["state"]["items"]:
            if item["id"] == item_id:
                item.setdefault("logs", []).append({"time": "t", "message": message})
        return self.box["state"]


class PublishCopyLockTests(unittest.TestCase):
    """封面一旦出好，发布文案就锁住 —— 否则「重新开始」改标题会把封面全部作废。

    用户 2026-09-19：「都用原来的啊 不然封面都得改」（019 的标题在一次「重新开始」里
    从「成熟与天真之间，刚好是我」被改成「成熟里藏着天真的一支舞」，两张封面当场作废）。
    """

    def _locked(self, root, *, with_cover: bool, recorded: bool = True):
        folder = root / "019_跳舞_旧标题_123"
        folder.mkdir(exist_ok=True)
        if with_cover:
            (folder / "B站4x3.png").write_bytes(b"cover")
        item = {
            "title": "旧标题",
            "awemeId": "123",
            "ai": {"title": "旧标题", "introduction": "旧简介", "tags": ["a", "b"]},
            "outputs": {"folder": str(folder)} if recorded else {},
        }
        with patch.object(_batch_worker_module, "BATCH_OUTPUT_ROOT", root):
            return _batch_worker_module.locked_publish_copy(item)

    def test_copy_is_locked_when_a_cover_is_already_published(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            locked = self._locked(Path(raw), with_cover=True)
        self.assertIsNotNone(locked)
        self.assertEqual(locked["title"], "旧标题")
        self.assertEqual(locked["introduction"], "旧简介")
        self.assertEqual(locked["tags"], ["a", "b"])

    def test_copy_stays_editable_when_no_cover_exists_yet(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            self.assertIsNone(self._locked(Path(raw), with_cover=False))

    def test_deleting_the_covers_does_not_unlock_the_copy(self) -> None:
        """删封面去重出**不许**顺手解锁文案（2026-09-20 用户要求重出手部模糊的图）。

        以前 `locked_publish_copy()` 只看发布目录里还有没有封面文件 —— 把封面删掉重出，
        锁就没了，重出封面时会带上新写的标题。现在封面落盘会在条目里留下 `ai.copyLocked`，
        删文件不再影响它。
        """
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            folder = root / "019_跳舞_旧标题_123"
            folder.mkdir()
            item = {
                "title": "旧标题",
                "awemeId": "123",
                "ai": {
                    "title": "旧标题", "introduction": "旧简介", "tags": ["a"],
                    "copyLocked": True,
                },
                "outputs": {"folder": str(folder)},
            }
            with patch.object(_batch_worker_module, "BATCH_OUTPUT_ROOT", root):
                locked = _batch_worker_module.locked_publish_copy(item)
            self.assertIsNotNone(locked, "封面删掉了，但文案的锁必须还在")
            self.assertEqual(locked["title"], "旧标题")


class FullAutoImageTests(unittest.TestCase):
    """全自动链路：备料直接出图、封面走中转站、自动放行 —— 但**已经有的绝不重出**。

    用户 2026-09-19：「全自动吧。不要我去备料了。现在直接用api生成」＋
    「对于已经有的不要重复得去生成」。
    """

    def _item(self, *, image="", covers=(), copy=True):
        root = Path(tempfile.mkdtemp(prefix="h3-fullauto-")) / "发布成品"
        self.addCleanup(shutil.rmtree, root.parent, ignore_errors=True)
        folder = root / "001_跳舞_标题_7674846165070563451"
        folder.mkdir(parents=True)
        if copy:
            (folder / "发布文案.txt").write_text("标题：\n标题\n\n简介：\n简介\n", encoding="utf-8")
        if image:
            (folder / "人物图.png").write_bytes(b"person")
        for name in covers:
            (folder / name).write_bytes(b"cover")
        item = {
            "id": "item-1",
            "kind": "dance",
            "awemeId": "7674846165070563451",
            "ai": {"title": "标题"},
            "outputs": {"folder": str(folder)},
        }
        return root, folder, item

    def test_existing_covers_are_never_regenerated(self) -> None:
        from backend import batch_worker

        root, folder, item = self._item(image="x", covers=("B站4x3.png", "抖音3x4.png"))
        with patch.object(batch_worker, "BATCH_OUTPUT_ROOT", root), patch.object(
            batch_worker, "_item", lambda *_: item
        ):
            self.assertFalse(batch_worker.spawn_api_covers("b", "item-1"))

    def test_missing_cover_is_scheduled_without_touching_the_existing_one(self) -> None:
        from backend import batch_worker

        root, folder, item = self._item(image="x", covers=("B站4x3.png",))
        created: list[str] = []

        def fake_create_task(coro):
            created.append(getattr(coro, "__qualname__", "task"))
            coro.close()
            return type("T", (), {"add_done_callback": lambda self, cb: cb(self)})()

        with patch.object(batch_worker, "BATCH_OUTPUT_ROOT", root), patch.object(
            batch_worker, "_item", lambda *_: item
        ), patch.object(batch_worker.asyncio, "create_task", fake_create_task):
            self.assertTrue(batch_worker.spawn_api_covers("b", "item-1"))
        self.assertEqual(len(created), 1)

    def test_same_item_cannot_queue_two_cover_tasks(self) -> None:
        """备料与出片前体检同时触发时，同一条只能留一个封面任务。"""
        from backend import batch_worker

        root, folder, item = self._item(image="x")
        batch_worker._COVER_ITEM_KEYS.clear()
        self.addCleanup(batch_worker._COVER_ITEM_KEYS.clear)
        self.addCleanup(batch_worker._COVER_TASKS.clear)

        class PendingTask:
            def add_done_callback(self, callback):
                self.callback = callback

        def fake_create_task(coro):
            coro.close()
            return PendingTask()

        with patch.object(batch_worker, "BATCH_OUTPUT_ROOT", root), patch.object(
            batch_worker, "_item", lambda *_: item
        ), patch.object(batch_worker.asyncio, "create_task", fake_create_task):
            self.assertTrue(batch_worker.spawn_api_covers("b", "item-1"))
            self.assertFalse(batch_worker.spawn_api_covers("b", "item-1"))

    def test_cover_task_skips_a_cover_that_is_already_there(self) -> None:
        """任务真的跑起来时也要逐张跳过已有的那张（不能因为另一张缺就两张都重出）。"""
        from backend import batch_worker, image_workbench

        root, folder, item = self._item(image="x", covers=("B站4x3.png",))
        calls: list[str] = []
        item["milestones"] = [{"id": "prepare", "status": "running"}]
        milestones: list[str] = []

        async def fake_generate(folder_arg, stage, kind):
            calls.append(stage)
            self.assertEqual(item.get("coverStage"), stage)
            self.assertTrue(item.get("coverStartedAt"))

        def set_item(_batch, _item_id, **changes):
            item.update(changes)

        def set_milestone(_batch, _item_id, _step, **changes):
            milestones.append(str(changes.get("currentNode")))

        with patch.object(batch_worker, "BATCH_OUTPUT_ROOT", root), patch.object(
            batch_worker, "_item", lambda *_: item
        ), patch.object(batch_worker.batch_store, "add_item_log", lambda *a, **k: None), patch.object(
            image_workbench, "_generate_cover", fake_generate
        ), patch.object(batch_worker, "_set_item", set_item), patch.object(
            batch_worker.batch_store, "set_item_milestone", set_milestone
        ):
            asyncio.run(batch_worker._generate_covers_via_api("b", "item-1", folder))

        self.assertEqual(calls, ["douyin"])  # B站那张已经在 → 只出抖音
        self.assertEqual(milestones, ["正在生成抖音封面"])
        self.assertIsNone(item.get("coverStage"))

    def test_prepare_reuses_an_existing_person_image(self) -> None:
        """备料时已经有候选人物图 → 直接沿用，绝不重复烧出图钱（源码级：生成前先看 previous_image）。"""
        source = Path(_batch_worker_module.__file__).read_text(encoding="utf-8")
        marker = 'if provider in {"api", "local"} and mode != "image" and previous_image.is_file():'
        self.assertIn(marker, source)
        # 全自动开着时，来源必须是中转站（而不是「只备料」）
        with process_env_only(), patch.object(
            _batch_worker_module.batch_image, "configured", lambda: True
        ):
            self.assertEqual(_image_provider(), "api")

    def test_publish_folder_image_is_adopted_instead_of_regenerated(self) -> None:
        """发布目录里已经有出好的人物图 → 收编成候选图，**不重新出图**（用户：「已经有的不要重复得去生成」）。"""
        from backend import batch_worker

        root, folder, item = self._item(image="x")
        work = Path(tempfile.mkdtemp(prefix="h3-adopt-"))
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        with patch.object(batch_worker, "BATCH_OUTPUT_ROOT", root), patch.object(
            batch_worker.batch_store, "add_item_log", lambda *a, **k: None
        ):
            adopted = batch_worker._adopt_published_image("b", "item-1", item, work, 0)
        self.assertIsNotNone(adopted)
        self.assertEqual(Path(adopted).read_bytes(), b"person")
        self.assertEqual(Path(adopted).parent, work)

    def test_nothing_to_adopt_when_the_publish_folder_has_no_image(self) -> None:
        from backend import batch_worker

        root, folder, item = self._item()  # 没有人物图
        work = Path(tempfile.mkdtemp(prefix="h3-adopt-"))
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        with patch.object(batch_worker, "BATCH_OUTPUT_ROOT", root), patch.object(
            batch_worker.batch_store, "add_item_log", lambda *a, **k: None
        ):
            self.assertIsNone(batch_worker._adopt_published_image("b", "item-1", item, work, 0))


class WorkflowPreparationTests(unittest.TestCase):
    def test_batch_state_is_serial_and_requires_review_before_video(self) -> None:
        singing = "https://v.douyin.com/song"
        dance = "https://www.douyin.com/video/123"
        state = new_batch_state([singing, singing], [dance])
        self.assertEqual([item["kind"] for item in state["items"]], ["singing", "dance"])
        self.assertEqual(state["total"], 2)
        self.assertEqual(state["deletedCount"], 0)
        self.assertTrue(all(item["reviewApproved"] is False for item in state["items"]))
        self.assertTrue(all(next(step for step in item["milestones"] if step["id"] == "video")["status"] == "pending" for item in state["items"]))
        self.assertEqual(unique_urls([singing, "", singing]), [singing])

    def test_batch_item_ratio_defaults_by_kind_and_group_override(self) -> None:
        """每条视频单独存比例：歌曲默认 4:3、跳舞默认 9:16，分组选择可覆盖默认值。"""
        from backend.batch_worker import item_ratio, ratio_hint
        from backend.settings import normalize_batch_ratio

        state = new_batch_state(["https://v.douyin.com/song"], ["https://www.douyin.com/video/1"])
        self.assertEqual([item["ratio"] for item in state["items"]], ["4:3", "9:16"])

        custom = new_batch_state(
            ["https://v.douyin.com/song"], ["https://www.douyin.com/video/1"],
            singing_ratio="9:16", dance_ratio="4:3",
        )
        self.assertEqual([item["ratio"] for item in custom["items"]], ["9:16", "4:3"])

        # 历史批次没有 ratio 字段 → 按类型默认值兜底，行为与旧版一致
        self.assertEqual(item_ratio({"kind": "singing", "ratio": None}), "4:3")
        self.assertEqual(item_ratio({"kind": "dance"}), "9:16")
        self.assertEqual(item_ratio({"kind": "singing", "ratio": "怪值"}), "4:3")

        self.assertEqual(normalize_batch_ratio("", "dance"), "9:16")
        with self.assertRaises(ValueError):
            normalize_batch_ratio("16:9", "singing")
        # 提示文案按链路给不同生成分辨率
        self.assertIn("480×864", ratio_hint("singing", "9:16"))
        self.assertIn("512×896", ratio_hint("dance", "9:16"))

    def test_batch_item_ratio_rewrites_material_prompt_and_locks_after_confirm(self) -> None:
        """改比例要按新比例重拼出图提示词；已出片/已结束的条目拒绝修改。"""
        from backend import batch_worker

        captured: dict = {}
        calls: list[str] = []

        class StubStore:
            def __init__(self) -> None:
                self.state = {
                    "items": [
                        {
                            "id": "it1",
                            "kind": "singing",
                            "ratio": "4:3",
                            "status": "awaiting_review",
                            "ai": {"imagePrompt": "旧提示词", "song_name": "爱如潮水", "style_source": "video"},
                        },
                        {"id": "it2", "kind": "dance", "ratio": "9:16", "status": "confirmed", "ai": {}},
                    ]
                }

            def get(self, _batch_id):
                return self.state

            def mutate_item(self, _batch_id, item_id, mutator):
                for item in self.state["items"]:
                    if item["id"] == item_id:
                        mutator(item)
                return self.state

            def add_item_log(self, _batch_id, _item_id, message):
                calls.append(message)

        def fake_compose(kind, style_source, feedback, mode, song_name="", song_mood="", ratio=""):
            captured.update(kind=kind, ratio=ratio)
            return f"提示词-{ratio}"

        stub = StubStore()
        originals = (batch_worker.batch_store, batch_worker.batch_ai.compose_image_prompt)
        with tempfile.TemporaryDirectory() as folder:
            try:
                batch_worker.batch_store = stub
                batch_worker.batch_ai.compose_image_prompt = fake_compose
                with patch.object(batch_worker, "DATA_DIR", Path(folder)):
                    batch_worker.set_item_ratio("b1", "it1", "9:16")

                    self.assertEqual(captured, {"kind": "singing", "ratio": "9:16"})
                    self.assertEqual(stub.state["items"][0]["ratio"], "9:16")
                    self.assertEqual(stub.state["items"][0]["ai"]["imagePrompt"], "提示词-9:16")
                    self.assertTrue(any("画布比例已改为" in message for message in calls))
                    # 备料提示词落盘也跟着换成新比例，用户拿到的素材与最终成片一致
                    written = (Path(folder) / "batches" / "b1" / "it1" / "出图提示词.txt").read_text(encoding="utf-8")
                    self.assertEqual(written, "提示词-9:16")

                    # 用户 2026-09-14：「未开始前的任务都允许修改」——已确认但还没开始出片的
                    # 条目（confirmed）也能改，改了之后提交的就是新比例
                    batch_worker.set_item_ratio("b1", "it2", "4:3")
                    self.assertEqual(stub.state["items"][1]["ratio"], "4:3")

                    # 正在出片 / 已出片：锁定，必须先点「回到确认」
                    for blocked in ("running", "revising", "completed"):
                        stub.state["items"][1]["status"] = blocked
                        with self.assertRaises(ValueError):
                            batch_worker.set_item_ratio("b1", "it2", "9:16")
                    with self.assertRaises(ValueError):
                        batch_worker.set_item_ratio("b1", "it1", "16:9")  # 非法比例
            finally:
                batch_worker.batch_store, batch_worker.batch_ai.compose_image_prompt = originals
        self.assertEqual(stub.state["items"][1]["ratio"], "4:3")

    def test_batch_items_can_be_appended_anytime(self) -> None:
        """批次随时能加任务：跑着的、暂停的、刚做完的都能往队尾追加，重复链接自动跳过。"""
        from backend import batch_worker
        from backend.app import app

        state = new_batch_state(["https://v.douyin.com/song"], ["https://www.douyin.com/video/1"])
        state["status"] = "completed"   # 批次已经跑完也能继续加

        class StubStore:
            def __init__(self, payload: dict) -> None:
                self.state = payload

            def get(self, _batch_id):
                return self.state

            def mutate(self, _batch_id, mutator):
                mutator(self.state)
                return self.state

        stub = StubStore(state)
        with patch.object(batch_worker, "batch_store", stub):
            result = batch_worker.append_batch_items(
                "b1", ["https://v.douyin.com/song2", "https://v.douyin.com/song"], []
            )
            # 同类型同链接已经存在（未删除）→ 跳过；song2 追加成功
            self.assertEqual((result["added"], result["duplicates"]), (1, 1))
            items = stub.state["items"]
            self.assertEqual([item["index"] for item in items], [1, 2, 3])
            self.assertEqual(items[2]["url"], "https://v.douyin.com/song2")
            self.assertEqual(items[2]["ratio"], "4:3")           # 歌曲默认 4:3
            self.assertEqual(items[2]["status"], "pending")
            self.assertEqual(stub.state["total"], 3)
            self.assertIsNone(stub.state["finishedAt"])           # 追加后清掉完成时间

            # 空链接必须报错，不能悄悄什么都不做
            with self.assertRaises(ValueError):
                batch_worker.append_batch_items("b1", [], [])

            # 单批上限
            full = new_batch_state([f"https://v.douyin.com/s{i}" for i in range(50)], [])
            stub.state = full
            with self.assertRaises(ValueError):
                batch_worker.append_batch_items("b1", ["https://v.douyin.com/one-more"], [])

        paths = {getattr(route, "path", "") for route in app.routes}
        self.assertIn("/api/batches/{batch_id}/items", paths)           # 随时追加
        self.assertIn("/api/batches/{batch_id}/items/{item_id}", paths)  # 删除单条

    def test_append_endpoint_queues_without_auto_start(self) -> None:
        """追加只排队：跑完 / 暂停的批次都不会因为「加任务」被自动跑起来。"""
        import asyncio

        from backend import app as app_module
        from backend import batch_worker

        state = new_batch_state(["https://v.douyin.com/song"], [])
        state["status"] = "completed"

        class StubStore:
            def __init__(self, payload: dict) -> None:
                self.state = payload

            def get(self, _batch_id):
                return self.state

            def update(self, _batch_id, **changes):
                self.state.update(changes)
                return self.state

            def mutate(self, _batch_id, mutator):
                mutator(self.state)
                return self.state

        stub = StubStore(state)
        spawned: list = []
        with patch.object(batch_worker, "batch_store", stub), patch.object(
            app_module, "batch_store", stub
        ), patch.object(app_module, "spawn", lambda coro: (spawned.append(coro), coro.close())):
            result = asyncio.run(
                app_module.append_batch_items_endpoint(
                    "b1", app_module.BatchAppendRequest(singingUrls=["https://v.douyin.com/song2"])
                )
            )
            self.assertEqual(result["status"], "completed")   # 只入队，等用户点「启动」
            self.assertIn("已加入 1 条", result["notice"])
            self.assertEqual(len(stub.state["items"]), 2)
            self.assertEqual(spawned, [])

            # 暂停中的批次：加进去但保持暂停，也不唤醒 runner
            stub.state["status"] = "paused"
            result = asyncio.run(
                app_module.append_batch_items_endpoint(
                    "b1", app_module.BatchAppendRequest(singingUrls=["https://v.douyin.com/song3"])
                )
            )
            self.assertEqual(result["status"], "paused")
            self.assertIn("暂停", result["notice"])
            self.assertEqual(len(stub.state["items"]), 3)
            self.assertEqual(spawned, [])

            # 取消的批次不能复活
            stub.state["status"] = "cancelled"
            with self.assertRaises(app_module.HTTPException):
                asyncio.run(
                    app_module.append_batch_items_endpoint(
                        "b1", app_module.BatchAppendRequest(singingUrls=["https://v.douyin.com/song4"])
                    )
                )

    def test_duplicate_links_and_works_are_filtered_out(self) -> None:
        """重复的必须过滤掉：链接写法不同也算同一条，短链/完整链接则靠作品号兜底。"""
        from backend import batch_worker
        from backend.batch_worker import DuplicateItem, duplicate_item_by_aweme, item_key, url_key

        # 1) 链接指纹：大小写、结尾斜杠、跟踪参数都不算新任务
        self.assertEqual(url_key("HTTPS://V.Douyin.com/AbC/?from=share#x"), url_key("https://v.douyin.com/AbC"))
        self.assertEqual(item_key("singing", "https://v.douyin.com/a/"), item_key("singing", "https://v.douyin.com/a"))
        # 作品号在 query 里（抖音「喜欢列表」链接）必须保留，否则不同视频会被误判成同一条
        likes_a = "https://www.douyin.com/user/self?from_tab_name=main&modal_id=7663001746131065849&showTab=like"
        likes_b = "https://www.douyin.com/user/self?from_tab_name=main&modal_id=7660090995610134771&showTab=like"
        self.assertNotEqual(url_key(likes_a), url_key(likes_b))
        self.assertEqual(
            url_key(likes_a),
            url_key("https://www.douyin.com/user/self?from_tab_name=other&modal_id=7663001746131065849"),
        )
        # 同一段素材当歌曲和当跳舞是两件事，不能互相吃掉
        self.assertNotEqual(item_key("singing", "https://v.douyin.com/a"), item_key("dance", "https://v.douyin.com/a"))

        state = new_batch_state(["https://v.douyin.com/song"], [])

        class StubStore:
            def __init__(self, payload: dict) -> None:
                self.state = payload

            def get(self, _batch_id):
                return self.state

            def update(self, _batch_id, **changes):
                self.state.update(changes)
                return self.state

            def mutate(self, _batch_id, mutator):
                mutator(self.state)
                return self.state

        stub = StubStore(state)
        with patch.object(batch_worker, "batch_store", stub):
            # 写法不同但同一条 → 跳过
            result = batch_worker.append_batch_items(
                "b1", ["https://v.douyin.com/song/?from=share"], []
            )
            self.assertEqual((result["added"], result["duplicates"]), (0, 1))
            self.assertEqual(len(stub.state["items"]), 1)
            # 一次贴进来两条一样的 → 只留一条
            result = batch_worker.append_batch_items(
                "b1", ["https://v.douyin.com/new", "https://v.douyin.com/new/"], []
            )
            self.assertEqual((result["added"], result["duplicates"]), (1, 1))
            self.assertEqual(len(stub.state["items"]), 2)

            # 2) 作品号兜底：短链与 www.douyin.com/video/{id} 是同一个作品
            kept, dup = stub.state["items"][0], stub.state["items"][1]
            kept["awemeId"] = "7300000000000000000"
            dup["awemeId"] = "7300000000000000000"
            dup["kind"] = kept["kind"]
            self.assertEqual(
                duplicate_item_by_aweme("b1", dup["id"], "7300000000000000000", kept["kind"])["id"],
                kept["id"],
            )
            # 不同类型（同一素材既做歌曲又做跳舞）不算重复
            self.assertIsNone(
                duplicate_item_by_aweme("b1", dup["id"], "7300000000000000000", "dance")
            )
            # 用户明确不要的（跳过/删除）不算重复，可以重新加回来
            kept["status"] = "skipped"
            self.assertIsNone(
                duplicate_item_by_aweme("b1", dup["id"], "7300000000000000000", kept["kind"])
            )
            kept["status"] = "completed"
            self.assertIsNotNone(
                duplicate_item_by_aweme("b1", dup["id"], "7300000000000000000", kept["kind"])
            )
            self.assertIsNone(duplicate_item_by_aweme("b1", dup["id"], "", kept["kind"]))

        # 3) 下载后才发现重复：标成「已跳过」，不当失败，也不拦住后面的条目
        import asyncio

        run_state = new_batch_state(["https://v.douyin.com/one"], ["https://v.douyin.com/two"])
        box = {"state": run_state}
        first_id = run_state["items"][0]["id"]

        class RunStore:
            def get(self, _batch_id):
                return box["state"]

            def update(self, _batch_id, **changes):
                box["state"].update(changes)
                return box["state"]

            def mutate_item(self, _batch_id, item_id, mutator):
                for item in box["state"]["items"]:
                    if item["id"] == item_id:
                        mutator(item)
                return box["state"]

            def set_item_milestone(self, _batch_id, item_id, milestone_id, **changes):
                for item in box["state"]["items"]:
                    if item["id"] == item_id:
                        for milestone in item["milestones"]:
                            if milestone["id"] == milestone_id:
                                milestone.update(changes)
                return box["state"]

            def add_item_log(self, _batch_id, item_id, message):
                for item in box["state"]["items"]:
                    if item["id"] == item_id:
                        item.setdefault("logs", []).append({"time": "t", "message": message})
                return box["state"]

        async def fake_download(_batch_id, item_id):
            if item_id == first_id:
                raise DuplicateItem("和第 2 条是同一个抖音作品")
            return Path("stub.mp4")

        async def fake_prepare(_batch_id, item_id, **_kwargs):
            for item in box["state"]["items"]:
                if item["id"] == item_id:
                    item["status"] = "awaiting_review"

        with patch.object(batch_worker, "batch_store", RunStore()), patch.object(
            batch_worker, "_download", fake_download
        ), patch.object(batch_worker, "_prepare_review", fake_prepare), patch.object(
            batch_worker, "_finish_prep_covers", new_callable=AsyncMock
        ):
            asyncio.run(batch_worker.run_batch("b1"))

        by_index = {item["index"]: item for item in box["state"]["items"]}
        self.assertEqual(by_index[1]["status"], "skipped")
        self.assertIn("同一个抖音作品", by_index[1]["warning"])
        self.assertEqual(by_index[2]["status"], "awaiting_review")   # 后面的条目照常跑
        self.assertNotEqual(box["state"]["status"], "failed")

    def test_a_late_item_waits_until_the_rendering_one_finishes(self) -> None:
        """出片跑着时，新追加的条目**停在排队**，不许提前下载/备料。

        这条是 2026-09-13 那条「备料和出片并行」的**推翻**：用户 2026-09-20 明确
        「流程改下 现在不需要先提前备料了」——上一条没交付完之前，后面的条目连下载都不开始
        （行为层面的钉子；静态那一条在 `test_batch_strictly_runs_one_item_at_a_time`）。
        """
        import asyncio

        from backend import batch_worker
        from backend.batch_worker import new_batch_state

        state = new_batch_state(["https://v.douyin.com/sing"], [])
        state["items"][0]["status"] = "confirmed"       # 第 1 条已放行、开始出片
        confirmed_id = state["items"][0]["id"]
        box = {"state": state}
        order: list[str] = []
        video_started = asyncio.Event()
        release = asyncio.Event()
        appended: dict = {}

        class RunStore:  # 与 run_batch 交互的最小状态存储
            def get(self, _batch_id):
                return box["state"]

            def update(self, _batch_id, **changes):
                box["state"].update(changes)
                return box["state"]

            def mutate_item(self, _batch_id, item_id, mutator):
                for item in box["state"]["items"]:
                    if item["id"] == item_id:
                        mutator(item)
                return box["state"]

            def set_item_milestone(self, _batch_id, item_id, milestone_id, **changes):
                for item in box["state"]["items"]:
                    if item["id"] == item_id:
                        for milestone in item["milestones"]:
                            if milestone["id"] == milestone_id:
                                milestone.update(changes)
                return box["state"]

            def add_item_log(self, _batch_id, item_id, message):
                for item in box["state"]["items"]:
                    if item["id"] == item_id:
                        item.setdefault("logs", []).append({"time": "t", "message": message})
                return box["state"]

        async def fake_video(_batch_id, item_id):
            # 真实的 _process_confirmed 第一件事就是把条目推进 running
            for item in box["state"]["items"]:
                if item["id"] == item_id:
                    item["status"] = "running"
            order.append("video-start")
            video_started.set()
            await release.wait()
            for item in box["state"]["items"]:
                if item["id"] == item_id:
                    item["status"] = "completed"
            order.append("video-end")

        async def fake_download(_batch_id, item_id):
            order.append(f"download-{item_id}")

        async def fake_prepare(_batch_id, item_id, **_kwargs):
            for item in box["state"]["items"]:
                if item["id"] == item_id:
                    item["status"] = "awaiting_review"

        async def scenario() -> None:
            with patch.object(batch_worker, "batch_store", RunStore()), patch.object(
                batch_worker, "_process_confirmed", fake_video
            ), patch.object(batch_worker, "_download", fake_download), patch.object(
                batch_worker, "_prepare_review", fake_prepare
            ), patch.object(batch_worker, "_covers_ready", return_value=True), patch.object(
                batch_worker, "_finish_prep_covers", new_callable=AsyncMock
            ):
                runner = asyncio.create_task(batch_worker.run_batch("b1"))
                await asyncio.wait_for(video_started.wait(), timeout=5)
                # 出片**跑起来之后**用户才追加一条（页面上「加入队列」的行为：排到队尾）
                late = new_batch_state(["https://v.douyin.com/late"], [])["items"][0]
                late["index"] = 2
                box["state"]["items"].append(late)
                box["state"]["total"] = 2
                appended["id"] = late["id"]
                # 给 runner 足够多的机会去"顺手备料"：它必须什么都不做
                for _ in range(30):
                    await asyncio.sleep(0.01)
                self.assertEqual(late["status"], "pending")
                self.assertNotIn(f"download-{late['id']}", order)
                self.assertNotIn("video-end", order)
                # 出片交付完，runner 才轮到这一条：下载 → 备料 → 停在等你确认
                release.set()
                await asyncio.wait_for(runner, timeout=5)

        asyncio.run(scenario())
        self.assertEqual(order[0], "video-start")
        self.assertEqual(order[1], "video-end")
        self.assertIn(f"download-{appended['id']}", order)
        self.assertEqual(box["state"]["items"][1]["status"], "awaiting_review")
        self.assertEqual(box["state"]["items"][1]["id"], appended["id"])
        # 出片落定后批次停在「等你确认」，而不是把待审核的条目当成已完成
        self.assertEqual(box["state"]["status"], "awaiting_review")
        self.assertEqual(box["state"]["items"][0]["id"], confirmed_id)

    def test_batch_review_shows_which_source_video_the_item_is(self) -> None:
        """确认时必须能认出「这是哪条抖音视频」。

        2026-09-13 用户：「现在跳舞视频生成的内容不对没有对应上跳舞的视频，你可以在让我确认
        的时候让我知道现在的是哪个视频吗」——条目上显示的是模型重起的发布标题（例如
        「只对你心动的花季暗号」），用户根本认不出它对应哪条抖音视频。所以条目详情必须另外给出：
        源作品自己的文案（`sourceMetadata.desc`）、可播放的源视频（`stage/source`）、抖音作品号。
        """
        source = (Path(__file__).parents[1] / "src" / "BatchRoute.tsx").read_text(encoding="utf-8")
        self.assertIn("本条源视频", source)
        self.assertIn("stage/source", source)          # 内嵌播放器直接放源视频
        self.assertIn("function sourceCaption(", source)
        self.assertIn("sourceMetadata", source)
        self.assertIn("抖音作品号", source)
        self.assertIn("batch-item-source", source)     # 队列列表里也能分辨是哪条

    def test_batch_page_does_not_render_publish_status_chatter(self) -> None:
        """批量页不许出现「发布文件已整理 / 还没整理」这类话术。

        用户 2026-09-13 先要求去掉「发布文件还没整理」与「发布目录已收到人物图与文案」，
        成品面板还留着标题「发布文件已整理」时又追问一次（「发布文件已整理 没有去掉吗」）。
        成品面板只保留能点开的入口（最终成片 / 人物图 / 发布文案 / 打开文件夹），不写状态句；
        条目日志同样不渲染。
        """
        source = (Path(__file__).parents[1] / "src" / "BatchRoute.tsx").read_text(encoding="utf-8")
        # 注释里可以写这些词（要记录「为什么去掉」），**渲染出来的文字**里不许有：
        # 去掉 {/* … */} 块注释与整行 // 注释后再断言。
        rendered = re.sub(r"\{/\*.*?\*/\}", "", source, flags=re.S)
        rendered = re.sub(r"^\s*//.*$", "", rendered, flags=re.M)
        for phrase in (
            "发布文件已整理",
            "发布文件还没整理",
            "发布目录已收到人物图与文案",
            "均已保存",
            "条目日志",
            # 整个成品面板（最终成片 / 人物图 / 发布文案 / 打开文件夹）都不要
            # （2026-09-13 用户指着截图：「这个没有去掉吗 不是说去掉吗」）
            "batch-output-panel",
            "batch-output-grid",
            "打开文件夹",
            "output/videoFinal",
            "output/copy",
        ):
            self.assertNotIn(phrase, rendered, f"批量页不该再渲染「{phrase}」")

    def test_batch_never_starts_two_renders_at_once(self) -> None:
        """出片仍然严格一条一条：后台已经有一条在出片时，不得再挑第二条 confirmed。"""
        from backend.batch_worker import _next_work

        state = {
            "items": [
                {"id": "a", "status": "confirmed"},
                {"id": "b", "status": "confirmed"},
                {"id": "c", "status": "pending"},
            ]
        }
        self.assertEqual(_next_work(state, allow_confirmed=False)["id"], "c")   # 只挑备料
        self.assertIsNone(_next_work({"items": state["items"][:2]}, allow_confirmed=False))
        self.assertEqual(_next_work({"items": state["items"][:2]})["id"], "a")  # 空闲时才出片

    def test_batch_item_source_can_be_replaced(self) -> None:
        """贴错链接 / 放错槽位时可以就地替换源视频（用户 2026-09-13：「要有让我可以替换的操作」）。

        替换必须：换 url（可一并换类型）、清掉按旧视频做的分析（联系表 / 取景帧 / 出图提示词）、
        作废候选图指针与文案、回到 pending 重新备料；类型换了就用新类型的默认画布比例。
        已经出片 / 正在出片的条目必须先取消或回到确认。
        """
        from backend import batch_store as batch_store_module
        from backend import batch_worker
        from backend.batch_store import BatchStore

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            original_db = batch_store_module.DB_PATH
            original_data = batch_worker.DATA_DIR
            try:
                batch_store_module.DB_PATH = root / "queue.db"
                batch_worker.DATA_DIR = root / "data"
                store = BatchStore()
                state = new_batch_state([], ["https://www.douyin.com/video/7000000000000000001"])
                item = state["items"][0]
                item.update(
                    status="awaiting_review",
                    stage="review",
                    sourcePath=str(root / "old.mp4"),
                    sourceName="old.mp4",
                    awemeId="7000000000000000001",
                    title="模型起的标题",
                    ai={"reference_image_path": str(root / "old.png"), "imagePrompt": "旧提示词"},
                    reviewApproved=False,
                )
                work = root / "data" / "batches" / state["id"] / item["id"]
                work.mkdir(parents=True, exist_ok=True)
                for name in ("source-contact-sheet.jpg", "scene-frame.jpg", "出图提示词.txt"):
                    (work / name).write_text("stale", encoding="utf-8")
                (work / "candidate_r0_upload.png").write_bytes(b"user-image")
                store.create(state)

                def row():
                    return store.get(state["id"])["items"][0]

                with patch.object(batch_worker, "batch_store", store):
                    # 校验：类型 + 链接
                    with self.assertRaises(ValueError):
                        batch_worker.replace_item_source(state["id"], item["id"], "https://example.com/x")
                    with self.assertRaises(ValueError):
                        batch_worker.replace_item_source(
                            state["id"], item["id"], "https://www.douyin.com/video/7000000000000000002", "talking"
                        )
                    # 正常替换：顺便把唱歌视频放回跳舞槽的错改成唱歌
                    batch_worker.replace_item_source(
                        state["id"],
                        item["id"],
                        "https://www.douyin.com/video/7000000000000000002",
                        "singing",
                    )
                fresh = row()
                self.assertEqual(fresh["url"], "https://www.douyin.com/video/7000000000000000002")
                self.assertEqual(fresh["kind"], "singing")
                self.assertEqual(fresh["ratio"], "4:3")            # 换成唱歌 → 该类型默认比例
                self.assertEqual((fresh["status"], fresh["stage"]), ("pending", "queued"))
                self.assertEqual(fresh["sourcePath"], "")
                self.assertEqual(fresh["ai"], {})
                self.assertIsNone(fresh["reviewApproved"] or None)
                self.assertEqual(
                    {step["status"] for step in fresh["milestones"]}, {"pending"}
                )
                # 旧视频的分析缓存必须删掉，否则新视频会复用旧联系表/取景帧/提示词
                self.assertFalse((work / "source-contact-sheet.jpg").exists())
                self.assertFalse((work / "scene-frame.jpg").exists())
                self.assertFalse((work / "出图提示词.txt").exists())
                # 用户上传过的图不删，只是不再指向它
                self.assertTrue((work / "candidate_r0_upload.png").exists())

                # 正在出片 / 已出片的条目不许直接换
                for blocked in ("running", "revising", "completed"):
                    def set_status(row_, value=blocked):
                        row_["status"] = value

                    store.mutate_item(state["id"], item["id"], set_status)
                    with patch.object(batch_worker, "batch_store", store):
                        with self.assertRaises(ValueError):
                            batch_worker.replace_item_source(
                                state["id"], item["id"], "https://www.douyin.com/video/7000000000000000003"
                            )

                # 同一条链接已经在别的条目里 → 拒绝（自己不算重复）
                store.mutate_item(
                    state["id"], item["id"], lambda row_: row_.update(status="pending", kind="singing")
                )
                with patch.object(batch_worker, "batch_store", store):
                    batch_worker.append_batch_items(
                        state["id"], ["https://www.douyin.com/video/7000000000000000009"], []
                    )
                    with self.assertRaises(ValueError):
                        batch_worker.replace_item_source(
                            state["id"], item["id"], "https://www.douyin.com/video/7000000000000000009"
                        )
            finally:
                batch_store_module.DB_PATH = original_db
                batch_worker.DATA_DIR = original_data

    def test_batch_queue_only_runs_after_explicit_start(self) -> None:
        """页面入口是「加入队列并开始」：带 autoStart 直接跑，不带则只排队（API 用法）。"""
        import asyncio

        from backend import app as app_module
        from backend import batch_worker

        state = new_batch_state(["https://v.douyin.com/a"], [])

        class StubStore:
            def __init__(self, payload: dict) -> None:
                self.state = payload
                self.active_state = None

            def get(self, _batch_id):
                return self.state

            def create(self, payload: dict) -> dict:
                self.state = payload
                return payload

            def update(self, _batch_id, **changes):
                self.state.update(changes)
                return self.state

            def mutate(self, _batch_id, mutator):
                mutator(self.state)
                return self.state

            def mutate_item(self, _batch_id, item_id, mutator):
                for item in self.state["items"]:
                    if item["id"] == item_id:
                        mutator(item)
                return self.state

            def active(self):
                return self.active_state

            def latest(self):
                return self.state

        stub = StubStore(state)
        spawned: list = []
        with patch.object(batch_worker, "batch_store", stub), patch.object(
            app_module, "batch_store", stub
        ), patch.object(app_module, "new_batch_state", lambda *a, **k: dict(state)), patch.object(
            app_module, "spawn", lambda coro: (spawned.append(coro), coro.close())
        ):
            # 不带 autoStart（API 用法）：只入队、状态仍是 queued、没有起 runner
            created = asyncio.run(
                app_module.create_batch(
                    app_module.BatchCreateRequest(singingUrls=["https://v.douyin.com/a"])
                )
            )
            self.assertEqual(created["status"], "queued")
            self.assertIn("点「开跑」后开始处理", created["notice"])
            self.assertEqual(spawned, [])

            # 追加任务同样不自动开跑
            stub.state["status"] = "queued"
            asyncio.run(
                app_module.append_batch_items_endpoint(
                    "b1", app_module.BatchAppendRequest(singingUrls=["https://v.douyin.com/b"])
                )
            )
            self.assertEqual(stub.state["status"], "queued")
            self.assertEqual(spawned, [])
            self.assertEqual(len(stub.state["items"]), 2)

            # 删除/跳过也不会把没启动的批次跑起来
            stub.state["status"] = "queued"
            stub.state["items"][0]["status"] = "pending"
            batch_worker.batch_store = stub
            asyncio.run(app_module.delete_batch_item("b1", stub.state["items"][0]["id"]))
            self.assertEqual(stub.state["status"], "queued")
            self.assertEqual(spawned, [])

            # 点「加入队列并开始」→ 入库 + 立即开跑（预热 ComfyUI + runner）
            stub.state["items"][1]["status"] = "pending"
            started = asyncio.run(
                app_module.append_batch_items_endpoint(
                    "b1",
                    app_module.BatchAppendRequest(
                        singingUrls=["https://v.douyin.com/fresh"], autoStart=True
                    ),
                )
            )
            self.assertEqual(started["status"], "running")
            self.assertEqual(len(spawned), 1)   # 只启动 runner；备料阶段不预热 ComfyUI
            self.assertIn("下载并生成人物图与发布文案", started["notice"])

            # 暂停中的批次：即使 autoStart 也只入队，不违背用户的暂停
            stub.state["status"] = "paused"
            paused = asyncio.run(
                app_module.append_batch_items_endpoint(
                    "b1",
                    app_module.BatchAppendRequest(
                        singingUrls=["https://v.douyin.com/whilepaused"], autoStart=True
                    ),
                )
            )
            self.assertEqual(paused["status"], "paused")
            self.assertEqual(len(spawned), 1)
            # 逐条启动的入口已经收掉：入口只有「加入队列并开始」
            self.assertNotIn(
                "/api/batches/{batch_id}/items/{item_id}/start",
                {getattr(route, "path", "") for route in app_module.app.routes},
            )

            # 队列里没有待处理任务时不给启动
            stub.state["status"] = "completed"
            for item in stub.state["items"]:
                item["status"] = "completed"
            with self.assertRaises(app_module.HTTPException):
                asyncio.run(app_module.start_batch("b1"))

    def test_deleted_items_do_not_take_numbers(self) -> None:
        """删掉的条目不占号：队列里剩几条就是第 1..N 条（用户只贴了 2 个链接却看到「第 3 条」）。"""
        from backend import batch_store as batch_store_module
        from backend import batch_worker
        from backend.batch_store import BatchStore

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            original = batch_store_module.DB_PATH
            try:
                batch_store_module.DB_PATH = Path(folder) / "queue.db"
                store = BatchStore()
                state = new_batch_state(
                    ["https://v.douyin.com/a", "https://v.douyin.com/b"], ["https://v.douyin.com/c"]
                )
                self.assertEqual([item["index"] for item in state["items"]], [1, 2, 3])
                store.create(state)

                # 删掉前两条 → 剩下那条变成「第 1 条」，而不是继续叫第 3 条
                for item in state["items"][:2]:
                    store.mutate_item(
                        state["id"], item["id"], lambda row: row.update(status="deleted")
                    )
                after = store.get(state["id"])
                visible = [item for item in after["items"] if item["status"] != "deleted"]
                self.assertEqual([item["index"] for item in visible], [1])
                self.assertEqual(sorted(item["index"] for item in after["items"]), [1, 2, 3])

                # 再追加一条：接着可见队列排（第 2 条），不跟已删除的撞号
                with patch.object(batch_worker, "batch_store", store):
                    result = batch_worker.append_batch_items(
                        state["id"], ["https://v.douyin.com/d"], []
                    )
                items = result["state"]["items"]
                visible = [item for item in items if item["status"] != "deleted"]
                self.assertEqual([item["index"] for item in visible], [1, 2])
                self.assertEqual(sorted(item["index"] for item in items), [1, 2, 3, 4])
            finally:
                batch_store_module.DB_PATH = original

    def test_batch_items_can_be_reordered(self) -> None:
        """后台表格化后支持上移/下移调处理顺序；运行中/已完成的条目锁死不能动。"""
        from backend import batch_store as batch_store_module
        from backend import batch_worker
        from backend.batch_store import BatchStore

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            original = batch_store_module.DB_PATH
            try:
                batch_store_module.DB_PATH = Path(folder) / "queue.db"
                store = BatchStore()
                state = new_batch_state(
                    ["https://v.douyin.com/a", "https://v.douyin.com/b", "https://v.douyin.com/c"],
                    ["https://v.douyin.com/d"],
                )
                store.create(state)
                ids = [item["id"] for item in state["items"]]

                def order():
                    return [
                        item["id"]
                        for item in store.get(state["id"])["items"]
                        if item["status"] != "deleted"
                    ]

                with patch.object(batch_worker, "batch_store", store):
                    # 把第 3 条上移 → 变成第 2 条，编号跟着重新排
                    batch_worker.move_batch_item(state["id"], ids[2], "up")
                    self.assertEqual(order(), [ids[0], ids[2], ids[1], ids[3]])
                    after = store.get(state["id"])
                    self.assertEqual([item["index"] for item in after["items"]], [1, 2, 3, 4])

                    # 队首再上移是 no-op，不报错也不动
                    batch_worker.move_batch_item(state["id"], ids[0], "up")
                    self.assertEqual(order(), [ids[0], ids[2], ids[1], ids[3]])

                    # 下移回到原位
                    batch_worker.move_batch_item(state["id"], ids[0], "down")
                    self.assertEqual(order(), [ids[2], ids[0], ids[1], ids[3]])

                    # 已出片的条目不能调
                    store.mutate_item(
                        state["id"], ids[3], lambda row: row.update(status="completed")
                    )
                    with self.assertRaises(ValueError):
                        batch_worker.move_batch_item(state["id"], ids[3], "up")
            finally:
                batch_store_module.DB_PATH = original

    def test_batch_confirm_many_only_releases_ready_items(self) -> None:
        """批量确认：只有「待确认 + 已有候选图」的条目被放行，其余逐个说明原因。"""
        from backend import batch_store as batch_store_module
        from backend import batch_worker
        from backend.batch_store import BatchStore

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            original = batch_store_module.DB_PATH
            try:
                batch_store_module.DB_PATH = Path(folder) / "queue.db"
                store = BatchStore()
                state = new_batch_state(
                    ["https://v.douyin.com/a", "https://v.douyin.com/b", "https://v.douyin.com/c"],
                    [],
                )
                store.create(state)
                ids = [item["id"] for item in state["items"]]

                # 都备好料、有候选图 → 都能批量确认（出片仍一条一条排队）
                for i, item_id in enumerate(ids):
                    store.mutate_item(
                        state["id"],
                        item_id,
                        lambda row, img=f"E:/tmp/cand{i}.png": row.update(
                            status="awaiting_review",
                            ai={"title": "t", "reference_image_path": img},
                        ),
                    )
                with patch.object(batch_worker, "batch_store", store):
                    result = batch_worker.confirm_batch_items(state["id"], ids)
                self.assertEqual(result["confirmed"], ids)
                self.assertEqual(result["skipped"], [])
                self.assertTrue(
                    all(item["status"] == "confirmed" for item in store.get(state["id"])["items"])
                )

                # 缺候选图 / 不在待确认状态 → 不放行，原因写清楚
                for item in store.get(state["id"])["items"]:
                    store.mutate_item(
                        state["id"], item["id"], lambda row: row.update(
                            status="awaiting_review", ai={"title": "t"}
                        )
                    )
                store.mutate_item(state["id"], ids[1], lambda row: row.update(status="confirmed"))
                store.mutate_item(
                    state["id"],
                    ids[2],
                    lambda row: row.update(ai={"title": "t", "reference_image_path": "E:/tmp/cand2.png"}),
                )
                with patch.object(batch_worker, "batch_store", store):
                    result = batch_worker.confirm_batch_items(state["id"], ids)
                self.assertEqual(result["confirmed"], [ids[2]])
                reasons = {entry["id"]: entry["reason"] for entry in result["skipped"]}
                self.assertEqual(reasons[ids[0]], "还没有候选人物图")
                self.assertEqual(reasons[ids[1]], "不在待确认状态")
            finally:
                batch_store_module.DB_PATH = original

    def test_batch_skip_and_delete_many(self) -> None:
        """批量跳过/删除：没在出片的直接落 skipped/deleted，已结束的拒绝。"""
        from backend import batch_store as batch_store_module
        from backend import batch_worker
        from backend.batch_store import BatchStore

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            original = batch_store_module.DB_PATH
            try:
                batch_store_module.DB_PATH = Path(folder) / "queue.db"
                store = BatchStore()
                state = new_batch_state(
                    ["https://v.douyin.com/a", "https://v.douyin.com/b", "https://v.douyin.com/c"],
                    [],
                )
                store.create(state)
                ids = [item["id"] for item in state["items"]]
                store.mutate_item(state["id"], ids[0], lambda row: row.update(status="awaiting_review"))
                store.mutate_item(state["id"], ids[1], lambda row: row.update(status="confirmed"))
                store.mutate_item(state["id"], ids[2], lambda row: row.update(status="completed"))

                with patch.object(batch_worker, "batch_store", store):
                    skipped = batch_worker.mark_items_skipped(state["id"], ids)
                self.assertEqual(skipped["marked"], [ids[0], ids[1]])
                self.assertEqual(skipped["rejected"], [{"id": ids[2], "reason": "已经结束"}])
                after = {item["id"]: item["status"] for item in store.get(state["id"])["items"]}
                self.assertEqual(after[ids[0]], "skipped")
                self.assertEqual(after[ids[1]], "skipped")
                self.assertEqual(after[ids[2]], "completed")

                with patch.object(batch_worker, "batch_store", store):
                    deleted = batch_worker.mark_items_deleted(state["id"], ids[:1])
                self.assertEqual(deleted["marked"], [ids[0]])
                self.assertEqual(
                    store.get(state["id"])["items"][0]["status"], "deleted"
                )
            finally:
                batch_store_module.DB_PATH = original

    def test_batch_retries_a_failed_video_once_then_continues(self) -> None:
        """出片失败 → **自动重试 1 次**；重试成功就照常交付、批次不停（失败策略 2026-09-20）。"""
        import asyncio

        from backend import batch_worker
        from backend.batch_worker import new_batch_state

        state = new_batch_state(["https://v.douyin.com/a"], [])
        state["items"][0]["status"] = "confirmed"
        box = {"state": state}
        calls = {"n": 0}

        async def flaky_video(_batch_id, item_id):
            calls["n"] += 1
            for item in box["state"]["items"]:
                if item["id"] == item_id:
                    item["status"] = "running"
            if calls["n"] == 1:
                raise RuntimeError("ComfyUI 抽风")
            for item in box["state"]["items"]:
                if item["id"] == item_id:
                    item["status"] = "completed"

        async def no_prep(*_args, **_kwargs):
            raise AssertionError("这一条已经备好，不该再备料")

        async def scenario() -> None:
            with patch.object(batch_worker, "batch_store", _BoxStore(box)), patch.object(
                batch_worker, "_process_confirmed", flaky_video
            ), patch.object(batch_worker, "_run_prep", no_prep), patch.object(
                batch_worker, "_covers_ready", return_value=True
            ):
                await asyncio.wait_for(batch_worker.run_batch("b1"), timeout=5)

        asyncio.run(scenario())
        row = box["state"]["items"][0]
        self.assertEqual(calls["n"], 2, "失败后应当自动重试一次")
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["videoAttempts"], 1)
        self.assertEqual(box["state"]["status"], "completed")
        self.assertTrue(
            any("自动重试一次" in log["message"] for log in row.get("logs") or []),
            row.get("logs"),
        )

    def test_failed_cover_does_not_block_ready_items(self) -> None:
        from backend import batch_worker

        state = {"items": [{"status": "failed"}, {"status": "confirmed"}]}
        with patch.object(batch_worker, "full_auto_enabled", return_value=True), patch.object(
            batch_worker, "_covers_ready", side_effect=lambda item: item["status"] == "confirmed"
        ):
            self.assertFalse(batch_worker._prep_remaining(state))
            state["items"][1]["status"] = "pending"
            self.assertTrue(batch_worker._prep_remaining(state))

    def test_two_failures_in_a_row_finish_with_exceptions(self) -> None:
        """连续失败的条目进入异常区，批次仍能完成。"""
        import asyncio

        from backend import batch_worker
        from backend.batch_worker import new_batch_state

        state = new_batch_state(["https://v.douyin.com/a", "https://v.douyin.com/b"], [])
        for item in state["items"]:
            item["status"] = "confirmed"
        box = {"state": state}

        async def always_fail(_batch_id, item_id):
            for item in box["state"]["items"]:
                if item["id"] == item_id:
                    item["status"] = "running"
            raise RuntimeError("ComfyUI 挂了")

        async def no_prep(*_args, **_kwargs):
            raise AssertionError("两条都已备好，不该再备料")

        async def scenario() -> None:
            with patch.object(batch_worker, "batch_store", _BoxStore(box)), patch.object(
                batch_worker, "_process_confirmed", always_fail
            ), patch.object(batch_worker, "_run_prep", no_prep), patch.object(
                batch_worker, "_covers_ready", return_value=True
            ):
                await asyncio.wait_for(batch_worker.run_batch("b1"), timeout=5)

        asyncio.run(scenario())
        self.assertEqual(box["state"]["status"], "completed")
        self.assertIn("2 条异常", box["state"]["notice"])
        self.assertIn("出片", box["state"]["lastFailure"])
        self.assertEqual([row["status"] for row in box["state"]["items"]], ["failed", "failed"])
        # 每条都试过两次（首次 + 一次重试）
        self.assertEqual([row["videoAttempts"] for row in box["state"]["items"]], [1, 1])

    def test_batch_finishes_prep_before_render_and_never_overlaps(self) -> None:
        """备料和出片共用单链路；待备料全部做完才开始视频。"""
        import asyncio

        from backend import batch_worker
        from backend.batch_worker import new_batch_state

        state = new_batch_state(["https://v.douyin.com/first"], ["https://v.douyin.com/second"])
        state["items"][0]["status"] = "confirmed"       # 第 1 条已放行、开始出片
        state["items"][1]["status"] = "pending"        # 第 2 条等着备料
        box = {"state": state}
        order: list[str] = []
        video_started = asyncio.Event()
        release = asyncio.Event()

        class RunStore:  # 与 run_batch 交互的最小状态存储
            def get(self, _batch_id):
                return box["state"]

            def update(self, _batch_id, **changes):
                box["state"].update(changes)
                return box["state"]

            def mutate_item(self, _batch_id, item_id, mutator):
                for item in box["state"]["items"]:
                    if item["id"] == item_id:
                        mutator(item)
                return box["state"]

            def mutate(self, _batch_id, mutator):
                mutator(box["state"])
                return box["state"]

            def set_item_milestone(self, _batch_id, item_id, milestone_id, **changes):
                for item in box["state"]["items"]:
                    if item["id"] == item_id:
                        for milestone in item["milestones"]:
                            if milestone["id"] == milestone_id:
                                milestone.update(changes)
                return box["state"]

            def add_item_log(self, _batch_id, item_id, message):
                for item in box["state"]["items"]:
                    if item["id"] == item_id:
                        item.setdefault("logs", []).append({"time": "t", "message": message})
                return box["state"]

        async def fake_video(_batch_id, item_id):
            order.append("video-start")
            video_started.set()
            await release.wait()
            for item in box["state"]["items"]:
                if item["id"] == item_id:
                    item["status"] = "completed"
            order.append("video-end")

        async def fake_prep(_batch_id, item_id):
            order.append(f"prep-{item_id}")
            for item in box["state"]["items"]:
                if item["id"] == item_id:
                    item["status"] = "confirmed"

        async def scenario() -> None:
            with patch.object(batch_worker, "batch_store", RunStore()), patch.object(
                batch_worker, "_process_confirmed", fake_video
            ), patch.object(batch_worker, "_run_prep", fake_prep), patch.object(
                batch_worker, "_covers_ready", return_value=True
            ):
                runner = asyncio.create_task(batch_worker.run_batch("b1"))
                await asyncio.wait_for(video_started.wait(), timeout=5)
                self.assertEqual(order[0], f"prep-{state['items'][1]['id']}")
                self.assertEqual(order[1], "video-start")
                release.set()
                await asyncio.wait_for(runner, timeout=5)

        asyncio.run(scenario())
        self.assertEqual(order.count("video-start"), 2)
        self.assertEqual(order.count("video-end"), 2)

    def test_batch_strictly_runs_one_video_at_a_time(self) -> None:
        """出片**永远是单链路**：源码级钉子（备料可以并行，出片不行）。"""
        import inspect

        from backend import batch_worker

        source = inspect.getsource(batch_worker.run_batch)
        self.assertIn("asyncio.create_task(_process_confirmed", source)
        self.assertIn("prep_task: asyncio.Task | None = None", source)
        self.assertIn("_next_prep(state)", source)
        self.assertIn("_next_confirmed(state)", source)
        # 「出片在跑就什么都不挑」那版已经被 2026-09-20 的口径取代
        self.assertNotIn("item = None if video_task is not None else _next_work(state)", source)
        # 备料任务也留强引用（否则可能在跑的过程中被 GC）
        self.assertIn("_PREP_TASKS.add(task)", source)

    def test_batch_publish_folder_completeness_is_checked_on_restart(self) -> None:
        """发布成品（人物图 + 两张封面）必须能查、缺了能补（2026-09-16 用户要求）。

        用户原话：「点击重新开始的时候要检查发布成品是否 人物图.png 和 封面_抖音3x4.png
        和 封面_B站4x3.png 是否完整 如果不完整看下是不是需要补充」。
        实测背景：走「重新开始」直接续跑的条目**不经过备料**，`_maybe_spawn_covers()` 永远
        不会再触发 —— 007 只剩人物图 + 文案、两张封面一直没补，009 连人物图都没有。
        """
        from backend import batch_store as batch_store_module
        from backend import batch_worker
        from backend.batch_store import BatchStore

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            original = batch_store_module.DB_PATH
            root = Path(folder) / "发布成品"
            root.mkdir(parents=True, exist_ok=True)
            try:
                batch_store_module.DB_PATH = Path(folder) / "queue.db"
                with patch.object(batch_worker, "BATCH_OUTPUT_ROOT", root):
                    store = BatchStore()
                    state = new_batch_state(["https://v.douyin.com/a"], [])
                    store.create(state)
                    item_id = state["items"][0]["id"]
                    candidate = Path(folder) / "candidate.png"
                    candidate.write_bytes(b"png-bytes")
                    store.mutate_item(
                        state["id"],
                        item_id,
                        lambda row: row.update(
                            awemeId="7674846165070563451",
                            ai={"title": "标题", "reference_image_path": str(candidate)},
                        ),
                    )

                    with patch.object(batch_worker, "batch_store", store):
                        item = batch_worker._item(state["id"], item_id)
                        target = batch_worker.publish_folder(item)
                        # 注意**不能**提前 mkdir：`publish_folder()` 会优先复用发布根里
                        # 同作品号的既有目录，先建出来就测不到「三件都缺」那一档了。

                        # ① 三件都缺：一次调用内就把能补的人物图补上，只剩两张封面
                        #    （封面名两代都要认：现在是 `B站4x3.png` / `抖音3x4.png`，
                        #     早期是 `封面_*.png` —— 只认老名字会让新目录永远报「缺封面」）
                        empty = batch_worker.publish_image_state(target)  # 目录还不存在
                        self.assertEqual(
                            sorted(empty["missing"]),
                            sorted(["人物图.png", "B站4x3.png", "抖音3x4.png"]),
                        )
                        got = batch_worker._sync_publish_images(state["id"], item_id)
                        self.assertNotIn("人物图.png", got["missing"])
                        self.assertEqual(
                            sorted(got["missing"]),
                            sorted(["B站4x3.png", "抖音3x4.png"]),
                        )
                        self.assertTrue(got["needs_covers"])
                        # 候选图还在 → 人物图被补进发布目录
                        self.assertTrue((target / "人物图.png").is_file())
                        self.assertTrue(got["image"])
                        # 补齐后必须把目录记在条目上，重开时才不会写去别的目录
                        self.assertEqual(
                            batch_worker._item(state["id"], item_id)["outputs"].get("folder"),
                            str(target),
                        )

                        # ② 封面缺、人物图已在 → 只报封面，且要求重开封面生成
                        again = batch_worker._sync_publish_images(state["id"], item_id)
                        self.assertNotIn("人物图.png", again["missing"])
                        self.assertEqual(
                            sorted(again["missing"]),
                            sorted(["B站4x3.png", "抖音3x4.png"]),
                        )
                        self.assertTrue(again["needs_covers"])

                        # ③ 三件齐全 → 什么都不缺、不需要重开封面
                        for name in batch_worker.PUBLISH_COVER_NAMES:
                            (target / name).write_bytes(b"cover")
                        done = batch_worker.publish_image_state(target)
                        self.assertEqual(done["missing"], [])
                        self.assertFalse(done["needs_covers"])

                        # ④ 已有的一张封面不重复生成：`_maybe_spawn_covers(only_missing=True)`
                        #    在两张都齐时**不触发**任何后台任务
                        with patch(
                            "backend.chatgpt_image.auto_cover_enabled", return_value=True
                        ):
                            self.assertFalse(
                                batch_worker._maybe_spawn_covers(
                                    state["id"], item_id, only_missing=True
                                )
                            )

                        # ⑥ 封面必须以**发布目录里那张人物图**为基准（用户 2026-09-16：
                        #    「生成的封面图背景要和人物图背景相同」）——实测 006 的
                        #    `人物图.png` 与 `ai.reference_image_path` 不是同一张
                        picked = batch_worker.cover_reference_image(
                            batch_worker._item(state["id"], item_id), target
                        )
                        self.assertEqual(picked, target / "人物图.png")
                        self.assertNotEqual(picked, candidate)

                        # ⑤ 「重新开始」**必须保住发布目录的记录**，否则检查与补齐会写到
                        #    另一个目录去（标题变过时 `publish_folder` 就会猜错）
                        batch_worker.restart_item_to_run(state["id"], item_id)
                        kept = batch_worker._item(state["id"], item_id)["outputs"]
                        self.assertEqual(kept.get("folder"), str(target))
                        self.assertIn("image", kept)
            finally:
                batch_store_module.DB_PATH = original

    def test_publish_folder_also_gets_the_source_frame(self) -> None:
        """源视频抽帧要复制一份进发布目录（用户 2026-09-16：「人物抽帧 也放到发布目录」）。

        抽帧（图一）本来就落在条目目录里当出图素材，用户要拿去自己出图，
        所以备料/交付时都往发布目录补一份 `抽帧.jpg`。只动这一条，别的不许变。
        """
        from backend import batch_store as batch_store_module
        from backend import batch_worker
        from backend.batch_store import BatchStore

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            original = batch_store_module.DB_PATH
            root = Path(folder) / "发布成品"
            root.mkdir(parents=True, exist_ok=True)
            try:
                batch_store_module.DB_PATH = Path(folder) / "queue.db"
                with patch.object(batch_worker, "BATCH_OUTPUT_ROOT", root), patch.object(
                    batch_worker, "DATA_DIR", Path(folder)
                ):
                    store = BatchStore()
                    state = new_batch_state(["https://v.douyin.com/a"], [])
                    store.create(state)
                    item_id = state["items"][0]["id"]
                    work = Path(folder) / "batches" / state["id"] / item_id
                    work.mkdir(parents=True, exist_ok=True)
                    frame = work / "scene-frame.jpg"
                    frame.write_bytes(b"frame-bytes")
                    store.mutate_item(
                        state["id"],
                        item_id,
                        lambda row: row.update(
                            awemeId="7674846165070563451",
                            ai={"title": "标题", "sceneFramePath": str(frame)},
                        ),
                    )
                    with patch.object(batch_worker, "batch_store", store):
                        merged = batch_worker.deliver_review_materials(state["id"], item_id)
                        target = batch_worker.publish_folder(batch_worker._item(state["id"], item_id))
                    self.assertTrue((target / batch_worker.PUBLISH_FRAME_NAME).is_file())
                    self.assertEqual(
                        (target / batch_worker.PUBLISH_FRAME_NAME).read_bytes(), b"frame-bytes"
                    )
                    self.assertEqual(merged["frame"], str(target / batch_worker.PUBLISH_FRAME_NAME))
                    self.assertEqual(batch_worker.PUBLISH_FRAME_NAME, "抽帧.jpg")
            finally:
                batch_store_module.DB_PATH = original

    def test_batch_cancel_clears_the_skip_request_flag(self) -> None:
        """「取消」落地后必须清掉 `skipRequested`，否则「重新开始」会被 runner 直接丢弃。

        2026-09-16 用户实测：「批量制作视频点击全部重新开始之后没有运行 comfyui」。
        根因不在 ComfyUI：`mark_items_skipped` 给**没在出片**的条目也打了
        `skipRequested=True`（它只把 status 改成 skipped，旗标留着），而
        `restart_item_to_run` 之后 runner 一回到循环就先看 `skipRequested`：
        `run_batch` 把每一条都当「正在取消中的条目」走 `_finish_abandoned` → 整批
        瞬间变成「全部条目已完成」，出片任务一个都没提交，ComfyUI 自然一次都没被拉起来。

        `skipRequested` 是「请 runner 去取消」的**请求**，条目已经不在跑了就该清掉。
        """
        from backend import batch_store as batch_store_module
        from backend import batch_worker
        from backend.batch_store import BatchStore

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            original = batch_store_module.DB_PATH
            try:
                batch_store_module.DB_PATH = Path(folder) / "queue.db"
                store = BatchStore()
                state = new_batch_state(
                    ["https://v.douyin.com/a", "https://v.douyin.com/b", "https://v.douyin.com/c"],
                    [],
                )
                store.create(state)
                ids = [item["id"] for item in state["items"]]
                store.mutate_item(state["id"], ids[0], lambda row: row.update(status="confirmed"))
                store.mutate_item(state["id"], ids[1], lambda row: row.update(status="awaiting_review"))
                store.mutate_item(state["id"], ids[2], lambda row: row.update(status="completed"))

                with patch.object(batch_worker, "batch_store", store):
                    result = batch_worker.mark_items_skipped(state["id"], ids)
                self.assertEqual(sorted(result["marked"]), sorted(ids[:2]))

                after = {item["id"]: item for item in store.get(state["id"])["items"]}
                for item_id in ids[:2]:
                    row = after[item_id]
                    self.assertEqual(row["status"], "skipped")
                    self.assertFalse(row.get("skipRequested"), "取消落地后必须清掉 skipRequested")
                self.assertFalse(after[ids[2]].get("skipRequested"))

                # 再「重新开始」这两条：旗标必须还是干净的，runner 才不会一上来就丢弃它们
                with patch.object(batch_worker, "batch_store", store):
                    for item_id in ids[:2]:
                        batch_worker.restart_item_to_run(state["id"], item_id)
                for item_id in ids[:2]:
                    row = {item["id"]: item for item in store.get(state["id"])["items"]}[item_id]
                    self.assertIn(row["status"], {"confirmed", "pending"})
                    self.assertFalse(row.get("skipRequested"))

                # 删除同样不能留 deleteRequested（同一条道理）
                with patch.object(batch_worker, "batch_store", store):
                    batch_worker.mark_items_deleted(state["id"], [ids[1]])
                deleted = {item["id"]: item for item in store.get(state["id"])["items"]}[ids[1]]
                self.assertEqual(deleted["status"], "deleted")
                self.assertFalse(deleted.get("deleteRequested"))
            finally:
                batch_store_module.DB_PATH = original

    def test_batch_restart_many_resets_finished_items(self) -> None:
        """批量重新开始：失败 / 已跳过 / 已出片的一起重跑；运行中、已删除、还没出片的拒绝。

        2026-09-15 用户：「批量选择选择后加个批量重新开始」。重置规则必须与单条
        「重试 / 重新开始」完全一致（同一个 `restart_item_to_run`）：备过料 + 源视频还在
        → 回 `confirmed` 只重跑出片；否则回 `pending` 从下载与备料重做。
        """
        from backend import batch_store as batch_store_module
        from backend import batch_worker
        from backend.batch_store import BatchStore

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            original = batch_store_module.DB_PATH
            try:
                batch_store_module.DB_PATH = Path(folder) / "queue.db"
                store = BatchStore()
                state = new_batch_state(
                    [
                        "https://v.douyin.com/a",
                        "https://v.douyin.com/b",
                        "https://v.douyin.com/c",
                        "https://v.douyin.com/d",
                        "https://v.douyin.com/e",
                    ],
                    [],
                )
                store.create(state)
                ids = [item["id"] for item in state["items"]]
                # ① 已出片且备过料（有图 + 源视频在磁盘上 + 已放行）→ 只重跑出片
                source = Path(folder) / "source.mp4"
                source.write_bytes(b"fake")
                candidate = Path(folder) / "candidate.png"
                candidate.write_bytes(b"fake-image")   # 重启只认**真实存在**的候选图
                # 发布目录记录要指向**发布根里真实存在的一个目录**（旧测试写的 "E:/x"
                # 在发布根之外，体检会把它当成脏记录换掉）
                recorded = Path(folder) / "发布成品" / f"001_唱歌_等待处理_{ids[0]}"
                recorded.mkdir(parents=True)
                store.mutate_item(
                    state["id"],
                    ids[0],
                    lambda row: row.update(
                        status="completed",
                        stage="completed",
                        reviewApproved=True,
                        sourcePath=str(source),
                        ai={"reference_image_path": str(candidate)},
                        childJob={"id": "old-child", "status": "completed"},
                        videoJobId="old-video",
                        outputs={"folder": str(recorded)},
                    ),
                )
                # ② 已跳过但没备过料 → 从下载与备料重做
                store.mutate_item(state["id"], ids[1], lambda row: row.update(status="skipped"))
                # ③ 失败、备过料但**源视频已经不在了** → 也必须回落 pending（否则提交时报文件不存在）
                store.mutate_item(
                    state["id"],
                    ids[2],
                    lambda row: row.update(
                        status="failed",
                        reviewApproved=True,
                        sourcePath=str(Path(folder) / "gone.mp4"),
                        ai={"reference_image_path": str(Path(folder) / "candidate.png")},
                        error="出片失败",
                    ),
                )
                # ④ 正在出片 / ⑤ 已删除 → 都不能被批量重开
                store.mutate_item(state["id"], ids[3], lambda row: row.update(status="running"))
                store.mutate_item(state["id"], ids[4], lambda row: row.update(status="deleted"))

                with patch.object(batch_worker, "batch_store", store), patch.object(
                    batch_worker, "BATCH_OUTPUT_ROOT", Path(folder) / "发布成品"
                ):
                    result = batch_worker.restart_batch_items(state["id"], ids)

                self.assertEqual(result["restarted"], [ids[0], ids[1], ids[2]])
                self.assertEqual(
                    result["rejected"],
                    [
                        {"id": ids[3], "reason": "正在出片，请先「取消」再重新开始"},
                        {"id": ids[4], "reason": "已删除"},
                    ],
                )
                rows = {item["id"]: item for item in store.get(state["id"])["items"]}
                # 备过料 + 源视频还在 → confirmed（只重跑出片，不重复下载备料）
                self.assertEqual(rows[ids[0]]["status"], "confirmed")
                self.assertIsNone(rows[ids[0]]["childJob"])
                self.assertIsNone(rows[ids[0]]["videoJobId"])
                # 旧的成片/文案记录作废，但**发布目录的记录要留住**（2026-09-16：
                # 发布目录按 `outputs.folder` 认，清空后标题一变就会写到别的目录去）；
                # 体检还会把已有的候选人物图同步进那个目录，所以 image 也在
                kept = rows[ids[0]]["outputs"]
                self.assertEqual(kept["folder"], str(Path(folder) / "发布成品" / f"001_唱歌_等待处理_{ids[0]}"))
                self.assertNotIn("videoFinal", kept)
                # 没备料 / 源视频丢了 → pending（从下载与备料重做）
                self.assertEqual(rows[ids[1]]["status"], "pending")
                self.assertEqual(rows[ids[2]]["status"], "pending")
                self.assertIsNone(rows[ids[2]]["error"])
                # 出片里程碑回到待办，页面上流程重新变成待办而不是停在 ✗
                video_milestones = [
                    milestone
                    for milestone in rows[ids[0]]["milestones"]
                    if milestone.get("id") == "video"
                ]
                self.assertTrue(video_milestones)
                self.assertTrue(all(m["status"] == "pending" for m in video_milestones))
                # 正在出片 / 已删除的条目一个字都没动
                self.assertEqual(rows[ids[3]]["status"], "running")
                self.assertEqual(rows[ids[4]]["status"], "deleted")

                # 一条都重开不了时也要把原因说清楚（页面靠它提示）
                store.mutate_item(state["id"], ids[3], lambda row: row.update(status="running"))
                with patch.object(batch_worker, "batch_store", store):
                    none_result = batch_worker.restart_batch_items(state["id"], [ids[4]])
                self.assertEqual(none_result["restarted"], [])
                self.assertEqual(none_result["rejected"][0]["reason"], "已删除")
            finally:
                batch_store_module.DB_PATH = original

    def test_batch_bulk_bar_can_restart_selected_items(self) -> None:
        """勾选后能批量「重新开始」（2026-09-15 用户：「批量选择选择后加个批量重新开始」）。

        只对「已经跑完一轮」的状态开放（失败 / 已跳过 / 已出片），与单条的
        「重试 / 重新开始」按钮同一条规则；出片仍然严格一条一条。
        """
        source = (Path(__file__).parents[1] / "src" / "BatchRoute.tsx").read_text(encoding="utf-8")
        # 批量接口的联合类型里加上 retry-many
        self.assertIn('"confirm-many" | "retry-many" | "skip-many" | "delete-many"', source)
        # 可重开的状态判定
        self.assertIn("const anyRestartable = selectedItems.some(", source)
        self.assertIn('["failed", "skipped", "completed"].includes(item.status),', source)
        # 批量操作栏里的按钮 + 走 askConfirm（会在弹框里列出受影响的条目）
        self.assertIn("<ArrowClockwise />重新开始", source)
        self.assertIn('void batchOp("retry-many", {', source)
        self.assertIn("没有可重新开始的条目（只有失败 / 已跳过 / 已出片的能重开）", source)
        # 按钮受 anyRestartable 控制（勾选里没有可重开的就禁用）
        self.assertIn("disabled={!anyRestartable || Boolean(busyAction)}", source)

    def test_batch_prepare_clears_the_submitted_link_boxes(self) -> None:
        """点「准备任务」提交成功后清空输入框（链接已经进队列了，直接粘下一批）。

        2026-09-15 用户：「准备任务点击之后清空现有的歌曲视频链接和跳舞视频链接」。
        只清这次真的提交了的那一类；请求失败时不清（否则用户白粘一遍）。
        """
        source = (Path(__file__).parents[1] / "src" / "BatchRoute.tsx").read_text(encoding="utf-8")
        # 拿到批次状态之后清空（= 只有成功才清；失败走 catch，不碰输入框）
        self.assertIn('if (singingOn && singingUrls.length) setSinging("");', source)
        self.assertIn('if (danceOn && danceUrls.length) setDance("");', source)
        # 旧行为（提交后保留输入框内容）不许回来
        rendered = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
        rendered = re.sub(r"^\s*//.*$", "", rendered, flags=re.M)
        self.assertNotIn("输入框内容保留", rendered)
        # 清空必须在 setBatch(state) 之后、catch 之前
        body = source[source.index("const prepare = async () => {"):source.index("const call = async (action: string")]
        self.assertLess(body.index("setBatch(state);"), body.index('setSinging("");'))
        self.assertLess(body.index('setSinging("");'), body.index("} catch (reason) {"))

    def test_batch_table_endpoints_are_registered(self) -> None:
        """后台表格化的批量接口（重排序 + 批量确认/重新开始/跳过/删除）必须都注册在 app 上。"""
        from backend.app import app

        paths = {getattr(route, "path", "") for route in app.routes}
        self.assertIn("/api/batches/{batch_id}/items/{item_id}/move", paths)
        self.assertIn("/api/batches/{batch_id}/items/confirm-many", paths)
        self.assertIn("/api/batches/{batch_id}/items/retry-many", paths)
        self.assertIn("/api/batches/{batch_id}/items/skip-many", paths)
        self.assertIn("/api/batches/{batch_id}/items/delete-many", paths)

    def test_batch_image_upload_blocks_running_items(self) -> None:
        """未完成都能编辑，但正在出片/重新备料的条目不能换图（避免图文脱节）。

        2026-09-15 用户：「只要状态是未完成的任务都可以进行编辑，当然正在运行的那条不允许编辑」。
        """
        import asyncio
        import io

        from starlette.datastructures import UploadFile

        from backend import app as app_module

        state = new_batch_state(["https://v.douyin.com/a"], [])
        item_id = state["items"][0]["id"]

        class StubStore:
            def __init__(self, payload):
                self.state = payload

            def get(self, _batch_id):
                return self.state

        stub = StubStore(state)
        with patch.object(app_module, "batch_store", stub):
            for running_status in ("running", "revising"):
                state["items"][0]["status"] = running_status
                with self.assertRaises(app_module.HTTPException) as ctx:
                    asyncio.run(
                        app_module.upload_batch_item_image(
                            "b1",
                            item_id,
                            UploadFile(file=io.BytesIO(b"fake"), filename="a.png"),
                        )
                    )
                self.assertEqual(ctx.exception.status_code, 409)
            # 待确认 / 已放行 / 失败 / 已跳过 仍能换图（守卫只拦运行中与已结束）
            for ok_status in ("awaiting_review", "confirmed", "failed", "skipped"):
                state["items"][0]["status"] = ok_status
                # 走到文件落盘这步前先被空文件校验拦下即可证明「没被状态守卫拦」
                with self.assertRaises(app_module.HTTPException) as ctx:
                    asyncio.run(
                        app_module.upload_batch_item_image(
                            "b1",
                            item_id,
                            UploadFile(file=io.BytesIO(b""), filename="a.png"),
                        )
                    )
                self.assertEqual(ctx.exception.status_code, 400)

    def test_batch_local_source_swap_keeps_everything_else(self) -> None:
        """用本机文件换源视频**只换视频**：标题/简介/标签/候选图/状态/里程碑全部不动。

        2026-09-15 用户：「替换源视频可以让我进行本地选择」+「其他内容都不需要改变只需要改变
        视频而且，所有定义好的内容都不需要变」——与换抖音链接（清空重备料）是两条路。
        """
        from backend import batch_store as batch_store_module
        from backend import batch_worker
        from backend.batch_store import BatchStore

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            original = batch_store_module.DB_PATH
            try:
                batch_store_module.DB_PATH = Path(folder) / "queue.db"
                store = BatchStore()
                state = new_batch_state(["https://v.douyin.com/a"], [])
                item_id = state["items"][0]["id"]
                store.create(state)
                video = Path(folder) / "local.mp4"
                video.write_bytes(b"fake-video")
                store.mutate_item(
                    state["id"],
                    item_id,
                    lambda row: row.update(
                        status="awaiting_review",
                        stage="review",
                        sourcePath="E:/old/source.mp4",
                        sourceName="old-source.mp4",
                        awemeId="7000000000000000001",
                        sourceMetadata={"desc": "旧的源作品文案", "tags": ["旧的"]},
                        ratio="9:16",
                        ai={
                            "title": "已经定好的标题",
                            "introduction": "已经定好的简介",
                            "tags": ["a", "b", "c", "d", "e"],
                            "reference_image_path": "E:/img.png",
                            "action_prompt": "已经定好的动作",
                        },
                        milestones=[{"id": "video", "label": "生成最终视频", "status": "pending"}],
                    ),
                )
                with patch.object(batch_worker, "batch_store", store):
                    batch_worker.replace_item_source_file(state["id"], item_id, video)

                item = store.get(state["id"])["items"][0]
                # 源视频本身换成这个本机文件，并打上「本机视频」标记
                self.assertEqual(item["sourcePath"], str(video))
                self.assertEqual(item["sourceName"], "local.mp4")
                self.assertEqual(item["sourceOrigin"], "local")
                # 旧的抖音身份不再成立 → 必须清掉，否则「本条源视频」卡片显示的还是旧视频信息
                # （2026-09-15 用户：「本条源视频 那边的内容也替换一下 不然我不知道是否修改成功了」）
                self.assertEqual(item["awemeId"], "")
                self.assertEqual(item.get("sourceMetadata") or {}, {})
                # 其余内容一律原样
                self.assertEqual(item["status"], "awaiting_review")
                self.assertEqual(item["ratio"], "9:16")
                self.assertEqual(item["ai"]["title"], "已经定好的标题")
                self.assertEqual(item["ai"]["introduction"], "已经定好的简介")
                self.assertEqual(item["ai"]["reference_image_path"], "E:/img.png")
                self.assertEqual(item["ai"]["action_prompt"], "已经定好的动作")
                self.assertEqual(item["ai"]["tags"], ["a", "b", "c", "d", "e"])
                self.assertEqual([m["id"] for m in item["milestones"]], ["video"])

                # 运行中 / 已结束的条目拒绝（和换链接同一条规则）
                for blocked in ("running", "revising", "completed"):
                    store.mutate_item(
                        state["id"], item_id, lambda row, s=blocked: row.update(status=s)
                    )
                    with patch.object(batch_worker, "batch_store", store):
                        with self.assertRaises(ValueError):
                            batch_worker.replace_item_source_file(state["id"], item_id, video)

                # 接口已注册
                from backend.app import app

                self.assertIn(
                    "/api/batches/{batch_id}/items/{item_id}/source-file",
                    {getattr(route, "path", "") for route in app.routes},
                )
                # 前端「本条源视频」卡片也要跟着变：显示「本机视频」、隐藏「打开抖音原链接」
                ui = (Path(__file__).parents[1] / "src" / "BatchRoute.tsx").read_text(
                    encoding="utf-8"
                )
                self.assertIn("function sourceOriginLabel(item: BatchItem)", ui)
                self.assertIn('return " · 本机视频";', ui)
                # 2026-09-18 源作品文案与「打开抖音原链接」移进「本条全部信息」后，
                # 本机换源的隐藏逻辑从 JSX 三元改成 `reviewCoreFacts` 里的 if 条件。
                self.assertIn('if (item.sourceOrigin !== "local" && item.url) {', ui)
            finally:
                batch_store_module.DB_PATH = original

    def test_batch_dance_migration_mode_can_be_chosen(self) -> None:
        """跳舞条目可选「动作迁移 / 人物替换」，出片时按所选模式提交。

        2026-09-15 用户：「跳舞可以选择人物迁移吗 现在是动作迁移 生成的效果不好我想看下人物迁移
        会是什么效果」——以前 `_post_video_job` 把迁移模式**写死成 animation**。
        """
        from backend import batch_store as batch_store_module
        from backend import batch_worker
        from backend.batch_store import BatchStore

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            original = batch_store_module.DB_PATH
            try:
                batch_store_module.DB_PATH = Path(folder) / "queue.db"
                store = BatchStore()
                state = new_batch_state([], ["https://www.douyin.com/video/7000000000000000001"])
                item_id = state["items"][0]["id"]
                store.create(state)
                store.mutate_item(
                    state["id"],
                    item_id,
                    lambda row: row.update(status="awaiting_review", ai={"title": "t"}),
                )
                with patch.object(batch_worker, "batch_store", store):
                    result = batch_worker.set_item_migrate_mode(state["id"], item_id, "replacement")
                self.assertEqual(result["items"][0]["ai"]["migrate_mode"], "replacement")
                with patch.object(batch_worker, "batch_store", store):
                    batch_worker.set_item_migrate_mode(state["id"], item_id, "animation")
                self.assertEqual(
                    store.get(state["id"])["items"][0]["ai"]["migrate_mode"], "animation"
                )

                # 非法值 / 已出片 / 唱歌条目都拒绝
                with patch.object(batch_worker, "batch_store", store):
                    with self.assertRaises(ValueError):
                        batch_worker.set_item_migrate_mode(state["id"], item_id, "person")
                store.mutate_item(state["id"], item_id, lambda row: row.update(status="completed"))
                with patch.object(batch_worker, "batch_store", store):
                    with self.assertRaises(ValueError):
                        batch_worker.set_item_migrate_mode(state["id"], item_id, "replacement")

                singing = new_batch_state(["https://v.douyin.com/a"], [])
                store.create(singing)
                with patch.object(batch_worker, "batch_store", store):
                    with self.assertRaises(ValueError):
                        batch_worker.set_item_migrate_mode(
                            singing["id"], singing["items"][0]["id"], "replacement"
                        )

                # 出片提交按条目所选模式（不再写死 animation）
                source = (Path(__file__).parents[1] / "backend" / "batch_worker.py").read_text(
                    encoding="utf-8"
                )
                self.assertNotIn('"mode": "animation"', source)
                self.assertIn('"mode": migrate_mode', source)

                from backend.app import app

                self.assertIn(
                    "/api/batches/{batch_id}/items/{item_id}/migrate-mode",
                    {getattr(route, "path", "") for route in app.routes},
                )
                # 「本条全部信息」里也要显示当前是哪一种模式（2026-09-15 用户要求）
                ui = (Path(__file__).parents[1] / "src" / "BatchRoute.tsx").read_text(
                    encoding="utf-8"
                )
                self.assertIn('facts.push(["迁移模式"', ui)
                self.assertIn("MIGRATE_MODE_LABEL[mode]", ui)
            finally:
                batch_store_module.DB_PATH = original

    def test_comfy_stop_endpoint_and_jobless_shutdown(self) -> None:
        """手动关闭 ComfyUI：接口在，且交接用的关闭逻辑能在没有 job 的情况下调用。"""
        import inspect

        from backend.app import app
        from backend.pipeline import ResourceManager

        self.assertIn("/api/comfy/stop", {getattr(route, "path", "") for route in app.routes})
        # job_id 缺省为空 → 只关闭、不写任何任务状态（队列面板按钮走这条）
        self.assertIsNone(inspect.signature(ResourceManager.shutdown_comfy).parameters["job_id"].default)
        # 链路内的交接步骤仍然必须传 job_id 才能推进 handoff 里程碑
        self.assertIs(
            inspect.signature(ResourceManager.stop_comfy).parameters["job_id"].default,
            inspect.Parameter.empty,
        )

    def test_batch_queue_survives_restart(self) -> None:
        """队列必须落盘：重开页面/重启服务后，排好队还没启动的任务一条都不能丢。"""
        from backend import batch_store as batch_store_module
        from backend.batch_store import BatchStore

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            db = Path(folder) / "queue.db"
            original = batch_store_module.DB_PATH
            try:
                batch_store_module.DB_PATH = db
                first = BatchStore()
                queued = new_batch_state(["https://v.douyin.com/a"], ["https://v.douyin.com/b"])
                queued["notice"] = "已加入队列，等启动"
                first.create(queued)
            finally:
                batch_store_module.DB_PATH = original

            try:
                batch_store_module.DB_PATH = db
                # 新实例＝重启后的后端：队列与每条的比例都还在
                second = BatchStore()
                restored = second.get(queued["id"])
                self.assertIsNotNone(restored)
                self.assertEqual(restored["status"], "queued")
                self.assertEqual(
                    [item["url"] for item in restored["items"]],
                    ["https://v.douyin.com/a", "https://v.douyin.com/b"],
                )
                self.assertEqual([item["ratio"] for item in restored["items"]], ["4:3", "9:16"])
                self.assertEqual(second.latest()["id"], queued["id"])

                # 重启扫描：只排队、没启动过的批次保持原样（等用户点「启动」），不会被标成暂停
                second.interrupt_active()
                after = second.get(queued["id"])
                self.assertEqual(after["status"], "queued")
                self.assertFalse(after.get("pauseRequested"))

                # 已经跑起来被打断的批次：保留进度并提示重试，数据不丢
                running = new_batch_state(["https://v.douyin.com/c"], [])
                running["status"] = "running"
                running["startedAt"] = "2026-09-10T00:00:00+00:00"
                running["runnerActive"] = True
                running["items"][0]["status"] = "running"
                second.create(running)
                second.interrupt_active()
                interrupted = second.get(running["id"])
                self.assertIn(interrupted["status"], {"failed", "paused"})
                self.assertEqual(len(interrupted["items"]), 1)
            finally:
                batch_store_module.DB_PATH = original

    def test_batch_preflight_no_longer_drives_the_codex_cli(self) -> None:
        """预审必须直连模型 + 本地出图；不能再起 codex exec agent 会话烧订阅额度。"""
        source = (Path(__file__).parents[1] / "backend" / "batch_worker.py").read_text(encoding="utf-8")
        self.assertNotIn("codex", source.lower())
        self.assertIn("batch_ai.analyze(", source)
        self.assertIn("batch_portrait.generate_portrait(", source)

    def test_batch_portrait_workflow_wires_scene_identity_prompt_and_ratio(self) -> None:
        """Krea2 双图编辑：图像-1 造型场景、图像-2 身份、提示词、画布档位、输出前缀。"""
        workflow = {
            "nodes": [
                {"id": 12, "type": "LoadImage", "title": "加载图像-1", "widgets_values": ["old1", "image"]},
                {"id": 11, "type": "LoadImage", "title": "加载图像-2", "widgets_values": ["old2", "image"]},
                {"id": 5, "type": "PrimitiveStringMultiline", "title": "提示词", "widgets_values": ["old"]},
                {"id": 7, "type": "ResolutionSelector", "widgets_values": ["16:9 (Widescreen)", 0.8, 32]},
                {"id": 17, "type": "SaveImage", "widgets_values": ["old_prefix"]},
            ],
            "links": [],
        }
        prepare_portrait_workflow(
            workflow,
            scene_image="scene.png",
            identity_image="identity.png",
            prompt="合成提示词",
            ratio="9:16",
            prefix="batch_abc",
        )
        nodes = {node["id"]: node for node in workflow["nodes"]}
        self.assertEqual(nodes[12]["widgets_values"][0], "scene.png")
        self.assertEqual(nodes[11]["widgets_values"][0], "identity.png")
        self.assertEqual(nodes[5]["widgets_values"][0], "合成提示词")
        self.assertEqual(nodes[7]["widgets_values"][0], "9:16 (Portrait Widescreen)")
        self.assertEqual(nodes[17]["widgets_values"][0], "batch_abc")

        with self.assertRaises(RuntimeError):
            prepare_portrait_workflow(
                workflow,
                scene_image="a",
                identity_image="b",
                prompt="c",
                ratio="1:1",
                prefix="d",
            )

    def test_batch_prompts_come_from_the_repo_not_the_desktop(self) -> None:
        """造型提示词已收进仓库，不能再依赖桌面的绝对路径。"""
        source = (Path(__file__).parents[1] / "backend" / "batch_worker.py").read_text(encoding="utf-8")
        self.assertNotIn("Desktop", source)
        compose = batch_ai.compose_prompt("singing", "video")
        self.assertGreater(len(compose), 500)
        self.assertIn("图二", compose)
        self.assertNotEqual(compose, batch_ai.compose_prompt("singing", "redesign"))

    def test_batch_ai_reads_key_from_process_env(self) -> None:
        """凭据只从环境读取；进程环境有值时直接用，不去碰仓库或数据库。"""
        with patch.dict(
            os.environ,
            {"H3_BATCH_TEXT_API_KEY": "", "OPENAI_API_KEY": "sk-from-env"},
            clear=False,
        ):
            batch_ai._CACHED_KEY = None
            self.assertTrue(batch_ai.configured())
            self.assertEqual(batch_ai._headers()["Authorization"], "Bearer sk-from-env")

    def test_batch_image_requires_explicit_relay_config(self) -> None:
        """出图必须显式配中转站：官方账号没有 gpt-image 余额，不能默认打到官方接口。"""
        relay_names = ("H3_BATCH_IMAGE_BASE_URL", "H3_BATCH_IMAGE_API_KEY", "H3_BATCH_IMAGE_MODEL")
        saved = {name: os.environ.pop(name, None) for name in relay_names}
        try:
            with process_env_only():
                with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-official"}, clear=False):
                    self.assertFalse(batch_image.configured())
                    self.assertEqual(batch_image.base_url(), "https://api.openai.com/v1")
                with patch.dict(
                    os.environ,
                    {
                        "H3_BATCH_IMAGE_BASE_URL": "https://relay.example/v1",
                        "H3_BATCH_IMAGE_API_KEY": "sk-relay",
                        "H3_BATCH_IMAGE_MODEL": "gpt-image-2.5-sunburst",
                    },
                    clear=False,
                ):
                    self.assertTrue(batch_image.configured())
                    self.assertEqual(batch_image.base_url(), "https://relay.example/v1")
                    self.assertEqual(batch_image.model(), "gpt-image-2.5-sunburst")
            self.assertEqual(batch_image.IMAGE_SIZES_2K["4:3"], "1536x1152")
            self.assertEqual(batch_image.IMAGE_SIZES_2K["9:16"], "1152x2048")
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            settings._USER_ENV_CACHE.clear()

    def test_batch_image_sizes_match_relay_constraints(self) -> None:
        """中转站约束：宽高均为 16 的倍数、长宽比 ≤3:1、总像素 655360~8294400，且比例精确。"""
        for ratio, expected in (("4:3", (4, 3)), ("9:16", (9, 16))):
            width, height = (int(part) for part in batch_image.image_sizes()[ratio].split("x"))
            self.assertEqual(width % 16, 0, f"{ratio} 宽不是 16 的倍数")
            self.assertEqual(height % 16, 0, f"{ratio} 高不是 16 的倍数")
            self.assertLessEqual(max(width, height), 3840)
            self.assertLessEqual(max(width, height) / min(width, height), 3.0)
            pixels = width * height
            self.assertGreaterEqual(pixels, 655_360)
            self.assertLessEqual(pixels, 8_294_400)
            # 比例必须精确：不能用 1536x1024 这种 3:2 冒充 4:3
            self.assertEqual(width * expected[1], height * expected[0], f"{ratio} 比例不精确")
            self.assertTrue(batch_image.needs_local_upscale(ratio))

    def test_batch_image_uses_one_explicit_request_shape(self) -> None:
        """计费出图不能自动试三种格式：一次操作只允许一种请求形态。"""
        with process_env_only():
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("H3_BATCH_IMAGE_FIELD", None)
                os.environ.pop("H3_BATCH_IMAGE_MODE", None)
                self.assertEqual(
                    batch_image.request_plans(),
                    [("multi", "image")],
                )
            with patch.dict(os.environ, {"H3_BATCH_IMAGE_FIELD": "image"}, clear=False):
                self.assertEqual(batch_image.request_plans(), [("multi", "image")])
            with patch.dict(
                os.environ,
                {"H3_BATCH_IMAGE_MODE": "composite", "H3_BATCH_IMAGE_FIELD": ""},
                clear=False,
            ):
                self.assertEqual(batch_image.request_plans(), [("composite", "image")])

    def test_batch_image_composite_stitches_two_references(self) -> None:
        """只接受单图的中转站走 composite：把两张参考图左右拼成一张，不叠文字。"""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            scene = root / "scene.png"
            identity = root / "identity.png"
            Image.new("RGB", (640, 480), "#223344").save(scene)
            Image.new("RGB", (512, 768), "#884466").save(identity)
            combined = batch_image.compose_reference(scene, identity, root / "combined.png")
            with Image.open(combined) as image:
                self.assertEqual(image.height, 768)
                self.assertGreater(image.width, 512 * 2)
                self.assertLess(image.width, 512 * 2 + 768)

    def test_batch_image_does_not_retry_an_uncertain_transport_error(self) -> None:
        """连接失败时服务端可能已计费：不能自动改格式或换线路再出一张。"""
        import asyncio

        import httpx as _httpx

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            scene, identity = root / "s.png", root / "i.png"
            Image.new("RGB", (320, 240), "#223344").save(scene)
            Image.new("RGB", (240, 320), "#884466").save(identity)
            calls: list[str] = []

            async def boom(data, files, *, proxy=None):
                calls.append(files[0][0])
                raise _httpx.ConnectError("All connection attempts failed")

            with process_env_only(), patch.dict(
                os.environ,
                {"H3_BATCH_IMAGE_BASE_URL": "http://127.0.0.1:9/v1", "H3_BATCH_IMAGE_API_KEY": "sk-fake"},
                clear=False,
            ), patch.object(batch_image, "_post", boom):
                with self.assertRaises(RuntimeError) as caught:
                    asyncio.run(
                        batch_image.generate_candidate_image(
                            scene_image=scene,
                            identity_image=identity,
                            prompt="p",
                            ratio="4:3",
                            output_path=root / "out.png",
                        )
                    )
            message = str(caught.exception)
            self.assertIn("连接失败", message)
            self.assertLessEqual(len(calls), 2)
            self.assertTrue(all(field == "image" for field in calls))

    def test_same_billed_image_request_key_shares_one_upstream_call(self) -> None:
        """批量后台与图片工作台撞在一起时，共享同一张结果，只调用一次中转站。"""
        import asyncio

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            entered = asyncio.Event()
            release = asyncio.Event()
            calls = 0

            async def fake_once(**kwargs):
                nonlocal calls
                calls += 1
                entered.set()
                await release.wait()
                target = Path(kwargs["output_path"])
                target.write_bytes(b"one-paid-result")
                return target

            async def run():
                first = asyncio.create_task(batch_image.generate_candidate_image(
                    scene_image=root / "scene.png", prompt="p", ratio="4:3",
                    output_path=root / "a.png", request_key="person:123",
                ))
                await entered.wait()
                second = asyncio.create_task(batch_image.generate_candidate_image(
                    scene_image=root / "scene.png", prompt="p", ratio="4:3",
                    output_path=root / "b.png", request_key="person:123",
                ))
                release.set()
                await asyncio.gather(first, second)

            with patch.object(batch_image, "_generate_candidate_image_once", fake_once):
                asyncio.run(run())
            self.assertEqual(calls, 1)
            self.assertEqual((root / "a.png").read_bytes(), (root / "b.png").read_bytes())

    def test_same_person_judge_request_key_shares_one_ds_call(self) -> None:
        """两个入口同时验同一张人物图时，只产生一次 DS 视觉计费。"""
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            identity = root / "identity.png"
            candidate = root / "candidate.png"
            Image.new("RGB", (8, 8), "white").save(identity)
            Image.new("RGB", (8, 8), "black").save(candidate)
            entered = asyncio.Event()
            release = asyncio.Event()
            calls = 0
            result = {
                "same_person": True,
                "similarity": 92,
                "differences": [],
                "compliant": True,
                "issues": [],
                "verdict": "通过",
            }

            async def fake_chat(**_kwargs):
                nonlocal calls
                calls += 1
                entered.set()
                await release.wait()
                return result

            async def run():
                first = asyncio.create_task(batch_ai.judge_person_image(
                    identity, candidate, request_key="judge:person:123",
                ))
                await entered.wait()
                second = asyncio.create_task(batch_ai.judge_person_image(
                    identity, candidate, request_key="judge:person:123",
                ))
                release.set()
                return await asyncio.gather(first, second)

            with patch.object(batch_ai, "_chat_structured", fake_chat):
                first_result, second_result = asyncio.run(run())
            self.assertEqual(calls, 1)
            self.assertEqual(first_result, result)
            self.assertEqual(second_result, result)

    def test_batch_image_provider_selection(self) -> None:
        """全自动开着 → 用中转站出图；关掉才回到「用户自己出图」的老默认。

        用户 2026-09-19：「全自动吧。不要我去备料了。现在直接用api生成」。
        注意机器上还留着老流程要求的 `H3_BATCH_IMAGE_PROVIDER=manual`（当初为避免批量备料
        与页面按钮**生成两遍**才钉死），全自动开着时它**不许**把链路降级成「只备料」。
        """
        with process_env_only():
            with patch.object(_batch_worker_module.batch_image, "configured", lambda: True):
                with patch.dict(os.environ, {"H3_BATCH_IMAGE_PROVIDER": "manual"}, clear=False):
                    self.assertEqual(_image_provider(), "api")
                # 显式指名 local / frame 有明显用途，仍然听环境变量
                with patch.dict(os.environ, {"H3_BATCH_IMAGE_PROVIDER": "local"}, clear=False):
                    self.assertEqual(_image_provider(), "local")
                with patch.dict(os.environ, {"H3_BATCH_IMAGE_PROVIDER": "frame"}, clear=False):
                    self.assertEqual(_image_provider(), "frame")
            # 中转站没配好（没有 base_url / key）→ 只能退回手动，绝不空跑
            with patch.object(_batch_worker_module.batch_image, "configured", lambda: False):
                with patch.dict(os.environ, {"H3_BATCH_IMAGE_PROVIDER": "api"}, clear=False):
                    self.assertEqual(_image_provider(), "manual")
            # 关掉全自动 → 完全回到老流程
            with patch.dict(os.environ, {"H3_FULL_AUTO": "0"}, clear=False):
                os.environ.pop("H3_BATCH_IMAGE_PROVIDER", None)
                self.assertEqual(_image_provider(), "manual")
                with patch.dict(os.environ, {"H3_BATCH_IMAGE_PROVIDER": "api"}, clear=False):
                    self.assertEqual(_image_provider(), "api")
                # 非法值回落到默认值
                with patch.dict(os.environ, {"H3_BATCH_IMAGE_PROVIDER": "没这个值"}, clear=False):
                    self.assertEqual(_image_provider(), "manual")

    def test_batch_ai_image_prompt_carries_review_feedback(self) -> None:
        """默认人物图保留参考造型，明确修改意见只在需要改图时追加。"""
        prompt = batch_ai.compose_image_prompt("singing", "video", "", "both")
        self.assertTrue(prompt.startswith("生成一张供视频制作使用的写实单人照片"))
        self.assertLess(len(prompt), 1400, "默认模板不应退回堆叠长提示词")
        self.assertIn("沿用图一的发型", prompt)
        self.assertIn("姿态和人物位置可自然调整", prompt)
        self.assertIn("图二是唯一的面部身份参考", prompt)
        self.assertNotIn("肤色与年龄感", prompt)
        self.assertNotIn("只以源视频抽帧图一为准", prompt)
        self.assertIn("自然精致淡妆", prompt)
        self.assertIn("脸和颈部明暗一致", prompt)
        self.assertIn("手在画外时不补入", prompt)
        self.assertIn("领口、下摆与开衩适度", prompt)
        self.assertNotIn(batch_ai.compose_prompt("singing", "video")[:40], prompt)
        # 只调文案时不应把反馈写进出图提示词（那条路径本来就不重新出图）
        copy_prompt = batch_ai.compose_image_prompt("singing", "video", "换背景", "copy")
        self.assertNotIn("换背景", copy_prompt)
        for mode in ("image", "both"):
            changed = batch_ai.compose_image_prompt("singing", "video", "头顶留白压到 2%", mode)
            self.assertIn("头顶留白压到 2%", changed)
            self.assertIn("【本次修改】", changed)

    def test_batch_ai_image_prompt_carries_the_song(self) -> None:
        """歌曲必须真的进入出图提示词，否则「图一给造型、歌曲给情绪」落不了地。"""
        # 1) 《歌曲名》是源文件里的字面占位符，必须被真实歌名替换（长提示词文件仍然要能替换）
        redesign = batch_ai.compose_prompt("singing", "redesign", "爱如潮水")
        self.assertIn("《爱如潮水》", redesign)
        self.assertNotIn(batch_ai.SONG_PLACEHOLDER, redesign)

        # 2) 自动出图：歌名 + 情绪进入提示词，并且把分工写清
        prompt = batch_ai.compose_image_prompt(
            "singing",
            "video",
            song_name="爱如潮水",
            song_mood="抒情慢板，克制的失恋感",
        )
        self.assertIn("【本次歌曲】《爱如潮水》", prompt)
        self.assertIn("抒情慢板，克制的失恋感", prompt)
        self.assertIn("歌曲辅助表情与整体氛围", prompt)
        self.assertNotIn("不改变服装或场景类型", prompt)

        # 3) 跳舞没有歌曲，不能凭空塞一个歌曲块
        self.assertNotIn("【本次歌曲】", batch_ai.compose_image_prompt("dance"))
        # 4) 没识别出歌名时也不能留一个空歌曲块
        self.assertNotIn("【本次歌曲】", batch_ai.compose_image_prompt("singing", "video", song_name=""))

    def test_song_redesign_prompt_does_not_claim_a_source_style(self) -> None:
        prompt = batch_ai.compose_image_prompt("singing", "redesign", song_name="爱如潮水")
        self.assertIn("图二是唯一的面部身份参考", prompt)
        self.assertNotIn("图一与图二都是同一人物的身份参考", prompt)
        self.assertIn("根据歌曲氛围设计自然的发型", prompt)
        self.assertIn("歌曲决定造型、背景氛围与表情", prompt)
        self.assertNotIn("歌曲只影响表情", prompt)
        self.assertNotIn("图一提供姿态、服装", prompt)
        self.assertNotIn("保留图一的室内或室外属性", prompt)
        self.assertNotIn("参考图一的造型与配色", prompt)
        self.assertNotIn("按图一的居家或户外类别", prompt)
        self.assertLess(len(prompt), 1400)

    def test_batch_ai_image_prompt_carries_composition_spec(self) -> None:
        """构图规格取自用户的构图参考图实测值，且必须按画布比例分别注入。"""
        singing = batch_ai.compose_image_prompt("singing")
        self.assertIn("【构图】", singing)
        self.assertIn("0%~1%", singing)          # 4:3 参考实测：头发几乎贴边
        self.assertIn("50%~55%", singing)        # 4:3 参考实测：脸占画面高度

        dance = batch_ai.compose_image_prompt("dance")
        self.assertIn("9:16", dance)
        self.assertIn("约 12%", dance)           # 跳舞参考实测：发际线距上边缘
        self.assertIn("34%", dance)
        self.assertNotIn("50%~55%", dance)

        # 批量里每条能单独改比例：显式传入的比例必须压过类型默认值
        singing_portrait = batch_ai.compose_image_prompt("singing", ratio="9:16")
        self.assertIn("约 12%", singing_portrait)
        self.assertNotIn("0%~1%", singing_portrait)
        dance_landscape = batch_ai.compose_image_prompt("dance", ratio="4:3")
        self.assertIn("0%~1%", dance_landscape)
        self.assertNotIn("约 12%", dance_landscape)

    def test_batch_ratio_endpoint_and_candidate_image_note(self) -> None:
        """条目比例接口存在；候选图与新比例不符时给出人话提示。"""
        from backend.app import app
        from backend.batch_worker import image_ratio_note

        self.assertIn(
            "/api/batches/{batch_id}/items/{item_id}/ratio",
            {getattr(route, "path", "") for route in app.routes},
        )
        with tempfile.TemporaryDirectory() as folder:
            landscape = Path(folder) / "wide.png"
            portrait = Path(folder) / "tall.png"
            Image.new("RGB", (1536, 1152), "#223344").save(landscape)
            Image.new("RGB", (1152, 2048), "#223344").save(portrait)
            self.assertEqual(image_ratio_note(landscape, "4:3"), "")
            self.assertIn("1536×1152", image_ratio_note(landscape, "9:16"))
            self.assertEqual(image_ratio_note(portrait, "9:16"), "")
            self.assertIn("1152×2048", image_ratio_note(portrait, "4:3"))
            self.assertEqual(image_ratio_note(Path(folder) / "missing.png", "4:3"), "")

    def test_batch_ai_identity_block_is_last_and_mandatory(self) -> None:
        """「五官必须和原型图一致」是硬性要求，必须放在提示词最末尾压住其他要求。"""
        prompt = batch_ai.compose_image_prompt(
            "singing", "video", "换个背景", "both", song_name="爱如潮水", song_mood="抒情"
        )
        self.assertTrue(prompt.rstrip().endswith(batch_ai.IDENTITY_PRIORITY_BLOCK.strip()))
        self.assertIn("图二是唯一的面部身份参考", prompt)
        self.assertIn("不混入图一的脸", prompt)
        # 跳舞同样受身份约束保护
        self.assertIn("图二是唯一的面部身份参考", batch_ai.compose_image_prompt("dance"))

    def test_platform_compliance_block_is_in_every_image_prompt(self) -> None:
        """普通露肤保留，只有明确非常暴露才允许局部处理。"""
        for kind in ("singing", "dance"):
            prompt = batch_ai.compose_image_prompt(kind)
            self.assertIn("【服装】", prompt)
            self.assertIn("领口、下摆与开衩适度", prompt)
            self.assertIn("面料不透、与肤色有清楚区分", prompt)
            self.assertIn("款式可适当调整", prompt)
            self.assertNotIn("无需改变正常的露肩", prompt)
        # 只管裸露：姿态、动作、裁切都不许出现在这一段里
        for word in ("摆胯", "腰部波浪", "姿态", "下边缘", "胸线"):
            self.assertNotIn(word, batch_ai.PLATFORM_SAFE_BLOCK)
        # 合规块必须在身份块**之前**：脸仍是最后一条（身份优先级最高）
        prompt = batch_ai.compose_image_prompt("dance", "video", "换成吊带", "both")
        self.assertLess(
            prompt.index("【服装】"),
            prompt.index(batch_ai.IDENTITY_PRIORITY_BLOCK.strip()),
        )
        self.assertTrue(prompt.rstrip().endswith(batch_ai.IDENTITY_PRIORITY_BLOCK.strip()))

    def test_image_prompts_forbid_blurry_hands(self) -> None:
        """2026-09-20 用户：「生成的图片有的手会模糊 禁止模糊」。

        实测根因两条（013 人物图 + 它自己的 `抽帧.jpg` 对比）：① 自动出图走的是精简模板
        （`COMPACT_IMAGE_PROMPT`），**一个字都没提清晰度** —— 长提示词文件里的【禁止虚化】
        只作用于「仅复制提示词 / 下载素材包」那条手工入口；② 抽帧里手正在挥、本身就是糊的，
        精简模板又写着「以图像-1 为唯一基准」，模型就把那层运动模糊照抄进了成图。

        所以口径必须写进**自动出图那一份**，并明确「不要照搬参考图的模糊」——
        光写「禁止虚化」挡不住第②条（模型会认为模糊本来就来自参考图、理应保留）。
        """
        block = batch_ai.SHARPNESS_BLOCK
        self.assertIn("脸、头发及画面内可见的手部清晰", block)
        self.assertIn("若图一的手有拖影", block)
        self.assertIn("参考图一的肩肘位置和手臂大致朝向", block)
        self.assertIn("手势简单、结构与遮挡合理", block)
        self.assertNotIn("抬起的手轻松半握", block)
        self.assertIn("手腕与前臂衔接自然", block)
        self.assertIn("肤色、亮度和皮肤纹理与脸及手臂一致", block)
        self.assertIn("袖口纹理及衣肤交界清楚", block)
        for kind in ("dance", "singing"):
            prompt = batch_ai.compose_image_prompt(kind)
            self.assertIn("【清晰度】", prompt)
            self.assertIn("若图一的手有拖影", prompt)
            # 仍然必须是精简模板（长提示词会把硬要求淹掉，别借这次改动把它拼回去）
            self.assertLess(len(prompt), 1800)
            self.assertIn("【手部姿态】", prompt)
            self.assertIn("手势简单、结构与遮挡合理", prompt)
            self.assertNotIn("身体朝向及主要手臂动作", prompt)
            # 清晰度块在身份块之前（脸仍然排在最后）
            self.assertLess(
                prompt.index("【清晰度】"),
                prompt.index(batch_ai.IDENTITY_PRIORITY_BLOCK.strip()),
            )
        # 口径指纹要跟着清晰度块一起变（否则自检会误判「线上已是最新」）
        local = batch_ai.compliance_fingerprint()
        with patch.object(batch_ai, "SHARPNESS_BLOCK", block + "\n再清楚一点"):
            self.assertNotEqual(batch_ai.compliance_fingerprint(), local)

    def test_image_prompts_clean_the_background(self) -> None:
        """2026-09-21 用户：「如果抽帧图片背景里有人需要将其他人去掉，现在生成的背景里的人会变得
        很奇怪；如果背景很杂乱需要处理一下」。

        根因：精简模板第一句就是「以图像-1 为唯一的**场景、背景**基准」，抽帧背景里的同框其他人
        （路人/观众/伴舞/镜面倒影）会被照抄进成图，而且画得很怪；原来只在【出片可用性】里带了
        一句「不要……路人」，被淹没了。所以升级成一个独立的硬要求块，并**收进口径指纹**。
        口径：① 其他人**全部去掉**、只留图像-2 本人一个；② 2026-09-24 用户选择 B，
        保留室内外属性、光线与氛围，允许重布置更好看的同类场景；
        ③ **不许用重度虚化糊掉**杂乱背景（那是"藏"不是"清"，而且背景一糊 ComfyUI 就没有可依据的
        空间结构）。
        """
        block = batch_ai.BACKGROUND_CLEAN_BLOCK
        self.assertIn("只保留主体一人", block)
        self.assertIn("移除其他人物、镜子及人物倒影、字幕与杂物", block)
        self.assertIn("原位置补成合理环境", block)
        self.assertIn("按图一的居家或户外类别重新设计", block)
        self.assertIn("不用虚化掩盖杂乱", block)
        for kind in ("dance", "singing"):
            prompt = batch_ai.compose_image_prompt(kind)
            self.assertIn("【背景与实拍感】", prompt)
            self.assertIn(block.strip(), prompt)
            self.assertLess(len(prompt), 1800)
            self.assertLess(
                prompt.index("【背景与实拍感】"),
                prompt.index(batch_ai.IDENTITY_PRIORITY_BLOCK.strip()),
            )
        # 口径指纹要跟着背景块一起变（否则自检会误判「线上已是最新」）
        local = batch_ai.compliance_fingerprint()
        with patch.object(batch_ai, "BACKGROUND_CLEAN_BLOCK", block + "\n再干净一点"):
            self.assertNotEqual(batch_ai.compliance_fingerprint(), local)

    def test_frame_picker_also_avoids_bystanders(self) -> None:
        """挑抽帧那一步要一并挑「背景里没有其他人」的帧（同一次视觉调用，不多花钱）。

        与「不加自动判据」的口径不冲突：它只做**定位 + 比清晰度/干净度**，挑不出来照样退回
        「整幅最清楚的那张」（`choose_sharp_hands_frame` 返回 None）。
        """
        text = batch_ai.SHARP_FRAME_INSTRUCTION
        self.assertIn("双手最清楚", text)
        self.assertIn("背景里没有其他人", text)
        self.assertIn("伴舞", text)
        self.assertIn("index=0", text)          # 分不出高下时的老回退口径不能丢

    def test_clothing_flexibility_and_exposure_rule(self) -> None:
        """参考原穿搭并允许调整，面料、剪裁及运动时的遮盖保持稳妥。"""
        block = batch_ai.PLATFORM_SAFE_BLOCK
        self.assertIn("参考图一的穿搭风格、配色和整体气质", block)
        self.assertIn("款式可适当调整", block)
        self.assertIn("抬手、转身时遮盖稳定", block)
        self.assertIn("领口、下摆与开衩适度", block)
        self.assertIn("面料不透、与肤色有清楚区分", block)
        for kind in ("dance", "singing"):
            prompt = batch_ai.compose_image_prompt(kind)
            self.assertLess(len(prompt), 1400)
            self.assertEqual(prompt.count("【服装】"), 1)
            self.assertNotIn("原有长度", prompt)
            self.assertNotIn("拿不准时保持原样", prompt)

    def test_compliance_judge_uses_the_platform_rule_verbatim(self) -> None:
        """出图后的自检也必须按平台三小条判，并显式豁免吊带/露肩/露脐（别再误杀 012/017）。"""
        text = batch_ai.COMPLIANCE_JUDGE_INSTRUCTION
        self.assertIn("大面积裸露", text)
        self.assertIn("透视、裸色", text)
        self.assertIn("隐私部位轮廓线过于突出", text)
        self.assertIn("明确不算违规", text)
        self.assertIn("吊带、抹胸、一字肩、露肩", text)
        self.assertIn("短裤、长筒袜、网袜", text)
        self.assertIn("只判裸露", text)
        # 「大面积」这条的阈值写清楚：只露一点点不算（用户 2026-09-20：「感觉露一点不算大面积吧」）
        self.assertIn("只露出一点点", text)
        self.assertIn("算合规", text)
        self.assertIn("明显", text)

    def test_config_exposes_the_compliance_fingerprint_the_selftest_compares(self) -> None:
        """`/api/config` 必须暴露口径指纹 —— 自检脚本靠它判断「后端还是不是旧进程」。

        2026-09-20 的坑：uvicorn 没带 `--reload`，改完提示词不重启等于没改。当时的探针是
        「线上提示词里有没有『过度裸露』」，**本机 14:21 起的旧进程照样判通过**（那个词在更早
        的提交里就有），而它缺的正是当天最新那条口径。指纹跟着三处源头正文走，改口径必变，
        探针不会再"改一次口径就失效一次"。
        """
        from backend import app as app_module

        async def no_comfy():
            return None

        local = batch_ai.compliance_fingerprint()
        self.assertEqual(len(local), 16)
        with patch.object(app_module, "comfy_health", no_comfy):
            payload = asyncio.run(app_module.get_config())
        self.assertEqual(payload["complianceFingerprint"], local)
        # 口径正文一改，指纹必须跟着变（否则自检又会变成"永远通过"）
        with patch.object(batch_ai, "PLATFORM_SAFE_BLOCK", batch_ai.PLATFORM_SAFE_BLOCK + "\n再收一点"):
            self.assertNotEqual(batch_ai.compliance_fingerprint(), local)
        with patch.object(batch_ai, "COMPLIANCE_JUDGE_INSTRUCTION", "换一份判据"):
            self.assertNotEqual(batch_ai.compliance_fingerprint(), local)

    def test_dance_preflight_only_touches_the_outfit_not_the_moves(self) -> None:
        """跳舞预审只对**衣服**提要求：动作/运镜照参考视频写，着装暴露也不算 redesign。"""
        preflight = batch_ai.preflight_prompt(
            kind="dance", duration=20.0, description="", tags=[]
        )
        self.assertIn("唯一的例外是**过度裸露**", preflight)
        self.assertIn("不得露出事业线/乳沟", preflight)
        self.assertIn("动作都不需要处理", preflight)
        self.assertIn("不要因为衣服而返回 redesign", preflight)
        # 动作/姿态红线已按用户要求撤掉；文案/标签也没动（「只是图片做出变更」）
        self.assertNotIn("不许写摆胯、扭胯、腰部波浪", preflight)
        self.assertNotIn("动作不得有性暗示", batch_ai.ACTION_RULES)
        self.assertFalse(hasattr(batch_ai, "COPY_COMPLIANCE_RULE"))
        self.assertNotIn("文案与标签的合规红线", preflight)

    def test_composition_spec_keeps_the_calibrated_framing(self) -> None:
        """参考图是喂 ComfyUI 的（下装/手臂靠它给），构图**保持用户原标定**、不再收紧。"""
        dance = batch_ai.compose_image_prompt("dance")
        self.assertIn("大腿上部", dance)
        self.assertNotIn("腰部以上", dance)
        singing = batch_ai.compose_image_prompt("singing")
        self.assertIn("胸部上方至上胸位置", singing)
        self.assertNotIn("胸线以上", singing)

    def test_source_prompt_files_carry_the_compliance_rule(self) -> None:
        """两份造型提示词文件也要改：低领那条「不要进一步扩大」是明确允许低领。"""
        root = Path(__file__).parents[1] / "backend" / "prompts"
        dance = (root / "9x16跳舞构图.txt").read_text(encoding="utf-8-sig")
        self.assertIn("过度裸露 · 发布级硬要求", dance)
        self.assertIn("不得**明显**露出**事业线（乳沟）**", dance)
        self.assertIn("只露上胸一点点", dance)
        self.assertIn("都不算违规、必须照常保留", dance)
        self.assertIn("本段只管上面这三条", dance)
        # 构图保持原标定（参考图要能给出下装，收太紧会让动作迁移脑补下装）
        self.assertIn("从人物头顶拍摄到腰部附近", dance)
        singing = (root / "4x3唱歌构图.txt").read_text(encoding="utf-8-sig")
        self.assertIn("过度裸露 · 发布级硬要求", singing)
        self.assertIn("都不算违规，照常保留", singing)
        self.assertNotIn("不要进一步扩大领口", singing)
        self.assertNotIn("禁止吊带、抹胸、露肩", singing)

    def test_desktop_cdp_person_prompt_carries_the_no_blur_rule(self) -> None:
        """桌面 CDP 那条按需出图路（`scripts/chat_imagegen.py`）也要带「禁止模糊」。

        那条路的人物图提示词走**用户桌面那份规则 .txt 附件**，正文里只有身份块 + 比例；
        桌面文件里有【禁止虚化】但没有最关键的那句「参考图里的手若是模糊的，不要照搬」
        （抽帧里手在动，模型会照抄）。所以这一句必须由 `person_body()` 补进正文，
        且顺序是：禁止模糊 → 身份块 → 比例（身份与比例要最后读到）。
        """
        from scripts.chat_imagegen import SHARPNESS_NOTE, person_body

        body = person_body("【身份优先级】保住本人", "【画面比例·硬要求】9:16")
        self.assertIn("不要照搬那份模糊", body)
        self.assertIn("禁止景深虚化/浅景深把手糊掉", SHARPNESS_NOTE)
        self.assertLess(body.index("禁止模糊"), body.index("【身份优先级】"))
        self.assertLess(body.index("【身份优先级】"), body.index("【画面比例·硬要求】"))
        # 关掉身份块时也不会把比例弄丢
        self.assertIn("【画面比例·硬要求】", person_body("", "【画面比例·硬要求】9:16"))

    def test_desktop_cdp_person_prompt_carries_the_background_cleanup_rule(self) -> None:
        """桌面 CDP 那条按需出图路的人物图正文里也要有「背景清理」（同一份口径一起同步）。

        桌面 .txt 只说「背景参考图一」，模型于是把抽帧里同框的路人一起照抄进来 —— 这一句
        必须由 `person_body()` 补进正文，且排在身份块之前（身份与比例要最后读到）。
        """
        from scripts.chat_imagegen import BACKGROUND_NOTE, person_body

        body = person_body("【身份优先级】保住本人", "【画面比例·硬要求】9:16")
        self.assertIn("【背景清理 · 硬要求】", body)
        self.assertIn("只能有本人这一个人", body)
        self.assertIn("允许重新布置更好看的同类场景", body)
        self.assertIn("不许用重度虚化", BACKGROUND_NOTE)
        self.assertLess(body.index("【背景清理"), body.index("【身份优先级】"))
        self.assertLess(body.index("【身份优先级】"), body.index("【画面比例·硬要求】"))

    def test_batch_tags_are_exactly_five(self) -> None:
        """发布标签固定 5 个。"""
        schema = json.loads((Path(__file__).parents[1] / "backend" / "batch_ai_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["tags"]["maxItems"], 5)
        self.assertEqual(schema["properties"]["tags"]["minItems"], 5)
        self.assertIn("恰好 5 个", batch_ai.preflight_prompt(
            kind="singing", duration=20.0, description="", tags=[]
        ))
        fallback = batch_ai.fallback_result(
            kind="singing", description="标题", tags=["a", "b", "c", "d", "e", "f", "g"]
        )
        self.assertEqual(fallback["tags"], ["a", "b", "c", "d", "e"])

    def test_singing_voice_conversion_uses_the_source_audio(self) -> None:
        """歌曲链路的音色转换必须基于**源视频的原唱音轨**，不是 H3 生成的音频。

        用户 2026-09-10：「生成的音频不对，你用原音频然后用 kikiV1 去合成最终版」。
        独立 /rvc 路由没有单独的源视频，保持用待转换视频自己的音频。
        """
        source = (Path(__file__).parents[1] / "backend" / "pipeline.py").read_text(encoding="utf-8")
        self.assertIn("def video_with_source_audio(", source)
        self.assertIn("async def run_rvc(job_id: str, enhanced_path: Path, audio_from: Path | None = None)", source)
        self.assertIn("async def _run_voice(job_id: str, enhanced_path: Path, audio_from: Path | None = None)", source)
        # 歌曲链路与 retry_voice 都显式传源视频音轨
        self.assertGreaterEqual(
            source.count("audio_from=source_audio if source_audio.is_file() else None"), 2
        )
        # 独立 /rvc 路由不传 audio_from
        self.assertIn("final = await run_rvc(job_id, source)\n", source)

    def test_batch_prepares_every_item_before_waiting_for_review(self) -> None:
        """先整批备料再逐条审核：`awaiting_review` 不能被当成可跑任务，否则第一条就卡住整批。"""
        from backend.batch_worker import _next_work

        state = {
            "items": [
                {"id": "a", "status": "awaiting_review"},
                {"id": "b", "status": "pending"},
                {"id": "c", "status": "confirmed"},
            ]
        }
        self.assertEqual(_next_work(state)["id"], "b")      # 还有没备料的 → 继续备料
        state["items"][1]["status"] = "awaiting_review"
        self.assertEqual(_next_work(state)["id"], "c")      # 备料跑完 → 才轮到出片
        state["items"][2]["status"] = "completed"
        self.assertIsNone(_next_work(state))                # 都在等用户 → 无事可做
        # revising（按审核意见重做）也要排进备料阶段
        state["items"][0]["status"] = "revising"
        self.assertEqual(_next_work(state)["id"], "a")

    def test_batch_reuse_previous_analysis_only_for_image_mode(self) -> None:
        """「只调图片」不得重跑模型：否则文案和动作/运镜会被一起改写。"""
        from backend.batch_worker import reuse_previous_analysis

        previous = {"reference_image_path": "candidate_r1.png", "title": "旧标题", "camera_prompt": "旧运镜"}
        reused = reuse_previous_analysis("image", previous)
        self.assertEqual(reused, previous)
        self.assertIsNot(reused, previous)          # 必须是副本，不能原地改到旧状态
        self.assertIsNone(reuse_previous_analysis("copy", previous))
        self.assertIsNone(reuse_previous_analysis("both", previous))
        self.assertIsNone(reuse_previous_analysis("image", {}))
        # 没有出图结果时（例如上一步降级过）也不该复用
        self.assertIsNone(reuse_previous_analysis("image", {"title": "只有文案"}))

    def test_batch_copy_prompt_is_written_from_the_final_image(self) -> None:
        """文案必须看着最终候选图写：图文一致是硬要求（但不许写成画面描述）。"""
        prompt = batch_ai.copy_prompt(
            song_name="爱如潮水", song_mood="抒情慢板，克制的失恋感", description="原作品描述"
        )
        self.assertIn("第一张图就是本条最终要发布的人物图", prompt)
        self.assertIn("不要写画面里没有的颜色、道具或场景", prompt)
        self.assertIn("绝对不要复述画面", prompt)
        self.assertIn("《爱如潮水》", prompt)
        self.assertIn("抒情慢板，克制的失恋感", prompt)
        self.assertIn("恰好 5 个", prompt)
        self.assertNotIn("用户的修改意见", prompt)
        with_feedback = batch_ai.copy_prompt(
            song_name="爱如潮水", song_mood="", description="", feedback="标题太夸张"
        )
        self.assertIn("标题太夸张", with_feedback)

        copy_schema = json.loads(
            (Path(__file__).parents[1] / "backend" / "batch_copy_schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual(copy_schema["properties"]["tags"]["maxItems"], 5)
        self.assertEqual(copy_schema["properties"]["tags"]["minItems"], 5)

    def test_copy_prefers_the_original_video_copy_and_tags(self) -> None:
        """2026-09-21 用户：「还有生成的标题简介标签都尽量参考原视频的」。

        口径（逐条确认）：① **能沿用就沿用**、以原作品描述为主 —— 旧口径正好相反：预审写着
        「参考原文风格但**不要照抄**」，出图后那次重写更写着「**不要照抄原作品描述**」、
        而且连原标签都没传进去；② **原标签优先占满 5 个**；③ 旧红线继续生效
        （不许互动喊话、不许客观描述句）。
        """
        source = {"description": "随手一跳 今天的风很好", "tags": ["舞蹈", "日常", "卡点舞"]}
        prompts = {
            "预审": batch_ai.preflight_prompt(kind="dance", duration=15.0, **source),
            "按图重写": batch_ai.copy_prompt(
                kind="dance", song_name="", song_mood="",
                description=source["description"], source_tags=source["tags"],
            ),
        }
        for label, prompt in prompts.items():
            self.assertIn("第一来源", prompt, label)
            self.assertIn("能沿用就直接沿用", prompt, label)
            self.assertIn("优先取原作品描述里最像标题的那一句", prompt, label)
            self.assertIn("优先用原标签", prompt, label)
            self.assertIn("不足 5 个才自己补", prompt, label)
            # 旧口径必须消失，否则模型照样另起炉灶
            self.assertNotIn("不要照抄原作品描述", prompt, label)
            self.assertNotIn("参考原文风格但不要照抄", prompt, label)
            # 旧红线继续生效
            self.assertIn("不许互动喊话", prompt, label)
        # 原标签必须**真的进正文**（以前这条路只看得到描述）
        self.assertIn(
            '原标签：["舞蹈", "日常", "卡点舞"]',
            batch_ai.copy_prompt(
                kind="dance", song_name="", song_mood="",
                description=source["description"], source_tags=source["tags"],
            ),
        )

    def test_write_copy_hands_the_source_tags_to_the_prompt(self) -> None:
        """出图后那次重写也要看得到原标签：`write_copy()` 必须把 `source_tags` 传进去。"""
        captured: dict = {}

        async def fake_chat_structured(*, content, schema, name, label):
            captured["text"] = content[0]["text"]
            return {}

        with tempfile.TemporaryDirectory() as raw, patch.object(
            batch_ai, "_chat_structured", fake_chat_structured
        ):
            image = Path(raw) / "candidate.png"
            Image.new("RGB", (8, 8), "#123456").save(image)
            asyncio.run(batch_ai.write_copy(
                kind="dance",
                candidate_image=image,
                description="原描述",
                source_tags=["舞蹈", "日常"],
            ))
        self.assertIn('原标签：["舞蹈", "日常"]', captured["text"])
        self.assertIn("能沿用就直接沿用", captured["text"])

    def test_platform_tags_are_removed_and_replaced(self) -> None:
        result = {
            "introduction": "已有简介",
            "tags": ["#抖音热歌", "舞蹈", "B站翻跳", "小红书日常", "卡点"],
        }
        batch_ai.ensure_copy_fields(
            result, kind="dance", source_tags=["抖音热门", "夜景", "B站推荐"]
        )
        self.assertEqual(result["tags"][:3], ["舞蹈", "卡点", "夜景"])
        self.assertEqual(len(result["tags"]), 5)
        self.assertFalse(any(batch_ai._has_platform_word(tag) for tag in result["tags"]))

    def test_source_tags_fill_the_five_slots_before_the_song_name(self) -> None:
        """标签不足 5 个时**原标签优先**（用户 2026-09-21 选了「原标签优先占满 5 个」）。"""
        result = {
            "title": "标题", "introduction": "简介",
            "tags": ["评论区点歌"], "song_name": "爱如潮水",
        }
        batch_ai.ensure_copy_fields(
            result, kind="singing", description="", source_tags=["原标签A", "原标签B"]
        )
        self.assertEqual(result["tags"][:2], ["原标签A", "原标签B"])
        self.assertNotIn("评论区点歌", result["tags"])
        # ⚠️ 已经够 5 个时**一个字都不许动**：读取路径每次都过 `ensure_copy_fields`，
        # 动了就会把存量条目的标签默默改掉。
        keep = {
            "title": "t", "introduction": "i",
            "tags": ["模型1", "模型2", "模型3", "模型4", "模型5"],
        }
        batch_ai.ensure_copy_fields(keep, kind="singing", description="", source_tags=["原标签A"])
        self.assertEqual(keep["tags"], ["模型1", "模型2", "模型3", "模型4", "模型5"])

    def test_batch_ai_reads_user_env_when_process_env_is_missing(self) -> None:
        """后端由别的进程拉起时 `os.getenv` 只有启动快照：用户 setx 存的配置必须回读注册表。

        2026-09-13 把文本模型切到 DeepSeek 时实测：用「设置用户环境变量」的方式配置，
        新起的后端进程根本读不到 `H3_BATCH_TEXT_BASE_URL` / `H3_BATCH_TEXT_API_KEY`。
        """
        from backend import settings as settings_module

        recorded: dict[str, str] = {}

        def fake_env_value(name: str, default: str = "") -> str:
            recorded[name] = name
            return {
                "H3_BATCH_TEXT_BASE_URL": "https://api.deepseek.com/v1",
                "H3_BATCH_TEXT_API_KEY": "sk-user-level",
                "H3_BATCH_LUNA_MODEL": "deepseek-flash",
            }.get(name, default)

        with patch.dict(
            os.environ,
            {"H3_BATCH_TEXT_API_KEY": "", "OPENAI_API_KEY": ""},
            clear=False,
        ), patch.object(
            settings_module, "env_value", fake_env_value
        ), patch.object(batch_ai, "env_value", fake_env_value):
            batch_ai._CACHED_KEY = None
            self.assertEqual(batch_ai._api_key(), "sk-user-level")
            self.assertIn("H3_BATCH_TEXT_API_KEY", recorded)

    def test_batch_ai_falls_back_to_json_object_when_schema_is_refused(self) -> None:
        """端点不吃 json_schema 时自动改用 json_object（DeepSeek 实测 400）。

        2026-09-13：`deepseek-flash` 对 `response_format: json_schema` 直接 400
        「This response_format type is unavailable now」，但同一把 key 看图是 200 —— 模型
        多模态没问题，是结构化输出的模式不同。降级必须**记住**并且仍然校验必填字段。
        """
        import asyncio

        import httpx as _httpx

        schema = {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
            "additionalProperties": False,
        }
        seen: list[dict] = []

        class FakeResponse:
            def __init__(self, status: int, payload: dict, text: str = "") -> None:
                self.status_code = status
                self._payload = payload
                self.text = text

            def json(self) -> dict:
                return self._payload

        async def fake_post(self, url, headers=None, json=None):  # noqa: A002
            seen.append(json)
            if (json.get("response_format") or {}).get("type") == "json_schema":
                return FakeResponse(
                    400,
                    {"error": {"message": "This response_format type is unavailable now"}},
                    "This response_format type is unavailable now",
                )
            return FakeResponse(
                200, {"choices": [{"message": {"content": '```json\n{"title": "只准你看我的眼睛"}\n```'}}]}
            )

        with patch.object(batch_ai, "LUNA_MODEL", "schema-capable-test-model"), patch.object(
            batch_ai, "_JSON_SCHEMA_SUPPORTED", None
        ), patch.object(
            batch_ai, "_headers", lambda: {"Authorization": "Bearer test"}
        ), patch.object(_httpx.AsyncClient, "post", fake_post):
            result = asyncio.run(
                batch_ai._chat_structured(
                    content=[{"type": "text", "text": "写标题"}],
                    schema=schema,
                    name="t",
                    label="测试",
                )
            )
            self.assertEqual(result, {"title": "只准你看我的眼睛"})
            self.assertIs(batch_ai._JSON_SCHEMA_SUPPORTED, False)
            # 第一次 json_schema → 400，第二次 json_object → 200，且 schema 被写进提示词
            self.assertEqual(len(seen), 2)
            self.assertEqual(seen[1]["response_format"]["type"], "json_object")
            self.assertIn("JSON schema", json.dumps(seen[1], ensure_ascii=False))

        # 降级要**记住**：下一次调用直接走 json_object，不再浪费一次 400
        with patch.object(batch_ai, "_JSON_SCHEMA_SUPPORTED", False):
            self.assertEqual(batch_ai._json_mode(), "object")
        batch_ai._JSON_SCHEMA_SUPPORTED = None

    def test_ds_server_error_is_not_automatically_retried(self) -> None:
        """服务端可能已完成推理：5xx/断线后不能再发第二个计费请求。"""
        import asyncio
        import httpx as _httpx

        from backend import batch_ai

        schema = {
            "type": "object", "properties": {"title": {"type": "string"}},
            "required": ["title"], "additionalProperties": False,
        }
        calls = 0

        class FakeResponse:
            status_code = 500
            text = '{"error":{"message":"upstream response lost"}}'

            def json(self):
                return {"error": {"message": "upstream response lost"}}

        async def fake_post(self, url, headers=None, json=None):  # noqa: A002
            nonlocal calls
            calls += 1
            return FakeResponse()

        with patch.object(batch_ai, "LUNA_MODEL", "deepseek-flash"), patch.object(
            batch_ai, "_headers", lambda: {"Authorization": "Bearer test"}
        ), patch.object(_httpx.AsyncClient, "post", fake_post):
            with self.assertRaisesRegex(RuntimeError, "未自动重试"):
                asyncio.run(batch_ai._chat_structured(
                    content=[{"type": "text", "text": "写标题"}], schema=schema,
                    name="t", label="测试",
                ))
        self.assertEqual(calls, 1)

    def test_submitted_ds_analysis_is_not_resubmitted_after_restart(self) -> None:
        """同一输入已有未确认的 DS 请求时，恢复流程必须走免计费兜底。"""
        from backend import batch_worker

        key = "same-analysis-input"
        self.assertTrue(batch_worker._analysis_request_uncertain({
            "analysisRequestKey": key,
            "analysisRequestState": "submitted",
        }, key))
        self.assertTrue(batch_worker._analysis_request_uncertain({
            "analysisRequestKey": key,
            "analysisRequestState": "unknown",
        }, key))
        self.assertFalse(batch_worker._analysis_request_uncertain({
            "analysisRequestKey": key,
            "analysisRequestState": "completed",
        }, key))
        self.assertFalse(batch_worker._analysis_request_uncertain({
            "analysisRequestKey": "changed-input",
            "analysisRequestState": "submitted",
        }, key))

    def test_unhandled_errors_answer_json_with_a_readable_detail(self) -> None:
        """未处理异常必须回 **JSON**（HTTP 500），不能回纯文本 `Internal Server Error`。

        2026-09-13 用户实测「Unexpected token 'I', "Internal S"... is not valid JSON」：FastAPI 默认
        纯文本 500，前端 `response.json()` 抛解析错误，真正的原因完全看不到；而且当时后端由启动
        脚本拉起、输出没落盘，连 traceback 都没了。
        """
        import asyncio

        from backend import app as app_module
        from starlette.requests import Request

        request = Request({"type": "http", "method": "GET", "path": "/api/boom", "headers": []})
        response = asyncio.run(app_module.unhandled_exception_handler(request, ValueError("模型超时")))
        self.assertEqual(response.status_code, 500)
        body = json.loads(response.body.decode("utf-8"))
        self.assertIn("ValueError", body["detail"])
        self.assertIn("模型超时", body["detail"])
        self.assertEqual(body["path"], "/api/boom")

    def test_system_stats_degrades_instead_of_500(self) -> None:
        """系统采样失败也要回可解析的 JSON（前端拿到非 JSON 只会显示解析错误）。"""
        import asyncio

        from backend import app as app_module
        from backend import system_stats as system_stats_module

        def boom() -> dict:
            raise RuntimeError("nvidia-smi 不见了")

        with patch.object(system_stats_module, "collect_system_stats", boom):
            payload = asyncio.run(app_module.system_stats())
        self.assertIn("degraded", payload)
        self.assertIn("nvidia-smi", payload["degraded"])
        self.assertEqual(payload["gpu"], [])

    def test_backend_modules_import_every_settings_constant_they_use(self) -> None:
        """不许出现「用了 settings 里的常量却没导入」—— 那就是运行期 NameError。

        2026-09-13 用户实测：点「音色转换」直接 500
        「服务内部错误（NameError）：name 'RVC_MODEL' is not defined」——
        `app.py` 用了 `RVC_MODEL` 却没 import。这类错误只有真跑到那一行才会炸，
        单元测试与类型检查都拦不住，所以用 AST 静态扫一遍。
        """
        import ast

        from backend import settings as settings_module

        constants = {name for name in dir(settings_module) if name.isupper()}
        root = Path(__file__).parents[1] / "backend"
        problems: list[str] = []
        for path in sorted(root.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            known: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    known.update(a.asname or a.name for a in node.names)
                elif isinstance(node, ast.Import):
                    known.update((a.asname or a.name).split(".")[0] for a in node.names)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    known.add(node.name)
                elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                    known.add(node.id)
                elif isinstance(node, ast.arg):
                    known.add(node.arg)
                elif isinstance(node, ast.ExceptHandler) and node.name:
                    known.add(node.name)
            used = {
                n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
            }
            missing = sorted((constants & used) - known)
            if missing:
                problems.append(f"{path.name}: {missing}")
        self.assertEqual(problems, [], "这些模块用了 settings 常量却没导入（运行期会 NameError）")
        # 音色转换用到的那个常量必须真的能从 app 里取到
        from backend import app as app_module

        self.assertTrue(str(app_module.RVC_MODEL))

    def test_frontend_never_parses_a_response_as_json_blindly(self) -> None:
        """前端必须走 `readJson` 读响应：非 2xx / 非 JSON 都要翻成人话。

        守住 `.../latest` 那几处 `response.status === 204 ? null : response.json()` ——
        后端一旦回 500（哪怕是纯文本），那种写法就把解析错误当提示抛给用户。
        """
        root = Path(__file__).parents[1] / "src"
        api = (root / "api.ts").read_text(encoding="utf-8")
        self.assertIn("export async function readJson", api)
        self.assertIn("不是 JSON", api)
        for name in ("App.tsx", "MigrateRoute.tsx", "BatchRoute.tsx"):
            source = (root / name).read_text(encoding="utf-8")
            self.assertIn('from "./api"', source, name)
            self.assertIn("readJson", source, name)
        # 这几个路由以前都是 `response.status === 204 ? null : response.json()`
        for name in ("RvcRoute.tsx", "UpScaleRoute.tsx"):
            source = (root / name).read_text(encoding="utf-8")
            self.assertNotIn("? null : response.json()", source, name)
            self.assertIn("readJsonOrNull", source, name)

    def test_batch_ai_fallback_and_action_plan_keep_item_runnable(self) -> None:
        """模型降级时条目仍可继续：文案退到源作品信息，动作/运镜按时长铺满。"""
        result = batch_ai.fallback_result(
            kind="singing",
            description="粉色限定 #爱如潮水remix",
            tags=["爱如潮水", "#翻唱"],
        )
        self.assertEqual(result["style_source"], "video")
        self.assertEqual(result["title"], "粉色限定")
        # 源作品标签排在前面，缺的用兜底池补到恰好 5 个（发布标签固定 5 个）
        self.assertEqual(result["tags"][:2], ["爱如潮水", "翻唱"])
        self.assertEqual(len(result["tags"]), 5)
        # 降级时简介不能是空的（2026-09-13 用户：「流程中简介和标签没有的话自动生成」）
        self.assertTrue(result["introduction"].strip())
        self.assertIn("粉色限定", result["introduction"])
        self.assertIsInstance(result["remove_subtitles"], bool)
        self.assertTrue(
            all(
                isinstance(result[key], str)
                for key in ("title", "introduction", "action_prompt", "camera_prompt")
            )
        )

        action, camera = default_action_plan(20.6)
        self.assertTrue(action.startswith("0–10.3秒："))
        self.assertIn("20.6秒", action)
        self.assertIn("：", camera)
        self.assertNotIn("秒：", camera.split("：")[0])

    def test_batch_copy_fields_are_always_filled(self) -> None:
        """简介/标签缺了就自动生成，覆盖所有路径（用户 2026-09-13）。

        - 模型返回空简介、标签不足 5 个 → 用歌曲信息 + 源作品标签 + 类型兜底池补；
        - 标签多于 5 个 → 截断（发布标签固定 5 个）；
        - 源作品连标签都没有、歌曲也没识别出来 → 也必须有一句简介和 5 个标签，
          否则确认页就是空的（这次 429 降级实测就是这种情况）。
        """
        result = {"song_name": "樱花草", "song_mood": "轻快甜蜜", "introduction": "", "tags": ["翻唱"]}
        filled = batch_ai.ensure_copy_fields(result, kind="singing", description="樱花草花语")
        self.assertEqual(filled, ["简介", "标签"])
        self.assertIn("樱花草", result["introduction"])
        self.assertEqual(len(result["tags"]), 5)
        self.assertEqual(result["tags"][0], "翻唱")

        many = {"introduction": "已有简介", "tags": ["a", "b", "c", "d", "e", "f"]}
        self.assertEqual(batch_ai.ensure_copy_fields(many, kind="singing"), ["标签"])
        self.assertEqual(many["tags"], ["a", "b", "c", "d", "e"])
        self.assertEqual(many["introduction"], "已有简介")

        empty: dict = {"introduction": "   ", "tags": []}
        self.assertEqual(
            batch_ai.ensure_copy_fields(empty, kind="dance", description="", source_tags=[]),
            ["简介", "标签"],
        )
        self.assertTrue(empty["introduction"].strip())
        self.assertEqual(len(empty["tags"]), 5)
        self.assertEqual(len(set(empty["tags"])), 5)

        # 备用池本身必须够 5 个且两类各不相同
        for kind in ("singing", "dance"):
            self.assertEqual(len(batch_ai.TAG_POOL[kind]), 5)
        self.assertNotEqual(batch_ai.TAG_POOL["singing"], batch_ai.TAG_POOL["dance"])

        # 「未识别」这类占位值绝不能当标签（补标签时会把 song_name 也算进候选）
        placeholders = {"song_name": "未识别", "introduction": "有简介", "tags": ["翻唱", "未识别", "未知"]}
        batch_ai.ensure_copy_fields(placeholders, kind="singing")
        self.assertEqual(len(placeholders["tags"]), 5)
        self.assertNotIn("未识别", placeholders["tags"])
        self.assertNotIn("未知", placeholders["tags"])

    def test_batch_image_success_clears_previous_warning(self) -> None:
        """出图成功后必须清掉上一次失败的 warning。

        用户 2026-09-15：「桌面端未产出图片（生成超时或取图失败） 其实已经成功了
        为什么会提示在这个」——那次是前一回 CDP 端口配错留下的 warning，
        重试成功后界面上仍然挂着旧报错，让人以为这次也失败了。
        """
        import inspect

        from backend import chatgpt_image

        source = inspect.getsource(chatgpt_image._generate_candidate_sync)
        self.assertIn("warning=None", source)
        # 必须在成功上传之后
        self.assertLess(source.index("_upload_item_image"), source.index("warning=None"))

    def test_image_automation_is_off_but_subtitles_stay(self) -> None:
        """图片相关的自动化**全部默认关闭**，批量出片后也不再自动加字幕。

        2026-09-16 用户：「去除图片生成的自动化流程吧 只保留生成字幕的流程」——
        以前默认开着的三件事全部关掉：自动出候选人物图、自动出封面、出图后自动放行出片；
        人物图改回**用户自己出、自己上传**，上传后停在「等待你的确认」等人点确认。
        同一天稍晚用户又要求「生成完成唱歌视频之后自动运行的字幕生成……不要自动生成了」，
        所以字幕也退出批量链路，只在独立路由 `/subtitle` 手动跑（实现保留便于恢复）。
        """
        import inspect

        from backend import batch_worker, chatgpt_image

        # 注意：本机 Windows 用户级变量里可能还留着历史值，这里用 `process_env_only`
        # 隔离成「进程环境说了算」，验的是**代码默认值**（默认关闭）。
        with process_env_only():
            self.assertFalse(chatgpt_image.auto_candidate_enabled())  # 不自动出人物图
            self.assertFalse(chatgpt_image.auto_cover_enabled())  # 不自动出封面
            self.assertFalse(chatgpt_image.auto_confirm_enabled())  # 不自动放行出片

        source = inspect.getsource(batch_worker._prepare_review_work)
        # 自动放行的代码还在（在 `_auto_confirm_prepared_item` 里），但必须**有开关守着**
        auto = inspect.getsource(batch_worker._auto_confirm_prepared_item)
        self.assertIn("已自动放行出片", auto)
        self.assertIn("_cgi_auto_confirm()", auto)
        # 2026-10-18：封面改到出片后从成片抽帧生成，备料里**不再**触发任何封面生成
        self.assertNotIn("_maybe_spawn_covers(", source)
        self.assertNotIn("_finish_prep_covers(", source)
        # 关掉时也要给用户一句明确提示（素材已就绪，等你自己出图上传）
        self.assertIn("自动出图已关闭", source)

        # 出片前的发布成品检查只体检、不自动出图
        ensure = inspect.getsource(batch_worker.ensure_publish_images)
        self.assertIn("allow_spawn", ensure)
        confirmed = inspect.getsource(batch_worker._process_confirmed)
        self.assertIn("allow_spawn=False", confirmed)

        # 批量链路**不再自动加字幕**：实现保留（便于恢复），但 `_process_confirmed` 不许再调用
        self.assertNotIn("await _add_subtitles_if_singing", confirmed)
        self.assertIn("最终成片_字幕版", inspect.getsource(batch_worker._add_subtitles_if_singing))

    def test_batch_stopping_also_closes_comfyui(self) -> None:
        """停止/取消之后要顺手关掉 ComfyUI，别让它空转占显存。

        用户 2026-09-15：「停止的话 comfyui 你也要关闭」——实测取消一条之后
        ComfyUI 进程从 19:31 一直开到晚上、队列里还留着被取消的那个 prompt 白烧显存。
        注意必须先 `/interrupt` + 清队列：`/api/comfy/stop` 见到队列非空会 409 拒绝关闭。
        """
        import inspect

        from backend import batch_worker

        helper = inspect.getsource(batch_worker._stop_comfy_if_idle)
        self.assertIn("/api/comfy/stop", helper)
        self.assertIn("interrupt", helper)  # 先掐残留 prompt
        self.assertIn("clear", helper)
        # 批次里还有没结束的条目就不关（免得来回启停）
        self.assertIn("awaiting_review", helper)

        # 三条停止路径都要接上
        for fn in (
            batch_worker._abandon_without_runner,
            batch_worker.salvage_abandoned_items,
            batch_worker.run_batch,
        ):
            self.assertIn("_stop_comfy_if_idle", inspect.getsource(fn))

    def test_batch_auto_image_never_regenerates_existing(self) -> None:
        """已有图就不再重复生成（用户 2026-09-15：「自动化流程如果有对应图片了不需要重复生成」）。

        候选人物图与两张封面都要守：否则「重新备料 / 重新开始」这类路径会白跑一次出图，
        既费时间也吃 ChatGPT 额度。
        """
        import inspect

        from backend import chatgpt_image

        candidate = inspect.getsource(chatgpt_image._generate_candidate_sync)
        self.assertIn("跳过重复生成", candidate)
        self.assertIn("reference_image_path", candidate)

        covers = inspect.getsource(chatgpt_image._covers_sync)
        self.assertIn("封面已存在，跳过生成", covers)

    def test_batch_prompt_goes_as_file_attachment_not_typed(self) -> None:
        """长提示词必须以 **.txt 附件**发出，不再粘进输入框。

        用户 2026-09-16：「9比16图片.txt 和 4比3图片.txt 提示词太长了自动化的适合需要
        将它变更为文本 而不是在输入框框中输入了 不然会被风控」——9:16 那份有 1.3 万字，
        一次性 `insert_text` 进 ProseMirror 既招风控又把输入框撑到 1700px 高。
        同日追加：「你不是已经又提示词文件了吗 问什么输入框还要重新输入一遍」——
        所以**输入框保持空的，什么都不打，直接发附件**（`H3_CHATGPT_PROMPT_SAY=1` 才补一句）。
        实现：`cd.generate(prompt_file=…)` 把 .txt 挂到「没有 accept 的 file input」，
        发送走发送按钮（空输入框下 Enter 无效），并用「停止生成」按钮二次确认已发出；
        挂不上才退回粘贴（宁可长文也不要发一条没有规则的请求）。
        """
        import inspect

        from backend import chatgpt_cdp, chatgpt_image

        self.assertFalse(chatgpt_cdp.prompt_as_file_enabled())  # 默认**不上传文本**
        self.assertFalse(chatgpt_cdp.prompt_say_enabled())  # 默认**不**再打字

        source = inspect.getsource(chatgpt_cdp.generate)
        self.assertIn("prompt_file", source)
        self.assertIn("attach_prompt_file", source)
        # 附件模式下输入框必须是空的；只有开关打开时才补短说明
        self.assertIn('typed_prompt = ""', source)
        self.assertIn("prompt_say_enabled", source)
        # 打字必须是**有条件**的（没内容就不碰输入框）
        self.assertIn("if typed_prompt.strip():", source)
        # 2026-09-19 修正：写入提示词的实现从命名辅助函数 `insert_text(typed_prompt)` 换成了
        # 内联 `page.evaluate(JS, typed_prompt)`（见 chatgpt_cdp.generate），所以旧标记已不存在。
        # 断言意图不变：**真正的写入必须发生在非空判据之内**，用紧跟该判据的日志行做锚点。
        self.assertLess(
            source.index("if typed_prompt.strip():"), source.index("开始写入提示词")
        )
        # 挂不上时不能静默发一条空规则，也不能再粘进输入框 → 只能当消息正文发
        self.assertIn("改为把提示词作为消息正文发出", source)

        attach = inspect.getsource(chatgpt_cdp.attach_prompt_file)
        self.assertIn('input[type="file"]:not([accept])', attach)  # 挑通用附件口，别塞进 image/*
        # 芯片是异步渲染的，必须**轮询**等，不能只 sleep 一下就判没挂上
        # （2026-09-16 实测：等 1.8 秒误判 → 走了「退回粘贴」→ 用户看到输入框里又是长文）
        self.assertIn("_wait_for_attachment", attach)
        self.assertNotIn("time.sleep(1.8)", attach)

        # 挂不上的兜底只能是「当消息正文发出去」，**绝不许**再粘进输入框
        self.assertIn("改为把提示词作为消息正文发出", source)
        self.assertNotIn("退回直接输入提示词", source)

        send = inspect.getsource(chatgpt_cdp._send)
        self.assertIn("_generating_now", send)  # 空输入框下只能靠 stop 按钮确认
        self.assertIn("if not _composer_empty():", send)  # 空输入框不试 Enter

        # 短说明（仅在 SAY=1 时用）本身必须点名附件，否则模型会回一句「请告诉我需求」
        text = chatgpt_cdp.default_prompt_instruction("提示词.txt")
        self.assertIn("提示词.txt", text)
        self.assertLess(len(text), 120)

        # 两个出图调用点都要带附件（候选人物图 + 封面）
        candidate = inspect.getsource(chatgpt_image._generate_candidate_sync)
        self.assertIn("write_prompt_attachment", candidate)
        self.assertIn("prompt_file=prompt_file", candidate)
        covers = inspect.getsource(chatgpt_image._covers_sync)
        self.assertIn("prompt_file=write_prompt_attachment", covers)

        # 落盘的附件必须是本次真正要发的内容
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(tempfile, "gettempdir", return_value=tmp):
                path = chatgpt_image.write_prompt_attachment("规则正文")
            self.assertEqual(path.read_text(encoding="utf-8"), "规则正文")
            self.assertEqual(path.name, "提示词.txt")

    def test_batch_cover_prompts_keep_the_portrait_background(self) -> None:
        """封面的背景必须和人物图**完全相同**（2026-09-16 用户：「生成的封面图背景要和人物图背景相同」）。

        封面与人物图是同一套物料，背景一换就图文脱节；所以两条封面指令都要带
        `COVER_BACKGROUND_RULE`，并且要明确禁止「换背景/改环境/改色调/加光斑装饰」。
        """
        from backend import chatgpt_image

        rule = chatgpt_image.COVER_BACKGROUND_RULE
        self.assertIn("背景", rule)
        for forbidden in ("不要换背景", "不要重新设计环境", "同一色调", "不要加新的光斑"):
            self.assertIn(forbidden, rule)
        self.assertEqual(len(chatgpt_image.COVER_TASKS), 2)
        names = [task[2] for task in chatgpt_image.COVER_TASKS]
        self.assertEqual(names, ["封面_B站4x3.png", "封面_抖音3x4.png"])
        for _tag, instruction, _name in chatgpt_image.COVER_TASKS:
            self.assertIn("背景", instruction)
            self.assertIn(rule, instruction)  # 两块指令都要真的带上这条规则
        # 第一条要贴人物图（背景来源），第二条只能复用上文
        import inspect

        covers = inspect.getsource(chatgpt_image._covers_sync)
        self.assertIn("imgs = [image_file] if first else []", covers)

    def test_batch_camera_timeline_never_changes_framing(self) -> None:
        """运镜时间轴**不得改变构图**（推近/拉远/变焦）。

        2026-09-15 用户：「唱歌视频的运镜出现了一瞬间的从远到近的弹跳式的镜头 为什么」——
        视频由多个 ~15 秒片段各自独立生成后再拼接，片段之间不共享镜头状态，
        接缝（14.17–15.08 秒等）前后一旦构图在变就会跳一下（实测提示词写了
        「6.5–13.5秒：镜头缓慢向人物轻微推近」后，接缝处出现从远到近的弹跳）。
        所以提示词里必须是硬禁止，而不是仅提示「镜头基本不动也可以」。
        """
        from backend.batch_ai import ACTION_RULES, preflight_prompt

        self.assertIn("不得改变构图", ACTION_RULES)
        for forbidden in ("推近", "拉远", "变焦"):
            self.assertIn(forbidden, ACTION_RULES)  # 明确点名禁止
        self.assertIn("轻微自然手持漂移", ACTION_RULES)
        # 模板仍可正常按 duration 格式化（两处提示词都靠它）
        rendered = ACTION_RULES.format(duration="27.3")
        self.assertIn("27.3", rendered)
        preflight = preflight_prompt(kind="singing", duration=27.3, description="", tags=[])
        self.assertIn("不得改变构图", preflight)

    def test_batch_copy_prompts_ask_for_creator_voice_not_description(self) -> None:
        """发布文案必须是**创作者口吻**（第一人称情绪），不能是画面描述、也不能喊话互动。

        用户 2026-09-13：「这个完全不像啊 你这是在陈述啊 我是内容创作者啊」——旧的提示词只有
        「introduction：一到两句简短简介」+「必须能对上画面」，模型于是写出「长发女孩身穿酒红色
        上衣，在蓝色夜景前直视镜头」这种画面说明，根本不能直接发。
        同日追加：「简介：🤍 评论区告诉我下一首想看我跳什么～ 不要这种话」——互动喊话/向观众提问
        也一律不要，提示词禁止 + `sanitize_introduction` 事后剪掉双保险。
        """
        from backend.batch_ai import (
            compose_introduction,
            copy_prompt,
            preflight_prompt,
            sanitize_introduction,
        )

        preflight = preflight_prompt(kind="singing", duration=20.0, description="", tags=[])
        self.assertIn("发布用文案，不是画面说明", preflight)
        self.assertIn("禁止客观描述句", preflight)
        self.assertIn("创作者口吻", preflight)
        self.assertIn("不许互动喊话", preflight)
        self.assertIn("用户明确说过「不要这种话」", preflight)
        copy_text = copy_prompt(song_name="", song_mood="", description="")
        self.assertIn("内容创作者的发布文案", copy_text)
        self.assertIn("创作者口吻", copy_text)
        self.assertIn("绝对不要复述画面", copy_text)
        self.assertIn("不许互动喊话", copy_text)

        intro = compose_introduction("singing", song_name="爱情专属权")
        self.assertIn("爱情专属权", intro)
        for banned in ("画面", "身穿", "光线", "评论区", "你会想起谁", "？", "?"):
            self.assertNotIn(banned, intro)
        dance_intro = compose_introduction("dance")
        self.assertIn("懂的人", dance_intro)
        self.assertNotIn("画面", dance_intro)
        for banned in ("评论区", "看到最后", "？"):
            self.assertNotIn(banned, dance_intro)

        # 模型不听话时也要能兜住：互动喊话截断、向观众提问整句丢掉
        dirty = "甜到忍不住想拉你一起跳，你会先牵哪只手？🤍 评论区告诉我下一首想看我跳什么～"
        cleaned = sanitize_introduction(dirty, kind="dance")
        self.assertNotIn("评论区", cleaned)
        self.assertNotIn("你会先牵哪只手", cleaned)
        self.assertIn("甜到忍不住想拉你一起跳", cleaned)
        # 整句都是喊话 → 退回本地兜底文案（不能留空）
        fallback = sanitize_introduction("评论区告诉我你们想听什么？", kind="dance")
        self.assertNotIn("评论区", fallback)
        self.assertTrue(fallback.strip())

    def test_batch_dance_copy_never_says_cover_song(self) -> None:
        """跳舞视频的标题 / 简介 / 标签里不许出现「翻唱」这类唱歌用词。

        2026-09-15 用户：「正常跳舞视频都是舞蹈，手势舞之类的 我看你现在的简介或者标题跳舞
        都会写上翻唱 这是不对的」。根因是 `copy_prompt` 没有类型参数，跳舞也走「歌曲」那套
        背景与要求；这里同时守住「提示词按类型分开」与「落盘前确定性清掉」两层。
        """
        # ① 提示词：跳舞分支明确禁止翻唱，唱歌分支保留原来的「歌曲」背景
        dance_prompt = batch_ai.copy_prompt(kind="dance", song_name="", song_mood="", description="手势舞")
        self.assertIn("不是唱歌视频", dance_prompt)
        self.assertIn("严禁", dance_prompt)
        self.assertIn("翻唱", dance_prompt)             # 作为**禁止项**出现在提示词里
        self.assertNotIn("歌曲：《", dance_prompt)       # 跳舞不再用「歌曲」背景
        singing_prompt = batch_ai.copy_prompt(
            kind="singing", song_name="爱如潮水", song_mood="抒情", description=""
        )
        self.assertIn("歌曲：《爱如潮水》", singing_prompt)
        self.assertNotIn("不是唱歌视频", singing_prompt)
        # 预审提示词同样带类型守则
        dance_preflight = batch_ai.preflight_prompt(kind="dance", duration=20.0, description="", tags=[])
        self.assertIn("不是唱歌视频", dance_preflight)

        # ② 落盘兜底：模型/源作品给出的「翻唱」标题、简介、标签都被就地纠正
        result = {
            "title": "《爱如潮水》翻唱",
            "introduction": "《爱如潮水》翻唱｜戴上耳机听更清楚🎧",
            "tags": ["翻唱", "手势舞"],
        }
        filled = batch_ai.ensure_copy_fields(
            result, kind="dance", description="手势舞 #热门", source_tags=["翻唱"]
        )
        self.assertNotIn("翻唱", result["title"])
        self.assertNotIn("翻唱", result["introduction"])
        self.assertNotIn("翻唱", "".join(result["tags"]))
        self.assertIn("标题", filled)
        self.assertIn("简介", filled)
        self.assertEqual(len(result["tags"]), 5)

        # 跳过的条目在读取路径上也会自愈（batch_store._normalize → _backfill_copy_fields）
        self.assertTrue(batch_ai.has_singing_wording("《X》翻唱"))

        # ③ 降级标题不再写死「翻唱作品」
        fallback = batch_ai.fallback_result(kind="dance", description="", tags=[])
        self.assertNotIn("翻唱", fallback["title"])
        self.assertTrue(fallback["title"].strip())

        # ④ 唱歌条目不受影响：「翻唱」照旧保留
        keep = {"title": "翻唱《爱如潮水》", "introduction": "《爱如潮水》翻唱🎧", "tags": ["翻唱"]}
        batch_ai.ensure_copy_fields(keep, kind="singing", description="", source_tags=["翻唱"])
        self.assertIn("翻唱", keep["tags"])
        self.assertEqual(batch_ai._clean_tags(["翻唱"], kind="singing"), ["翻唱"])
        self.assertEqual(batch_ai._clean_tags(["翻唱"], kind="dance"), [])
        # 「舞蹈翻跳」是舞蹈自己的说法，不能被误伤
        self.assertEqual(batch_ai._clean_tags(["舞蹈翻跳"], kind="dance"), ["舞蹈翻跳"])

    def test_batch_item_title_follows_the_published_title(self) -> None:
        """条目标题只认发布标题 `ai.title`（用户 2026-09-13：「批量生成任务 4 为什么标题不一致」）。

        队列左侧显示 `item.title`（预审阶段写的），审核面板「标题」与发布文案/发布目录用
        `ai.title`（上传候选图后按图重写过），两个字段各自更新就会出现两个标题。读取时同步。
        """
        from backend import batch_store as batch_store_module
        from backend.batch_store import BatchStore

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            original = batch_store_module.DB_PATH
            try:
                batch_store_module.DB_PATH = Path(folder) / "queue.db"
                store = BatchStore()
                state = new_batch_state([], ["https://www.douyin.com/video/7680438814691729585"])
                state["items"][0].update(
                    status="awaiting_review",
                    stage="review",
                    title="一支舞，把整段行程成了痛",          # 预审阶段写的旧标题
                    ai={"title": "这支没人听过的曲子，我跳了很久"},  # 上传候选图后重写的发布标题
                )
                store.create(state)
                stored = store.get(state["id"])["items"][0]
                self.assertEqual(stored["title"], "这支没人听过的曲子，我跳了很久")

                # 还没有发布标题（没备过料）时不乱改
                blank = new_batch_state(["https://v.douyin.com/x"], [])
                blank["items"][0].update(title="原始标题", ai={"introduction": "有简介"})
                store.create(blank)
                self.assertEqual(store.get(blank["id"])["items"][0]["title"], "原始标题")
            finally:
                batch_store_module.DB_PATH = original

    def test_batch_queue_and_detail_show_one_title(self) -> None:
        """前端队列与详情必须用同一个标题来源，不许一边 item.title、一边 ai.title。"""
        source = (Path(__file__).parents[1] / "src" / "BatchRoute.tsx").read_text(encoding="utf-8")
        self.assertIn("function itemTitle(item: BatchItem)", source)
        self.assertIn("<strong>{itemTitle(item)}</strong>", source)
        self.assertIn("<h2>{itemTitle(selected)}</h2>", source)
        self.assertNotIn("{selected.title}</h2>", source)
        # 队列行不再直接用 item.title（只能通过 itemTitle() 走统一来源）
        self.assertNotIn("<strong>{item.title", source)

    def test_batch_store_backfills_missing_copy_on_read(self) -> None:
        """已经备过料的条目（比如 429 降级留下的空简介/空标签）读取时就自愈。"""
        from backend import batch_store as batch_store_module
        from backend.batch_store import BatchStore

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            original = batch_store_module.DB_PATH
            try:
                batch_store_module.DB_PATH = Path(folder) / "queue.db"
                store = BatchStore()
                state = new_batch_state([], ["https://www.douyin.com/video/7000000000000000123"])
                item = state["items"][0]
                item.update(
                    status="awaiting_review",
                    stage="review",
                    title="你在哪 我的心就在哪",
                    sourceMetadata={"desc": "你在哪 我的心就在哪", "tags": []},
                    ai={"imagePrompt": "本地拼好的提示词", "introduction": "", "tags": []},
                )
                store.create(state)

                fresh = store.get(state["id"])["items"][0]["ai"]
                self.assertTrue(fresh["introduction"].strip())
                self.assertEqual(len(fresh["tags"]), 5)
                self.assertEqual(len(set(fresh["tags"])), 5)
                self.assertEqual(fresh["imagePrompt"], "本地拼好的提示词")   # 原有字段不许被破坏

                # 已经有完整文案的条目不动它
                def keep(row: dict) -> None:
                    row["ai"] = {
                        "introduction": "手写简介",
                        "tags": ["a", "b", "c", "d", "e"],
                    }

                store.mutate_item(state["id"], item["id"], keep)
                self.assertEqual(store.get(state["id"])["items"][0]["ai"]["introduction"], "手写简介")
                self.assertEqual(store.get(state["id"])["items"][0]["ai"]["tags"][0], "a")
            finally:
                batch_store_module.DB_PATH = original

    def test_batch_deliver_no_longer_renders_covers(self) -> None:
        """双封面改由用户自己在 GPT 聊天里出，交付阶段只留成片与发布文案。"""
        source = (Path(__file__).parents[1] / "backend" / "batch_worker.py").read_text(encoding="utf-8")
        self.assertNotIn("render_covers", source)
        self.assertNotIn("coverBilibili", source)
        self.assertNotIn("coverDouyin", source)
        # 交付仍然要写发布文案
        self.assertIn("发布文案.txt", source)

    def test_batch_only_the_in_progress_item_shows_its_time(self) -> None:
        """表格里的时间只属于**出片**，且只算「生成最终视频」这一段。

        2026-09-15 用户：「你只需统计 本条的时间 我不关心所有任务的时间」→ 表格不再给每条任务
        都显示时间。随后用户指出「我想知道的是单条用时，现在的时间不对都 22 小时还多了」——
        旧计时从条目 `createdAt`（加入队列）算起，批次放了 22 小时就一直累加。
        2026-09-20 用户看到**备料中**的那条顶着 `00:00`（并行编排里 `currentItemId` 会指到备料的
        那一条）、而真正在出片（33%）的那条反而是空的，明确：「时间应该在出片上 备料我不关心时间」
        —— 现在 `itemRenderMs` **只看 `video` 里程碑**（下载 / 备料 / 等确认一律返回 null），
        行内则给「正在出片的那一条」显示（`itemIsRendering` 或 `currentItemId`）。
        批次总耗时仍然不显示（2026-09-13 用户也要求过）。
        """
        source = (Path(__file__).parents[1] / "src" / "BatchRoute.tsx").read_text(encoding="utf-8")
        # 出片用时：只认 video 里程碑，下载 / 备料 / 等确认不算
        self.assertIn("function itemRenderMs(item: BatchItem, nowMs: number)", source)
        self.assertIn('step.id === "video"', source)
        # 谁在出片就给谁显示时间（并行时 currentItemId 可能是备料那条）
        self.assertIn("function itemIsRendering(item: BatchItem)", source)
        self.assertIn('item.stage === "video" || item.stage === "deliver"', source)
        self.assertIn("(itemIsRendering(item) || item.id === batch?.currentItemId)", source)
        self.assertIn("本条出片用时", source)
        self.assertIn("formatElapsedMs(itemElapsedMs)", source)
        # 不再从「加入队列的那一刻」算起（22 小时的根因）
        self.assertNotIn("elapsedMs(selected.createdAt", source)
        self.assertNotIn("elapsedMs(item.createdAt", source)
        # 表格里只给正在出片的那一条显示时间（只要时间本身，不加「已用 / 用时」这类标签字）
        self.assertIn("{itemRenderText(item)}", source)
        # 不再给每条任务都显示时间
        self.assertNotIn("queueTime(item)", source)
        self.assertNotIn("batch-time-cell", source)
        # 跳秒逻辑保留，让计时器实时更新（批次可能刚收尾）
        self.assertIn("const queueLive = visibleItems.some(", source)
        self.assertIn("useNowTick(batchLive || queueLive)", source)
        # 批次总耗时整块去掉（注释里提到这几个字不算）
        rendered = re.sub(r"\{/\*.*?\*/\}", "", source, flags=re.S)
        rendered = re.sub(r"^\s*//.*$", "", rendered, flags=re.M)
        self.assertNotIn("batchElapsedMs", rendered)
        self.assertNotIn("批次总耗时", rendered)
        # 备料区后来单独加入了「已用」计时，旧的全文件禁词断言已失效。
        self.assertNotIn("用时 {", rendered)

    def test_batch_row_shows_real_progress_percent(self) -> None:
        """列表行也显示进度百分比（不用点开），且只认真实非零进度、绝不造假。

        2026-09-15 用户：「除了时间 进入也同步的列表中 像现在的 17% 这样的 我有时候不想点开来看」。
        """
        source = (Path(__file__).parents[1] / "src" / "BatchRoute.tsx").read_text(encoding="utf-8")
        # 行内徽章里带百分比
        self.assertIn("function itemPercent(item: BatchItem)", source)
        self.assertIn('className="batch-status-percent"', source)
        # 来源：出片子任务进度优先，其次**正在出片**的那一格
        self.assertIn("item.childJob?.progress", source)
        self.assertIn('step.id === "video" && step.status === "running"', source)
        # 只认 > 0 的真实进度（0/null 一律不显示数字）
        self.assertIn('typeof child === "number" && child > 0', source)
        self.assertNotIn("item.childJob?.progress || 0", source)
        self.assertNotIn("itemPercent(item) ?? 0", source)
        # 备料阶段的本地粗刻度不往列表上放
        self.assertNotIn('step.id === "prepare" && step.status === "running"', source)

    def test_batch_cancel_asks_nothing_when_nothing_is_rendering(self) -> None:
        """没在出片的条目取消时直接执行，不弹「不会动到其它条目」这类无意义提示。

        2026-09-15 用户：「这一条还没开始出片，停止这次放行不会动到其它条目。停止后可以点
        「重新开始」。不会影响到的不需要又这个提示 可以直接取消就好了」——只有 `renderingNow`
        为真（真在出片、进度会作废）时才需要确认。
        """
        source = (Path(__file__).parents[1] / "src" / "BatchRoute.tsx").read_text(encoding="utf-8")
        # 注释里提到那句提示不算（注释会被剥掉再断言）
        rendered = re.sub(r"\{/\*.*?\*/\}", "", source, flags=re.S)
        rendered = re.sub(r"^\s*//.*$", "", rendered, flags=re.M)
        # 那条「不会动到其它条目」的提示整句删掉
        self.assertNotIn("不会动到其它条目", rendered)
        # 行内「取消」：只有 renderingNow 为真（真在出片、进度会作废）才拦一次
        self.assertIn("renderingNow(item)", source)
        self.assertIn("!(await askConfirm({", source)
        self.assertIn("已经生成到一半的进度会作废", source)
        # 批量「跳过」同理：勾选里没有正在出片的就不过弹框
        self.assertIn("selectedItems.some(renderingNow)", source)

    def test_batch_confirm_dialog_is_a_styled_page_modal(self) -> None:
        """确认框改成页内深色弹框，不再用系统原生的 window.confirm。

        2026-09-15 用户：「弹出框的效果样式改下 现在的不好看」——原生灰白弹框跟深色壳子不是一个
        东西，也没法把「影响哪几条」列出来。现在统一走 `askConfirm()`（Promise<boolean>）+
        `.batch-modal` 样式，Esc / 点遮罩 = 取消，危险操作用红色确认键。
        """
        root = Path(__file__).parents[1]
        source = (root / "src" / "BatchRoute.tsx").read_text(encoding="utf-8")
        styles = (root / "src" / "styles.css").read_text(encoding="utf-8")
        # 原生弹框一个都不许留（注释里提到这几个字不算）
        code = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
        code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
        self.assertNotIn("window.confirm", code)
        self.assertNotIn("window.alert", code)
        # 页内弹框：Promise 化的 askConfirm + 受控渲染
        self.assertIn("interface ConfirmAsk", source)
        self.assertIn("const askConfirm = (ask: ConfirmAsk): Promise<boolean>", source)
        self.assertIn('className="batch-modal-backdrop"', source)
        self.assertIn('role="alertdialog"', source)
        self.assertIn("answerConfirm(false)", source)
        # Esc 取消、危险操作红色
        self.assertIn('event.key === "Escape"', source)
        self.assertIn("danger: true", source)
        # 真的给它写了样式（不是只有结构）
        for selector in (
            ".batch-modal-backdrop",
            ".batch-modal {",
            ".batch-modal-icon",
            ".batch-modal-items",
            ".batch-modal-actions button.danger",
        ):
            self.assertIn(selector, styles)
        # 批量删除 / 跳过要把受影响的条目列出来
        self.assertIn("items: selectedItems.map((item) => `第 ${item.index} 条 · ${itemTitle(item)}`)", source)

    def test_batch_status_tabs_show_prep_and_render_zones(self) -> None:
        """批量页分开备料、出片、已完成；状态徽章仍保留。"""
        source = (Path(__file__).parents[1] / "src" / "BatchRoute.tsx").read_text(encoding="utf-8")
        self.assertIn('type TabId = "prep" | "render" | "exception" | "archive" | "completed";', source)
        self.assertIn('{ id: "prep", label: "备料区" }', source)
        self.assertIn('{ id: "render", label: "出片区" }', source)
        self.assertIn('{ id: "exception", label: "异常" }', source)
        self.assertIn('{ id: "completed", label: "已完成" }', source)
        self.assertIn('exception: (item) => item.status === "failed"', source)
        self.assertLess(source.index('{ id: "archive", label: "已跳过" }'), source.index('{ id: "completed", label: "已完成" }'))
        self.assertIn('completed: (item) => item.status === "completed"', source)
        self.assertNotIn('showCompleted', source)
        self.assertIn('useState<TabId>("prep")', source)
        self.assertIn('prepBlocked', source)
        # 细状态没有被删掉：行内徽章仍按 item.status 显示（再带上 stage，好把并行的
        # 「备料中」与「出片中」分开 —— 2026-09-20 用户看到两条「正在处理」问过为什么）
        self.assertIn("batchStatusLabel(item.status, item.stage)", source)
        # 旧的细标签不该再出现在 TABS 里
        for gone in (
            '{ id: "preparing"',
            '{ id: "rendering"',
            '{ id: "skipped"',
            '{ id: "failed"',
            '{ id: "all"',
        ):
            self.assertNotIn(gone, source)

    def test_batch_edit_rule_toggle_and_reopen_scope(self) -> None:
        """未完成（非运行中/已完成）都能直接编辑；再点同一行收起；「回到确认」只给运行中+已完成。

        2026-09-15 用户：「首次点击现在是张开，再次点击要收起」+「只要状态是未完成的任务都
        可以进行编辑，当然正在运行的那条不允许编辑」；随后确认「回到确认」保留，只给出片中/
        重新备料与已完成（confirmed/failed/skipped 用直接编辑替代）。
        """
        source = (Path(__file__).parents[1] / "src" / "BatchRoute.tsx").read_text(encoding="utf-8")
        # 再点同一行就收起
        self.assertIn("setSelectedId((current) => (current === itemId ? null : itemId))", source)
        # 详情完全手动：没有自动跟随 / 自动展开，启动时列表全收起
        self.assertNotIn("followedItemRef", source)
        self.assertNotIn("userInteractedRef", source)
        self.assertNotIn("setSelectedId(batch.currentItemId)", source)
        self.assertNotIn("setSelectedId(state.currentItemId", source)
        # 编辑规则：运行中/重新备料/已完成/已删除 不可编辑，其余都能
        self.assertIn(
            '!["running", "revising", "completed", "deleted"].includes(selected.status)',
            source,
        )
        # 换图对**所有可编辑**条目开放（不再只限审核点）
        self.assertIn("{hasImage && editable && (", source)
        # 「回到确认」只保留给 出片中/重新备料/已完成
        self.assertIn('["running", "revising", "completed"].includes(selected.status)', source)

    def test_batch_detail_is_manual_only(self) -> None:
        """详情只由用户点开：启动时全收起、不自动跟随当前条目、标签可以自由切换。

        用户 2026-09-15：「现在批量默认展开详情的时间切换不了」+「我希望启动页面的时候
        列表默认都是收起来的 由我自己点击要查看哪个」——旧的两套自动行为（跟随 `currentItemId`
        自动展开、以及状态变化时把标签切回选中条目所在状态）正是「切换不了」的根因：
        点开别的标签会被立刻切回去，收起也会被重新展开。
        """
        source = (Path(__file__).parents[1] / "src" / "BatchRoute.tsx").read_text(encoding="utf-8")
        # 点行 = 展开 / 再点 = 收起
        self.assertIn("const selectItem = (itemId: string)", source)
        self.assertIn("onClick={() => selectItem(item.id)}", source)
        self.assertIn("setSelectedId((current) => (current === itemId ? null : itemId))", source)
        # 不再有任何「自动选中 currentItemId」的路径
        self.assertNotIn("followedItemRef", source)
        self.assertNotIn("setSelectedId(batch.currentItemId)", source)
        self.assertNotIn("setSelectedId(state.currentItemId", source)
        # 不再因为「选中的条目不属于当前标签」就把标签切回去
        self.assertNotIn("tabForStatus", source)
        self.assertIn("const switchTab = (id: TabId) => {", source)
        # 选中项是空（全收起）时不该自动展开任何一条
        self.assertIn("const [selectedId, setSelectedId] = useState<string | null>(null);", source)

    def test_batch_page_hides_the_prompt_blocks(self) -> None:
        """「已填写的迁移提示词 / 动作与运镜」整块不再展示。

        用户 2026-09-13：「已填写的迁移提示词 这块内容整个都可以去掉 我不关心」。
        提示词仍然照常提交给工作流，只是页面上不再是一块只读内容。
        """
        source = (Path(__file__).parents[1] / "src" / "BatchRoute.tsx").read_text(encoding="utf-8")
        rendered = re.sub(r"\{/\*.*?\*/\}", "", source, flags=re.S)
        rendered = re.sub(r"^\s*//.*$", "", rendered, flags=re.M)
        for phrase in ("已填写的迁移提示词", "已填写的动作与运镜", "batch-prompt-list", "promptBlocks"):
            self.assertNotIn(phrase, rendered, f"批量页不该再渲染「{phrase}」")
        # 提交给工作流的字段本身不能被误删
        for field in ("content_prompt", "video_prompt", "image_prompt", "action_prompt", "camera_prompt"):
            self.assertIn(field, source)

    def test_batch_flow_has_four_steps_only(self) -> None:
        """进度只有 下载 / 备料 / 审核 / 出片 四步：歌词字幕与「整理发布文件」都不占格。

        - 歌词字幕：2026-09-14 用户确认去掉这一步（流程里挂着一个永远不产出的步骤）；
        - 交付：2026-09-13 用户看到「发布文件已整理」后要求「直接去掉这一格」，交付照做但不再占进度。
        新条目与历史条目（`RETIRED_MILESTONE_IDS` 在读取路径剔除）都不显示这两步。
        """
        from backend import batch_store as batch_store_module
        from backend.batch_store import BatchStore
        from backend.batch_worker import item_milestones

        ids = [step["id"] for step in item_milestones("singing")]
        self.assertEqual(ids, ["download", "prepare", "review", "video"])
        self.assertEqual(ids, [step["id"] for step in item_milestones("dance")])

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            original = batch_store_module.DB_PATH
            try:
                batch_store_module.DB_PATH = Path(folder) / "queue.db"
                store = BatchStore()
                state = new_batch_state(["https://v.douyin.com/old"], [])
                # 模拟历史条目里残留的歌词字幕与发布整理里程碑
                state["items"][0]["milestones"].append(
                    {"id": "lyrics", "label": "生成歌词字幕版", "status": "skipped"}
                )
                state["items"][0]["milestones"].append(
                    {"id": "deliver", "label": "整理发布文件", "status": "completed"}
                )
                store.create(state)
                stored = store.get(state["id"])
                kept = [step["id"] for step in stored["items"][0]["milestones"]]
                self.assertNotIn("lyrics", kept)
                self.assertNotIn("deliver", kept)
            finally:
                batch_store_module.DB_PATH = original

    def test_batch_reuses_previous_prep_for_the_same_aweme(self) -> None:
        """同一条作品之前已备过料（没出片就重跑了）→ 直接沿用，不再下载/抽帧/调模型。

        用户 2026-09-13：「如果已经建立的文件 当我复制抖音链接的时候不要在重复建立了直接往下走」
        +「我可能只跑完了前面的几步没有让 comfyui 去生成又重新跑了这条任务」。
        """
        from backend import batch_store as batch_store_module
        from backend import batch_worker
        from backend.batch_store import BatchStore

        aweme = "7684126327402778850"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            original_db = batch_store_module.DB_PATH
            original_data = batch_worker.DATA_DIR
            try:
                batch_store_module.DB_PATH = root / "queue.db"
                batch_worker.DATA_DIR = root / "data"
                store = BatchStore()

                # ① 上一批：这条已经备过料（有提示词、成图、文案），但从未出片
                old = new_batch_state([], [f"https://www.douyin.com/video/{aweme}"])
                old_item = old["items"][0]
                old_work = root / "data" / "batches" / old["id"] / old_item["id"]
                old_work.mkdir(parents=True, exist_ok=True)
                old_image = old_work / "candidate_r0_upload.png"
                Image.new("RGB", (48, 64), "white").save(old_image)
                (old_work / "scene-frame.jpg").write_bytes(b"frame")
                (old_work / "source-contact-sheet.jpg").write_bytes(b"sheet")
                old_item.update(
                    status="deleted",
                    stage="deleted",
                    awemeId=aweme,
                    title="上一次的标题",
                    ai={
                        "imagePrompt": "上一次拼好的提示词",
                        "imageRatio": "9:16",
                        "reference_image_path": str(old_image),
                        "title": "上一次的标题",
                        "introduction": "上一次的简介",
                        "tags": ["手势舞", "甜妹舞", "白色系穿搭", "心动氛围", "跟我一起跳"],
                        "style_source": "video",
                    },
                )
                store.create(old)

                # ② 新一批：同一条作品被重新加进来
                fresh = new_batch_state([], [f"https://www.douyin.com/video/{aweme}"])
                fresh_item = fresh["items"][0]
                fresh_item.update(awemeId=aweme, status="pending")
                store.create(fresh)

                # 发布根必须一起换成临时目录：提前落盘（deliver_review_materials）会真的写
                # `E:\AI_Exports\H3-MotionStudio\发布成品`，2026-09-13 实测这个测试污染了真实
                # 发布目录（多出一个 `001_上一次的标题_<作品号>` 目录）。
                with patch.object(batch_worker, "batch_store", store), patch.object(
                    batch_worker, "BATCH_OUTPUT_ROOT", root / "发布成品"
                ), patch.object(
                    batch_worker.batch_ai, "analyze", side_effect=AssertionError("不该重新调模型")
                ):
                    adopted = batch_worker._adopt_previous_work(fresh["id"], fresh_item["id"])

                self.assertTrue(adopted)
                row = store.get(fresh["id"])["items"][0]
                self.assertEqual(row["status"], "awaiting_review")
                self.assertEqual(row["ai"]["title"], "上一次的标题")
                self.assertEqual(row["ai"]["introduction"], "上一次的简介")
                self.assertEqual(len(row["ai"]["tags"]), 5)
                # 图与取景帧都复制进了**本条自己的**目录，不跨条目共用同一份文件
                copied = Path(row["ai"]["reference_image_path"])
                self.assertTrue(copied.is_file())
                self.assertEqual(copied.parent, root / "data" / "batches" / fresh["id"] / fresh_item["id"])
                self.assertNotEqual(copied, old_image)
                self.assertTrue((copied.parent / "scene-frame.jpg").is_file())
                # 提示词按**本条比例**重拼并落盘（提示词本来就是模板拼的，必须按本条比例重拼）
                self.assertTrue((copied.parent / "出图提示词.txt").is_file())
                self.assertIn("9:16", row["ai"]["imagePrompt"])
                self.assertEqual(row["ai"]["imageRatio"], "9:16")
                steps = {step["id"]: step["status"] for step in row["milestones"]}
                self.assertEqual(steps["prepare"], "completed")
                self.assertEqual(steps["review"], "running")
                self.assertTrue(
                    any("之前已经备过料" in log["message"] for log in row.get("logs") or []),
                    row.get("logs"),
                )
                # 发布目录里同步有了人物图与文案（提前落盘）
                self.assertTrue(Path(row["outputs"]["image"]).is_file())
                self.assertTrue(Path(row["outputs"]["copy"]).is_file())
                # 而且必须落在临时发布根里 —— 绝不许写到真实发布目录
                for key in ("image", "copy", "folder"):
                    self.assertTrue(
                        Path(row["outputs"][key]).is_relative_to(root),
                        f"{key} 写到真实发布目录里去了：{row['outputs'][key]}",
                    )

                # 没有可复用结果时不能误判
                stranger = new_batch_state([], ["https://www.douyin.com/video/7000000000000000000"])
                stranger_item = stranger["items"][0]
                stranger_item.update(awemeId="7000000000000000000", status="pending")
                store.create(stranger)
                with patch.object(batch_worker, "batch_store", store):
                    self.assertFalse(batch_worker._adopt_previous_work(stranger["id"], stranger_item["id"]))
            finally:
                batch_store_module.DB_PATH = original_db
                batch_worker.DATA_DIR = original_data

    def test_publish_folder_name_includes_the_kind_label(self) -> None:
        """发布目录名带上类型标签：`001_唱歌_标题_作品号` / `001_跳舞_标题_作品号`。

        用户 2026-09-18：「在对应的标签后面加上唱歌还是跳舞，如
        `001_唱歌_夜空中最亮的星_7686514674456574953`」。类型未知的历史条目不拼标签，
        保持旧格式 `{编号}_{标题}_{作品号}`（老目录按作品号后缀照样能复用）。
        """
        from backend import batch_worker

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            with patch.object(batch_worker, "BATCH_OUTPUT_ROOT", root):
                singing = {
                    "id": "s1", "index": 1, "kind": "singing",
                    "awemeId": "7686514674456574953",
                    "ai": {"song_name": "夜空中最亮的星"}, "outputs": {},
                }
                dance = {
                    "id": "d1", "index": 2, "kind": "dance",
                    "awemeId": "7686514674456574954",
                    "ai": {"song_name": "舞蹈BGM"}, "outputs": {},
                }
                legacy = {
                    "id": "l1", "index": 3, "kind": "",
                    "awemeId": "7686514674456574955",
                    "ai": {"title": "旧标题"}, "outputs": {},
                }
                self.assertEqual(
                    batch_worker.publish_folder(singing).name,
                    "001_唱歌_夜空中最亮的星_7686514674456574953",
                )
                self.assertEqual(
                    batch_worker.publish_folder(dance).name,
                    "002_跳舞_舞蹈BGM_7686514674456574954",
                )
                self.assertEqual(
                    batch_worker.publish_folder(legacy).name,
                    "003_旧标题_7686514674456574955",
                )
                # 旧格式目录（没有类型标签）按作品号照样被复用，不新建第二个
                old = root / "001_老标题_7686514674456574953"
                old.mkdir()
                self.assertEqual(batch_worker.publish_folder(singing), old)

    def test_dance_items_are_picked_before_singing(self) -> None:
        """用户 2026-09-20：「如果有唱歌和跳舞的视频，**跳舞优先**」。

        挑选下一条活时必须先给跳舞（备料与出片两条路都走同一份类型优先级），
        同类型内部仍然按编号顺序 —— 只改"先跑谁"，不改编号、不改发布目录名。
        """
        from backend import batch_worker

        state = new_batch_state(
            ["https://v.douyin.com/s1", "https://v.douyin.com/s2"],
            ["https://v.douyin.com/d1", "https://v.douyin.com/d2"],
        )
        # 建批次时唱歌写在前面（页面粘贴区的顺序），但挑选要跳舞优先
        self.assertEqual([row["kind"] for row in state["items"]],
                         ["singing", "singing", "dance", "dance"])
        picked = batch_worker._next_work(state)
        self.assertEqual(picked["kind"], "dance")
        self.assertEqual(picked["kindIndex"], 1)

        # 备料那一路（allow_confirmed=False）同样跳舞优先
        self.assertEqual(batch_worker._next_work(state, allow_confirmed=False)["kind"], "dance")

        # 跳舞都跑完了 → 才轮到唱歌，而且仍按编号（唱歌 1 在前）
        for row in state["items"]:
            if row["kind"] == "dance":
                row["status"] = "completed"
        self.assertEqual(batch_worker._next_work(state)["kind"], "singing")
        self.assertEqual(batch_worker._next_work(state)["kindIndex"], 1)

    def test_running_notice_names_the_other_task_by_its_own_kind(self) -> None:
        """出片与备料并行时，提示里那句「同时在…」必须说清另一条是出片还是备料。

        两条并行是**设计如此**（2026-09-20 起出片与备料重叠），但用户看到表格里两条
        「正在处理」还是问「怎么又两个正在处理」—— 提示以前把另一条写成了和自己同一种活
        （备料时说成「同时在备料第 N 条」），越看越像两件重复的活。
        """
        from backend import batch_worker

        state = new_batch_state(["https://v.douyin.com/s1"], ["https://v.douyin.com/d1"])
        prep_row = dict(next(row for row in state["items"] if row["kind"] == "dance"))
        video_row = dict(next(row for row in state["items"] if row["kind"] == "singing"))
        prep_row["status"] = "pending"
        video_row["status"] = "confirmed"

        preparing = batch_worker._running_notice(prep_row, state, video_row)
        self.assertEqual(preparing, "正在备料第 2 / 2 条（同时在出片第 1 条）。")
        rendering = batch_worker._running_notice(video_row, state, prep_row)
        self.assertEqual(rendering, "正在出片第 1 / 2 条（同时在备料第 2 条）。")
        # 只有一条在跑时不画蛇添足
        self.assertEqual(batch_worker._running_notice(prep_row, state, None), "正在备料第 2 / 2 条。")
        self.assertEqual(
            batch_worker._running_notice(prep_row, state, prep_row), "正在备料第 2 / 2 条。"
        )

    def test_restart_puts_a_dangling_running_item_back_in_the_queue(self) -> None:
        """重启对账：出片与备料并行时，重启只收得掉 `currentItemId` 那一条，另一条会卡死。

        2026-09-20 起 runner 同时跑两条，而 `batch_store.interrupt_active()` 只看
        `currentItemId` —— 落在 `running` 里的另一条：新一代 runner 不挑它（`_next_work`
        只认 pending/revising/confirmed），页面上的「重新开始」又拒绝 running 的条目
        （"正在出片，请先取消再重新开始"），只能人工两步救回来。所以启动时逐条对账。
        """
        from backend import batch_worker
        from backend.batch_worker import new_batch_state

        state = new_batch_state(["https://v.douyin.com/s1"], ["https://v.douyin.com/d1"])
        prep_row, video_row = state["items"]        # 唱歌那条在备料、跳舞那条在出片
        prep_row.update(status="running", stage="prepare")
        video_row.update(status="running", stage="video", videoJobId="live-job")
        box = {"state": state}

        with patch.object(batch_worker, "batch_store", _BoxStore(box)):
            # 出片那条的 ComfyUI 任务还在（恢复流程会接回来）→ 一个都不许动它
            touched = batch_worker.reset_dangling_running_items("b1", {"live-job"})
            self.assertEqual(touched, [prep_row["id"]])
            self.assertEqual(prep_row["status"], "pending")
            self.assertEqual(prep_row["stage"], "queued")
            self.assertFalse(
                [row for row in prep_row["milestones"] if row["status"] in {"running", "error"}],
                prep_row["milestones"],
            )
            self.assertEqual(video_row["status"], "running")
            self.assertEqual(video_row["videoJobId"], "live-job")

            # 出片那条的 ComfyUI 任务也没了 → 放回 confirmed，runner 会重新出片
            touched = batch_worker.reset_dangling_running_items("b1", set())
            self.assertEqual(touched, [video_row["id"]])
            self.assertEqual(video_row["status"], "confirmed")
            self.assertIsNone(video_row["videoJobId"])

            # 幂等：已经没有 running 的条目了，再对账一次什么都不做
            self.assertEqual(batch_worker.reset_dangling_running_items("b1", set()), [])

    def test_confirmed_dance_renders_before_confirmed_singing(self) -> None:
        """已确认的两类都在排队时，GPU 先跑跳舞那一条（编号不动）。"""
        from backend import batch_worker

        state = new_batch_state(
            ["https://v.douyin.com/s1"], ["https://v.douyin.com/d1"]
        )
        for row in state["items"]:
            row["status"] = "confirmed"
        picked = batch_worker._next_confirmed(state)
        self.assertEqual(picked["kind"], "dance")
        self.assertEqual(picked["kindIndex"], 1)

        state["items"][1]["status"] = "completed"      # 跳舞出完了
        picked = batch_worker._next_confirmed(state)
        self.assertEqual(picked["kind"], "singing")

    def test_priority_keeps_each_kind_numbering_and_order(self) -> None:
        """优先级不动编号：唱歌仍是 singing/001..、跳舞仍是 dance/001..，同类型内保持粘贴顺序。"""
        from backend import batch_worker

        state = new_batch_state(
            ["https://v.douyin.com/s1", "https://v.douyin.com/s2", "https://v.douyin.com/s3"],
            ["https://v.douyin.com/d1"],
        )
        singing = [row for row in state["items"] if row["kind"] == "singing"]
        dance = [row for row in state["items"] if row["kind"] == "dance"]
        self.assertEqual([row["kindIndex"] for row in singing], [1, 2, 3])
        self.assertEqual([row["kindIndex"] for row in dance], [1])
        self.assertEqual([row["url"] for row in singing],
                         ["https://v.douyin.com/s1", "https://v.douyin.com/s2", "https://v.douyin.com/s3"])
        # 优先级只影响挑选，不改 items 本身的顺序（页面显示的粘贴顺序不变）
        self.assertEqual([row["kind"] for row in state["items"]],
                         ["singing", "singing", "singing", "dance"])

    def test_singing_and_dance_numbers_are_independent(self) -> None:
        """唱歌与跳舞的编号各自从 001 起（用户 2026-09-20：「唱歌的序号和跳舞的序号都是独立的」）。

        全局 `index` 仍然连续（它管处理顺序、进度、拖动换位），**类型内编号**放在
        `kindIndex` 里，发布目录名与页面显示用它。
        """
        state = new_batch_state(
            ["https://v.douyin.com/s1", "https://v.douyin.com/s2", "https://v.douyin.com/s3"],
            ["https://v.douyin.com/d1", "https://v.douyin.com/d2"],
        )
        rows = state["items"]
        self.assertEqual([row["index"] for row in rows], [1, 2, 3, 4, 5])
        self.assertEqual([row["kindIndex"] for row in rows], [1, 2, 3, 1, 2])

    def test_publish_folders_live_in_kind_subfolders(self) -> None:
        """发布目录落在 `singing/` `dance/` 两层里，编号用类型内序号，目录名保留中文标签。"""
        from backend import batch_worker

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            with patch.object(batch_worker, "BATCH_OUTPUT_ROOT", root):
                singing = {"id": "s1", "index": 5, "kindIndex": 5, "kind": "singing",
                           "awemeId": "7000000000000000001", "ai": {"title": "歌"}, "outputs": {}}
                dance = {"id": "d1", "index": 9, "kindIndex": 1, "kind": "dance",
                         "awemeId": "7000000000000000002", "ai": {"title": "舞"}, "outputs": {}}
                sing_path = batch_worker._ideal_publish_folder(singing)
                dance_path = batch_worker._ideal_publish_folder(dance)
            self.assertEqual(sing_path.parent.name, "singing")
            self.assertEqual(dance_path.parent.name, "dance")
            self.assertTrue(sing_path.name.startswith("005_唱歌_"), sing_path.name)
            self.assertTrue(dance_path.name.startswith("001_跳舞_"), dance_path.name)

    def test_old_flat_publish_folder_is_reused_and_never_renamed(self) -> None:
        """老目录（还在扁平发布根里的）继续被复用，而且**不会被改名搬进类型子目录**。

        用户 2026-09-20：「老项目不要用管」—— 老条目的理想名现在算在 `dance/` 下，但那个
        子目录并不存在；`publish_folder()` 的改名分支必须失败退回复用，而不是偷偷建目录搬走。
        """
        from backend import batch_worker

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            aweme = "7684126327402778850"
            legacy = root / f"016_跳舞_老标题_{aweme}"
            legacy.mkdir(parents=True)
            (legacy / "最终成片.mp4").write_bytes(b"video")
            item = {"id": "d1", "index": 16, "kindIndex": 16, "kind": "dance", "awemeId": aweme,
                    "ai": {"title": "新标题"}, "outputs": {}}
            with patch.object(batch_worker, "BATCH_OUTPUT_ROOT", root):
                self.assertEqual(batch_worker._existing_publish_folder(item), legacy)
                self.assertEqual(batch_worker._existing_publish_folder(
                    {"kind": "dance", "awemeId": aweme}), legacy)   # 子目录优先、扁平根兜底
                chosen = batch_worker.publish_folder(item)
                self.assertEqual(chosen, legacy)
                self.assertTrue(legacy.is_dir(), "老目录必须原样留着")
                self.assertFalse((root / "dance").exists(), "不该为了改名偷偷建子目录")

    def test_appended_items_continue_each_kind_numbering(self) -> None:
        """追加条目时，类型内编号接着自己那一类往下排（不从全局序号继承）。"""
        from backend import batch_store as batch_store_module
        from backend import batch_worker
        from backend.batch_store import BatchStore

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            original = batch_store_module.DB_PATH
            try:
                batch_store_module.DB_PATH = Path(folder) / "queue.db"
                store = BatchStore()
                state = new_batch_state(["https://v.douyin.com/s1"], ["https://v.douyin.com/d1"])
                store.create(state)
                with patch.object(batch_worker, "batch_store", store):
                    batch_worker.append_batch_items(
                        state["id"],
                        ["https://v.douyin.com/s2"],
                        ["https://v.douyin.com/d2"],
                    )
                    batch_worker.append_batch_items(
                        state["id"], ["https://v.douyin.com/s3"], []
                    )
                rows = store.get(state["id"])["items"]
                self.assertEqual(
                    [(row["kind"], row["kindIndex"]) for row in rows],
                    [("singing", 1), ("dance", 1), ("singing", 2), ("dance", 2), ("singing", 3)],
                )
                self.assertEqual([row["index"] for row in rows], [1, 2, 3, 4, 5])
            finally:
                batch_store_module.DB_PATH = original

    def test_appended_items_restart_at_001_after_a_full_cleanup(self) -> None:
        """已发布的都被删掉之后再追加：类型编号**从 001 重新开始**（用户 2026-09-20）。

        用户原话：「如果已完成列表都是空的说明所有记录我都已经发布完成了并且是我主动进行了
        删除操作。**你要从 01 序号开始**」—— 实测就踩在这条上：老批次 85 条（建编号功能之前
        建的，没有 kindIndex）全被删掉之后追加 31 条，`kind_starts` 还拿老条目的**全局序号**
        当基数，于是新的跳舞条目排到了 086 / 087（发布目录名成了 `086_跳舞_…`），而在用户
        眼里这一轮就是全新的 001。已删除的条目不再占号。
        """
        from backend import batch_store as batch_store_module
        from backend import batch_worker
        from backend.batch_store import BatchStore

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            original = batch_store_module.DB_PATH
            try:
                batch_store_module.DB_PATH = Path(folder) / "queue.db"
                store = BatchStore()
                state = new_batch_state(["https://v.douyin.com/s1"], ["https://v.douyin.com/d1"])
                # 老条目：编号功能之前建的（没有 kindIndex），而且已经排到 75 号往后
                for position, row in enumerate(state["items"], start=75):
                    row.pop("kindIndex", None)
                    row["index"] = position
                    row["status"] = "deleted"
                store.create(state)
                with patch.object(batch_worker, "batch_store", store):
                    batch_worker.append_batch_items(
                        state["id"], ["https://v.douyin.com/s2"], ["https://v.douyin.com/d2"]
                    )
                rows = store.get(state["id"])["items"]
                fresh = [row for row in rows if row["status"] != "deleted"]
                self.assertEqual(
                    [(row["kind"], row["kindIndex"]) for row in fresh],
                    [("singing", 1), ("dance", 1)],
                )
            finally:
                batch_store_module.DB_PATH = original

    def test_publish_folder_is_stable_when_the_title_changes(self) -> None:
        """一个条目只能有一个发布目录：标题变了就改名复用，**不许新建第二个**。

        用户 2026-09-13：「我只开始了两个任务啊 文件夹多了好多」—— 标题在备料时是一版、
        用户上传候选图后 `write_copy` 又改一版，而目录名里带标题，于是同一个作品号下
        留下好几个目录。
        """
        from backend import batch_worker

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            with patch.object(batch_worker, "BATCH_OUTPUT_ROOT", root):
                item = {
                    "id": "it1",
                    "index": 1,
                    "kind": "dance",
                    "awemeId": "7684126327402778850",
                    "ai": {"title": "第一版标题"},
                    "outputs": {},
                }
                first = batch_worker.publish_folder(item)
                first.mkdir(parents=True)
                (first / "人物图.png").write_bytes(b"x")
                item["outputs"] = {"folder": str(first)}

                item["ai"]["title"] = "第二版标题"
                second = batch_worker.publish_folder(item)
                # 2026-09-20 起发布根下多了一层类型子目录：跳舞条目落在 `dance/` 里
                self.assertEqual(second.parent, root / "dance")
                self.assertIn("第二版标题", second.name)
                self.assertFalse(first.exists(), "旧目录应当被改名，而不是留下两个")
                self.assertTrue((second / "人物图.png").is_file())
                # 反复调用必须稳定在同一个目录
                self.assertEqual(batch_worker.publish_folder(item), second)
                self.assertEqual(len(list((root / "dance").iterdir())), 1)
                # 记录的目录已经不在了（被清掉）→ 复用同作品号已有的目录，**不新建第三个**
                item["outputs"] = {"folder": str(root / "gone")}
                item["ai"]["title"] = "第三版标题"
                self.assertEqual(batch_worker.publish_folder(item), second)
                self.assertEqual(len(list((root / "dance").iterdir())), 1)
                # 记录的目录在发布根之外（脏数据）→ 也只认发布根里那一个
                item["outputs"] = {"folder": str(root.parent / "outside")}
                self.assertEqual(batch_worker.publish_folder(item), second)
                self.assertEqual(len(list((root / "dance").iterdir())), 1)

    def test_batch_re_added_work_reuses_the_publish_folder(self) -> None:
        """重新加入队列的**同一条作品**不许再建一套发布目录（用户 2026-09-13）。

        「现在加入队列目录又重复生成文件夹了……如果目录里已经有了最终成片说明已经生成过了……
        再次加入队列时候不要重复生成文件夹」：新批次的条目还没有 `outputs.folder`，旧实现按
        「编号_标题_作品号」新建目录，于是同一条作品多出一整套空目录。现在只要发布根里已经有
        这个作品号的目录就直接复用，**尤其是已经有 `最终成片.mp4` 的那个**（说明这条出过片）。
        """
        from backend import batch_worker

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            aweme = "7684126327402778850"
            with patch.object(batch_worker, "BATCH_OUTPUT_ROOT", root):
                # 旧批次留下的目录：已经出过片
                done = root / f"001_老标题_{aweme}"
                done.mkdir(parents=True)
                (done / "最终成片.mp4").write_bytes(b"video")
                (done / "人物图.png").write_bytes(b"x")
                # 另一个更晚创建、但没有成片的目录（旧实现造出来的重复目录）
                newer = root / f"002_新标题_{aweme}"
                newer.mkdir()
                (newer / "人物图.png").write_bytes(b"x")

                fresh = {"id": "it9", "index": 2, "kind": "dance", "awemeId": aweme,
                         "ai": {"title": "新标题"}, "outputs": {}}
                chosen = batch_worker.publish_folder(fresh)
                self.assertEqual(chosen, done, "必须复用已经有最终成片的那个目录")
                self.assertEqual(len(list(root.iterdir())), 2, "不许再新建目录")

                # 只有没成片的目录时也要复用（同样不许新建）
                (done / "最终成片.mp4").unlink()
                (done / "人物图.png").unlink()
                done.rmdir()
                self.assertEqual(batch_worker.publish_folder(fresh), newer)
                self.assertEqual(len(list(root.iterdir())), 1)

                # 全新作品（发布根里没有它的目录）才用理想名字
                stranger = {"id": "it10", "index": 3, "kind": "dance", "awemeId": "7000000000000000000",
                            "ai": {"title": "全新标题"}, "outputs": {}}
                created = batch_worker.publish_folder(stranger)
                self.assertEqual(created.name, "003_跳舞_全新标题_7000000000000000000")

    def test_batch_reuses_an_already_downloaded_source(self) -> None:
        """本地已经有这条作品就直接复用，**不启动下载器、也不提交下载任务**。

        用户 2026-09-13：「抖音下载的时候如果已经有了就不要下载了」。下载器子进程自己会跳过
        已存在的视频，但那条路要先拉起下载服务再提交一次任务；批量应该在提交之前先查本地。
        """
        import asyncio

        from backend import batch_store as batch_store_module
        from backend import batch_worker
        from backend.batch_store import BatchStore

        aweme = "7684126327402778850"
        url = f"https://www.douyin.com/user/self?from_tab_name=main&modal_id={aweme}&showTab=like"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            original_db = batch_store_module.DB_PATH
            original_manifest = batch_worker.MANIFEST_PATH
            original_output = batch_worker.DOUYIN_OUTPUT
            try:
                batch_store_module.DB_PATH = root / "queue.db"
                downloads = root / "EV"
                relative = Path("作者") / f"2026-09-11_你在哪_{aweme}" / f"2026-09-11_你在哪_{aweme}.mp4"
                video = downloads / relative
                video.parent.mkdir(parents=True, exist_ok=True)
                video.write_bytes(b"fake-mp4")
                manifest = downloads / "download_manifest.jsonl"
                manifest.write_text(
                    json.dumps(
                        {"aweme_id": aweme, "desc": "你在哪 我的心就在哪", "file_paths": [str(relative)]},
                        ensure_ascii=False,
                    ) + "\n",
                    encoding="utf-8",
                )
                batch_worker.MANIFEST_PATH = manifest
                batch_worker.DOUYIN_OUTPUT = downloads

                # ① 直接查缓存：走清单命中
                self.assertEqual(batch_worker.cached_download_path(url), video)
                # ② 清单没有记录时，按作品号在下载目录里搜
                self.assertEqual(batch_worker.cached_download_path(f"https://www.douyin.com/video/{aweme}"), video)
                self.assertIsNone(batch_worker.cached_download_path("https://www.douyin.com/video/7000000000000000000"))

                state = new_batch_state([], [url])
                item = state["items"][0]
                store = BatchStore()
                store.create(state)
                submit_calls: list[str] = []

                async def fake_submit(value: str) -> dict:
                    submit_calls.append(value)
                    raise AssertionError("本地已有视频时不该提交下载任务")

                async def fake_playable(source: Path, _aweme: str) -> Path:
                    return source

                with patch.object(batch_worker, "batch_store", store), patch.object(
                    batch_worker.douyin_service, "submit", fake_submit
                ), patch.object(batch_worker, "ensure_download_playable", fake_playable):
                    resolved = asyncio.run(batch_worker._download(state["id"], item["id"]))

                self.assertEqual(submit_calls, [])
                self.assertEqual(resolved, video)
                fresh = store.get(state["id"])["items"][0]
                self.assertEqual(fresh["awemeId"], aweme)
                self.assertEqual(fresh["sourcePath"], str(video.resolve()))
                self.assertEqual(fresh["title"], "你在哪 我的心就在哪")
                steps = {step["id"]: step["status"] for step in fresh["milestones"]}
                self.assertEqual(steps["download"], "completed")
                self.assertTrue(
                    any("跳过下载" in row["message"] for row in fresh.get("logs") or []),
                    fresh.get("logs"),
                )
            finally:
                batch_store_module.DB_PATH = original_db
                batch_worker.MANIFEST_PATH = original_manifest
                batch_worker.DOUYIN_OUTPUT = original_output

    def test_douyin_direct_parses_video_ids_that_do_not_start_with_v0(self) -> None:
        """直连路由的 video_id 正则不许再写死 `v0` 前缀。

        用户 2026-09-16：「作品详情或媒体下载失败：Cookie 可能已失效，或当前请求触发了抖音风控
        又报这个错误」。真实根因在 `backend/douyin_direct.py`：旧正则 `"uri":"v0[0-9a-z]+"`
        要求视频号以 `v0` 开头，而这次分享页给的是 `v2800fgi0000d8qi147og65j651va0ig`（`v2` 开头），
        解析不出 video_id → 有数据的分享页也被判成壳页 → 兜底整条失败，最后把下载器那句
        「Cookie 可能已失效」原样抛给用户。跟 Cookie / 风控其实没关系。
        """
        from backend import douyin_direct

        html = (
            '<script>window._ROUTER_DATA = {"video":{"play_addr":{"uri":'
            '"v2800fgi0000d8qi147og65j651va0ig","url_list":["https:\\u002F\\u002Faweme.snssdk.com'
            '\\u002Faweme\\u002Fv1\\u002Fplaywm\\u002F?line=0&logo_name=aweme_diversion_search'
            '&ratio=720p&video_id=v2800fgi0000d8qi147og65j651va0ig"]},"cover":{"uri":'
            '"tos-cn-p-0015c000-ce\\u002FoUkPaIlTaAPj1AgWiIMCFzaIBPEmUBM1wiZAU"}},"desc":'
            '"好想跟你表白#无声卡翻唱","nickname":"任白","hashtag_name":"表白手势舞"}</script>'
        )
        self.assertEqual(
            douyin_direct.extract_video_id(html), "v2800fgi0000d8qi147og65j651va0ig"
        )
        # 旧前缀（之前实测能下的那批）必须继续认得，不能为了修新的把旧的改坏
        self.assertEqual(
            douyin_direct.extract_video_id(
                '"play_addr":{"uri":"v0200fg10000dajsr47og65ult99rdeg","url_list":[]}'
            ),
            "v0200fg10000dajsr47og65ult99rdeg",
        )
        # 只有封面 uri（`tos-cn-…`）或根本没有 play_addr 的壳页 → 不许瞎认
        self.assertIsNone(douyin_direct.extract_video_id('<html>video_layout:null</html>'))

    def test_douyin_direct_retries_the_shell_share_page(self) -> None:
        """分享页概率性被换成壳页（HTTP 200 但没有 play_addr）→ 必须重试，不许一次就判死。

        用户 2026-09-15 实测：同一个作品、同一份 cookie，一次壳页一次有数据。GitHub 上
        `cat-xierluo/legal-skills` 0.5.1 的结论一样（「概率性风控拒绝，改为两轮重试」）。
        """
        from backend import douyin_direct

        shell = "<html><body>video_layout:null</body></html>"
        data = (
            '"video":{"play_addr":{"uri":"v2800fgi0000d8qi147og65j651va0ig"},'
            '"desc":"好想跟你表白","nickname":"任白"},"hashtag_name":"表白手势舞"'
        )

        class FakeResponse:
            status_code = 200

            def __init__(self, text: str) -> None:
                self.text = text

        class FakeClient:
            def __init__(self, pages: list[str]) -> None:
                self.pages = list(pages)
                self.calls: list[str] = []

            def get(self, url: str) -> FakeResponse:
                self.calls.append(url)
                return FakeResponse(self.pages.pop(0))

        slept: list[float] = []
        client = FakeClient([shell, shell, data])
        payload = douyin_direct.share_payload("7653059686421996986", client, sleep=slept.append)
        self.assertIsNotNone(payload)
        assert payload is not None  # for type checkers
        self.assertEqual(payload["videoId"], "v2800fgi0000d8qi147og65j651va0ig")
        self.assertEqual(payload["desc"], "好想跟你表白")
        self.assertEqual(payload["nickname"], "任白")
        self.assertEqual(payload["tags"], ["表白手势舞"])
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(slept, [douyin_direct.SHARE_RETRY_SECONDS] * 2)

        # 一直壳页 → 有限次后放弃（不无限重试），而且会在两个分享域名之间轮换
        always = FakeClient([shell] * douyin_direct.SHARE_ATTEMPTS)
        self.assertIsNone(douyin_direct.share_payload("1", always, sleep=lambda _s: None))
        self.assertEqual(len(always.calls), douyin_direct.SHARE_ATTEMPTS)
        self.assertGreater(len(set(always.calls)), 1)

    def test_batch_download_prefers_direct_by_default(self) -> None:
        """用户 2026-09-20 最终拍板：「**改成原来的 直连 =》REST**」。

        默认**直连优先**（不依赖 Cookie），失败再回退下载器 REST。实测依据（用户这批 116 条）：
        直连成功 79 条、平均 3.1 秒；REST 成功 3 条、平均 19.1 秒（详情接口被 Argus 门禁，
        每次都要白等几轮重试）。只有显式 `H3_DOUYIN_DIRECT=0` 才是"只走 REST"。
        """
        import asyncio

        from backend import batch_store as batch_store_module
        from backend import batch_worker
        from backend.batch_store import BatchStore

        aweme = "7653059686421996986"
        url = f"https://www.douyin.com/user/self?from_tab_name=main&modal_id={aweme}&showTab=like"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            original_db = batch_store_module.DB_PATH
            original_manifest = batch_worker.MANIFEST_PATH
            original_output = batch_worker.DOUYIN_OUTPUT
            try:
                batch_store_module.DB_PATH = root / "queue.db"
                downloads = root / "EV"
                downloads.mkdir(parents=True, exist_ok=True)
                batch_worker.DOUYIN_OUTPUT = downloads
                batch_worker.MANIFEST_PATH = downloads / "download_manifest.jsonl"
                state = new_batch_state([], [url])
                item = state["items"][0]
                store = BatchStore()
                store.create(state)

                video = root / "direct" / f"直连拿到的_{aweme}.mp4"
                video.parent.mkdir(parents=True, exist_ok=True)
                video.write_bytes(b"fake-mp4")
                order: list[str] = []

                def fake_direct(_url: str, _dest: Path, **_kwargs: object):
                    order.append("direct")
                    return video, {"awemeId": aweme, "desc": "直连拿到的作品", "tags": []}

                async def fake_submit(_url: str) -> dict:
                    order.append("service")
                    raise AssertionError("直连已经成功，不该再走 REST")

                async def fake_playable(source: Path, _aweme: str) -> Path:
                    return source

                with patch.object(batch_worker, "batch_store", store), patch(
                    "backend.douyin_direct.fetch", fake_direct
                ), patch.object(
                    batch_worker.douyin_service, "submit", fake_submit
                ), patch.object(
                    batch_worker, "mirror_upsert", lambda _jobs: None
                ), patch.object(
                    batch_worker, "ensure_download_playable", fake_playable
                ), patch.object(
                    batch_worker, "env_value", lambda name, default="": default
                ):
                    resolved = asyncio.run(batch_worker._download(state["id"], item["id"]))

                self.assertEqual(order, ["direct"], "直连成功就不该再碰 REST")
                self.assertEqual(resolved, video)
                fresh = store.get(state["id"])["items"][0]
                self.assertEqual(fresh["awemeId"], aweme)
                self.assertEqual(fresh["sourcePath"], str(video.resolve()))
                steps = {step["id"]: step["status"] for step in fresh["milestones"]}
                self.assertEqual(steps["download"], "completed")
                self.assertTrue(
                    any("直连下载成功" in row["message"] for row in fresh.get("logs") or []),
                    fresh.get("logs"),
                )
            finally:
                batch_store_module.DB_PATH = original_db
                batch_worker.MANIFEST_PATH = original_manifest
                batch_worker.DOUYIN_OUTPUT = original_output

    def test_batch_download_falls_back_to_rest_when_direct_fails(self) -> None:
        """直连失败（分享页被换成壳页那类）→ **回退下载器 REST**（「直连 =》REST」的后半段）。"""
        import asyncio

        from backend import batch_store as batch_store_module
        from backend import batch_worker
        from backend.batch_store import BatchStore

        aweme = "7653059686421996986"
        url = f"https://www.douyin.com/user/self?from_tab_name=main&modal_id={aweme}&showTab=like"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            original_db = batch_store_module.DB_PATH
            original_manifest = batch_worker.MANIFEST_PATH
            original_output = batch_worker.DOUYIN_OUTPUT
            try:
                batch_store_module.DB_PATH = root / "queue.db"
                downloads = root / "EV"
                downloads.mkdir(parents=True, exist_ok=True)
                batch_worker.DOUYIN_OUTPUT = downloads
                batch_worker.MANIFEST_PATH = downloads / "download_manifest.jsonl"
                state = new_batch_state([], [url])
                item = state["items"][0]
                store = BatchStore()
                store.create(state)

                video = root / "rest" / f"兜底拿到的_{aweme}.mp4"
                video.parent.mkdir(parents=True, exist_ok=True)
                video.write_bytes(b"fake-mp4")
                order: list[str] = []

                def failing_direct(_url: str, _dest: Path, **_kwargs: object):
                    order.append("direct")
                    raise RuntimeError("分享页连续 4 轮都被抖音换成壳页，拿不到 video_id")

                async def ok_submit(_url: str) -> dict:
                    order.append("service")
                    return {"job_id": "job-1", "status": "success", "total": 1, "success": 1}

                async def fake_job(_job_id: str) -> dict:
                    return {"job_id": "job-1", "status": "success", "total": 1, "success": 1}

                def fake_result(_job: dict) -> dict:
                    return {"awemeId": aweme, "path": str(video)}

                async def fake_stop() -> None:
                    return None

                async def fake_playable(source: Path, _aweme: str) -> Path:
                    return source

                with patch.object(batch_worker, "batch_store", store), patch(
                    "backend.douyin_direct.fetch", failing_direct
                ), patch.object(
                    batch_worker.douyin_service, "submit", ok_submit
                ), patch.object(
                    batch_worker.douyin_service, "job", fake_job
                ), patch.object(
                    batch_worker.douyin_service, "result_for", fake_result
                ), patch.object(
                    batch_worker.douyin_service, "stop", fake_stop
                ), patch.object(
                    batch_worker, "mirror_upsert", lambda _jobs: None
                ), patch.object(
                    batch_worker, "ensure_download_playable", fake_playable
                ), patch.object(
                    batch_worker, "env_value", lambda name, default="": default
                ):
                    resolved = asyncio.run(batch_worker._download(state["id"], item["id"]))

                self.assertEqual(order, ["direct", "service"], "直连先试，失败才回退 REST")
                self.assertEqual(resolved, video)
                fresh = store.get(state["id"])["items"][0]
                self.assertEqual(fresh["awemeId"], aweme)
                logs = " ".join(row["message"] for row in fresh.get("logs") or [])
                self.assertIn("改用下载器 REST 再试", logs)
                self.assertIn("下载器 REST 下载成功", logs)
            finally:
                batch_store_module.DB_PATH = original_db
                batch_worker.MANIFEST_PATH = original_manifest
                batch_worker.DOUYIN_OUTPUT = original_output

    def test_batch_download_can_be_pinned_to_rest_only(self) -> None:
        """`H3_DOUYIN_DIRECT=0` → **只走 REST**、不兜底（明确不要直连时才这样配）。"""
        import asyncio

        from backend import batch_store as batch_store_module
        from backend import batch_worker
        from backend.batch_store import BatchStore

        aweme = "7653059686421996986"
        url = f"https://www.douyin.com/user/self?from_tab_name=main&modal_id={aweme}&showTab=like"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            original_db = batch_store_module.DB_PATH
            original_output = batch_worker.DOUYIN_OUTPUT
            original_manifest = batch_worker.MANIFEST_PATH
            try:
                batch_store_module.DB_PATH = root / "queue.db"
                batch_worker.DOUYIN_OUTPUT = root / "EV"
                (root / "EV").mkdir(parents=True, exist_ok=True)
                # 必须钉住清单路径：真实清单里若已有这个作品号，`cached_download_path` 会
                # 直接复用本地文件、根本不进下载路由（这条测试就测不到 REST-only 了）。
                batch_worker.MANIFEST_PATH = root / "EV" / "download_manifest.jsonl"
                state = new_batch_state([], [url])
                item = state["items"][0]
                store = BatchStore()
                store.create(state)

                def fake_direct(*_args, **_kwargs):
                    raise AssertionError("配了 H3_DOUYIN_DIRECT=0 就不该走直连")

                async def fake_submit(_url: str) -> dict:
                    raise RuntimeError(f"{batch_worker.DOWNLOADER_COOKIE_HINT}：Cookie 可能已失效")

                def env(name: str, default: str = "") -> str:
                    return "0" if name == "H3_DOUYIN_DIRECT" else default

                with patch.object(batch_worker, "batch_store", store), patch(
                    "backend.douyin_direct.fetch", fake_direct
                ), patch.object(
                    batch_worker.douyin_service, "submit", fake_submit
                ), patch.object(
                    batch_worker, "env_value", env
                ):
                    with self.assertRaises(RuntimeError) as ctx:
                        asyncio.run(batch_worker._download(state["id"], item["id"]))

                self.assertIn("下载器路由", str(ctx.exception))
                self.assertNotIn("直连路由", str(ctx.exception))
            finally:
                batch_store_module.DB_PATH = original_db
                batch_worker.DOUYIN_OUTPUT = original_output
                batch_worker.MANIFEST_PATH = original_manifest

    def test_batch_download_failure_names_both_routes(self) -> None:
        """两条路由都失败时的报错必须**能定位**，不许只甩下载器那句「Cookie 可能已失效」。"""
        import asyncio

        from backend import batch_store as batch_store_module
        from backend import batch_worker, douyin_direct
        from backend.batch_store import BatchStore

        aweme = "7653059686421996986"
        url = f"https://www.douyin.com/user/self?from_tab_name=main&modal_id={aweme}&showTab=like"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            original_db = batch_store_module.DB_PATH
            original_manifest = batch_worker.MANIFEST_PATH
            original_output = batch_worker.DOUYIN_OUTPUT
            try:
                batch_store_module.DB_PATH = root / "queue.db"
                downloads = root / "EV"
                downloads.mkdir(parents=True, exist_ok=True)
                batch_worker.DOUYIN_OUTPUT = downloads
                batch_worker.MANIFEST_PATH = downloads / "download_manifest.jsonl"
                state = new_batch_state([], [url])
                item = state["items"][0]
                store = BatchStore()
                store.create(state)

                def failing_direct(_url: str, _dest: Path, **_kwargs: object):
                    raise douyin_direct.DouyinDirectError(
                        f"分享页连续 {douyin_direct.SHARE_ATTEMPTS} 轮都被抖音换成壳页，拿不到 video_id"
                    )

                async def failing_submit(_url: str) -> dict:
                    raise RuntimeError(
                        "作品详情或媒体下载失败：Cookie 可能已失效，或当前请求触发了抖音风控"
                    )

                async def fake_stop() -> None:
                    return None

                with patch.object(batch_worker, "batch_store", store), patch(
                    "backend.douyin_direct.fetch", failing_direct
                ), patch.object(
                    batch_worker.douyin_service, "submit", failing_submit
                ), patch.object(
                    batch_worker.douyin_service, "stop", fake_stop
                ):
                    with self.assertRaises(RuntimeError) as caught:
                        asyncio.run(batch_worker._download(state["id"], item["id"]))

                message = str(caught.exception)
                # 默认是「REST 先试 → 失败兜直连」（2026-09-20 用户：「要加回来」）→ 两条都试过，
                # 所以两条路由的原因都要报出来，不能只剩下载器那句误导人的话。
                self.assertIn("直连路由", message)
                self.assertIn("壳页", message)
                self.assertIn("下载器路由", message)
                self.assertIn("Argus", message)
                # 不能只剩下载器那句误导人的话
                self.assertNotEqual(message.strip(), "作品详情或媒体下载失败：Cookie 可能已失效，或当前请求触发了抖音风控")

                # `H3_DOUYIN_DIRECT=1` 时反过来：直连先试、失败回退 REST —— 同样是两条都报
                with patch.object(
                    batch_worker, "env_value",
                    lambda name, default="": "1" if name == "H3_DOUYIN_DIRECT" else default,
                ), patch.object(
                    batch_worker, "batch_store", store
                ), patch(
                    "backend.douyin_direct.fetch", failing_direct
                ), patch.object(
                    batch_worker.douyin_service, "submit", failing_submit
                ), patch.object(
                    batch_worker.douyin_service, "stop", fake_stop
                ):
                    with self.assertRaises(RuntimeError) as both_routes:
                        asyncio.run(batch_worker._download(state["id"], item["id"]))

                both = str(both_routes.exception)
                self.assertIn("直连路由", both)
                self.assertIn("壳页", both)
                self.assertIn("下载器路由", both)
            finally:
                batch_store_module.DB_PATH = original_db
                batch_worker.MANIFEST_PATH = original_manifest
                batch_worker.DOUYIN_OUTPUT = original_output

    def test_publish_root_guard_survives_a_stray_patch_stopall(self) -> None:
        """一句 `patch.stopall()` 不许把发布根的安全补丁带走（2026-09-20 实测事故）。

        `DirectDownloadResumeTests` 曾经 `addCleanup(patch.stopall)`，而按类名字母序排在它
        后面的 `WorkflowPreparationTests` / `SubtitleStepTests` 就真的写进了用户真实的
        `E:\\AI_Exports\\H3-MotionStudio\\发布成品`（多了 12 个空的
        `001/003_唱歌_等待处理_<条目id>` + 2 个假字幕目录）。这里**真的调一次 stopall**，
        断言发布根仍然指向临时目录、并且真的建目录时也落在临时根里。
        """
        from unittest import mock

        from backend import batch_worker

        mock.patch.stopall()  # 就是当初那一下

        root = Path(str(batch_worker.BATCH_OUTPUT_ROOT))
        self.assertIn(_TEST_PUBLISH_ROOT.name, str(root))
        self.assertNotIn("AI_Exports", str(root))
        # 真正走一遍建目录的路径，确认落点在临时根（而不是只断言变量）
        folder = batch_worker.publish_folder(
            {"index": 901, "kind": "singing", "title": "等待处理", "id": "guardtest901"}
        )
        folder.mkdir(parents=True, exist_ok=True)
        self.assertIn(_TEST_PUBLISH_ROOT.name, str(folder))
        self.assertTrue(folder.is_dir())

    def test_batch_review_lands_image_and_copy_in_publish_folder(self) -> None:
        """审核点就把「人物图 + 发布文案」写进发布目录（用户 2026-09-13：

        「批量创建的时候 发布成品 这个目录下怎么没有生成对应内容呢」）。
        提前落盘不能碰里程碑、不能写 videoFinal —— 成片还得等出片，之后由 `_deliver` 覆盖补上。
        """
        import asyncio

        from backend import batch_store as batch_store_module
        from backend import batch_worker
        from backend.batch_store import BatchStore

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            original_db = batch_store_module.DB_PATH
            try:
                batch_store_module.DB_PATH = root / "queue.db"
                store = BatchStore()
                state = new_batch_state([], ["https://v.douyin.com/dance"])
                item = state["items"][0]
                upload = root / "uploaded.png"
                Image.new("RGB", (48, 64), "white").save(upload)
                item.update(
                    status="awaiting_review",
                    stage="review",
                    awemeId="7684126327402778850",
                    ai={
                        "song_name": "",
                        "title": "你在哪，我的心就舞到哪",
                        "introduction": "这支舞只想跳给一个人看💗",
                        "tags": ["手势舞", "甜妹舞", "白色系穿搭", "心动氛围", "跟我一起跳"],
                        "reference_image_path": str(upload),
                    },
                )
                store.create(state)

                def row() -> dict:
                    return store.get(state["id"])["items"][0]

                with patch.object(batch_worker, "batch_store", store), patch.object(
                    batch_worker, "BATCH_OUTPUT_ROOT", root / "发布成品"
                ):
                    outputs = batch_worker.deliver_review_materials(state["id"], item["id"])

                self.assertIsNotNone(outputs)
                assert outputs is not None
                self.assertEqual(Path(outputs["image"]).name, "人物图.png")
                self.assertTrue(Path(outputs["image"]).is_file())
                self.assertTrue(Path(outputs["copy"]).is_file())
                self.assertNotIn("videoFinal", outputs)
                self.assertIn("7684126327402778850", Path(outputs["folder"]).name)
                self.assertIn("#手势舞", Path(outputs["copy"]).read_text(encoding="utf-8-sig"))
                # 里程碑不许被提前打勾（成片还没跑）
                steps = {step["id"]: step["status"] for step in row()["milestones"]}
                self.assertNotEqual(steps["video"], "completed")
                self.assertNotIn("deliver", steps)
                self.assertEqual(row()["status"], "awaiting_review")

                # 出片之后 `_deliver` 用同一个目录补上最终成片，且不会留下两张人物图
                final = root / "final.mp4"
                final.write_bytes(b"fake-video")
                with patch.object(batch_worker, "batch_store", store), patch.object(
                    batch_worker, "BATCH_OUTPUT_ROOT", root / "发布成品"
                ):
                    delivered = asyncio.run(
                        batch_worker._deliver(state["id"], item["id"], {"finalOutput": str(final)})
                    )
                self.assertEqual(Path(delivered["folder"]), Path(outputs["folder"]))
                self.assertEqual(Path(delivered["videoFinal"]).name, "最终成片.mp4")
                self.assertTrue(Path(delivered["videoFinal"]).is_file())
                self.assertEqual(
                    sorted(p.name for p in Path(delivered["folder"]).glob("人物图.*")),
                    ["人物图.png"],
                )
                # 换图后重跑：只留一张新图
                Image.new("RGB", (48, 64), "black").save(upload)
                with patch.object(batch_worker, "batch_store", store), patch.object(
                    batch_worker, "BATCH_OUTPUT_ROOT", root / "发布成品"
                ):
                    batch_worker.deliver_review_materials(state["id"], item["id"])
                self.assertEqual(
                    sorted(p.name for p in Path(delivered["folder"]).glob("人物图.*")),
                    ["人物图.png"],
                )
            finally:
                batch_store_module.DB_PATH = original_db

    def test_batch_deliver_copies_final_video_and_uploaded_image(self) -> None:
        """发布目录必须同时拿到最终成片、用户上传的人物图和发布文案。"""
        import asyncio

        from backend import batch_store as batch_store_module
        from backend import batch_worker
        from backend.batch_store import BatchStore

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            original_db = batch_store_module.DB_PATH
            try:
                batch_store_module.DB_PATH = root / "queue.db"
                store = BatchStore()
                state = new_batch_state(["https://v.douyin.com/song"], [])
                state["items"][0].update(
                    status="running",
                    awemeId="7663001746131065849",
                    ai={
                        "song_name": "爱如潮水 Remix",
                        "title": "标题",
                        "introduction": "简介",
                        "tags": ["粉色长发", "齐刘海"],
                        "reference_image_path": str(root / "uploaded.png"),
                    },
                )
                store.create(state)
                item = state["items"][0]
                Image.new("RGB", (64, 48), "pink").save(root / "uploaded.png")
                final = root / "final.mp4"
                final.write_bytes(b"fake-video")

                with patch.object(batch_worker, "batch_store", store), patch.object(
                    batch_worker, "BATCH_OUTPUT_ROOT", root / "发布成品"
                ):
                    outputs = asyncio.run(
                        batch_worker._deliver(state["id"], item["id"], {"finalOutput": str(final)})
                    )

                self.assertEqual(Path(outputs["videoFinal"]).name, "最终成片.mp4")
                self.assertEqual(Path(outputs["image"]).name, "人物图.png")
                self.assertTrue(Path(outputs["videoFinal"]).is_file())
                self.assertTrue(Path(outputs["image"]).is_file())
                self.assertTrue(Path(outputs["copy"]).is_file())
                self.assertIn("爱如潮水 Remix", Path(outputs["folder"]).name)
                self.assertIn("#粉色长发", Path(outputs["copy"]).read_text(encoding="utf-8-sig"))
            finally:
                batch_store_module.DB_PATH = original_db

    def test_skipping_after_the_video_finished_still_delivers(self) -> None:
        """跳过/删除时子任务其实已经跑完：成片照样整理进发布目录，已完成的步骤打勾。"""
        import asyncio

        from backend import batch_store as batch_store_module
        from backend import batch_worker
        from backend.batch_store import BatchStore

        class StubJobs:
            def __init__(self, job):
                self.job = job

            def get(self, job_id):
                return self.job if job_id == self.job.get("id") else None

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            original_db = batch_store_module.DB_PATH
            try:
                batch_store_module.DB_PATH = root / "queue.db"
                store = BatchStore()
                state = new_batch_state(["https://v.douyin.com/song"], [])
                final = root / "final.mp4"
                final.write_bytes(b"fake-video")
                image = root / "uploaded.png"
                Image.new("RGB", (64, 48), "pink").save(image)
                state["items"][0].update(
                    status="running",
                    stage="video",
                    awemeId="7663001746131065849",
                    videoJobId="83ea862bc2864faf893fd708edd97979",
                    ai={"song_name": "爱如潮水 Remix", "reference_image_path": str(image)},
                )
                store.create(state)
                item = state["items"][0]
                for milestone in item["milestones"]:
                    milestone["status"] = "completed"
                store.mutate_item(
                    state["id"], item["id"], lambda row: row.update(milestones=item["milestones"])
                )
                job = {
                    "id": "83ea862bc2864faf893fd708edd97979",
                    "status": "completed",
                    "finalOutput": str(final),
                }
                with patch.object(batch_worker, "batch_store", store), patch.object(
                    batch_worker, "store", StubJobs(job)
                ), patch.object(batch_worker, "BATCH_OUTPUT_ROOT", root / "发布成品"):
                    asyncio.run(
                        batch_worker._finish_abandoned(state["id"], item["id"], deleted=False)
                    )

                row = next(
                    it for it in store.get(state["id"])["items"] if it["id"] == item["id"]
                )
                self.assertEqual(row["status"], "completed")
                self.assertTrue(Path(row["outputs"]["videoFinal"]).is_file())
                self.assertTrue(Path(row["outputs"]["image"]).is_file())
                self.assertEqual(
                    [step["status"] for step in row["milestones"]], ["completed"] * 4
                )
            finally:
                batch_store_module.DB_PATH = original_db

    def test_manual_redeliver_requires_a_finished_video_job(self) -> None:
        """「重新整理发布文件」只对真的出过片、且成片还在磁盘上的条目生效。"""
        import asyncio

        from backend import batch_store as batch_store_module
        from backend import batch_worker
        from backend.batch_store import BatchStore

        class StubJobs:
            def __init__(self, job):
                self.job = job

            def get(self, job_id):
                return self.job if job_id == self.job.get("id") else None

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            original_db = batch_store_module.DB_PATH
            try:
                batch_store_module.DB_PATH = root / "queue.db"
                store = BatchStore()
                state = new_batch_state(["https://v.douyin.com/song"], [])
                store.create(state)
                item = state["items"][0]

                with patch.object(batch_worker, "batch_store", store), patch.object(
                    batch_worker, "store", StubJobs({"id": "job1", "status": "running"})
                ):
                    with self.assertRaises(ValueError):
                        asyncio.run(batch_worker.deliver_item_now(state["id"], item["id"]))

                store.mutate_item(
                    state["id"], item["id"], lambda row: row.update(videoJobId="job1")
                )
                with patch.object(batch_worker, "batch_store", store), patch.object(
                    batch_worker, "store",
                    StubJobs({"id": "job1", "status": "completed", "finalOutput": str(root / "missing.mp4")}),
                ):
                    with self.assertRaises(ValueError):
                        asyncio.run(batch_worker.deliver_item_now(state["id"], item["id"]))
            finally:
                batch_store_module.DB_PATH = original_db

    def test_skipped_item_can_be_restarted(self) -> None:
        """跳过的视频允许重新开始（2026-09-14 用户要求）：已确认过的只重跑出片，没备齐料的从下载重来。"""
        import asyncio

        from backend import app as app_module
        from backend import batch_store as batch_store_module
        from backend.batch_store import BatchStore

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            original_db = batch_store_module.DB_PATH
            spawned: list = []
            try:
                batch_store_module.DB_PATH = root / "queue.db"
                store = BatchStore()
                state = new_batch_state(
                    ["https://v.douyin.com/song", "https://v.douyin.com/dance", "https://v.douyin.com/old"],
                    [],
                )
                approved, unapproved, source_gone = state["items"]
                source = root / "source.mp4"
                source.write_bytes(b"fake-source")
                image = root / "candidate.png"
                image.write_bytes(b"fake-image")
                approved.update(
                    status="skipped",
                    stage="skipped",
                    reviewApproved=True,
                    skipRequested=True,
                    videoJobId="job-old",
                    sourcePath=str(source),
                    outputs={"videoFinal": "E:/old/最终成片.mp4", "folder": "E:/old"},
                    ai={"reference_image_path": str(image)},
                )
                unapproved.update(status="skipped", stage="skipped", skipRequested=True)
                # 审核过了、候选图也在，但源视频已经被清理掉：必须回到 pending 重新下载，
                # 否则提交视频任务时会因为文件不存在直接失败（2026-09-14 实测就是这样）。
                source_gone.update(
                    status="skipped",
                    stage="skipped",
                    reviewApproved=True,
                    skipRequested=True,
                    sourcePath=str(root / "gone.mp4"),
                    ai={"reference_image_path": str(image)},
                )
                for item in (approved, unapproved, source_gone):
                    for milestone in item["milestones"]:
                        if milestone["id"] == "video":
                            milestone.update(status="skipped", finishedAt="2026-09-13T00:00:00+00:00")
                        else:
                            milestone.update(status="completed")
                state["status"] = "completed"
                store.create(state)

                def row_of(item_id):
                    return next(it for it in store.get(state["id"])["items"] if it["id"] == item_id)

                with patch.object(app_module, "batch_store", store), patch.object(
                    app_module, "spawn", lambda coro: (spawned.append(coro), coro.close())
                ):
                    # 已确认且源视频还在：回到 confirmed，只重跑出片，旧的成片记录清空
                    # （发布目录的记录保留 —— 见 `restart_item_to_run`，2026-09-16）
                    asyncio.run(app_module.retry_batch_item(state["id"], approved["id"]))
                    row = row_of(approved["id"])
                    self.assertEqual((row["status"], row["stage"]), ("confirmed", "confirmed"))
                    self.assertEqual(row["outputs"], {"folder": "E:/old"})
                    self.assertIsNone(row["videoJobId"])
                    self.assertFalse(row["skipRequested"])
                    steps = {step["id"]: step["status"] for step in row["milestones"]}
                    self.assertEqual(steps["video"], "pending")
                    self.assertNotIn("deliver", steps)
                    self.assertEqual(steps["review"], "completed")

                    # 没备齐料的条目：回到 pending，从下载/备料重来
                    asyncio.run(app_module.retry_batch_item(state["id"], unapproved["id"]))
                    self.assertEqual(row_of(unapproved["id"])["status"], "pending")

                    # 源视频没了：也要回到 pending 重新下载
                    asyncio.run(app_module.retry_batch_item(state["id"], source_gone["id"]))
                    row = row_of(source_gone["id"])
                    self.assertEqual((row["status"], row["stage"]), ("pending", "queued"))

                    self.assertEqual(len(spawned), 3)  # 每次重新开始都会唤醒 runner
                    self.assertEqual(store.get(state["id"])["status"], "running")

                    # 已经出片的条目也能再出一版（用户 2026-09-14：「开始中的任务允许取消重新开始」，
                    # 取消后落在 skipped、出片完成后落在 completed，两种都要能直接重来）
                    store.mutate_item(
                        state["id"], approved["id"], lambda row: row.update(status="completed")
                    )
                    asyncio.run(app_module.retry_batch_item(state["id"], approved["id"]))
                    row = row_of(approved["id"])
                    self.assertEqual((row["status"], row["stage"]), ("confirmed", "confirmed"))
                    # 旧成片的记录作废；发布目录的记录要留住（2026-09-16，见
                    # `restart_item_to_run`：目录按 `outputs.folder` 认，清空会写错目录）
                    self.assertEqual(row["outputs"], {"folder": "E:/old"})

                    # 正在出片的条目必须先「取消出片 / 回到确认」，不能直接重来
                    store.mutate_item(
                        state["id"], approved["id"], lambda row: row.update(status="running")
                    )
                    with self.assertRaises(app_module.HTTPException):
                        asyncio.run(app_module.retry_batch_item(state["id"], approved["id"]))
            finally:
                batch_store_module.DB_PATH = original_db

    def test_uncertain_person_request_needs_explicit_retry(self) -> None:
        from backend import batch_worker
        from backend.batch_store import BatchStore

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            from backend import batch_store as store_module
            with patch.object(store_module, "DB_PATH", root / "batches.db"):
                store = BatchStore()
                state = new_batch_state(["https://v.douyin.com/a"], [])
                state["items"][0]["status"] = "failed"
                store.create(state)
                item_id = state["items"][0]["id"]
                work = root / "batches" / state["id"] / item_id
                work.mkdir(parents=True)
                marker = work / "candidate_r0_attempt1.png.request-pending"
                marker.write_text("submitted", encoding="utf-8")
                with patch.object(batch_worker, "DATA_DIR", root), patch.object(batch_worker, "batch_store", store):
                    with self.assertRaises(ValueError):
                        batch_worker.restart_item_to_run(state["id"], item_id)
                    self.assertTrue(marker.exists())
                    batch_worker.restart_item_to_run(state["id"], item_id, allow_uncertain_retry=True)
                    self.assertFalse(marker.exists())
                    self.assertEqual(len(list(work.glob("*.previous-unknown-*"))), 1)

    def test_uploaded_person_image_resumes_despite_old_request_checkpoint(self) -> None:
        from backend import batch_worker
        from backend.batch_store import BatchStore
        from backend import batch_store as store_module

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            with patch.object(store_module, "DB_PATH", root / "batches.db"):
                store = BatchStore()
                state = new_batch_state([], ["https://v.douyin.com/a"])
                item = state["items"][0]
                source = root / "source.mp4"
                source.write_bytes(b"source")
                work = root / "batches" / state["id"] / item["id"]
                work.mkdir(parents=True)
                candidate = work / "candidate_r0_upload.png"
                candidate.write_bytes(b"user image")
                marker = work / "candidate_r0_attempt1.png.request-pending"
                marker.write_text("submitted", encoding="utf-8")
                item.update(status="failed", sourcePath=str(source),
                            ai={"reference_image_path": str(candidate)})
                store.create(state)
                with patch.object(batch_worker, "DATA_DIR", root), patch.object(batch_worker, "batch_store", store):
                    direct = batch_worker.restart_item_to_run(state["id"], item["id"])
                self.assertTrue(direct)
                self.assertEqual(store.get(state["id"])["items"][0]["status"], "confirmed")
                self.assertTrue(marker.exists(), "旧请求检查点应保留供核对")

    def test_restart_without_images_goes_back_to_prep(self) -> None:
        """图被删掉之后「重新开始」必须回备料**重新出图**（2026-09-20 用户要求）。

        用户：「把现在已经完成的手部模糊的图片删除…让我点击批量重新开始的时候他会重新开始
        按照你的新的规则生成图片」。这条钉住的正是那个前提：候选图和发布目录里的人物图都不在
        → 条目回到 `pending`（备料会按当前提示词重新出人物图 + 两张封面），而不是拿着一个
        不存在的图「直接续跑出片」。
        """
        from backend import batch_store as batch_store_module
        from backend import batch_worker
        from backend.batch_store import BatchStore

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            original = batch_store_module.DB_PATH
            try:
                batch_store_module.DB_PATH = root / "queue.db"
                store = BatchStore()
                state = new_batch_state(["https://v.douyin.com/dance"], [])
                item = state["items"][0]
                source = root / "source.mp4"
                source.write_bytes(b"fake-source")
                item.update(
                    status="completed",
                    stage="completed",
                    sourcePath=str(source),
                    # 故意指向一个已经不存在的候选图（= 用户把模糊的图删了）
                    ai={"reference_image_path": str(root / "candidate-deleted.png")},
                )
                state["status"] = "completed"
                store.create(state)
                with patch.object(batch_worker, "batch_store", store), patch.object(
                    batch_worker, "BATCH_OUTPUT_ROOT", root / "发布成品"
                ):
                    batch_worker.restart_item_to_run(state["id"], item["id"])
                row = next(it for it in store.get(state["id"])["items"] if it["id"] == item["id"])
            finally:
                batch_store_module.DB_PATH = original
        self.assertEqual((row["status"], row["stage"]), ("pending", "queued"))

    def test_restart_keeps_the_three_existing_images(self) -> None:
        """人物图 / B站封面 / 抖音封面都在时，「重新开始」**不许重新生成**这三张。

        用户 2026-09-20：「我的意思是如果人物图 B站图片 抖音图片都有的话点击重新开始不要
        重新生成」。做法两条：① 有候选人物图 + 源视频还在 → 直接回到 `confirmed` 只重跑出片
        （这条路根本不碰图片）；② 万一回备料，已经出好的图只会被沿用/收编，封面只补缺的。
        """
        import asyncio

        from backend import batch_store as batch_store_module
        from backend import batch_worker
        from backend.batch_store import BatchStore

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            original_db = batch_store_module.DB_PATH
            original_data = batch_worker.DATA_DIR
            generated: list = []
            spawned: list = []
            try:
                batch_store_module.DB_PATH = root / "queue.db"
                batch_worker.DATA_DIR = root / "data"
                store = BatchStore()
                state = new_batch_state(["https://v.douyin.com/dance"], [])
                item = state["items"][0]
                publish = root / "发布成品" / "015_跳舞_标题_7687160799476134799"
                publish.mkdir(parents=True)
                for name in ("人物图.png", "B站4x3.png", "抖音3x4.png", "发布文案.txt"):
                    (publish / name).write_text("已有内容", encoding="utf-8")
                source = root / "source.mp4"
                source.write_bytes(b"fake-source")
                image = root / "candidate.png"
                image.write_bytes(b"fake-image")
                item.update(
                    status="skipped",
                    stage="skipped",
                    awemeId="7687160799476134799",
                    skipRequested=True,
                    sourcePath=str(source),
                    # 故意不给 reviewApproved：有图就够了，重新开始不该因为"没放行过"退回备料
                    reviewApproved=False,
                    revisionMode="image",          # 上一次「只调图片」的残留，必须被清掉
                    outputs={"folder": str(publish)},
                    ai={"reference_image_path": str(image)},
                )
                for milestone in item["milestones"]:
                    milestone.update(status="completed")
                state["status"] = "completed"
                store.create(state)

                async def fake_generate_person(*args, **kwargs):
                    generated.append((args, kwargs))
                    raise AssertionError("重新开始不该重新出人物图")

                def fake_spawn_covers(*args, **kwargs):
                    spawned.append((args, kwargs))
                    return True

                with patch.object(batch_worker, "batch_store", store), patch.object(
                    batch_worker, "BATCH_OUTPUT_ROOT", root / "发布成品"
                ), patch.object(
                    batch_image, "generate_person_image", fake_generate_person
                ), patch.object(batch_worker, "spawn_api_covers", fake_spawn_covers):
                    result = batch_worker.restart_batch_items(state["id"], [item["id"]])

                    row = store.get(state["id"])["items"][0]
                    self.assertEqual(result["restarted"], [item["id"]])
                    # 只重跑出片：状态直接回到 confirmed，源视频与候选图都还在
                    self.assertEqual((row["status"], row["stage"]), ("confirmed", "confirmed"))
                    # 上一次「只调图片」的意图不许跨过一次重新开始
                    self.assertEqual(row["revisionMode"], "")
                    self.assertEqual(row["revisionFeedback"], "")
                    self.assertIn("不会重新出图", batch_worker.restart_item_log(True))
                    self.assertIn("不会重新出图", batch_worker.restart_item_log(False))
                    self.assertEqual(generated, [], "重新开始不该重新出图")
                    self.assertEqual(spawned, [], "三张都在时不该再补封面")
                    # 三张图一个字节都没被改写（体检会顺手把目录改成理想名，按作品号重新定位）
                    folder_now = batch_worker._existing_publish_folder(row)
                    self.assertIsNotNone(folder_now)
                    for name in ("人物图.png", "B站4x3.png", "抖音3x4.png"):
                        self.assertEqual((folder_now / name).read_text(encoding="utf-8"), "已有内容")
            finally:
                batch_store_module.DB_PATH = original_db
                batch_worker.DATA_DIR = original_data

    def test_restart_adopts_the_published_portrait_instead_of_regenerating(self) -> None:
        """候选图文件没了、但发布目录里还有人物图：先收编，同样不重新出图。"""
        from backend import batch_store as batch_store_module
        from backend import batch_worker
        from backend.batch_store import BatchStore

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            original_db = batch_store_module.DB_PATH
            original_data = batch_worker.DATA_DIR
            try:
                batch_store_module.DB_PATH = root / "queue.db"
                batch_worker.DATA_DIR = root / "data"
                store = BatchStore()
                state = new_batch_state(["https://v.douyin.com/dance"], [])
                item = state["items"][0]
                publish = root / "发布成品" / "015_跳舞_标题_7687160799476134799"
                publish.mkdir(parents=True)
                (publish / "人物图.png").write_bytes(b"published-portrait")
                source = root / "source.mp4"
                source.write_bytes(b"fake-source")
                item.update(
                    status="failed",
                    stage="failed",
                    awemeId="7687160799476134799",
                    sourcePath=str(source),
                    outputs={"folder": str(publish)},
                    # 条目里记着的候选图已经被清理掉了
                    ai={"reference_image_path": str(root / "gone.png")},
                )
                state["status"] = "failed"
                store.create(state)

                with patch.object(batch_worker, "batch_store", store), patch.object(
                    batch_worker, "BATCH_OUTPUT_ROOT", root / "发布成品"
                ):
                    direct = batch_worker.restart_item_to_run(state["id"], item["id"])

                row = store.get(state["id"])["items"][0]
                self.assertTrue(direct)
                self.assertEqual(row["status"], "confirmed")
                adopted = Path(row["ai"]["reference_image_path"])
                self.assertTrue(adopted.is_file(), "发布目录里的人物图应被收编成候选图")
                self.assertEqual(adopted.read_bytes(), b"published-portrait")
                self.assertTrue(adopted.is_relative_to(root), "收编的图不许写到真实 data 目录")
            finally:
                batch_store_module.DB_PATH = original_db
                batch_worker.DATA_DIR = original_data

    def test_confirmed_item_redownloads_a_missing_source_before_submitting(self) -> None:
        """重新开始时源视频已被清理：提交前先补一次下载，不能直接抛「文件不存在」。"""
        import asyncio

        from backend import batch_store as batch_store_module
        from backend import batch_worker
        from backend.batch_store import BatchStore

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            original_db = batch_store_module.DB_PATH
            try:
                batch_store_module.DB_PATH = root / "queue.db"
                store = BatchStore()
                state = new_batch_state(["https://v.douyin.com/song"], [])
                state["items"][0].update(status="confirmed", reviewApproved=True, sourcePath=str(root / "gone.mp4"))
                store.create(state)
                item = state["items"][0]
                order: list[str] = []

                async def fake_download(batch_id, item_id):
                    order.append("download")
                    store.mutate_item(
                        batch_id,
                        item_id,
                        lambda row: row.update(sourcePath=str(root / "redownloaded.mp4")),
                    )

                async def fake_submit(batch_id, item_id):
                    order.append("submit")
                    return {"id": "job-new"}

                async def fake_watch(batch_id, item_id, child_id, milestone_id):
                    return {"id": child_id, "status": "completed", "finalOutput": str(root / "final.mp4")}

                async def fake_deliver(batch_id, item_id, video_job):
                    return {"videoFinal": "x", "folder": "y"}

                with patch.object(batch_worker, "batch_store", store), patch.object(
                    batch_worker, "_download", fake_download
                ), patch.object(batch_worker, "_post_video_job", fake_submit), patch.object(
                    batch_worker, "_watch_child", fake_watch
                ), patch.object(batch_worker, "_deliver", fake_deliver):
                    asyncio.run(batch_worker._process_confirmed(state["id"], item["id"]))

                self.assertEqual(order, ["download", "submit"])
            finally:
                batch_store_module.DB_PATH = original_db

    def test_reopen_review_brings_the_item_back_to_the_confirmation_page(self) -> None:
        """回到「等待你的确认」（2026-09-14 用户：「现在回不去」）：出片后能退回去改内容。

        正在出片的条目先安全取消（交给 runner 收尾时退回审核点），没在出片的直接退。
        """
        import asyncio

        from backend import app as app_module
        from backend import batch_store as batch_store_module
        from backend import batch_worker
        from backend.batch_store import BatchStore

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            original_db = batch_store_module.DB_PATH
            try:
                batch_store_module.DB_PATH = root / "queue.db"
                store = BatchStore()
                state = new_batch_state(
                    [
                        "https://v.douyin.com/done",
                        "https://v.douyin.com/running",
                        "https://v.douyin.com/waiting",
                    ],
                    [],
                )
                done, running, waiting = state["items"]
                image = root / "candidate.png"
                image.write_bytes(b"fake-image")
                done.update(
                    status="completed",
                    stage="completed",
                    reviewApproved=True,
                    videoJobId="job-old",
                    outputs={"videoFinal": "E:/old/最终成片.mp4", "folder": "E:/old"},
                    ai={"reference_image_path": str(image), "remove_subtitles": True},
                )
                running.update(
                    status="running",
                    stage="video",
                    reviewApproved=True,
                    videoJobId="job-live",
                    childJob={"id": "job-live", "status": "running"},
                    ai={"reference_image_path": str(image)},
                )
                # 已放行、但 runner 还没轮到它出片（用户 2026-09-13 点的就是这一种状态）
                waiting.update(
                    status="confirmed",
                    stage="confirmed",
                    reviewApproved=True,
                    videoJobId=None,
                    childJob=None,
                    ai={"reference_image_path": str(image)},
                )
                for item in (done, running):
                    for milestone in item["milestones"]:
                        if milestone["id"] == "review":
                            milestone.update(status="completed", progress=100)
                        elif milestone["id"] == "video":
                            milestone.update(status="completed", progress=100)
                for milestone in waiting["milestones"]:
                    if milestone["id"] == "review":
                        milestone.update(status="completed", progress=100)
                    elif milestone["id"] == "video":
                        milestone.update(status="pending", progress=0)
                state["status"] = "completed"
                store.create(state)

                def row_of(item_id):
                    return next(it for it in store.get(state["id"])["items"] if it["id"] == item_id)

                with patch.object(app_module, "batch_store", store), patch.object(
                    batch_worker, "batch_store", store
                ):
                    # 已经出完片的条目：直接退回确认页
                    asyncio.run(app_module.reopen_batch_item_review(state["id"], done["id"]))
                    row = row_of(done["id"])
                    self.assertEqual((row["status"], row["stage"]), ("awaiting_review", "review"))
                    self.assertFalse(row["reviewApproved"])
                    self.assertIsNone(row["videoJobId"])
                    steps = {step["id"]: step["status"] for step in row["milestones"]}
                    self.assertEqual(steps["review"], "running")
                    self.assertEqual(steps["video"], "pending")
                    self.assertNotIn("deliver", steps)
                    self.assertEqual(store.get(state["id"])["status"], "awaiting_review")
                    # 上一版的成片不丢：发布文件记录保留，磁盘上的文件不会被删
                    self.assertEqual(row["outputs"]["folder"], "E:/old")

                    # 已放行但**还没轮到**出片的条目：退回确认只是取消这次放行，
                    # 不取消任何子任务，也不能动到正在出片的别的条目。
                    asyncio.run(app_module.reopen_batch_item_review(state["id"], waiting["id"]))
                    row = row_of(waiting["id"])
                    self.assertEqual((row["status"], row["stage"]), ("awaiting_review", "review"))
                    self.assertFalse(row["reviewApproved"])
                    self.assertFalse(row.get("reopenRequested"))
                    self.assertFalse(row.get("skipRequested"))
                    self.assertIsNone(row.get("childJob"))
                    steps = {step["id"]: step["status"] for step in row["milestones"]}
                    self.assertEqual(steps["review"], "running")
                    self.assertEqual(steps["video"], "pending")
                    untouched = row_of(running["id"])
                    self.assertEqual(untouched["status"], "running")
                    self.assertEqual(untouched["childJob"]["id"], "job-live")
                    self.assertFalse(untouched.get("skipRequested"))
                    self.assertFalse(untouched.get("reopenRequested"))

                    # 正在出片的条目：先标记要退回 + 请求取消，由 runner 收尾时落到确认页
                    asyncio.run(app_module.reopen_batch_item_review(state["id"], running["id"]))
                    row = row_of(running["id"])
                    self.assertEqual(row["status"], "running")
                    self.assertTrue(row["reopenRequested"])
                    self.assertTrue(row["skipRequested"])
                    asyncio.run(batch_worker._finish_abandoned(state["id"], running["id"], deleted=False))
                    row = row_of(running["id"])
                    self.assertEqual((row["status"], row["stage"]), ("awaiting_review", "review"))
                    self.assertFalse(row["reopenRequested"])
                    self.assertFalse(row["skipRequested"])
            finally:
                batch_store_module.DB_PATH = original_db

    def test_spa_entry_is_never_cached(self) -> None:
        """SPA 入口必须 no-store：index.html 引用带 hash 的 bundle，缓存住刷新也只是旧前端。"""
        import asyncio

        from backend import app as app_module

        for path in ("/batch", "/singing", "/migrate", "/upscale", "/rvc", "/subtitle"):
            route = next(route for route in app_module.app.routes if getattr(route, "path", "") == path)
            response = asyncio.run(route.endpoint())
            self.assertEqual(response.headers.get("cache-control"), "no-store", path)

    def test_batch_item_progress_never_fakes_a_percentage(self) -> None:
        """跳舞链路没有节点级进度：条目进度不能冻在 0%，要给出真实的分段/去字幕/二采说明。

        2026-09-14 用户：「批量跳舞视频没有进度吗」——SCAIL 迁移全程没有 ComfyUI 采样
        progress 事件，子任务 progress 一直是 None，而 `_watch_child` 原先写 `or 0`，
        条目进度条整整 35 分钟显示 0%（实际已经跑完 7 段进了二采）。
        """
        from backend.batch_worker import child_progress_label

        label = child_progress_label(
            {
                "currentNodeTitle": "SamplerCustom",
                "currentSegment": 3,
                "estimatedSegments": 7,
                "cleanBatch": 2,
                "cleanBatches": 2,
                "upscaleBatch": 1,
                "upscaleBatches": 4,
            }
        )
        for piece in ("SamplerCustom", "分段 3/7", "去字幕 2/2", "二采 1/4"):
            self.assertIn(piece, label)
        self.assertEqual(child_progress_label({}), "")
        self.assertEqual(child_progress_label({"currentNodeTitle": "SamplerCustom"}), "SamplerCustom")

        # 进度未知时必须写 None（前端显示「进行中 + 已耗时」），不能再写成 0
        source = (Path(__file__).parents[1] / "backend" / "batch_worker.py").read_text(encoding="utf-8")
        self.assertNotIn('progress=child.get("progress") or 0', source)
        self.assertIn('progress=child.get("progress")', source)

    def test_elapsed_format_matches_ui(self) -> None:
        self.assertEqual(format_elapsed("2026-09-03T00:00:00+00:00", "2026-09-03T01:02:03+00:00"), "01:02:03")

    def test_required_local_resources_exist(self) -> None:
        missing = {name: str(path) for name, path in required_paths().items() if not path.is_file()}
        self.assertEqual(missing, {})

    def test_upscale_uses_the_required_realesrgan_workflow(self) -> None:
        self.assertEqual(
            UPSCALE_WORKFLOW.name,
            "视频-成片输入-独立二采-RealESRGAN4x转1080P-8GB高清加强版.json",
        )

    def test_singing_inputs_are_replaced_without_touching_source_file(self) -> None:
        prepared = prepare_singing_workflow(
            "unit-source.mp4",
            "unit-person.png",
            "unit action",
            "unit camera",
            "video/H3_MotionStudio/unit-original",
            SINGING_WORKFLOW,
        )
        self.assertEqual(node_by_id(prepared, 307)["widgets_values"][0], "unit-person.png")
        self.assertEqual(node_by_id(prepared, 300)["widgets_values"][0], "unit-source.mp4")
        self.assertEqual(node_by_id(prepared, 480)["widgets_values"][1], "unit action")
        self.assertEqual(node_by_id(prepared, 480)["widgets_values"][2], "unit camera")
        self.assertEqual(node_by_id(prepared, 59)["widgets_values"][0], "video/H3_MotionStudio/unit-original")

    def test_singing_canvas_groups_and_defaults(self) -> None:
        self.assertEqual(singing_canvas_params("4:3")["sing_width"], 640)
        self.assertEqual(singing_canvas_params("4:3")["sing_height"], 480)
        portrait = singing_canvas_params("9:16")
        self.assertEqual((portrait["sing_width"], portrait["sing_height"]), (480, 864))
        self.assertAlmostEqual(portrait["megapixels"], 0.41)
        with self.assertRaises(ValueError):
            singing_canvas_params("16:9")

    def test_singing_workflow_keeps_author_4x3_defaults_when_canvas_omitted(self) -> None:
        prepared = prepare_singing_workflow(
            "unit-source.mp4", "unit-person.png", "", "", "video/H3_MotionStudio/unit", SINGING_WORKFLOW
        )
        for clip_id in (15, 29, 400, 420, 440):
            widgets = node_by_id(prepared, clip_id)["widgets_values"]
            self.assertEqual(widgets[1:3], [640, 480], f"clip {clip_id} should stay 640x480")
        self.assertEqual(node_by_id(prepared, 269)["widgets_values"][1], 0.31)

    def test_singing_workflow_9x16_replaces_every_clip_canvas_and_reference_scale(self) -> None:
        prepared = prepare_singing_workflow(
            "unit-source.mp4",
            "unit-person.png",
            "",
            "",
            "video/H3_MotionStudio/unit",
            SINGING_WORKFLOW,
            canvas=singing_canvas_params("9:16"),
        )
        for clip_id in (15, 29, 400, 420, 440):
            widgets = node_by_id(prepared, clip_id)["widgets_values"]
            self.assertEqual(widgets[1:3], [480, 864], f"clip {clip_id} should be 480x864")
        self.assertAlmostEqual(node_by_id(prepared, 269)["widgets_values"][1], 0.41)
        # 4:3 显式传入与作者默认一致（幂等）
        horizontal = prepare_singing_workflow(
            "unit-source.mp4",
            "unit-person.png",
            "",
            "",
            "video/H3_MotionStudio/unit",
            SINGING_WORKFLOW,
            canvas=singing_canvas_params("4:3"),
        )
        for clip_id in (15, 29, 400, 420, 440):
            self.assertEqual(node_by_id(horizontal, clip_id)["widgets_values"][1:3], [640, 480])

    def test_h3_lyrics_canvas_injection_guards_stale_comfyui(self) -> None:
        prompt = {
            "480": {
                "class_type": "H3AutoLyricsFromAudio5StyleSafeCamera",
                "inputs": {"action_direction": "sings"},
            }
        }
        info_new = {
            "H3AutoLyricsFromAudio5StyleSafeCamera": {
                "input": {
                    "required": {"action_direction": ["STRING", {}]},
                    "optional": {"canvas_ratio": [["4:3", "9:16"], {}]},
                },
                "output": {},
            }
        }
        # 9:16 注入成功
        self.assertTrue(patch_h3_lyrics_canvas(prompt, info_new, "9:16"))
        self.assertEqual(prompt["480"]["inputs"]["canvas_ratio"], "9:16")
        # 4:3 是节点默认：不写输入也算成功
        untouched = {
            "480": {"class_type": "H3AutoLyricsFromAudio5StyleSafeCamera", "inputs": {}},
        }
        self.assertTrue(patch_h3_lyrics_canvas(untouched, info_new, "4:3"))
        self.assertNotIn("canvas_ratio", untouched["480"]["inputs"])
        # 旧版 ComfyUI（节点未升级、没有 canvas_ratio 输入）→ False，9:16 不得放行
        info_old = {
            "H3AutoLyricsFromAudio5StyleSafeCamera": {
                "input": {"required": {"action_direction": ["STRING", {}]}},
                "output": {},
            }
        }
        self.assertFalse(patch_h3_lyrics_canvas(prompt, info_old, "9:16"))
        # 4:3 在旧版节点上同样视为成功（提示词默认即 4:3）
        self.assertTrue(patch_h3_lyrics_canvas(untouched, info_old, "4:3"))

    def test_h3_lyrics_node_portrait_prompt_rewrites_canvas_text(self) -> None:
        """歌词节点（ComfyUI 环境）9:16 文案改写：4:3 原样、9:16 无残留横版字面量。"""
        node_file = Path(r"D:\Comfyui\ComfyUI\custom_nodes\h3_media_duration_router\__init__.py")
        if not node_file.is_file():
            self.skipTest("h3_media_duration_router 自定义节点未安装")
        import importlib.util

        spec = importlib.util.spec_from_file_location("h3_media_duration_router_canvas", node_file)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        landscape = (
            "Continue the same performance in the connected frame as a full-canvas 4:3 landscape "
            "composition at 640x480 from the connected 4:3 reference frame and the protected "
            "handover frames. Compose specifically for a 4:3 safe frame: keep comfortable headroom, "
            "keep the face, mouth, shoulders, and any requested hand gesture inside the frame, and "
            "keep lateral body movement restrained so the performer never drifts into an edge crop. "
            "Camera directions for this segment: 缓慢推近. Keep the motion restrained and physically "
            "coherent for a 4:3 landscape frame, preserve the same scene, maintain comfortable "
            "headroom, and keep the performer's face, mouth, shoulders, and active hands comfortably "
            "visible. Avoid large lateral travel, excessive push-in, edge cropping, or "
            "vertical-video-style composition. Do not add intentional camera movement; preserve the "
            "established framing with only an almost imperceptible natural handheld breathing drift, "
            "maintaining a balanced full-canvas 4:3 landscape composition with comfortable headroom."
        )
        portrait = module.portrait_prompt(landscape)
        self.assertIn("full-canvas 9:16 portrait composition at 480x864", portrait)
        self.assertIn("connected 9:16 reference frame", portrait)
        self.assertIn("Compose specifically for a 9:16 safe frame", portrait)
        self.assertIn("coherent for a 9:16 portrait frame", portrait)
        self.assertIn("horizontal-video-style composition", portrait)
        self.assertIn("maintaining a balanced full-canvas 9:16 portrait composition", portrait)
        self.assertNotIn("4:3", portrait)
        self.assertNotIn("640x480", portrait)
        self.assertNotIn("vertical-video-style", portrait)
        # 无横版字面量时原样返回
        plain = "普通提示词，无画布字面量。"
        self.assertEqual(module.portrait_prompt(plain), plain)

    def test_upscale_video_and_output_are_replaced(self) -> None:
        prepared = prepare_upscale_workflow(
            "unit-original.mp4",
            "video/H3_MotionStudio/unit-1080P",
            UPSCALE_WORKFLOW,
        )
        self.assertEqual(node_by_id(prepared, 2)["widgets_values"]["video"], "unit-original.mp4")
        self.assertEqual(
            node_by_id(prepared, 8)["widgets_values"]["filename_prefix"],
            "video/H3_MotionStudio/unit-1080P",
        )

    def test_upscale_scale_switches_to_vertical_1080x1920(self) -> None:
        prepared = prepare_upscale_workflow(
            "draft.mp4",
            "video/H3_MotionStudio/unit-1080P",
            UPSCALE_WORKFLOW,
            scale=(1080, 1920),
        )
        widgets = node_by_id(prepared, 5)["widgets_values"]
        self.assertEqual((widgets[1], widgets[2]), (1080, 1920))
        # 未指定模型时保持工作流默认（x4plus）
        self.assertEqual(node_by_id(prepared, 3)["widgets_values"][0], "RealESRGAN_x4plus.pth")

    def test_upscale_model_can_switch_to_x2plus(self) -> None:
        prepared = prepare_upscale_workflow(
            "draft.mp4",
            "video/H3_MotionStudio/unit-1080P",
            UPSCALE_WORKFLOW,
            upscale_model="RealESRGAN_x2plus.pth",
        )
        self.assertEqual(node_by_id(prepared, 3)["widgets_values"][0], "RealESRGAN_x2plus.pth")

    def test_clean_workflow_replaces_source_prefix_and_9x16_canvas(self) -> None:
        canvas = canvas_params("9:16")
        prepared = prepare_clean_workflow(
            "subtitles.mp4",
            "video/H3_MotionStudio/unit-clean",
            CLEAN_WORKFLOW,
            canvas=canvas,
        )
        self.assertEqual(node_by_id(prepared, 1)["widgets_values"]["video"], "subtitles.mp4")
        self.assertEqual(node_by_id(prepared, 1)["widgets_values"]["custom_width"], 512)
        self.assertEqual(node_by_id(prepared, 1)["widgets_values"]["custom_height"], 896)
        # [shape, frames, location_x, location_y, grow, frame_width, frame_height, shape_width, shape_height]
        mask = node_by_id(prepared, 2)["widgets_values"]
        self.assertEqual(mask[2:4], [256, 803])
        self.assertEqual(mask[5:9], [512, 896, 430, 135])
        self.assertEqual(node_by_id(prepared, 3)["widgets_values"][:2], [512, 896])
        self.assertEqual(
            node_by_id(prepared, 5)["widgets_values"]["filename_prefix"],
            "video/H3_MotionStudio/unit-clean",
        )

    def test_clean_workflow_keeps_author_4x3_defaults_when_canvas_omitted(self) -> None:
        prepared = prepare_clean_workflow("subtitles.mp4", "video/H3_MotionStudio/unit-clean", CLEAN_WORKFLOW)
        mask = node_by_id(prepared, 2)["widgets_values"]
        self.assertEqual(mask[3], 344)
        self.assertEqual(mask[7], 430)

    def test_migrate_workflow_replaces_drive_reference_mode_prompts_and_canvas(self) -> None:
        canvas = canvas_params("9:16")
        prepared = prepare_migrate_workflow(
            "clean.mp4",
            "portrait.png",
            "animation",
            "video/H3_MotionStudio/unit-migrate",
            MIGRATE_WORKFLOW,
            canvas=canvas,
            content_prompt="一位女孩在唱歌",
            video_prompt="singer",
            image_prompt="girl",
        )
        self.assertEqual(node_by_id(prepared, 563)["widgets_values"][0], "clean.mp4")
        self.assertEqual(node_by_id(prepared, 469)["widgets_values"]["video"], "clean.mp4")
        self.assertEqual(node_by_id(prepared, 543)["widgets_values"]["video"], "clean.mp4")
        self.assertEqual(node_by_id(prepared, 30)["widgets_values"][0], "portrait.png")
        self.assertEqual(node_by_id(prepared, 342)["widgets_values"][0], 512)
        self.assertEqual(node_by_id(prepared, 343)["widgets_values"][0], 896)
        self.assertFalse(node_by_id(prepared, 353)["widgets_values"][0])  # 动作迁移 = false
        self.assertEqual(node_by_id(prepared, 545)["widgets_values"][0], "一位女孩在唱歌")
        self.assertEqual(node_by_id(prepared, 509)["widgets_values"][0], "singer")
        self.assertEqual(node_by_id(prepared, 510)["widgets_values"][0], "girl")
        self.assertEqual(
            node_by_id(prepared, 456)["widgets_values"]["filename_prefix"],
            "video/H3_MotionStudio/unit-migrate",
        )

    def test_migrate_workflow_replacement_mode_maps_to_true(self) -> None:
        prepared = prepare_migrate_workflow(
            "clean.mp4",
            "portrait.png",
            "replacement",
            "video/H3_MotionStudio/unit-migrate",
            MIGRATE_WORKFLOW,
        )
        self.assertTrue(node_by_id(prepared, 353)["widgets_values"][0])

    def test_migrate_workflow_model_overrides_match_blogger_loop(self) -> None:
        prepared = prepare_migrate_workflow(
            "clean.mp4",
            "portrait.png",
            "animation",
            "video/H3_MotionStudio/unit-migrate",
            MIGRATE_WORKFLOW,
            unet_model="wan2.1_14B_SCAIL_2_int8_convrot.safetensors",
            lightx2v_lora=r"Wan2.1\lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors",
        )
        self.assertEqual(node_by_id(prepared, 329)["widgets_values"][0], "wan2.1_14B_SCAIL_2_int8_convrot.safetensors")
        self.assertEqual(
            node_by_id(prepared, 322)["widgets_values"][0],
            r"Wan2.1\lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors",
        )
        # 不指定时保持工作流默认
        default = prepare_migrate_workflow("clean.mp4", "portrait.png", "animation", "video/H3_MotionStudio/unit", MIGRATE_WORKFLOW)
        self.assertEqual(node_by_id(default, 329)["widgets_values"][0], "wan2.1_14B_SCAIL_2_fp8_scaled.safetensors")

    def test_migrate_milestones_follow_options(self) -> None:
        # 二采放大开关默认开启：迁移链路末尾追加 upscale 里程碑
        self.assertEqual(
            [m["id"] for m in migrate_milestones(False, "animation", "4:3")],
            ["prep", "sam", "migrate", "save", "upscale"],
        )
        ids = [m["id"] for m in migrate_milestones(True, "replacement", "9:16")]
        self.assertEqual(ids[:4], ["read", "mask", "paint", "clean_save"])
        self.assertEqual(ids[-1], "upscale")
        self.assertNotIn("hd", ids)
        # 关闭二采开关时回到纯迁移链路（无 upscale）
        without = [m["id"] for m in migrate_milestones(True, "replacement", "9:16", False)]
        self.assertEqual(without, ["read", "mask", "paint", "clean_save", "prep", "sam", "migrate", "save"])
        replacement = next(m for m in migrate_milestones(False, "replacement", "4:3") if m["id"] == "migrate")
        self.assertIn("替换", replacement["label"])

    def test_singing_milestones_follow_upscale_and_rvc_switches(self) -> None:
        from backend.store import initial_milestones
        self.assertEqual(
            [m["id"] for m in initial_milestones(True, True)],
            ["input", "h3", "stitch", "upscale", "handoff", "stems", "voice", "mux"],
        )
        # 二采在 RVC 之前；关闭二采只剩生成段 + RVC
        self.assertEqual(
            [m["id"] for m in initial_milestones(True, False)],
            ["input", "h3", "stitch", "handoff", "stems", "voice", "mux"],
        )
        # 关闭 RVC 时保留二采段，收尾即高清成片
        self.assertEqual(
            [m["id"] for m in initial_milestones(False, True)],
            ["input", "h3", "stitch", "upscale"],
        )

    def test_batch_singing_jobs_skip_rvc_so_comfyui_never_stops(self) -> None:
        """批量唱歌只出**原声**成片（`use_rvc=0`）：RVC 搬到「一键批量转换音色」独立入口。

        这还有个硬好处：`use_rvc=0` 时收尾走 `_complete_without_rvc`，它**不关闭 ComfyUI**
        —— 于是批量期间 ComfyUI 全程常驻，模型不用每条重新加载（用户要的提速）。
        """
        import asyncio
        import inspect

        from backend import batch_worker, pipeline

        source = inspect.getsource(batch_worker._post_video_job)
        self.assertIn('"use_rvc": "0"', source)
        self.assertNotIn('"use_rvc": "1"', source)

        stopped: list = []
        updates: list[dict] = []

        class _Store:
            def set_milestone(self, *_args, **_kwargs):
                return None

            def add_log(self, *_args, **_kwargs):
                return None

            def update(self, _job_id, **changes):
                updates.append(changes)
                return changes

        async def fake_meta(_path):
            return {}

        async def scenario() -> None:
            with patch.object(pipeline, "store", _Store()), patch.object(
                pipeline.resources, "stop_comfy", lambda *args, **kwargs: stopped.append(args)
            ), patch.object(pipeline, "media_metadata", fake_meta):
                await pipeline._complete_without_rvc("job1", Path("E:/x/高清.mp4"))

        asyncio.run(scenario())
        self.assertEqual(stopped, [], "use_rvc=0 时绝不能关 ComfyUI（关了就要重新加载模型）")
        self.assertEqual(updates[-1]["status"], "completed")
        self.assertTrue(updates[-1]["finalReady"])

    def test_media_entries_only_expose_original_and_final(self) -> None:
        from backend.app import OUTPUT_MEDIA_FIELDS, MEDIA_FIELD_BY_KEY
        self.assertEqual([key for key, *_ in OUTPUT_MEDIA_FIELDS], ["final", "original"])
        # source_key 解析仍兼容历史中间产物的键
        self.assertEqual(MEDIA_FIELD_BY_KEY["draft"], "draftOutput")
        self.assertEqual(MEDIA_FIELD_BY_KEY["final"], "finalOutput")

    def test_upscale_pass_reuses_in_place_input_and_writes_batch_fields(self) -> None:
        """唱歌/迁移链路内嵌二采：源已在 ComfyUI input 目录时不得重复 link（否则会删源）。"""
        import asyncio
        from backend import pipeline as P

        P.COMFY_INPUT.mkdir(parents=True, exist_ok=True)
        job_id = "unittestupscalepass"
        source = P.COMFY_INPUT / f"motionstudio_{job_id}_upscale_src.mp4"
        source.write_bytes(b"stub")

        class StubStore:
            def __init__(self) -> None:
                self.state: dict = {"id": job_id}

            def update(self, _job_id, **changes):
                self.state.update(changes)
                return self.state

            def get(self, _job_id):
                return self.state

            def add_log(self, _job_id, message):
                self.logs.append(message)

            logs: list = []

        captured: dict = {}
        stub = StubStore()
        originals = (P.store, P.media_metadata, P.probe_media_frames, P.run_comfy_workflow)

        async def fake_metadata(_path):
            return {"width": 640, "height": 480, "frames": 160}

        async def fake_frames(_path):
            return 160

        async def fake_run(_job_id, kind, workflow, _node, **kwargs):
            captured["kind"] = kind
            captured["video"] = node_by_id(workflow, 2)["widgets_values"]["video"]
            captured["scale"] = tuple(node_by_id(workflow, 5)["widgets_values"][1:3])
            captured["model"] = node_by_id(workflow, 3)["widgets_values"][0]
            captured["kwargs"] = kwargs
            return Path("stub-output.mp4")

        try:
            P.store = stub
            P.media_metadata = fake_metadata
            P.probe_media_frames = fake_frames
            P.run_comfy_workflow = fake_run
            with patch.object(P, "upscale_engine", return_value="esrgan"):
                result = asyncio.run(
                    P.run_upscale_pass(
                        job_id,
                        source,
                        input_tag="upscale_src",
                        batch_fields=("upscaleBatch", "upscaleBatches"),
                    )
                )
            source_survived = source.is_file()
        finally:
            P.store, P.media_metadata, P.probe_media_frames, P.run_comfy_workflow = originals
            source.unlink(missing_ok=True)

        self.assertTrue(source_survived, "内嵌二采不能删掉已在 input 目录里的源成片")
        self.assertEqual(result, Path("stub-output.mp4"))
        self.assertEqual(captured["kind"], "upscale")
        self.assertEqual(captured["video"], source.name)
        self.assertEqual(captured["scale"], (1440, 1080))  # 640×480 → 4:3 1080p 档
        self.assertEqual(captured["model"], "RealESRGAN_x4plus.pth")
        self.assertEqual(captured["kwargs"].get("batch_fields"), ("upscaleBatch", "upscaleBatches"))
        # 160 帧 / 每批 8 帧 = 20 批，进度写 upscaleBatch/upscaleBatches（不碰 H3 分段字段）
        self.assertEqual(stub.state.get("upscaleBatches"), 20)
        self.assertNotIn("currentSegment", stub.state)
        self.assertNotIn("estimatedSegments", stub.state)

    def test_rvc_route_milestones_and_endpoint(self) -> None:
        """独立音色转换路由：里程碑与提交接口都在。"""
        from backend.app import app
        from backend.store import rvc_milestones
        self.assertEqual(
            [m["id"] for m in rvc_milestones()],
            ["handoff", "stems", "voice", "mux"],
        )
        self.assertIn("/api/jobs/rvc", {getattr(route, "path", "") for route in app.routes})

    def test_upscale_milestones_and_target_1080p(self) -> None:
        from backend.app import _upscale_target
        from backend.store import upscale_milestones
        self.assertEqual([m["id"] for m in upscale_milestones()], ["upscale", "hd"])
        self.assertEqual(_upscale_target(512, 384), (1440, 1080))    # 4:3
        self.assertEqual(_upscale_target(512, 896), (1080, 1920))    # 9:16
        self.assertEqual(_upscale_target(1920, 1080), (1920, 1080))  # 16:9
        self.assertEqual(_upscale_target(640, 480), (1440, 1080))

    def test_estimate_migrate_segments_matches_workflow_formula(self) -> None:
        from backend.pipeline import estimate_migrate_segments
        self.assertEqual(estimate_migrate_segments(1034), 14)  # 17s @60fps
        self.assertEqual(estimate_migrate_segments(519), 7)    # 17s @30fps
        self.assertEqual(estimate_migrate_segments(81), 1)
        self.assertEqual(estimate_migrate_segments(40), 1)
        self.assertIsNone(estimate_migrate_segments(None))

    def test_estimate_singing_segments_matches_h3_duration_plan(self) -> None:
        """H3 每段 362 帧(@24fps≈15.08s)、段间 22 帧续接 → 容量 362/702/1042/1382/1722。"""
        from backend.pipeline import estimate_singing_segments
        self.assertIsNone(estimate_singing_segments(None))
        self.assertIsNone(estimate_singing_segments(0))
        self.assertEqual(estimate_singing_segments(10), 1)       # 240 帧
        self.assertEqual(estimate_singing_segments(15.0), 1)     # 360 帧
        self.assertEqual(estimate_singing_segments(15.083), 1)   # ≈362 帧
        self.assertEqual(estimate_singing_segments(15.125), 2)   # 363 帧
        self.assertEqual(estimate_singing_segments(29.25), 2)    # 702 帧
        self.assertEqual(estimate_singing_segments(29.3), 3)     # 704 帧
        self.assertEqual(estimate_singing_segments(40), 3)       # 960 帧（应用上传上限 40s）
        self.assertEqual(estimate_singing_segments(43.5), 4)     # 1044 帧
        self.assertEqual(estimate_singing_segments(58), 5)       # 1392 帧
        self.assertEqual(estimate_singing_segments(80), 5)       # 超出 5 段容量时封顶 5

    def test_graph_to_api_prompt_keeps_autogrow_expanded_inputs(self) -> None:
        """ComfyUI v3 Autogrow（ComfyMathExpression values.a/b…）的链接不能被丢弃。"""
        workflow = {
            "nodes": [
                {
                    "id": 1,
                    "type": "PrimitiveInt",
                    "mode": 0,
                    "inputs": [{"name": "value", "widget": {"name": "value"}}],
                    "widgets_values": [32, "fixed"],
                    "outputs": [{"name": "INT", "type": "INT", "links": [0]}],
                },
                {
                    "id": 2,
                    "type": "ComfyMathExpression",
                    "mode": 0,
                    "inputs": [
                        {"name": "values.a", "link": 0},
                        {"name": "values.b", "link": None},
                        {"name": "expression", "widget": {"name": "expression"}},
                    ],
                    "widgets_values": ["a // 32"],
                    "outputs": [],
                },
            ],
            "links": [[0, 1, 0, 2, 0, "INT"]],
        }
        autogrow_spec = [
            "COMFY_AUTOGROW_V3",
            {
                "template": {
                    "input": {"required": {"value": ["FLOAT,INT,BOOLEAN", {}]}},
                    "names": ["a", "b", "c"],
                    "min": 1,
                }
            },
        ]
        object_info = {
            "PrimitiveInt": {"input": {"required": {"value": ["INT", {}]}}, "output": {}},
            "ComfyMathExpression": {
                "input": {"required": {"expression": ["STRING", {}], "values": autogrow_spec}},
                "output": {},
            },
        }
        prompt = graph_to_api_prompt(workflow, object_info)
        self.assertEqual(prompt["2"]["inputs"]["values.a"], ["1", 0])
        self.assertEqual(prompt["2"]["inputs"]["expression"], "a // 32")
        # 常规节点（PrimitiveInt 无 autogrow 时）行为不受影响
        self.assertEqual(prompt["1"]["inputs"]["value"], 32)

    def test_graph_to_api_prompt_skips_control_after_generate_widget(self) -> None:
        """KSampler 的 seed 后跟一个 control_after_generate 幽灵 widget，不能让后续 widget 错位。"""
        workflow = {
            "nodes": [
                {
                    "id": 18,
                    "type": "Seed",
                    "mode": 0,
                    "inputs": [],
                    "outputs": [{"name": "随机种", "type": "INT", "links": [31]}],
                    "widgets_values": [-1],
                },
                {
                    "id": 22,
                    "type": "KSampler",
                    "mode": 0,
                    "inputs": [
                        {"name": "model", "type": "MODEL", "link": 27},
                        {"name": "positive", "type": "CONDITIONING", "link": 28},
                        {"name": "negative", "type": "CONDITIONING", "link": 29},
                        {"name": "latent_image", "type": "LATENT", "link": 30},
                        {"name": "seed", "type": "INT", "widget": {"name": "seed"}, "link": 31},
                        {"name": "steps", "type": "INT", "widget": {"name": "steps"}, "link": None},
                        {"name": "cfg", "type": "FLOAT", "widget": {"name": "cfg"}, "link": None},
                        {"name": "sampler_name", "type": "COMBO", "widget": {"name": "sampler_name"}, "link": None},
                        {"name": "scheduler", "type": "COMBO", "widget": {"name": "scheduler"}, "link": None},
                        {"name": "denoise", "type": "FLOAT", "widget": {"name": "denoise"}, "link": None},
                    ],
                    "outputs": [{"name": "LATENT", "type": "LATENT", "links": [25]}],
                    "widgets_values": [1088049369132323, "randomize", 10, 1, "euler", "simple", 1],
                },
            ],
            "links": [[27, 1, 0, 22, 0, "MODEL"], [31, 18, 0, 22, 4, "INT"]],
        }
        object_info = {
            "Seed": {"input": {"required": {"seed": ["INT", {}]}}, "output": {}},
            "KSampler": {
                "input": {
                    "required": {
                        "model": ["MODEL", {}],
                        "positive": ["CONDITIONING", {}],
                        "negative": ["CONDITIONING", {}],
                        "latent_image": ["LATENT", {}],
                        "seed": ["INT", {}],
                        "steps": ["INT", {}],
                        "cfg": ["FLOAT", {}],
                        "sampler_name": [["euler", "dpmpp_2m"], {}],
                        "scheduler": [["simple", "karras"], {}],
                        "denoise": ["FLOAT", {}],
                    }
                },
                "output": {},
            },
        }
        prompt = graph_to_api_prompt(workflow, object_info)
        # widgets_values[1] 是幽灵 widget，steps 必须拿到 10 而不是 "randomize"
        self.assertEqual(prompt["22"]["inputs"]["steps"], 10)
        self.assertEqual(prompt["22"]["inputs"]["cfg"], 1)
        self.assertEqual(prompt["22"]["inputs"]["sampler_name"], "euler")
        self.assertEqual(prompt["22"]["inputs"]["scheduler"], "simple")
        self.assertEqual(prompt["22"]["inputs"]["denoise"], 1)
        # seed 有连线时仍以连线为准
        self.assertEqual(prompt["22"]["inputs"]["seed"], ["18", 0])

    def test_graph_to_api_prompt_fills_widgets_for_nodes_without_inputs(self) -> None:
        """inputs 为空数组的节点（rgthree Seed）必须按声明顺序补 widget，否则服务端缺 seed。"""
        workflow = {
            "nodes": [
                {
                    "id": 18,
                    "type": "Seed (rgthree)",
                    "mode": 0,
                    "inputs": [],
                    "outputs": [{"name": "随机种", "type": "INT", "links": [31]}],
                    "widgets_values": [-1, "", "", "okay"],
                },
                {
                    "id": 20,
                    "type": "MarkdownNote",
                    "mode": 0,
                    "inputs": [],
                    "outputs": [],
                    "widgets_values": ["说明文字"],
                },
            ],
            "links": [],
        }
        object_info = {
            "Seed (rgthree)": {"input": {"required": {"seed": ["INT", {}]}}, "output": {}},
            "MarkdownNote": {"input": {"required": {"text": ["STRING", {}]}}, "output": {}},
        }
        prompt = graph_to_api_prompt(workflow, object_info)
        self.assertEqual(prompt["18"]["inputs"]["seed"], -1)
        # 没有输出的注释节点保持原样，不额外塞 widget
        self.assertEqual(prompt["20"]["inputs"], {})

    def test_wan_chunk_feedforward_injection_rewires_model_chain(self) -> None:
        prompt = {
            "561": {"class_type": "WanVideoMemoryEfficientSageAttentionPatch", "inputs": {"model": ["322", 0]}},
            "330": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["561", 0]}},
            "332": {"class_type": "BasicScheduler", "inputs": {"model": ["561", 0]}},
        }
        object_info = {
            "WanChunkFeedForward": {
                "input": {
                    "required": {
                        "model": ["MODEL", {}],
                        "chunks": ["INT", {}],
                        "dim_threshold": ["INT", {}],
                    }
                },
                "output": {},
            }
        }
        self.assertTrue(patch_wan_chunk_feedforward(prompt, object_info))
        self.assertIn("561_chunk_ffn", prompt)
        self.assertEqual(prompt["561_chunk_ffn"]["class_type"], "WanChunkFeedForward")
        self.assertEqual(prompt["561_chunk_ffn"]["inputs"]["model"], ["561", 0])
        self.assertEqual(prompt["561_chunk_ffn"]["inputs"]["chunks"], 2)
        self.assertEqual(prompt["330"]["inputs"]["model"], ["561_chunk_ffn", 0])
        self.assertEqual(prompt["332"]["inputs"]["model"], ["561_chunk_ffn", 0])
        # 未安装该节点时静默跳过、不修改原 prompt
        original = {"561": {"inputs": {"model": ["322", 0]}}}
        self.assertFalse(patch_wan_chunk_feedforward(original, {"WanChunkFeedForward": None}))
        self.assertNotIn("561_chunk_ffn", original)


class DouyinServiceTests(unittest.TestCase):
    def test_default_downloader_port_avoids_windows_reserved_9000(self) -> None:
        if "H3_DOUYIN_DOWNLOADER_URL" not in os.environ:
            self.assertEqual(DOUYIN_URL, "http://127.0.0.1:9100")

    def test_aweme_id_supports_video_and_profile_modal_urls(self) -> None:
        self.assertEqual(
            _extract_aweme_id("https://www.douyin.com/video/7613347091070692019"),
            "7613347091070692019",
        )

    def test_douyin_url_validation_accepts_share_text_but_not_embedded_domains(self) -> None:
        self.assertTrue(is_douyin_url("复制打开 https://v.douyin.com/unit-test/ 看视频"))
        self.assertTrue(is_douyin_url("https://www.iesdouyin.com/share/video/123"))
        self.assertFalse(is_douyin_url("https://example.com/?next=douyin.com"))
        self.assertEqual(
            _extract_aweme_id("https://www.douyin.com/user/self?modal_id=7613347091070692019"),
            "7613347091070692019",
        )

    def test_cookie_ready_requires_non_empty_json_cookie_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch("backend.douyin_service.DOUYIN_ROOT", root):
                self.assertFalse(_cookie_ready())
                (root / ".cookies.json").write_text("{}", encoding="utf-8")
                self.assertFalse(_cookie_ready())
                (root / ".cookies.json").write_text(
                    '{"sessionid": "unit-session"}', encoding="utf-8"
                )
                self.assertTrue(_cookie_ready())

    def test_result_for_returns_matching_downloaded_video(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir)
            video = output / "creator" / "作品_7613347091070692019.mp4"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"video")
            with patch("backend.douyin_service.DOUYIN_OUTPUT", output):
                result = DouyinServiceManager().result_for(
                    {"url": "https://www.douyin.com/video/7613347091070692019"}
                )
            self.assertIsNotNone(result)
            self.assertEqual(result["awemeId"], "7613347091070692019")
            self.assertEqual(result["filename"], video.name)

    def test_result_for_ignores_incomplete_conversion_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir)
            original = output / "作品_7613347091070692019.mp4"
            staging = output / "作品_7613347091070692019.h3-converted.part.mp4"
            original.write_bytes(b"original")
            staging.write_bytes(b"partial")
            staging.touch()
            with patch("backend.douyin_service.DOUYIN_OUTPUT", output):
                result = DouyinServiceManager().result_for(
                    {"url": "https://www.douyin.com/video/7613347091070692019"}
                )
            self.assertIsNotNone(result)
            self.assertEqual(result["path"], str(original.resolve()))

    def test_job_payload_normalizes_success_and_attaches_media_urls(self) -> None:
        result = {
            "awemeId": "123",
            "filename": "unit.mp4",
            "path": r"D:\unit.mp4",
            "size": 10,
            "mediaType": "video/mp4",
        }
        with patch("backend.app.douyin_service.result_for", return_value=result):
            payload = douyin_job_payload({"job_id": "job-1", "status": "success"})
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["result"]["mediaUrl"], "/api/douyin/jobs/job-1/media")

    def test_download_conversion_replaces_original_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "douyin.mp4"
            source.write_bytes(b"hevc-source")

            def write_h264(_ffmpeg, _source, target):
                target.write_bytes(b"h264-output")

            with patch("backend.douyin_preview._tool", return_value="ffmpeg"), patch(
                "backend.douyin_preview._encode_sync", side_effect=write_h264
            ):
                _convert_download_sync(source, source)

            self.assertEqual(source.read_bytes(), b"h264-output")
            self.assertFalse((Path(temp_dir) / "douyin.h3-converted.mp4").exists())


class DouyinMirrorTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch(
            "backend.douyin_mirror.MIRROR_PATH",
            Path(tempfile.mkdtemp()) / "douyin-jobs.json",
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_mirror_round_trip_and_upsert(self) -> None:
        import backend.douyin_mirror as mirror
        mirror.upsert_jobs(
            [
                {"job_id": "a", "status": "success", "created_at": "2026-09-03T00:00:01Z"},
                {"job_id": "b", "status": "running", "created_at": "2026-09-03T00:00:02Z"},
            ]
        )
        mirror.upsert_jobs([{"job_id": "b", "status": "success", "created_at": "2026-09-03T00:00:02Z"}])
        self.assertEqual(mirror.get_job("b")["status"], "success")
        self.assertEqual([job["job_id"] for job in mirror.all_jobs()], ["b", "a"])

    def test_settle_stale_marks_ghost_active_jobs_failed(self) -> None:
        from backend.app import _settle_stale
        stale = {"job_id": "a", "status": "running", "url": "https://www.douyin.com/video/1"}
        settled = _settle_stale(stale, live_ids={"b"})
        self.assertEqual(settled["status"], "failed")
        self.assertIn("重新提交", settled["error"])
        self.assertEqual(_settle_stale(stale, live_ids={"a"})["status"], "running")
        self.assertEqual(_settle_stale({"job_id": "c", "status": "success"}, None)["status"], "success")


class BatchShutdownTests(unittest.TestCase):
    """「所有任务完成后自动关机」开关（2026-09-15 用户要求，**默认关闭**）。

    这一组用例全部只走记录桩（见 `_SHUTDOWN_SAFETY_PATCHES`），永远不会真的关机。
    """

    def setUp(self) -> None:
        from backend import batch_store as batch_store_module

        self.folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.folder.cleanup)
        self.pending = Path(self.folder.name) / "pending-shutdown.json"
        patcher = patch.object(_batch_worker_module, "PENDING_SHUTDOWN_PATH", self.pending)
        patcher.start()
        self.addCleanup(patcher.stop)
        original = batch_store_module.DB_PATH
        batch_store_module.DB_PATH = Path(self.folder.name) / "queue.db"
        self.addCleanup(setattr, batch_store_module, "DB_PATH", original)
        _SHUTDOWN_CALLS.clear()
        _ABORT_CALLS.clear()

    def _store(self):
        from backend.batch_store import BatchStore

        return BatchStore()

    def _finished_state(self, store, count: int = 2):
        urls = [f"https://v.douyin.com/{index}" for index in range(count)]
        state = new_batch_state(urls, [], shutdown_on_complete=True)
        store.create(state)
        for item in state["items"][:-1]:
            store.mutate_item(state["id"], item["id"], lambda row: row.update(status="completed"))
        return store.get(state["id"])

    def test_batch_shutdown_defaults_off_and_is_toggleable(self) -> None:
        from backend import batch_worker

        store = self._store()
        state = new_batch_state(["https://v.douyin.com/a"], [])
        # 默认关闭：新建批次与老批次都不能自己打开
        self.assertFalse(state["shutdownOnComplete"])
        self.assertFalse(state["shutdownAfterPrep"])
        store.create(state)
        with patch.object(batch_worker, "batch_store", store):
            opened = batch_worker.set_batch_shutdown_on_complete(state["id"], True)
            self.assertTrue(opened["shutdownOnComplete"])
            closed = batch_worker.set_batch_shutdown_on_complete(state["id"], False)
            self.assertFalse(closed["shutdownOnComplete"])
            self.assertFalse(store.get(state["id"])["shutdownOnComplete"])

        # 硬开关 H3_AUTO_SHUTDOWN=0：这台机器永不自动关机
        with patch.object(
            batch_worker,
            "env_value",
            lambda name, default="": "0" if name == "H3_AUTO_SHUTDOWN" else default,
        ):
            self.assertTrue(batch_worker.shutdown_disabled())
            self.assertEqual(batch_worker.shutdown_tick(), "idle")
            with patch.object(batch_worker, "batch_store", store):
                with self.assertRaises(ValueError):
                    batch_worker.set_batch_shutdown_on_complete(state["id"], True)

    def test_shutdown_after_prep_waits_for_every_preparation_and_disables_render(self) -> None:
        from backend import batch_worker

        store = self._store()
        state = new_batch_state(["https://v.douyin.com/a", "https://v.douyin.com/b"], [])
        store.create(state)
        with patch.object(batch_worker, "batch_store", store):
            opened = batch_worker.set_batch_shutdown_after_prep(state["id"], True)
            self.assertFalse(opened["autoRender"])
            self.assertTrue(opened["shutdownAfterPrep"])
            self.assertFalse(batch_worker.batch_ready_for_shutdown(opened))
            for item in state["items"]:
                store.mutate_item(state["id"], item["id"], lambda row: row.update(status="confirmed"))
            ready = store.get(state["id"])
            self.assertTrue(batch_worker.batch_ready_for_shutdown(ready))
            self.assertEqual(batch_worker.shutdown_tick(), "scheduled")
            self.assertEqual(_SHUTDOWN_CALLS, [batch_worker.shutdown_delay_seconds()])

    def test_shutdown_after_prep_ignores_exception_items(self) -> None:
        from backend import batch_worker

        state = new_batch_state(["https://v.douyin.com/a", "https://v.douyin.com/b"], [], shutdown_after_prep=True)
        state["items"][0]["status"] = "confirmed"
        state["items"][1]["status"] = "failed"
        self.assertTrue(batch_worker.batch_ready_for_shutdown(state))
        state["items"][1]["status"] = "pending"
        self.assertFalse(batch_worker.batch_ready_for_shutdown(state))

    def test_shutdown_after_prep_counts_failed_and_historical_items_as_finished(self) -> None:
        from backend import batch_worker

        for first, second in (("confirmed", "failed"), ("completed", "failed"), ("failed", "failed"), ("failed", "skipped")):
            with self.subTest(statuses=(first, second)):
                state = new_batch_state(["https://v.douyin.com/a", "https://v.douyin.com/b"], [], shutdown_after_prep=True)
                state["items"][0].update(status=first, videoJobId="old-job")
                state["items"][1].update(status=second, videoJobId="old-failed-job")
                state["items"].append({"id": "deleted-history", "status": "deleted", "videoJobId": "old-deleted-job"})
                self.assertTrue(batch_worker.batch_ready_for_shutdown(state))
        for status in ("pending", "revising", "awaiting_review", "running", "unknown"):
            with self.subTest(blocking=status):
                state["items"][0]["status"] = status
                self.assertFalse(batch_worker.batch_ready_for_shutdown(state))
        for item in state["items"]:
            item["status"] = "deleted"
        self.assertFalse(batch_worker.batch_ready_for_shutdown(state))

    def test_shutdown_after_prep_with_failures_still_waits_for_active_work(self) -> None:
        from backend import batch_worker

        store = self._store()
        state = new_batch_state(["https://v.douyin.com/a", "https://v.douyin.com/b"], [], shutdown_after_prep=True)
        state["items"][0].update(status="confirmed", videoJobId="historical-job")
        state["items"][1].update(status="failed", videoJobId="historical-failed-job")
        store.create(state)
        with patch.object(batch_worker, "batch_store", store):
            with patch.object(batch_worker, "work_in_flight", return_value=True):
                self.assertEqual(batch_worker.shutdown_tick(), "waiting")
                self.assertEqual(_SHUTDOWN_CALLS, [])
            with patch.object(batch_worker, "work_in_flight", return_value=False):
                self.assertEqual(batch_worker.shutdown_tick(), "scheduled")
                self.assertEqual(_SHUTDOWN_CALLS, [batch_worker.shutdown_delay_seconds()])

    def test_batch_shutdown_waits_until_every_item_is_done(self) -> None:
        """只要有条目还在排队 / 等你确认 / 待出片 / 出片，就不许关机。"""
        from backend import batch_worker

        store = self._store()
        state = self._finished_state(store)
        last_id = state["items"][-1]["id"]
        with patch.object(batch_worker, "batch_store", store):
            for blocked in ("pending", "awaiting_review", "confirmed", "running", "revising"):
                store.mutate_item(
                    state["id"], last_id, lambda row, value=blocked: row.update(status=value)
                )
                self.assertEqual(batch_worker.shutdown_tick(), "waiting")
                self.assertFalse(batch_worker.shutdown_status()["pending"])
                self.assertEqual(_SHUTDOWN_CALLS, [])
            # 全部落在「已完成 / 已跳过 / 已失败」且至少有一条真的出完片 → 排关机
            store.mutate_item(
                state["id"], last_id, lambda row: row.update(status="completed")
            )
            self.assertEqual(batch_worker.shutdown_tick(), "scheduled")
            self.assertTrue(batch_worker.shutdown_status()["pending"])
            self.assertEqual(_SHUTDOWN_CALLS, [batch_worker.shutdown_delay_seconds()])
            # 已经排过就不再重复调用系统关机
            self.assertEqual(batch_worker.shutdown_tick(), "scheduled")
            self.assertEqual(len(_SHUTDOWN_CALLS), 1)
            # 页面上要能看到倒计时还剩多少秒与是哪个批次触发的
            status = batch_worker.shutdown_status()
            self.assertEqual(status["batchId"], state["id"])
            self.assertGreater(status["secondsLeft"], 0)
            self.assertEqual(
                store.get(state["id"])["notice"],
                f"全部条目已完成，{batch_worker.shutdown_delay_seconds()} 秒后自动关机；点「取消关机」可以撤销。",
            )

    def test_batch_shutdown_never_fires_on_a_batch_that_only_failed(self) -> None:
        """全是「失败 / 跳过」时不关机：那更像出了故障，用户还要回来处理。"""
        from backend import batch_worker

        store = self._store()
        state = new_batch_state(["https://v.douyin.com/a"], [], shutdown_on_complete=True)
        store.create(state)
        store.mutate_item(
            state["id"], state["items"][0]["id"], lambda row: row.update(status="failed")
        )
        with patch.object(batch_worker, "batch_store", store):
            self.assertEqual(batch_worker.shutdown_tick(), "waiting")
            self.assertFalse(batch_worker.shutdown_status()["pending"])
        self.assertEqual(_SHUTDOWN_CALLS, [])

    def test_batch_shutdown_is_revoked_when_new_work_appears(self) -> None:
        """倒计时期间又冒出新任务（追加链接 / 又确认了一条）→ 立刻撤销关机。"""
        from backend import batch_worker

        store = self._store()
        state = self._finished_state(store)
        store.mutate_item(
            state["id"], state["items"][-1]["id"], lambda row: row.update(status="completed")
        )
        with patch.object(batch_worker, "batch_store", store):
            self.assertEqual(batch_worker.shutdown_tick(), "scheduled")
            self.assertTrue(_SHUTDOWN_CALLS and batch_worker.shutdown_status()["pending"])

            def add_pending(row):
                row["items"] = list(row["items"]) + [
                    {
                        "id": "late-item",
                        "index": 3,
                        "kind": "dance",
                        "url": "https://v.douyin.com/late",
                        "status": "pending",
                        "stage": "queued",
                        "milestones": [],
                    }
                ]

            store.mutate(state["id"], add_pending)
            self.assertEqual(batch_worker.shutdown_tick(), "waiting")
            self.assertFalse(batch_worker.shutdown_status()["pending"])
            self.assertFalse(self.pending.exists())
            self.assertTrue(_ABORT_CALLS)

            # 用户自己点「取消关机」同样撤销
            store.mutate_item(state["id"], "late-item", lambda row: row.update(status="completed"))
            batch_worker.shutdown_tick()
            self.assertTrue(batch_worker.shutdown_status()["pending"])
            batch_worker.cancel_pending_shutdown(notice="已取消自动关机。")
            self.assertFalse(batch_worker.shutdown_status()["pending"])
            self.assertEqual(store.get(state["id"])["notice"], "已取消自动关机。")

    def test_batch_shutdown_does_not_fire_while_work_is_in_flight(self) -> None:
        """runner / 出片任务还在跑（哪怕条目状态看着已经收尾）也不许排关机。"""
        from backend import batch_worker

        store = self._store()
        state = self._finished_state(store)
        store.mutate_item(state["id"], state["items"][-1]["id"], lambda row: row.update(status="completed"))
        with patch.object(batch_worker, "batch_store", store), patch.object(
            batch_worker, "_RUNNING_BATCHES", {state["id"]}
        ):
            self.assertEqual(batch_worker.shutdown_tick(), "waiting")
            self.assertFalse(batch_worker.shutdown_status()["pending"])
        self.assertEqual(_SHUTDOWN_CALLS, [])

    def test_batch_shutdown_command_is_a_windows_countdown(self) -> None:
        """真正下发的命令必须是 Windows 的 `shutdown /s /t N`（用真实实现 + Popen 桩验证）。"""
        calls: list[list[str]] = []
        with patch.object(
            _batch_worker_module.subprocess, "Popen", lambda args, **kwargs: calls.append(args)
        ):
            self.assertTrue(_REAL_LAUNCH_SHUTDOWN(45))
        self.assertEqual(len(calls), 1)
        argv = calls[0]
        self.assertTrue(argv[0].lower().endswith("shutdown.exe") or argv[0].lower() == "shutdown")
        self.assertIn("/s", argv)
        self.assertEqual(argv[argv.index("/t") + 1], "45")

    def test_batch_shutdown_is_not_rearmed_after_a_reboot(self) -> None:
        """关过一次之后不能再关第二次：重启后发现早跑完的批次要把开关收掉。

        坑：开关留在已完成的批次上，服务重启时看护任务会立刻**再排一次关机**，
        用户第二天刚开机就又被关掉。`resume_shutdown_watch()` 必须只关开关、不排关机。
        """
        from backend import batch_worker

        store = self._store()
        state = self._finished_state(store)
        store.mutate_item(state["id"], state["items"][-1]["id"], lambda row: row.update(status="completed"))
        with patch.object(batch_worker, "batch_store", store):
            self.assertEqual(batch_worker.shutdown_tick(), "scheduled")
            # 到点后收记录（Windows 那边真的关了一次）
            self.pending.write_text(
                json.dumps({"batchId": state["id"], "executeAt": 1, "seconds": 60}),
                encoding="utf-8",
            )
            self.assertEqual(batch_worker.shutdown_tick(), "idle")
            self.assertFalse(self.pending.exists())
            self.assertFalse(store.get(state["id"])["shutdownOnComplete"])

            # 再来一次重启：早跑完的批次不会重新排关机（开关已经被收掉了）
            _SHUTDOWN_CALLS.clear()
            batch_worker.resume_shutdown_watch()
            self.assertEqual(batch_worker.shutdown_tick(), "idle")
            self.assertEqual(_SHUTDOWN_CALLS, [])

            # 开关还开着、批次还没跑完的：重启后继续等着，跑完才关
            store.update(state["id"], shutdownOnComplete=True)
            store.mutate_item(
                state["id"], state["items"][-1]["id"], lambda row: row.update(status="pending")
            )
            batch_worker.resume_shutdown_watch()
            self.assertEqual(batch_worker.shutdown_tick(), "waiting")
            self.assertEqual(_SHUTDOWN_CALLS, [])
            store.mutate_item(
                state["id"], state["items"][-1]["id"], lambda row: row.update(status="completed")
            )
            self.assertEqual(batch_worker.shutdown_tick(), "scheduled")
            self.assertEqual(len(_SHUTDOWN_CALLS), 1)

    def test_batch_shutdown_endpoints_and_ui_are_wired(self) -> None:
        from backend.app import app

        paths = {getattr(route, "path", "") for route in app.routes}
        self.assertIn("/api/batches/{batch_id}/shutdown-on-complete", paths)
        self.assertIn("/api/system/shutdown", paths)
        self.assertIn("/api/system/shutdown/cancel", paths)

        root = Path(__file__).parents[1]
        ui = (root / "src" / "BatchRoute.tsx").read_text(encoding="utf-8")
        # 开关默认关闭、文案说清什么时候才关、能取消
        self.assertIn("全部完成后自动关机", ui)
        self.assertIn("shutdownOn", ui)
        self.assertIn("shutdownOnComplete", ui)
        self.assertIn("shutdown-on-complete", ui)
        self.assertIn("/api/system/shutdown", ui)
        self.assertIn("取消关机", ui)
        worker = (root / "backend" / "batch_worker.py").read_text(encoding="utf-8")
        self.assertIn('"shutdownOnComplete": bool(shutdown_on_complete)', worker)


class SubtitleStepTests(unittest.TestCase):
    """唱歌条目自动加字幕（2026-09-16 用户确认的流程补充）。

    链路：剪映「识别歌词」拿时间轴（用户：「剪映的歌词识别 他的时间是最准的」）
    → 网易云官方歌词逐句核对**文字**（只改文字、不动时间轴）
    → ffmpeg 按剪映手书观感烧录 → `最终成片_字幕版.mp4`（与无字幕版并存）。

    这些用例**不需要剪映**：只覆盖纯 Python 部分（歌名提取、文字核对、ASS 生成、
    草稿克隆、批量挂钩）；真机识别在开发机上单独验过（26 秒出 11 句，时间轴与手工一致）。
    """

    root = Path(__file__).resolve().parent.parent

    @contextmanager
    def _temp_job_db(self, folder: Path):
        """把任务库换到临时目录。

        **换完必须 `_init_db()`**：`JobStore` 只在构造（模块导入）时建表，只改 `DB_PATH`
        拿到的是一个没有 jobs 表的空库（第一次 `create` 就 sqlite3.OperationalError）。
        临时目录用 `ignore_cleanup_errors`：sqlite 连接不会显式 close，Windows 上删不掉文件。
        """
        from backend import store as store_module

        original = store_module.DB_PATH
        store_module.DB_PATH = Path(folder) / "jobs.db"
        store_module.store._init_db()
        try:
            yield store_module.store
        finally:
            store_module.DB_PATH = original

    def test_song_name_falls_back_beyond_ai_song_name(self):
        """歌名优先 `ai.song_name`；为空时从标题/简介/源作品文案的《…》里提。"""
        from backend import subtitle_step

        self.assertEqual(subtitle_step.song_name_for({"ai": {"song_name": "有何不可"}}), "有何不可")
        self.assertEqual(
            subtitle_step.song_name_for({"ai": {"song_name": ""},
                                         "title": "把《有何不可》唱成一句轻声的叮嘱"}),
            "有何不可")
        self.assertEqual(
            subtitle_step.song_name_for({"ai": {"song_name": "未识别"},
                                         "sourceMetadata": {"desc": "才发现每一句都是叮嘱"}}),
            "")
        self.assertEqual(
            subtitle_step.song_name_for({"ai": {}, "sourceMetadata": {"desc": "翻唱《阴天》"}}),
            "阴天")

    def test_official_correction_fixes_text_and_keeps_timeline(self):
        """实测错句「果要是不那么开心」必须被官方歌词改成「如果有时不那么开心」，
        而且**时间轴一个字都不能动**（用户强调剪映的时间最准）。"""
        from backend import subtitle_step

        rows = [(0.97, 3.07, "夏天快要过去"),
                (10.30, 12.30, "果要是不那么开心"),
                (12.30, 14.47, "我愿意将格洛米借给你")]
        official = ["夏天快要过去", "如果有时不那么开心", "我愿意将格洛米借给你"]
        fixed, notes = subtitle_step.correct_with_official(rows, official)

        self.assertEqual([(a, b) for a, b, _t in fixed], [(a, b) for a, b, _t in rows],
                         "时间轴被改动了——剪映识别的时间必须原样保留")
        self.assertEqual(fixed[1][2], "如果有时不那么开心")
        self.assertTrue(any("如果有时不那么开心" in n for n in notes), f"应有核对备注：{notes}")

    def test_official_correction_keeps_jianying_line_breaks(self):
        """剪映会把官方一行拆成两段（如「为你唱这首歌 没有什么风格」）——
        这时不能拿官方整行覆盖，否则字幕会串行。"""
        from backend import subtitle_step

        rows = [(17.90, 20.17, "为你唱这首歌"), (20.30, 22.57, "没有什么风格")]
        official = ["为你唱这首歌 没有什么风格"]
        fixed, _notes = subtitle_step.correct_with_official(rows, official)

        self.assertEqual([t for _a, _b, t in fixed], ["为你唱这首歌", "没有什么风格"])

    def test_ass_uses_jianying_style_and_two_digit_centiseconds(self):
        """ASS 必须是剪映手书 + 标定参数，时间格式必须是 h:mm:ss.cc（两位百分秒）。"""
        from backend import jianying_style

        ass = jianying_style.build_ass([(0.97, 3.07, "夏天快要过去")])
        self.assertIn(jianying_style.FONT_NAME, ass)
        self.assertIn(f",{jianying_style.STYLE['size']},", ass)
        self.assertIn(f",{jianying_style.STYLE['margin_v']},1", ass)
        self.assertIn("Dialogue: 0,0:00:00.97,0:00:03.07,JY,,0,0,0,,夏天快要过去", ass)

    def test_clone_draft_patches_video_and_drops_old_subtitles(self):
        """克隆草稿必须把视频素材指向本条成片、时长/画布对齐，并清掉上次的字幕
        （否则「识别歌词」会追加到旧结果后面）。"""
        import tempfile

        from backend import jianying_recognize as jr

        self.assertTrue(jr.TEMPLATE.is_dir(), f"缺少草稿模板：{jr.TEMPLATE}")
        with tempfile.TemporaryDirectory() as tmp:
            fake_draft_root = Path(tmp) / "drafts"
            fake_draft_root.mkdir()
            video = Path(tmp) / "最终成片.mp4"
            video.write_bytes(b"x")
            with unittest.mock.patch.object(jr, "DRAFT_ROOT", fake_draft_root), \
                    unittest.mock.patch.object(jr, "_probe", return_value=(1440, 1080, 27133333)):
                draft = jr.clone_draft("H3字幕自动", video)
                self.assertEqual(draft.name, "H3字幕自动")
                content = json.loads((draft / "draft_content.json").read_text(encoding="utf-8"))
            self.assertEqual(content["duration"], 27133333)
            self.assertEqual(content["canvas_config"],
                             {"width": 1920, "height": 1440, "ratio": "4:3"})
            self.assertEqual(content["materials"]["texts"], [])
            self.assertEqual([t["type"] for t in content["tracks"]], ["video"])
            video_material = content["materials"]["videos"][0]
            self.assertEqual(video_material["duration"], 27133333)
            self.assertTrue(video_material["path"].endswith("最终成片.mp4"))

    def test_batch_no_longer_adds_subtitles_automatically(self):
        """用户 2026-09-16：「现在的生成完成唱歌视频之后自动运行的字幕生成有问你不要自动生成了」。

        批量出片后**不许再自动跑字幕**：字幕只在独立路由 `/subtitle` 上手动跑（用户自己挑成片）。
        `_add_subtitles_if_singing` 与 `subtitle_step.add_for_item` 保留（便于以后恢复自动链路），
        但 `_process_confirmed` 里不能再出现那次调用。
        """
        worker = (self.root / "backend" / "batch_worker.py").read_text(encoding="utf-8")
        self.assertIn("async def _add_subtitles_if_singing", worker, "实现保留，便于以后恢复")
        confirmed = worker[worker.index("async def _process_confirmed"):]
        confirmed = confirmed[: confirmed.index("\nasync def ")] if "\nasync def " in confirmed else confirmed
        self.assertNotIn("await _add_subtitles_if_singing", confirmed, "出片后不再自动加字幕")

        step = (self.root / "backend" / "subtitle_step.py").read_text(encoding="utf-8")
        self.assertIn("最终成片_字幕版.mp4", step)
        self.assertIn("warning=", step, "保留的那条链路失败仍要标黄")

    def test_subtitles_run_for_singing_and_never_touch_dance(self):
        """用户 2026-09-16：「只有歌曲要 跳舞不要」。

        行为级验证（不是源码断言）：
          · 跳舞条目 → 直接返回 None，**一次都不去调剪映识别**；
          · 唱歌条目 → 识别 → 官方歌词核对 → 烧录 全链路真的跑起来，产物是 `最终成片_字幕版.mp4`。
        剪映与 ffmpeg 都 mock 掉，所以这条用例不需要真机、也不联网。
        """
        import asyncio
        import tempfile

        from backend import jianying_recognize, jianying_style, subtitle_step

        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "最终成片.mp4"
            video.write_bytes(b"x")
            dance = {"id": "i-dance", "kind": "dance", "ai": {"song_name": "某首歌"}, "outputs": {}}
            singing = {"id": "i-sing", "kind": "singing", "ai": {"song_name": "有何不可"},
                       "outputs": {"videoFinal": str(video)}}

            # ① 跳舞条目：必须完全不碰剪映
            recognise = unittest.mock.Mock(side_effect=AssertionError("跳舞条目不该调用剪映识别"))
            with unittest.mock.patch.object(subtitle_step.batch_store, "get",
                                            return_value={"items": [dance]}), \
                    unittest.mock.patch.object(jianying_recognize, "recognize_video", recognise), \
                    unittest.mock.patch.object(subtitle_step.batch_store, "add_item_log",
                                               lambda *a, **k: None):
                self.assertIsNone(asyncio.run(subtitle_step.add_for_item("b1", "i-dance")))
            recognise.assert_not_called()
            self.assertFalse((video.parent / "最终成片_字幕版.mp4").exists())

            # ② 唱歌条目：全链路（剪映识别 → 官方歌词核对 → 烧录）
            rows = [(0.97, 3.07, "夏天快要过去"), (10.30, 12.30, "果要是不那么开心")]
            burned: dict = {}

            async def fake_official(_rows, _song="", **_kw):
                return ["夏天快要过去", "如果有时不那么开心"], "测试来源", \
                    subtitle_step._MATCH_MIN_RATIO

            def fake_burn(_video, out, lines, **_kw):
                burned["lines"] = list(lines)
                Path(out).write_bytes(b"out")
                return Path(out)

            logs: list = []
            with unittest.mock.patch.object(subtitle_step.batch_store, "get",
                                            return_value={"items": [singing]}), \
                    unittest.mock.patch.object(jianying_recognize, "recognize_video",
                                               return_value=rows), \
                    unittest.mock.patch.object(subtitle_step, "official_lyrics_for", fake_official), \
                    unittest.mock.patch.object(jianying_style, "burn", fake_burn), \
                    unittest.mock.patch.object(subtitle_step.batch_store, "add_item_log",
                                               lambda *a, **k: logs.append(a)), \
                    unittest.mock.patch.object(subtitle_step.batch_store, "mutate_item",
                                               lambda *a, **k: None):
                out = asyncio.run(subtitle_step.add_for_item("b2", "i-sing"))

            self.assertIsNotNone(out)
            self.assertEqual(Path(out).name, "最终成片_字幕版.mp4")
            # 文字被官方歌词修正，时间轴保持剪映的
            self.assertEqual([t for _a, _b, t in burned["lines"]],
                             ["夏天快要过去", "如果有时不那么开心"])
            self.assertEqual([(a, b) for a, b, _t in burned["lines"]], [(0.97, 3.07), (10.30, 12.30)])
            self.assertTrue(any("字幕版已生成" in str(entry) for entry in logs), logs)

    def test_subtitles_failure_only_warns_and_never_raises(self):
        """加字幕失败必须只记日志 + 标黄，绝不把异常抛给批量 runner（否则整批会被一条字幕拖停）。"""
        import asyncio
        import tempfile

        from backend import jianying_recognize, subtitle_step

        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "最终成片.mp4"
            video.write_bytes(b"x")
            item = {"id": "i1", "kind": "singing", "ai": {"song_name": "有何不可"},
                    "outputs": {"videoFinal": str(video)}}
            logs: list = []
            warnings: list = []
            with unittest.mock.patch.object(subtitle_step.batch_store, "get",
                                            return_value={"items": [item]}), \
                    unittest.mock.patch.object(jianying_recognize, "recognize_video",
                                               side_effect=RuntimeError("剪映没打开")), \
                    unittest.mock.patch.object(subtitle_step.batch_store, "add_item_log",
                                               lambda *a, **k: logs.append(a)), \
                    unittest.mock.patch.object(
                        subtitle_step.batch_store, "mutate_item",
                        lambda _b, _i, fn, **_k: (warnings.append(fn), fn({}))):
                result = asyncio.run(subtitle_step.add_for_item("b", "i1"))

        self.assertIsNone(result, "失败必须返回 None 而不是抛异常")
        self.assertTrue(any("字幕版生成失败" in str(entry) for entry in logs), logs)
        self.assertTrue(warnings, "失败要标黄（写 warning）")

    def test_subtitles_still_burn_when_the_item_has_no_song_name(self):
        """实测条目 #4《这首民谣》的 `ai.song_name` 是空的、标题里也没有《歌名》——
        这时**不该整条跳过**加字幕：先靠"用歌词搜歌"兜底，两路都没定位到才直接用识别结果。"""
        import asyncio
        import tempfile

        from backend import jianying_recognize, jianying_style, subtitle_step

        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "最终成片.mp4"
            video.write_bytes(b"x")
            item = {"id": "i4", "kind": "singing", "ai": {"song_name": ""},
                    "title": "这首民谣，唱给回不去的乡愁",
                    "sourceMetadata": {"desc": "扒苞米了，该回家了"}, "outputs": {"videoFinal": str(video)}}
            rows = [(1.0, 3.0, "是谁把牵挂埋在了土里"), (4.0, 6.0, "那是一座座山连着山的小村庄")]
            burned: dict = {}

            async def no_official(_rows, _song=None, **_kw):
                return [], "两路都没找到", 0.0

            def fake_burn(_video, out, lines, **_kw):
                burned["lines"] = list(lines)
                Path(out).write_bytes(b"out")
                return Path(out)

            with unittest.mock.patch.object(subtitle_step.batch_store, "get",
                                            return_value={"items": [item]}), \
                    unittest.mock.patch.object(jianying_recognize, "recognize_video",
                                               return_value=rows), \
                    unittest.mock.patch.object(subtitle_step, "official_lyrics_for", no_official), \
                    unittest.mock.patch.object(jianying_style, "burn", fake_burn), \
                    unittest.mock.patch.object(subtitle_step.batch_store, "add_item_log",
                                               lambda *a, **k: None), \
                    unittest.mock.patch.object(subtitle_step.batch_store, "mutate_item",
                                               lambda *a, **k: None):
                out = asyncio.run(subtitle_step.add_for_item("b", "i4"))

            self.assertIsNotNone(out, "没歌名也要产出字幕版")
            self.assertEqual(burned["lines"], rows, "定位不到官方歌词时直接用剪映识别结果")

    def test_subtitle_route_is_registered_with_the_same_three_steps(self):
        """独立「生成字幕」路由：接口注册 + 三格里程碑 = 批量那条链路的三个阶段。

        用户 2026-09-16 要的是「把昨天那套自动化生成字幕流程做成一个路由」，
        所以链路必须复用 `subtitle_step`（剪映识别时间轴 → 官方歌词只改文字 → 剪映手书烧录），
        不能另起一套。
        """
        from backend.app import app
        from backend.store import subtitle_milestones

        self.assertEqual([m["id"] for m in subtitle_milestones()], ["recognize", "verify", "burn"])
        self.assertIn("/api/jobs/subtitle", {getattr(route, "path", "") for route in app.routes})

    def test_subtitle_job_runs_the_chain_and_pushes_real_progress(self):
        """`run_subtitle_job` = 剪映识别 → 官方歌词核对 → 烧录，三格里程碑真的走完。

        用假的 `burn_for_video`：只验任务系统这一层（里程碑推进、日志、finalOutput、句数），
        真机识别在开发机上单独验过。
        """
        import asyncio
        import tempfile

        from backend import store as store_module
        from backend import subtitle_step

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            source = Path(folder) / "最终成片.mp4"
            source.write_bytes(b"src")
            out = Path(folder) / "字幕版.mp4"
            with self._temp_job_db(Path(folder)) as job_store:
                stages: list[str] = []

                async def fake_burn(_video, _song, target, *, on_stage=None, stats=None, **_kw):
                    self.assertEqual(Path(_video), source)
                    self.assertEqual(_song, "有何不可")
                    for milestone_id in ("recognize", "verify", "burn"):
                        stages.append(milestone_id)
                        if on_stage:
                            on_stage(milestone_id, f"进入 {milestone_id}")
                    Path(target).write_bytes(b"sub")
                    if stats is not None:
                        stats.update({"lines": 11, "lyricSource": "歌名搜索《有何不可》- 许嵩",
                                      "minRatio": 0.6, "corrected": 1})
                    return Path(target), ["官方歌词来源：歌名搜索《有何不可》- 许嵩；修正门槛 0.60"]

                async def fake_metadata(_path):
                    return {"width": 1440, "height": 1080, "duration": 30.0, "size": 1024}

                created = now_iso()
                job_store.create({
                    "id": "subtitle-job", "kind": "subtitle", "status": "queued", "stage": "upload",
                    "createdAt": created, "updatedAt": created,
                    "subtitleSource": str(source), "subtitleOutPath": str(out),
                    "songName": "有何不可", "milestones": store_module.subtitle_milestones(), "logs": [],
                })
                close_mock = unittest.mock.Mock(return_value=True)
                with unittest.mock.patch.object(subtitle_step, "burn_for_video", fake_burn), \
                        unittest.mock.patch.object(subtitle_step, "media_metadata", fake_metadata), \
                        unittest.mock.patch.object(subtitle_step, "close_jianying_quietly", close_mock):
                    asyncio.run(subtitle_step.run_subtitle_job("subtitle-job"))

                state = job_store.get("subtitle-job")
                self.assertEqual(stages, ["recognize", "verify", "burn"], "三个阶段必须依次上报")
                self.assertEqual(state["status"], "completed")
                self.assertTrue(state["finalReady"])
                self.assertEqual(Path(state["finalOutput"]), out)
                self.assertEqual([m["status"] for m in state["milestones"]], ["completed"] * 3)
                self.assertEqual(state["subtitleLineCount"], 11)
                self.assertEqual(state["subtitleCorrected"], 1)
                messages = [entry["message"] for entry in state["logs"]]
                self.assertTrue(any("字幕版成片已生成" in m for m in messages), messages)
                self.assertTrue(any("文字核对" in m for m in messages), messages)
                self.assertTrue(close_mock.called, "跑完必须关掉剪映（会和 ComfyUI 抢显存）")

    def test_subtitle_job_failure_is_reported_and_closes_jianying(self):
        """独立路由失败就是失败（批量里才降级标黄），并且**失败/取消也要关掉剪映**。"""
        import asyncio
        import tempfile

        from backend import store as store_module
        from backend import subtitle_step

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            source = Path(folder) / "最终成片.mp4"
            source.write_bytes(b"src")
            with self._temp_job_db(Path(folder)) as job_store:
                async def failing_burn(_video, _song, _target, *, on_stage=None, **_kw):
                    if on_stage:
                        on_stage("recognize", "正在用剪映识别歌词……")
                        on_stage("verify", "识别出 3 句，正在核对官方歌词……")
                    raise RuntimeError("剪映识别歌词超时（草稿里没有出现字幕）")


                created = now_iso()
                job_store.create({
                    "id": "subtitle-fail", "kind": "subtitle", "status": "queued", "stage": "upload",
                    "createdAt": created, "updatedAt": created,
                    "subtitleSource": str(source), "subtitleOutPath": str(Path(folder) / "字幕版.mp4"),
                    "songName": "", "milestones": store_module.subtitle_milestones(), "logs": [],
                    "finalReady": False, "finalOutput": None,
                })
                close_mock = unittest.mock.Mock(return_value=True)
                with unittest.mock.patch.object(subtitle_step, "burn_for_video", failing_burn), \
                        unittest.mock.patch.object(subtitle_step, "close_jianying_quietly", close_mock):
                    asyncio.run(subtitle_step.run_subtitle_job("subtitle-fail"))

                state = job_store.get("subtitle-fail")
                self.assertEqual(state["status"], "failed")
                self.assertFalse(state["finalReady"])
                self.assertIn("剪映识别歌词超时", state["errorSummary"] + state["errorDetail"])
                statuses = {m["id"]: m["status"] for m in state["milestones"]}
                self.assertEqual(statuses["recognize"], "completed")
                self.assertEqual(statuses["verify"], "error", "失败要落在真正卡住的那一格")
                self.assertEqual(statuses["burn"], "pending")
                self.assertTrue(close_mock.called, "失败也必须关掉剪映")

    def test_subtitle_job_stops_before_burning_when_cancelled(self):
        """剪映识别要跑几十秒：中途用户点了「取消」，就不能再往下烧字幕。

        （取消发生在识别那一格，阶段回调在进入「官方歌词核对」前就该抛出来。）
        """
        import asyncio
        import tempfile

        from backend import store as store_module
        from backend import subtitle_step

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            source = Path(folder) / "最终成片.mp4"
            source.write_bytes(b"src")
            out = Path(folder) / "字幕版.mp4"
            with self._temp_job_db(Path(folder)) as job_store:
                burned: list[str] = []

                async def burn_until_cancelled(_video, _song, target, *, on_stage=None, **_kw):
                    if on_stage:
                        on_stage("recognize", "正在用剪映识别歌词……")
                    job_store.update("subtitle-cancel", status="cancelled")   # 用户在识别期间点了取消
                    if on_stage:
                        on_stage("verify", "识别出 3 句……")
                    burned.append(str(target))          # 不该走到这里
                    return Path(target), []

                created = now_iso()
                job_store.create({
                    "id": "subtitle-cancel", "kind": "subtitle", "status": "queued", "stage": "upload",
                    "createdAt": created, "updatedAt": created,
                    "subtitleSource": str(source), "subtitleOutPath": str(out),
                    "songName": "", "milestones": store_module.subtitle_milestones(), "logs": [],
                    "finalReady": False, "finalOutput": None,
                })
                close_mock = unittest.mock.Mock(return_value=True)
                with unittest.mock.patch.object(subtitle_step, "burn_for_video", burn_until_cancelled), \
                        unittest.mock.patch.object(subtitle_step, "close_jianying_quietly", close_mock):
                    asyncio.run(subtitle_step.run_subtitle_job("subtitle-cancel"))

                state = job_store.get("subtitle-cancel")
                self.assertEqual(burned, [], "取消后不该再继续烧字幕")
                self.assertFalse(state["finalReady"])
                statuses = {m["id"]: m["status"] for m in state["milestones"]}
                self.assertEqual(statuses["recognize"], "skipped", "取消时运行中的那一格要标跳过")
                self.assertEqual(statuses["verify"], "pending")
                self.assertEqual(statuses["burn"], "pending")
                self.assertTrue(close_mock.called, "取消也要关掉剪映")

    def test_clone_draft_rebuilds_a_draft_that_jianying_emptied(self):
        """草稿被剪映回存成**空项目**后必须还能补回视频（2026-09-16 用户：「剪映里的文件是错误的」）。

        实测现场：`H3字幕自动` 被剪映存成 3257 字节的空项目（`duration: 0`、`materials.videos: []`、
        `tracks: []`），而模板是 8067 字节、带一条视频素材 + 一条视频轨。旧实现「就地改已有草稿」
        只更新*已存在*的素材与轨道，两者都没了就永远补不回来 → 剪映里是一条空时间线，
        「识别歌词」自然一行都出不来。现在骨架每次都从模板重建，写回**同一个草稿目录**。
        """
        import tempfile

        from backend import jianying_recognize as recognize

        with tempfile.TemporaryDirectory() as folder:
            drafts = Path(folder) / "drafts"
            draft = drafts / recognize.DEFAULT_DRAFT_NAME
            draft.mkdir(parents=True)
            # 剪映回存的空项目：没有素材、没有轨道、duration 0（真实现场的字段就长这样）
            (draft / "draft_content.json").write_text(json.dumps({
                "id": "06E9407A-2D8C-4711-B8D7-B614C7A56804",
                "duration": 0,
                "canvas_config": {"width": 1920, "height": 1440, "ratio": "4:3"},
                "materials": {"texts": [], "videos": []},
                "tracks": [],
            }, ensure_ascii=False), encoding="utf-8")
            # 草稿目录里原有的其它文件不能被删（删目录会让剪映弹「草稿列表异常」）
            (draft / "keep-me.txt").write_text("keep", encoding="utf-8")

            video = Path(folder) / "source.mp4"
            video.write_bytes(b"x")
            with unittest.mock.patch.object(recognize, "DRAFT_ROOT", drafts), \
                    unittest.mock.patch.object(recognize, "_probe", lambda _v: (1440, 1080, 23_166_667)):
                recognize.clone_draft(recognize.DEFAULT_DRAFT_NAME, video)

            content = json.loads((draft / "draft_content.json").read_text(encoding="utf-8"))
            videos = content["materials"]["videos"]
            tracks = [t for t in content["tracks"] if t.get("type") == "video"]
            self.assertEqual(len(videos), 1, "空草稿必须靠模板补回视频素材")
            self.assertEqual(videos[0]["path"], str(video).replace("\\", "/"))
            self.assertEqual(content["duration"], 23_166_667)
            self.assertEqual(len(tracks), 1)
            self.assertEqual(len(tracks[0]["segments"]), 1)
            self.assertEqual(tracks[0]["segments"][0]["material_id"], videos[0]["id"])
            self.assertEqual(tracks[0]["segments"][0]["target_timerange"]["duration"], 23_166_667)
            self.assertTrue((draft / "keep-me.txt").is_file(), "草稿目录里的文件一个都不许删")
            self.assertEqual(content["materials"]["texts"], [], "旧字幕必须先清掉")

    def test_clone_draft_refuses_a_template_without_video(self):
        """模板缺视频素材/轨道时当场报错，绝不生成一条空时间线让剪映里看起来「文件是错的」。"""
        import tempfile

        from backend import jianying_recognize as recognize

        with tempfile.TemporaryDirectory() as folder:
            template = Path(folder) / "template"
            template.mkdir()
            (template / "draft_content.json").write_text(json.dumps({
                "id": "tpl", "materials": {"videos": []}, "tracks": [],
            }), encoding="utf-8")
            with unittest.mock.patch.object(recognize, "TEMPLATE", template):
                with self.assertRaises(RuntimeError) as ctx:
                    recognize.template_skeleton()
            self.assertIn("模板", str(ctx.exception))

    def test_retry_endpoint_resets_a_failed_job_and_reuses_the_source(self):
        """「重新开始」：源成片还在任务目录里，所以只重置 + 重排，不用重新上传。"""
        import asyncio
        import tempfile

        from backend import app as app_module
        from backend import store as store_module

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            source = Path(folder) / "source.mp4"
            source.write_bytes(b"src")
            with self._temp_job_db(Path(folder)) as job_store:
                created = now_iso()
                job_store.create({
                    "id": "sub-retry", "kind": "subtitle", "status": "failed", "stage": "failed",
                    "createdAt": created, "updatedAt": created,
                    "subtitleSource": str(source), "subtitleOutPath": str(Path(folder) / "out.mp4"),
                    "milestones": store_module.subtitle_milestones(), "logs": [],
                    "finalReady": False, "finalOutput": None,
                    "errorSummary": "生成字幕失败", "errorDetail": "剪映识别歌词超时",
                })
                spawned: list[str] = []

                def fake_spawn(coro):
                    coro.close()
                    spawned.append("spawned")

                with unittest.mock.patch.object(app_module, "spawn", fake_spawn):
                    state = asyncio.run(app_module.retry_subtitle_job("sub-retry"))

                self.assertEqual(spawned, ["spawned"], "重新开始要真的把任务排回去")
                self.assertEqual(state["status"], "queued")
                self.assertIsNone(state["errorSummary"])
                self.assertIsNone(state["errorDetail"])
                self.assertEqual([m["status"] for m in state["milestones"]], ["pending"] * 3)
                self.assertFalse(state["finalReady"])
                self.assertFalse(state.get("restartRequested"))
                self.assertTrue(any("重新开始" in entry["message"] for entry in state["logs"]))

    def test_retry_endpoint_kills_jianying_when_the_run_is_still_going(self):
        """正在跑（或卡在剪映调用里）的那一次：打标记 + 强杀剪映，**不**当场重置。

        当场重置会被旧任务的失败分支覆盖（它发现 status 已不是 cancelling，就把这次识别失败
        当真实失败写回去，还会把新一轮正在跑的那一格标成 error）。
        """
        import asyncio
        import tempfile

        from backend import app as app_module
        from backend import store as store_module

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            source = Path(folder) / "source.mp4"
            source.write_bytes(b"src")
            with self._temp_job_db(Path(folder)) as job_store:
                created = now_iso()
                job_store.create({
                    "id": "sub-running", "kind": "subtitle", "status": "running", "stage": "subtitle",
                    "createdAt": created, "updatedAt": created,
                    "subtitleSource": str(source), "subtitleOutPath": str(Path(folder) / "out.mp4"),
                    "milestones": store_module.subtitle_milestones(), "logs": [],
                    "finalReady": False, "finalOutput": None,
                })
                spawned: list[str] = []

                def fake_spawn(coro):
                    coro.close()
                    spawned.append("spawned")

                kill_mock = unittest.mock.Mock(return_value=None)
                with unittest.mock.patch.object(app_module, "kill_app", kill_mock), \
                        unittest.mock.patch.object(app_module, "spawn", fake_spawn):
                    state = asyncio.run(app_module.retry_subtitle_job("sub-running"))

                self.assertTrue(kill_mock.called, "卡住时必须强杀剪映，让阻塞的调用立刻返回")
                self.assertTrue(state["restartRequested"], "标记交给当前这一轮自己收尾后重排")
                self.assertEqual(state["status"], "running", "还没收尾，不能提前重置")
                self.assertEqual(spawned, [])

    def test_the_running_job_restarts_itself_only_after_it_finishes(self):
        """打了标记的那一轮收尾时自己重排：状态先回到 queued，再由 `_spawn_restart` 排下一轮。"""
        import asyncio
        import tempfile

        from backend import store as store_module
        from backend import subtitle_step

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            source = Path(folder) / "最终成片.mp4"
            source.write_bytes(b"src")
            with self._temp_job_db(Path(folder)) as job_store:
                async def failing_burn(_video, _song, _target, *, on_stage=None, **_kw):
                    if on_stage:
                        on_stage("recognize", "正在用剪映识别歌词……")
                    raise RuntimeError("剪映在识别过程中被关闭了：草稿里没有出现任何字幕行")

                created = now_iso()
                job_store.create({
                    "id": "sub-self-restart", "kind": "subtitle", "status": "running",
                    "stage": "subtitle", "createdAt": created, "updatedAt": created,
                    "subtitleSource": str(source), "subtitleOutPath": str(Path(folder) / "字幕版.mp4"),
                    "songName": "", "milestones": store_module.subtitle_milestones(), "logs": [],
                    "finalReady": False, "finalOutput": None, "restartRequested": True,
                })
                restarted: list[str] = []
                with unittest.mock.patch.object(subtitle_step, "burn_for_video", failing_burn), \
                        unittest.mock.patch.object(subtitle_step, "close_jianying_quietly",
                                                   unittest.mock.Mock(return_value=True)), \
                        unittest.mock.patch.object(subtitle_step, "_spawn_restart",
                                                   lambda job_id: restarted.append(job_id)):
                    asyncio.run(subtitle_step.run_subtitle_job("sub-self-restart"))

                state = job_store.get("sub-self-restart")
                self.assertEqual(restarted, ["sub-self-restart"], "收尾后要自己重排一轮")
                self.assertEqual(state["status"], "queued")
                self.assertEqual([m["status"] for m in state["milestones"]], ["pending"] * 3)
                self.assertIsNone(state["errorSummary"])
                self.assertFalse(state["restartRequested"])

    def test_subtitle_output_lands_in_the_matching_publish_folder(self):
        """生成完成放进「对应的发布目录」（用户 2026-09-16）。

        源成片是某条批量作品出的片 → 条目里记着 `videoJobId`，就放进**那一条自己的**
        发布目录，文件名与批量链路一致：`最终成片_字幕版.mp4`（与无字幕的 `最终成片.mp4` 并存）。
        """
        import tempfile

        from backend import batch_worker

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "发布成品"
            root.mkdir()
            # 这条作品之前交付过：目录里已经有最终成片/人物图/发布文案
            publish = root / "002_好想跟你表白_7653059686421996986"
            publish.mkdir()
            (publish / "最终成片.mp4").write_bytes(b"clean")
            (publish / "人物图.png").write_bytes(b"img")

            subtitle = Path(folder) / "字幕版.mp4"
            subtitle.write_bytes(b"subtitled")
            item = {
                "id": "item-2", "index": 2, "kind": "singing", "awemeId": "7653059686421996986",
                "videoJobId": "job-source", "ai": {"song_name": "好想跟你表白"},
                "outputs": {"folder": str(publish)},
            }

            class _Store:
                def item_by_video_job(self, job_id):
                    return {**item, "_batchId": "batch-1"} if job_id == "job-source" else None

            with unittest.mock.patch.object(batch_worker, "BATCH_OUTPUT_ROOT", root), \
                    unittest.mock.patch.object(batch_worker, "batch_store", _Store()):
                out = batch_worker.deliver_subtitle_output(
                    "sub-job", subtitle, source_job_id="job-source",
                    song="好想跟你表白", source_name="f091d897 final")

            self.assertEqual(out, publish / "最终成片_字幕版.mp4")
            self.assertTrue(out.is_file())
            self.assertEqual(out.read_bytes(), b"subtitled")
            # 无字幕成片与其它发布文件原样保留（只加一件，不动别的）
            self.assertTrue((publish / "最终成片.mp4").is_file())
            self.assertTrue((publish / "人物图.png").is_file())
            # **不许顺手改名**：目录名还是原来那个（条目删过之后 index 变了，理想名会不一样）
            self.assertTrue(publish.is_dir())
            self.assertEqual(sorted(p.name for p in root.iterdir()), [publish.name])

    def test_subtitle_output_for_an_uploaded_video_gets_its_own_folder(self):
        """用户本地上传的视频没有对应作品 → 在发布根下按 `字幕_{歌名或文件名}_{job短号}` 建一个。"""
        import tempfile

        from backend import batch_worker

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "发布成品"
            subtitle = Path(folder) / "字幕版.mp4"
            subtitle.write_bytes(b"x")

            class _Store:
                def item_by_video_job(self, _job_id):
                    return None

            with unittest.mock.patch.object(batch_worker, "BATCH_OUTPUT_ROOT", root), \
                    unittest.mock.patch.object(batch_worker, "batch_store", _Store()):
                out = batch_worker.deliver_subtitle_output(
                    "abcdef1234567890", subtitle, source_name="我的演唱视频.mp4")

            # 2026-09-20 起字幕目录落在 `singing/` 子目录里（字幕只服务唱歌）
            self.assertEqual(
                out, root / "singing" / "字幕_我的演唱视频_abcdef12" / "最终成片_字幕版.mp4"
            )
            self.assertTrue(out.is_file())

    def test_subtitle_job_records_where_it_published(self):
        """跑完要把「放进发布目录」的结果写进任务状态，页面才显示得出目录与打开按钮。"""
        import asyncio
        import tempfile

        from backend import store as store_module
        from backend import subtitle_step

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            source = Path(folder) / "source.mp4"
            source.write_bytes(b"src")
            out = Path(folder) / "字幕版.mp4"
            published = Path(folder) / "发布成品" / "002_歌名_7653" / "最终成片_字幕版.mp4"
            with self._temp_job_db(Path(folder)) as job_store:
                async def fake_burn(_video, _song, target, *, on_stage=None, stats=None, **_kw):
                    Path(target).write_bytes(b"sub")
                    if stats is not None:
                        stats.update({"lines": 6, "lyricSource": "歌名搜索", "corrected": 0})
                    return Path(target), []

                async def fake_metadata(_path):
                    return {"width": 1440, "height": 1080}

                def fake_deliver(_job_id, subtitle, **_kw):
                    published.parent.mkdir(parents=True, exist_ok=True)
                    published.write_bytes(Path(subtitle).read_bytes())
                    return published

                created = now_iso()
                job_store.create({
                    "id": "sub-publish", "kind": "subtitle", "status": "queued", "stage": "upload",
                    "createdAt": created, "updatedAt": created,
                    "subtitleSource": str(source), "subtitleOutPath": str(out),
                    "sourceJobId": "f091d897", "sourceName": "f091d897 final", "songName": "有何不可",
                    "milestones": store_module.subtitle_milestones(), "logs": [],
                    "finalReady": False, "finalOutput": None,
                })
                with unittest.mock.patch.object(subtitle_step, "burn_for_video", fake_burn), \
                        unittest.mock.patch.object(subtitle_step, "media_metadata", fake_metadata), \
                        unittest.mock.patch.object(subtitle_step, "deliver_subtitle_output", fake_deliver), \
                        unittest.mock.patch.object(subtitle_step, "close_jianying_quietly",
                                                   unittest.mock.Mock(return_value=True)):
                    asyncio.run(subtitle_step.run_subtitle_job("sub-publish"))

                state = job_store.get("sub-publish")
                self.assertEqual(state["status"], "completed")
                self.assertEqual(state["subtitlePublishPath"], str(published))
                self.assertIsNone(state["subtitlePublishError"])
                messages = [entry["message"] for entry in state["logs"]]
                self.assertTrue(any("已放进发布目录" in m for m in messages), messages)

    def test_publish_failure_never_fails_the_subtitle_job(self):
        """拷进发布目录失败只是降级（字幕版还在任务目录里能下载），不能把任务判失败。"""
        import asyncio
        import tempfile

        from backend import store as store_module
        from backend import subtitle_step

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            source = Path(folder) / "source.mp4"
            source.write_bytes(b"src")
            with self._temp_job_db(Path(folder)) as job_store:
                async def fake_burn(_video, _song, target, *, on_stage=None, stats=None, **_kw):
                    Path(target).write_bytes(b"sub")
                    if stats is not None:
                        stats.update({"lines": 6, "lyricSource": "", "corrected": 0})
                    return Path(target), []

                async def fake_metadata(_path):
                    return {"width": 1440, "height": 1080}

                def failing_deliver(*_a, **_kw):
                    raise PermissionError("发布目录被占用")

                created = now_iso()
                job_store.create({
                    "id": "sub-publish-fail", "kind": "subtitle", "status": "queued", "stage": "upload",
                    "createdAt": created, "updatedAt": created,
                    "subtitleSource": str(source), "subtitleOutPath": str(Path(folder) / "字幕版.mp4"),
                    "songName": "", "milestones": store_module.subtitle_milestones(), "logs": [],
                    "finalReady": False, "finalOutput": None,
                })
                with unittest.mock.patch.object(subtitle_step, "burn_for_video", fake_burn), \
                        unittest.mock.patch.object(subtitle_step, "media_metadata", fake_metadata), \
                        unittest.mock.patch.object(subtitle_step, "deliver_subtitle_output", failing_deliver), \
                        unittest.mock.patch.object(subtitle_step, "close_jianying_quietly",
                                                   unittest.mock.Mock(return_value=True)):
                    asyncio.run(subtitle_step.run_subtitle_job("sub-publish-fail"))

                state = job_store.get("sub-publish-fail")
                self.assertEqual(state["status"], "completed", "拷贝失败不能把任务判失败")
                self.assertTrue(state["finalReady"])
                self.assertIsNone(state["subtitlePublishPath"])
                self.assertIn("发布目录被占用", state["subtitlePublishError"])

    def test_cancel_works_while_the_batch_is_paused(self):
        """暂停中的「一键批量加字幕」必须能取消。

        2026-09-19 用户实测反馈「取消任务点击不了吗」——`cancel_job` 只认
        queued/running/cancelling，暂停态直接落到那句 409「任务已结束，无法取消」，
        于是暂停的批量永远取消不掉。暂停态没有任何东西在跑，直接收尾即可，
        也不能去碰剪映/ComfyUI。
        """
        import asyncio
        import tempfile

        from backend import app as app_module

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            with self._temp_job_db(Path(folder)) as job_store:
                created = now_iso()
                job_store.create({
                    "id": "paused-batch", "kind": "subtitle_batch", "status": "paused",
                    "stage": "paused", "createdAt": created, "updatedAt": created,
                    "pauseRequested": True, "subtitleQueue": [],
                    "milestones": [{"id": "batch", "label": "批量生成字幕",
                                    "subtitle": "逐条剪映识别 → 官方歌词核对 → 烧录",
                                    "status": "running"}],
                    "logs": [],
                })
                state = asyncio.run(app_module.cancel_job("paused-batch"))

        self.assertEqual(state["status"], "cancelled")
        self.assertFalse(state["pauseRequested"], "取消后不能再留着暂停标记")
        self.assertEqual(state["milestones"][0]["status"], "skipped")
        self.assertTrue(any("已取消" in str(row.get("message")) for row in state["logs"]),
                        "要留下一条取消日志")

    def test_resuming_a_paused_batch_clears_the_cancel_imprint(self):
        """「继续」必须清掉取消印记，否则 runner 一开跑就被 `is_job_cancelled` 挡回去。

        2026-09-19 用户实测「点击继续没有效果」：他在暂停态点过一次「取消」，
        `cancelRequested` 被永久留在状态里（旧 runner 的暂停分支把取消吞了），
        之后每次「继续」都在 runner 第一行 `if is_job_cancelled(...): return` 空转。
        跟 `reset_subtitle_job` 一样：重排前一定清印记。
        """
        import tempfile

        from backend import subtitle_step

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            with self._temp_job_db(Path(folder)) as job_store:
                created = now_iso()
                job_store.create({
                    "id": "paused-batch", "kind": "subtitle_batch", "status": "paused",
                    "stage": "paused", "createdAt": created, "updatedAt": created,
                    "pauseRequested": False, "cancelRequested": True,
                    "subtitleQueue": [], "milestones": [], "logs": [],
                })
                spawned: list[str] = []
                with unittest.mock.patch.object(
                        subtitle_step, "_spawn_batch_restart",
                        lambda job_id: spawned.append(job_id)):
                    state = subtitle_step.resume_publish_batch("paused-batch")

                self.assertIsNotNone(state, "暂停态必须能继续")
                self.assertFalse(state["cancelRequested"], "继续时必须清掉取消印记")
                self.assertFalse(state["pauseRequested"])
                self.assertEqual(spawned, ["paused-batch"], "要继续排一轮 runner")
                self.assertFalse(subtitle_step.is_job_cancelled("paused-batch"),
                                 "清掉印记后 runner 才不会被第一行挡回去")

    def test_subtitle_endpoint_needs_a_source_and_refuses_a_busy_queue(self):
        """接口层：没给输入 → 400；已有任务在跑 → **不再 409，而是接单排队**。

        用户 2026-09-16 实测报错「任务需要处理 / 已有任务正在运行：74e4a68d」，并追问
        「生成字幕为什么运行不了 他不依赖comfyui啊」——那条 409 就是这里原来的行为。
        字幕链路确实不经过 ComfyUI，现在落库成 `waiting` 由 runner 等 `pipeline_lock`。
        关键：`waiting` **不能**算进 `store.active()`，否则排队期间批量连下一个子任务都提交不了。
        """
        import asyncio
        import tempfile

        from backend import app as app_module
        from backend import store as store_module

        with self.assertRaises(app_module.HTTPException) as ctx:
            asyncio.run(app_module.create_subtitle_job(
                video=None, source_job_id="", source_key="final", song_name=""))
        self.assertEqual(ctx.exception.status_code, 400)

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            source = Path(folder) / "最终成片.mp4"
            source.write_bytes(b"src")
            with self._temp_job_db(Path(folder)) as job_store:
                created = now_iso()
                job_store.create({
                    "id": "busy-job", "kind": "upscale", "status": "running", "stage": "upscaling",
                    "createdAt": created, "updatedAt": created, "milestones": [], "logs": [],
                })
                job_store.create({
                    "id": "source-job", "kind": "singing", "status": "completed",
                    "stage": "completed", "createdAt": created, "updatedAt": created,
                    "milestones": [], "logs": [], "finalOutput": str(source),
                })

                async def fake_metadata(_path):
                    return {"width": 1440, "height": 1080, "duration": 23.0, "size": 3}

                spawned: list[str] = []

                def fake_spawn(coroutine):
                    spawned.append("spawned")
                    coroutine.close()          # 只验接口层，不真跑 runner

                with unittest.mock.patch.object(app_module, "DATA_DIR", Path(folder) / "data"), \
                        unittest.mock.patch.object(app_module, "media_metadata", fake_metadata), \
                        unittest.mock.patch.object(app_module, "spawn", fake_spawn):
                    state = asyncio.run(app_module.create_subtitle_job(
                        video=None, source_job_id="source-job", source_key="final",
                        song_name="有何不可"))

                self.assertEqual(state["status"], "queued",
                                 "提交即跑：不再有 waiting 排队态，也不受别的任务影响")
                self.assertEqual(state["stage"], "upload")
                self.assertIsNone(state["progress"], "还没开跑时不写假进度")
                self.assertEqual(job_store.active()["id"], state["id"],
                                 "字幕现在提交即跑（queued 就是 active），不再躲在 waiting 里")
                self.assertEqual(spawned, ["spawned"], "提交后立刻把 runner 排上")
                self.assertEqual([row["id"] for row in job_store.waiting()], [],
                                 "不再有 waiting 排队态")

                # 可以取消：不许去动正在跑的那个出片任务
                cancelled = asyncio.run(app_module.cancel_job(state["id"]))
                self.assertEqual(cancelled["status"], "cancelled")
                self.assertEqual(job_store.get("busy-job")["status"], "running",
                                 "取消字幕任务不能碰正在跑的出片任务")

    def test_subtitle_job_does_not_wait_for_the_pipeline_lock(self):
        """**字幕不再等 `pipeline_lock`**（用户 2026-09-16：「不要锁这个啊 和生成字幕有什么关系 取消这个锁」）。

        这条链路只用剪映 + ffmpeg，不碰 ComfyUI、不吃采样显存；实测曾被一单跑到 66% 的迁移
        堵住半小时以上。所以即使 `pipeline_lock` 被别人攥着，runner 也必须**立刻开跑**，
        并且全程不碰这把锁（锁仍被别人持有到我们自己结束）。
        """
        import asyncio
        import tempfile

        from backend import store as store_module
        from backend import subtitle_step
        from backend.pipeline import pipeline_lock

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            source = Path(folder) / "最终成片.mp4"
            source.write_bytes(b"src")
            out = Path(folder) / "字幕版.mp4"
            with self._temp_job_db(Path(folder)) as job_store:
                burns: list[str] = []
                lock_seen: list[bool] = []

                async def fake_burn(_video, _song, target, *, on_stage=None, stats=None, **_kw):
                    burns.append(Path(target).name)
                    lock_seen.append(pipeline_lock.locked())
                    for milestone_id in ("recognize", "verify", "burn"):
                        if on_stage:
                            on_stage(milestone_id, f"进入 {milestone_id}")
                    Path(target).write_bytes(b"sub")
                    if stats is not None:
                        stats.update({"lines": 6, "lyricSource": "歌名搜索《表白》- 萧亚轩",
                                      "corrected": 1})
                    return Path(target), []

                async def fake_metadata(_path):
                    return {"width": 1440, "height": 1080, "duration": 23.0, "size": 1024}

                created = now_iso()
                job_store.create({
                    "id": "subtitle-queue", "kind": "subtitle", "status": "queued", "stage": "upload",
                    "createdAt": created, "updatedAt": created,
                    "subtitleSource": str(source), "subtitleOutPath": str(out),
                    "songName": "表白", "milestones": store_module.subtitle_milestones(), "logs": [],
                    "finalReady": False, "finalOutput": None,
                })

                async def scenario():
                    with unittest.mock.patch.object(subtitle_step, "burn_for_video", fake_burn), \
                            unittest.mock.patch.object(subtitle_step, "media_metadata", fake_metadata), \
                            unittest.mock.patch.object(subtitle_step, "close_jianying_quietly",
                                                       unittest.mock.Mock(return_value=True)):
                        async with pipeline_lock:      # 模拟出片/迁移任务正在跑
                            task = asyncio.create_task(subtitle_step.run_subtitle_job("subtitle-queue"))
                            await asyncio.wait_for(task, timeout=10)
                            self.assertTrue(pipeline_lock.locked(),
                                            "字幕跑完不该把别人的锁释放掉")

                asyncio.run(scenario())
                state = job_store.get("subtitle-queue")
                self.assertEqual(state["status"], "completed", "锁被占着也要自己跑完")
                self.assertEqual(burns, ["字幕版.mp4"], "不等锁，直接开跑")
                self.assertEqual(lock_seen, [True], "跑的时候锁确实在别人手里（证明没等它）")
                self.assertEqual([m["status"] for m in state["milestones"]], ["completed"] * 3)

    def test_batch_submit_waits_out_a_busy_pipeline_instead_of_failing_the_item(self):
        """批量提交视频子任务撞上 409（流水线被别人占着）→ **等着重试**，不把条目判失败。

        `_wait_for_free_pipeline` 是提交前的一次检查，但「检查完 → 真正提交」之间仍有竞态：
        排队中的「生成字幕」正好在这一刻拿到流水线锁，批量就会收到那句
        「已有任务正在运行」。原来的实现直接 `raise RuntimeError` → 整条作品的出片被丢掉。
        """
        import asyncio
        import tempfile

        from backend import batch_worker

        class FakeResponse:
            def __init__(self, status_code, payload=None):
                self.status_code = status_code
                self._payload = payload
                self.text = "已有任务正在运行：74e4a68d"

            @property
            def is_success(self):
                return 200 <= self.status_code < 300

            def json(self):
                if self._payload is None:
                    raise ValueError("不是 JSON")
                return self._payload

        posts: list[str] = []

        class FakeClient:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def post(self, url, **_kwargs):
                posts.append(url)
                if len(posts) == 1:                     # 第一次：流水线被占着
                    return FakeResponse(409, {"detail": "已有任务正在运行：74e4a68d"})
                return FakeResponse(200, {"id": "child-job", "status": "queued"})

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            source = Path(folder) / "源视频.mp4"
            reference = Path(folder) / "人物图.png"
            source.write_bytes(b"video")
            reference.write_bytes(b"image")
            item = {
                "id": "item-1", "kind": "migrate", "ratio": "9:16",
                "sourcePath": str(source), "ai": {"reference_image_path": str(reference)},
            }

            async def no_wait(_batch_id, _item_id):
                return None

            async def scenario():
                with unittest.mock.patch.object(batch_worker, "_item", lambda _b, _i: item), \
                        unittest.mock.patch.object(batch_worker, "_wait_for_free_pipeline", no_wait), \
                        unittest.mock.patch.object(batch_worker, "SUBMIT_BUSY_WAIT_SECONDS", 0), \
                        unittest.mock.patch.object(batch_worker.httpx, "AsyncClient", FakeClient), \
                        unittest.mock.patch.object(batch_worker.batch_store, "add_item_log",
                                                   unittest.mock.Mock()):
                    return await batch_worker._post_video_job("batch-1", "item-1")

            child = asyncio.run(scenario())

        self.assertEqual(child["id"], "child-job", "409 之后重试成功就该正常返回子任务")
        self.assertEqual(len(posts), 2, "409 必须重试一次，而不是直接把这一条判失败")
        self.assertTrue(all(url.endswith("/api/jobs/migrate") for url in posts), posts)

    def test_song_can_be_found_by_its_lyrics(self):
        """用户 2026-09-16：「你可以根据歌词找吗」——歌名缺失/写错时靠歌词搜歌兜底。

        实测：`夏天快要过去` → 《有何不可》- 许嵩；`是谁把牵挂埋在了土里` → 《茶花开了…》
        （后者正是条目 #4，它的 `ai.song_name` 是空的）。这里用假的候选验证打分与挑选逻辑，
        不联网。
        """
        import asyncio

        from backend import subtitle_step

        rows = [(0.0, 2.0, "夏天快要过去"), (2.0, 4.0, "请你少买冰淇淋"),
                (4.0, 6.0, "天凉就别穿短裙")]

        # ① 搜索词挑最长、去重、丢掉太短的
        self.assertEqual(subtitle_step.pick_lyric_query(rows)[0], "请你少买冰淇淋")

        # ② 重合度：正确候选高、错误候选低
        right = ["天空好想下雨", "夏天快要过去", "请你少买冰淇淋", "天凉就别穿短裙"]
        wrong = ["完全不相干的一句", "另一句也无关", "第三句无关"]
        self.assertGreater(subtitle_step.lyric_coverage([t for _a, _b, t in rows], right), 0.9)
        self.assertLess(subtitle_step.lyric_coverage([t for _a, _b, t in rows], wrong), 0.3)

        # ③ 双路搜索：歌名那路给错的、歌词那路给对的 → 必须选对的那个
        async def fake_by_name(_song, limit=6):
            return [{"id": "111", "name": "毫不相干的歌", "artist": "某人"}]

        async def fake_by_lyric(_line, limit=6):
            return [{"id": "222", "name": "有何不可", "artist": "许嵩"}]

        async def fake_lyric(song_id):
            if str(song_id) == "111":
                return {"lines": [{"time": 1.0, "orig": "完全不相干的一句"}]}
            return {"lines": [{"time": 1.0, "orig": t} for t in right]}

        with unittest.mock.patch.object(subtitle_step, "_search_songs", fake_by_name), \
                unittest.mock.patch.object(subtitle_step, "_search_songs_by_lyric", fake_by_lyric), \
                unittest.mock.patch.object(subtitle_step, "_song_lyric", fake_lyric):
            official, how, min_ratio = asyncio.run(
                subtitle_step.official_lyrics_for(rows, "一个错歌名"))

        self.assertEqual(official, right, "必须按重合度选中歌词搜到的那首")
        self.assertIn("歌词搜索", how)
        self.assertEqual(min_ratio, subtitle_step._MATCH_MIN_RATIO, "重合度高 → 允许按 0.6 改字")

    def test_loose_match_source_only_corrects_near_identical_lines(self):
        """只找到翻唱/说唱版（重合 70%~85%）时，只允许改几乎完全一致的句子——
        绝不拿"另一个版本"的歌词覆盖识别结果（实测条目 #4 的候选全是说唱版）。"""
        import asyncio

        from backend import subtitle_step

        rows = [(0.5, 3.0, "这些思念什么没日没夜"), (5.1, 8.0, "还是被它吹散的蒲公英")]
        official = ["他在思念什么每日每夜", "还是被它吹散的蒲公英"]

        # 严格门槛：第一句相似度约 0.6，达不到 0.85 → 保留原样；第二句完全相同 → 保留
        fixed, notes = subtitle_step.correct_with_official(
            rows, official, min_ratio=subtitle_step._STRICT_MATCH_RATIO)
        self.assertEqual([t for _a, _b, t in fixed], [t for _a, _b, t in rows])
        self.assertTrue(any("未达校对门槛" in n for n in notes), notes)

        # 宽松门槛（来源很可信时）：第一句会被修正
        loose, _notes = subtitle_step.correct_with_official(
            rows, official, min_ratio=subtitle_step._MATCH_MIN_RATIO)
        self.assertEqual(loose[0][2], "他在思念什么每日每夜")

    def test_whole_song_coverage_survives_a_wrong_song_name(self):
        """识别错几个字不该让整首歌失去校对资格（2026-09-22 用户：「歌词有的不对」）。

        换口径的原因：原来是「逐行相似度 ≥0.75 的行数占比 ≥0.7 才校对」，5 句错 2 句就到
        0.6 → 被门槛整首挡掉 —— 今天 8 条里 5 条（002/003/005/006/008）就是这么"一条都没
        校对"的。整段字符口径下，正确的歌稳稳 0.8 以上、不相干的歌仍在 0.4 以下。
        """
        from backend import subtitle_step

        # 005《你若成风》今天真实的剪映识别结果（错字：化成/幻化）
        recognized = ["老夫子带着假发 我不要三寸金莲胡话", "想和你跳超短裙的恰恰",
                      "想带你回家见妈妈", "你若化成风 我幻化成雨", "守护你身边 一笑为红颜"]
        right = ["老夫子带着假发", "我不要三寸金莲胡话", "想和你跳超短裙的恰恰",
                 "想带你回家见妈妈", "你若化成风", "我幻化成雨", "守护你身边", "一笑为红颜"]
        wrong = ["我在人民广场吃着炸鸡", "而此时此刻你在哪里", "虽然或许你在声东击西",
                 "但疲倦已让我懒得怀疑"]

        self.assertGreater(subtitle_step.lyric_coverage(recognized, right), 0.8,
                           "正确的歌必须能过 0.6 门槛")
        self.assertLess(subtitle_step.lyric_coverage(recognized, wrong), 0.4,
                        "不相干的歌不许被当成同一首")

    def test_hallucinated_lines_are_fixed_by_position(self):
        """**整句听错**的行也要能修回来（2026-09-22 实测 006《说书》）。

        剪映把「书上说 山上有人腾云驾鹤 / 书上说 仙人能点石成金箔」听成了
        「书上说先生有人腾云下鹤 / 书中说仙人有胭脂正轻薄」——第二句只有 0.36 像，
        全曲"找最像的一行"永远对不上它，只有**顺序对齐 + 长度相容**能救。
        """
        from backend import subtitle_step

        rows = [(1.40, 4.57, "书上说先生有人腾云下鹤"),
                (4.57, 7.77, "书中说仙人有胭脂正轻薄")]
        # 官方歌词后面还有一大段这段视频没唱到（真实情况就是 94 行里的中间几句）
        official = ["书上说 山上有人腾云驾鹤", "书上说 仙人能点石成金箔",
                    "才放下书 一册又记一折", "欲知后事如何 且听下回分解",
                    "谁说那帝王功过 一游成河", "谁听那人生苦短 一过三秋"]

        fixed, notes = subtitle_step.correct_with_official(
            rows, official, min_ratio=subtitle_step._MATCH_MIN_RATIO)

        self.assertEqual([(a, b) for a, b, _t in fixed], [(a, b) for a, b, _t in rows],
                         "时间轴一个字都不能动")
        self.assertEqual(fixed[0][2], "书上说 山上有人腾云驾鹤")
        self.assertEqual(fixed[1][2], "书上说 仙人能点石成金箔", f"整句听错也要按位置对齐：{notes}")

    def test_position_fill_needs_matching_counts(self):
        """按位置**推定**只在"两侧锚定且官方行数刚好对得上"时生效；对不上就不许动。

        这条防的是"另一个版本/没唱到的段落"被硬套进来。
        """
        from backend import subtitle_step

        rows = [(0.0, 2.0, "夏天快要过去"), (2.0, 4.0, "完全听不出来的第三句"),
                (4.0, 6.0, "天凉就别穿短裙")]
        exact = ["夏天快要过去", "请你少买冰淇淋", "天凉就别穿短裙"]
        fixed, _notes = subtitle_step.correct_with_official(
            rows, exact, min_ratio=subtitle_step._MATCH_MIN_RATIO)
        self.assertEqual(fixed[1][2], "完全听不出来的第三句",
                         "一个字都对不上就不猜（宁可留着，也不许凭位置硬套官方原文）")

        # 对上了几个字（哪怕整句听错）→ 就换成官方原文：006《说书》「书中说仙人有胭脂正轻薄」
        # 只对上「书上说仙人」四个字，也必须改成「书上说仙人能点石成金箔」
        guessed, _n = subtitle_step.correct_with_official(
            [(0.0, 2.0, "夏天快要过去"), (2.0, 4.0, "请你少买冰淇冰淇淋"),
             (4.0, 6.0, "天凉就别穿短裙")], exact, min_ratio=subtitle_step._MATCH_MIN_RATIO)
        self.assertEqual(guessed[1][2], "请你少买冰淇淋")

    def test_a_shared_official_line_is_split_between_two_rows(self):
        """一行识别文本跨了官方两句、以及官方一句被剪映拆成两段 —— 两边都不许重复、也不许丢。

        实测的三次翻车都在这里（2026-09-22）：
          · 003「想从头在和你但我左右在迟疑」= 官方「想从头再和你」+「但我左右在迟疑」，
            只取后半句 → **少字**；
          · 003 官方副歌重复出现，同一句被拼两遍 → **重复**；
          · 004「一些烂理由只为了拥有」跨官方两句 → 前半句被吃掉。
        """
        from backend import subtitle_step

        # ① 识别一行跨官方两句（还错了一个字）→ 两句都要在，且字改成官方的
        rows = [(0.0, 2.0, "想从头在和你但我左右在迟疑")]
        official = ["夏天快要过去", "想从头再和你", "但我左右在迟疑", "天凉就别穿短裙"]
        fixed, _notes = subtitle_step.correct_with_official(
            rows, official, min_ratio=subtitle_step._MATCH_MIN_RATIO)
        self.assertEqual(fixed[0][2], "想从头再和你 但我左右在迟疑")

        # ② 官方一句被剪映拆成两段 → 各留各的断句，不许把整句塞给其中一行
        rows2 = [(0.0, 2.0, "为你唱这首歌"), (2.0, 4.0, "没有什么风格")]
        official2 = ["为你唱这首歌 没有什么风格"]
        fixed2, _notes2 = subtitle_step.correct_with_official(
            rows2, official2, min_ratio=subtitle_step._MATCH_MIN_RATIO)
        self.assertEqual([t for _a, _b, t in fixed2], ["为你唱这首歌", "没有什么风格"])

        # ③ 官方副歌重复出现 → 同一句不许被拼进同一条字幕两遍
        rows3 = [(0.0, 2.0, "封印思念魔咒过往我逗留")]
        official3 = ["封印思念魔咒 过往我保留", "封印思念魔咒 过往我保留"]
        fixed3, _notes3 = subtitle_step.correct_with_official(
            rows3, official3, min_ratio=subtitle_step._MATCH_MIN_RATIO)
        self.assertEqual(fixed3[0][2], "封印思念魔咒 过往我保留")

    def test_position_alignment_is_off_for_untrusted_sources(self):
        """来源不够可信（翻唱/说唱版）时**不启用**按位置对齐 —— 位置也可能是另一个版本的。"""
        from backend import subtitle_step

        rows = [(1.40, 4.57, "书上说先生有人腾云下鹤"),
                (4.57, 7.77, "书中说仙人有胭脂正轻薄")]
        official = ["书上说 山上有人腾云驾鹤", "书上说 仙人能点石成金箔",
                    "才放下书 一册又记一折", "欲知后事如何 且听下回分解"]

        fixed, notes = subtitle_step.correct_with_official(
            rows, official, min_ratio=subtitle_step._STRICT_MATCH_RATIO)
        self.assertEqual(fixed[1][2], "书中说仙人有胭脂正轻薄",
                         "严格门槛下不许按位置改低相似度的句子")
        self.assertTrue(any("未达校对门槛" in n for n in notes), notes)

    def test_the_title_is_also_searched_as_a_lyric(self):
        """歌名搜不到时，**把标题当歌词再搜一次**（2026-09-22 实测最有效的一路）。

        004 的真实情况：`netease_search("看着你的笑我好像就足够")` 一个候选都没有，
        而 `type=1006` 用这句一搜就是《碎碎念》-队长。
        """
        import asyncio

        from backend import subtitle_step

        async def no_sleep(*_args, **_kwargs):
            return None

        rows = [(0.47, 4.13, "看着你的笑我好像就足够"), (4.13, 7.80, "看你皱眉头我也跟着难受"),
                (7.80, 10.60, "我知道我说着的太多了"), (10.60, 15.10, "一些烂理由只为了拥有")]
        queries: list[str] = []

        async def no_name(_song, limit=6):
            return []

        async def by_lyric(line, limit=6):
            queries.append(line)
            if "看着你的笑" in line:
                return [{"id": "999", "name": "碎碎念", "artist": "队长"}]
            return []

        async def lyric(song_id):
            return {"lines": [{"time": 1.0, "orig": t} for t in
                              ["看着你的笑我好像就足够", "看你皱眉头我也跟着难受",
                               "我知道我说着太多的一些烂理由", "一些烂理由只为了拥有",
                               "他们说着的话好像不经过脑袋"]]}

        with unittest.mock.patch.object(subtitle_step, "_search_songs", no_name), \
                unittest.mock.patch.object(subtitle_step, "_search_songs_by_lyric", by_lyric), \
                unittest.mock.patch.object(subtitle_step, "_song_lyric", lyric), \
                unittest.mock.patch.object(subtitle_step.asyncio, "sleep", no_sleep):
            official, how, min_ratio = asyncio.run(
                subtitle_step.official_lyrics_for(rows, "看着你的笑我好像就足够"))

        self.assertIn("看着你的笑我好像就足够", queries, "标题必须当歌词搜一次")
        self.assertTrue(official, f"应当靠标题那一路找到歌词：{how}")
        self.assertIn("碎碎念", how)
        self.assertEqual(min_ratio, subtitle_step._MATCH_MIN_RATIO)

    def test_empty_search_is_retried_because_throttling_looks_like_no_result(self):
        """网易云限流的表现是**返回空**而不是报错 —— 空结果要重试一次，否则静默退化。"""
        import asyncio

        from backend import subtitle_step

        async def no_sleep(*_args, **_kwargs):
            return None

        rows = [(0.0, 2.0, "夏天快要过去"), (2.0, 4.0, "请你少买冰淇淋")]
        calls = {"n": 0}

        async def flaky_by_name(_song, limit=6):
            calls["n"] += 1
            if calls["n"] == 1:
                return []                                  # 第一次接口抽风 → 空
            return [{"id": "7", "name": "有何不可", "artist": "许嵩"}]

        async def by_lyric(_line, limit=6):
            return []

        async def lyric(_song_id):
            return {"lines": [{"time": 1.0, "orig": t} for t in
                              ["夏天快要过去", "请你少买冰淇淋"]]}

        with unittest.mock.patch.object(subtitle_step, "_search_songs", flaky_by_name), \
                unittest.mock.patch.object(subtitle_step, "_search_songs_by_lyric", by_lyric), \
                unittest.mock.patch.object(subtitle_step, "_song_lyric", lyric), \
                unittest.mock.patch.object(subtitle_step.asyncio, "sleep", no_sleep):
            official, how, _min_ratio = asyncio.run(
                subtitle_step.official_lyrics_for(rows, "何不可"))

        self.assertTrue(official, f"空结果重试后应当拿到歌词：{how}")
        self.assertEqual(calls["n"], 2)

    def test_official_lyrics_are_fetched_once_and_reused(self):
        """同一首歌全程**只取一次**歌词（少给对端添压力，也避免被当成高频访问）。

        顺带钉住 QQ 那条链路的返回字段（搜索 → 取词 → 语种/行数/预览）与歌词源：
        2026-09-22 从网易云换到 QQ音乐（用户：「不要用她的了 经常限流」）。
        """
        import asyncio

        from backend import lyrics_worker

        calls: list[str] = []

        class _Resp:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        class _Client:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def get(self, url, params=None, headers=None):
                calls.append(url)
                if "client_search_cp" in url:
                    return _Resp({"data": {"song": {"list": [{
                        "songmid": "0039MnYb0qxYhV", "songname": "有何不可",
                        "singer": [{"name": "许嵩"}], "albumname": "自定义"}]}}})
                return _Resp({
                    "lyric": "[ti:有何不可]\n[ar:许嵩]\n[00:01.00]夏天快要过去\n"
                             "[00:03.00]请你少买冰淇淋\n[00:05.00]词：许嵩\n",
                    "trans": "",
                })

        lyrics_worker._LYRIC_CACHE.clear()
        lyrics_worker._HAVE_LYRIC_CACHE.clear()
        try:
            with unittest.mock.patch.object(lyrics_worker.httpx, "AsyncClient", _Client):
                first = asyncio.run(lyrics_worker.song_lyric("0039MnYb0qxYhV"))
                second = asyncio.run(lyrics_worker.song_lyric("0039MnYb0qxYhV"))
                found = asyncio.run(lyrics_worker.search_songs_with_lyrics("有何不可"))
            lyric_calls = [url for url in calls if "fcg_query_lyric_new" in url]
            self.assertEqual(len(lyric_calls), 1, f"歌词被重复取了：{calls}")
            self.assertEqual([line["orig"] for line in first["lines"]],
                             ["夏天快要过去", "请你少买冰淇淋"], "制作人员行必须被滤掉")
            self.assertEqual(second, first)
            self.assertEqual(found[0]["lineCount"], 2)
            self.assertEqual(found[0]["preview"], "夏天快要过去")
            self.assertEqual(found[0]["id"], "0039MnYb0qxYhV", "id 必须是 songmid（取词要用它）")
        finally:
            lyrics_worker._LYRIC_CACHE.clear()
            lyrics_worker._HAVE_LYRIC_CACHE.clear()

    def test_uia_has_thread_com_init_helper(self):
        """uiautomation 要求每个线程自己初始化 COM：批量里它是从 asyncio.to_thread
        的工作线程调用的，少了包装就会报「尚未调用 CoInitialize」。"""
        from backend import jianying_recognize, jianying_uia

        self.assertTrue(hasattr(jianying_uia, "uia_in_thread"))
        source = (self.root / "backend" / "jianying_recognize.py").read_text(encoding="utf-8")
        self.assertIn("uia_in_thread()", source)
        self.assertTrue(hasattr(jianying_recognize, "close_app"), "跑完必须能关掉剪映")

    def test_update_lock_script_supports_check_and_revert(self):
        """剪映版本锁定脚本必须可查、可回滚（用户要求「禁止更新 保持本版本」）。"""
        script = self.root / "scripts" / "lock_jianying_update.py"
        self.assertTrue(script.is_file())
        text = script.read_text(encoding="utf-8")
        for flag in ("--check", "--revert"):
            self.assertIn(flag, text)
        self.assertIn("enableAutoUpdate", text)
        self.assertIn("update.exe", text)


class BatchRunnerTakeoverTests(unittest.TestCase):
    """「重新开始」必须**真的重新跑**（2026-09-16 用户：「批量点击重新开始怎么没有效果」）。

    实测的现场：批次停在 `status=running` + `runnerActive=False`，7 条全躺在 `confirmed`
    等出片，可一个 runner 都没有 —— 用户点「批量重新开始」两次，界面都提示「已重新开始 7 条」，
    实际一条都没动。两个根因（都在这里钉住）：

    ① `run_batch` 开头那句 `if batch_id in _RUNNING_BATCHES: return` 是静默 no-op：
       上一轮 runner 卡在等一个被堵死的出片子任务上，却一直攥着 `_RUNNING_BATCHES`，
       于是新 runner 一去就返回，什么都不做；
    ② 一条上个进程留下的僵尸「生成字幕」任务（状态永远停在 running）占着 `store.active()`，
       把 `_wait_for_free_pipeline` 的等待循环无限期堵住 —— 字幕链路根本不占流水线锁。
    """

    root = Path(__file__).resolve().parent.parent

    class _Resp:
        """假的 httpx 响应（`_wait_for_free_pipeline` 只用到 `.json()`）。"""

        def __init__(self, payload: dict):
            self._payload = payload
            self.is_success = True

        def json(self) -> dict:
            return self._payload

    def _batch_state(self, batch_id: str = "b1") -> dict:
        state = new_batch_state(["https://v.douyin.com/a", "https://v.douyin.com/b"], [])
        state["id"] = batch_id
        state["status"] = "running"
        state["runnerActive"] = False
        for item in state["items"]:
            item["status"] = "confirmed"
        return state

    @contextmanager
    def _temp_job_db(self, folder: Path):
        """把任务库换到临时目录（`JobStore` 只在构造时建表，换完必须 `_init_db()`）。"""
        from backend import store as store_module

        original = store_module.DB_PATH
        store_module.DB_PATH = Path(folder) / "jobs.db"
        store_module.store._init_db()
        try:
            yield store_module.store
        finally:
            store_module.DB_PATH = original

    def _stub_store(self, state: dict):
        """只带 `run_batch` 真正用到的那几个方法的最小状态存储。"""

        class Stub:
            def __init__(self):
                self.logs: list[tuple[str, str]] = []

            def get(self, _batch_id):
                return state

            def update(self, _batch_id, **changes):
                state.update(changes)
                return state

            def mutate_item(self, _batch_id, item_id, mutator):
                for item in state["items"]:
                    if item["id"] == item_id:
                        mutator(item)
                return state

            def set_item_milestone(self, _batch_id, item_id, milestone_id, **changes):
                for item in state["items"]:
                    if item["id"] == item_id:
                        for milestone in item["milestones"]:
                            if milestone["id"] == milestone_id:
                                milestone.update(changes)
                return state

            def add_item_log(self, _batch_id, item_id, message):
                self.logs.append((item_id, message))
                for item in state["items"]:
                    if item["id"] == item_id:
                        item.setdefault("logs", []).append({"time": "t", "message": message})
                return state

            def add_log(self, _batch_id, message):
                self.logs.append(("", message))

        return Stub()

    def test_restart_takes_over_a_runner_that_is_stuck_on_a_video_task(self):
        """旧 runner 卡在等出片子任务时，「重新开始」要把批次**真的**交给新一代跑起来。"""
        from backend import batch_worker

        state = self._batch_state()
        stub = self._stub_store(state)
        parked: dict = {}

        async def scenario() -> None:
            # 第一次出片**永远不出结果**（模拟被堵死的子任务）；换代之后的新出片立刻完成。
            holder: dict = {"release": asyncio.Event()}

            async def fake_video(_batch_id, item_id):
                for item in state["items"]:
                    if item["id"] == item_id:
                        item["status"] = "running"
                await holder["release"].wait()
                for item in state["items"]:
                    if item["id"] == item_id:
                        item["status"] = "completed"

            with patch.object(batch_worker, "batch_store", stub), patch.object(
                batch_worker, "_process_confirmed", fake_video
            ), patch.object(batch_worker, "_covers_ready", return_value=True):
                # ① 第一代 runner 出片到一半（正 await 后台出片任务）
                first = asyncio.create_task(batch_worker.run_batch("b1"))
                for _ in range(300):
                    if any(item["status"] == "running" for item in state["items"]):
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(state["runnerActive"], "第一代 runner 应该已经在跑")
                # 旧 runner 还占着名额（它没退出），这正是原来「新 runner 一去就 no-op」的前提
                self.assertIn("b1", batch_worker._RUNNING_BATCHES)
                parked["in_flight"] = set(batch_worker._VIDEO_TASKS)

                # ② 用户点「重新开始」：条目重置回 confirmed + 换代 + 重新 spawn
                for item in state["items"]:
                    item["status"] = "confirmed"
                    item["childJob"] = None
                    item["videoJobId"] = None
                batch_worker.take_over_batch_run("b1")
                second = asyncio.create_task(batch_worker.run_batch("b1"))
                for _ in range(500):
                    if any(item["status"] == "running" for item in state["items"]):
                        break
                    await asyncio.sleep(0.01)
                # 新一代**真的**接上了这一批（旧的还卡在原地没退出）
                self.assertIn("b1", batch_worker._RUNNING_BATCHES)
                self.assertFalse(first.done(), "旧 runner 此时仍卡在等那个堵死的子任务")

                # ③ 旧 runner 看到自己的代过期，退出前把那个还在跑的子任务交代清楚
                #   （留强引用 + 日志；它自己跑完就自动从集合里摘掉）
                await asyncio.wait_for(first, timeout=5)
                self.assertTrue(first.done())
                self.assertFalse(first.cancelled())
                self.assertEqual(
                    batch_worker._STALE_VIDEO_TASKS, parked["in_flight"],
                    "被顶掉的 runner 留下的在跑子任务必须被接手（否则可能被 GC 掉）",
                )
                self.assertTrue(
                    any("交给新一轮接管" in message for _item, message in stub.logs), stub.logs
                )

                # ④ 新一代的出片任务跑完 → 条目出片完成
                holder["release"].set()
                for _ in range(500):
                    if any(item["status"] == "completed" for item in state["items"]):
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(
                    [item for item in state["items"] if item["status"] == "completed"],
                    "新一代 runner 必须真的把条目跑起来（不许静默 no-op）",
                )
                batch_worker._STALE_VIDEO_TASKS.clear()

                await second
                self.assertNotIn("b1", batch_worker._RUNNING_BATCHES)
                self.assertFalse(state["runnerActive"])

        try:
            asyncio.run(scenario())
        finally:
            # 那条假出片任务可能还挂着（代表「被堵死的旧子任务」）：收尾掉，
            # 免得残留的强引用影响后面的用例（真实的卡死任务由用户取消/重启收掉）。
            for task in list(parked.get("in_flight") or ()):
                task.cancel()
            batch_worker._STALE_VIDEO_TASKS.clear()
            batch_worker._RUN_GENERATION.pop("b1", None)
            batch_worker._RUNNING_BATCHES.discard("b1")

    def test_a_superseded_runner_never_marks_the_item_failed(self):
        """被顶掉的旧 runner 不许再写失败：否则它会把新一轮刚重置好的条目打回失败。"""
        from backend import batch_worker

        state = self._batch_state()
        stub = self._stub_store(state)
        item = state["items"][0]
        with patch.object(batch_worker, "batch_store", stub):
            generation = batch_worker.take_over_batch_run("b1")
            batch_worker.take_over_batch_run("b1")   # 用户又点了一次「重新开始」→ 换了一代
            batch_worker._fail_item_and_batch("b1", item["id"], "出片失败", generation=generation)
        self.assertEqual(item["status"], "confirmed", "过期的那一代不许再改条目状态")
        self.assertIsNone(item.get("error"))
        batch_worker._RUN_GENERATION.pop("b1", None)

    def test_pipeline_wait_does_not_block_on_a_stuck_subtitle_job(self):
        """字幕任务不占流水线锁，不该让批量出片无限期等着它。"""
        from backend import batch_worker

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def get(self, _url):
                return BatchRunnerTakeoverTests._Resp(
                    {"app": {"id": "5110fca3", "kind": "subtitle", "status": "running"}}
                )

        state = self._batch_state()
        stub = self._stub_store(state)
        with patch.object(batch_worker.httpx, "AsyncClient", lambda **_kw: FakeClient()), patch.object(
            batch_worker, "batch_store", stub
        ):
            asyncio.run(asyncio.wait_for(batch_worker._wait_for_free_pipeline("b1", "i1"), timeout=1))
        self.assertFalse(
            [message for _item, message in stub.logs if "等待资源" in message],
            "字幕任务不该被当成「占用流水线」的理由",
        )

    def test_pipeline_wait_does_not_block_on_a_voice_batch_that_is_still_waiting(self):
        """「一键批量转音色」在**等出片空档**时不许反过来挡住出片（否则两边互相等死）。

        2026-09-22 用户点「一键批量转换音色」却什么反应都没有：那条 rvc_batch 的状态是
        `queued`（正在等出片空档、ComfyUI 都没碰），出片侧却把它当成「显卡被占」不去提交，
        于是 rvc_batch 永远等不到空档 —— 与 `_batch_busy` 看到自己那一半是**同一对**死锁。
        """
        from backend import batch_worker

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def get(self, _url):
                return BatchRunnerTakeoverTests._Resp(
                    {"app": {"id": "d2b949da", "kind": "rvc_batch", "status": "queued", "stage": "waiting"}}
                )

        state = self._batch_state()
        stub = self._stub_store(state)
        with patch.object(batch_worker.httpx, "AsyncClient", lambda **_kw: FakeClient()), patch.object(
            batch_worker, "batch_store", stub
        ):
            asyncio.run(asyncio.wait_for(batch_worker._wait_for_free_pipeline("b1", "i1"), timeout=1))
        self.assertFalse(
            [message for _item, message in stub.logs if "等待资源" in message],
            "正在等空档的批量转音色不该被当成「占用流水线」的理由",
        )

    def test_pipeline_wait_still_waits_for_a_voice_batch_that_is_converting(self):
        """真在转音色（`running`，此时 ComfyUI 已被它关掉）时，出片仍然要等它，不许硬抢显卡。"""
        from backend import batch_worker

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def get(self, _url):
                return BatchRunnerTakeoverTests._Resp(
                    {"app": {"id": "5a54a8d6", "kind": "rvc_batch", "status": "running", "stage": "voice"}}
                )

        state = self._batch_state()
        stub = self._stub_store(state)
        with patch.object(batch_worker.httpx, "AsyncClient", lambda **_kw: FakeClient()), patch.object(
            batch_worker, "batch_store", stub
        ), patch.object(batch_worker, "PIPELINE_FREE_WAIT_SECONDS", 0.0), patch.object(
            batch_worker, "PIPELINE_WAIT_POLL_SECONDS", 0.0
        ):
            asyncio.run(asyncio.wait_for(batch_worker._wait_for_free_pipeline("b1", "i1"), timeout=5))
        self.assertTrue(
            [message for _item, message in stub.logs if "等待资源" in message],
            "正在转音色的批量任务仍然算「占着流水线」",
        )

    def test_pipeline_wait_gives_up_instead_of_waiting_forever(self):
        """兜底：真有一条任务把流水线占着不放，等满上限也要如实记一条并继续提交。"""
        from backend import batch_worker

        class FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def get(self, _url):
                return BatchRunnerTakeoverTests._Resp(
                    {"app": {"id": "deadbeef", "kind": "migrate", "status": "running"}}
                )

        state = self._batch_state()
        stub = self._stub_store(state)
        ticks = iter([0.0, 1.0, 999.0, 999.0, 999.0, 999.0])
        with patch.object(batch_worker.httpx, "AsyncClient", lambda **_kw: FakeClient()), patch.object(
            batch_worker, "batch_store", stub
        ), patch.object(batch_worker, "PIPELINE_FREE_WAIT_SECONDS", 10.0), patch.object(
            batch_worker, "_pipeline_wait_clock", lambda: next(ticks, 999.0)
        ), patch.object(batch_worker, "PIPELINE_WAIT_POLL_SECONDS", 0.0):
            asyncio.run(asyncio.wait_for(batch_worker._wait_for_free_pipeline("b1", "i1"), timeout=5))
        self.assertTrue(any("改为直接提交" in message for _item, message in stub.logs), stub.logs)

    def test_batch_runner_is_relaunched_when_it_died_without_finishing(self):
        """批次卡在「运行中」却没有任何 runner 时，读一次状态就把它重新拉起来。"""
        import asyncio as _asyncio

        from backend import app as app_module

        state = self._batch_state()
        state["total"] = 1
        state["items"][0]["status"] = "confirmed"
        spawned: list = []
        with patch.object(app_module, "spawn", lambda coro: (spawned.append(coro), coro.close())):
            async def scenario() -> None:
                app_module._heal_stalled_batch(state)

            _asyncio.run(scenario())
        self.assertEqual(len(spawned), 1, "该跑却没人跑的批次要自动补一个 runner")

    def test_batch_runner_is_not_relaunched_for_paused_or_waiting_states(self):
        """`paused` / `awaiting_review` / 已有 runner 的批次**绝不**偷偷继续跑。"""
        import asyncio as _asyncio

        from backend import app as app_module
        from backend import batch_worker

        def run_heal(state) -> list:
            spawned: list = []
            with patch.object(app_module, "spawn", lambda coro: (spawned.append(coro), coro.close())):
                _asyncio.run(_asyncio.sleep(0))
                app_module._heal_stalled_batch(state)
            return spawned

        paused = self._batch_state()
        paused["status"] = "paused"
        self.assertEqual(run_heal(paused), [])
        reviewing = self._batch_state()
        reviewing["status"] = "awaiting_review"
        self.assertEqual(run_heal(reviewing), [])

        # 有 runner 活着（这一轮真的 spawn 过）就不动它
        alive = self._batch_state()
        alive["runnerActive"] = True
        app_module._SPAWNED_BATCH_IDS.add("b1")
        try:
            with patch.object(batch_worker, "_RUNNING_BATCHES", {"b1"}):
                self.assertEqual(run_heal(alive), [])
        finally:
            app_module._SPAWNED_BATCH_IDS.discard("b1")

    def test_leftover_running_jobs_from_a_previous_process_are_interrupted(self):
        """重启后残留的 running 任务要标成中断（它挡着批量出片的等待循环，且没人在跑）。"""
        from backend import app as app_module
        from backend import store as store_module

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            with self._temp_job_db(Path(folder)) as job_store:
                created = now_iso()
                for job_id in ("zombie-job", "live-job"):
                    job_store.create({
                        "id": job_id, "kind": "subtitle", "status": "running", "stage": "subtitle",
                        "createdAt": created, "updatedAt": created, "milestones": [], "logs": [],
                    })
                app_module._SPAWNED_JOB_IDS.add("live-job")   # 这一轮真的被 spawn 起来了
                try:
                    with patch.object(app_module, "store", job_store):
                        app_module._interrupt_orphan_jobs()
                finally:
                    app_module._SPAWNED_JOB_IDS.discard("live-job")

                zombie = job_store.get("zombie-job")
                self.assertEqual(zombie["status"], "interrupted")
                self.assertIn("重启", zombie["errorSummary"])
                self.assertTrue(any("重新开始" in entry["message"] for entry in zombie["logs"]))
                self.assertEqual(job_store.get("live-job")["status"], "running", "在跑的不许误伤")

    def test_restart_endpoint_really_hands_the_batch_to_a_new_runner(self):
        """接口层：批量「重新开始」要换代（旧 runner 让位），而不是只改一句提示语。"""
        source = (self.root / "backend" / "app.py").read_text(encoding="utf-8")
        self.assertIn("take_over_batch_run(batch_id)", source)
        self.assertIn("def _heal_stalled_batch(", source)
        self.assertIn("batch_run_alive(batch_id)", source)


class _FakeLocator:
    """`page.locator(sel)` 的最小替身：只支持 chatgpt_cdp 用到的那几个调用。"""

    def __init__(self, page, selector: str):
        self.page = page
        self.selector = selector

    # chatgpt_cdp 里是 `page.locator(...).first` / `.count()` / `.set_input_files()`
    @property
    def first(self):
        return self

    def count(self) -> int:
        if self.selector.startswith('input[type="file"]'):
            return 1 if self.page.has_file_input else 0
        if self.selector.startswith('button[aria-label^="移除"]'):
            return self.page.chips
        return 0

    def set_input_files(self, paths) -> None:
        self.page.attached_files.extend([str(p) for p in paths])
        self.page.chips = max(self.page.chips, len(self.page.attached_files))

    def bounding_box(self):
        return {"x": 100, "y": 600, "width": 600, "height": 44}

    def inner_text(self) -> str:
        return self.page.content


class _FakeSession:
    def __init__(self, page):
        self.page = page

    def send(self, method, params=None) -> None:
        self.page.cdp_calls.append((method, params or {}))
        if method == "Input.dispatchDragEvent" and (params or {}).get("type") == "drop":
            self.page.attached_files.extend((params or {}).get("data", {}).get("files", []))
            self.page.chips = len(self.page.attached_files)


class _FakeContext:
    def __init__(self, page):
        self.page = page

    def new_cdp_session(self, _page):
        return _FakeSession(self.page)


class _FakeKeyboard:
    def __init__(self, page):
        self.page = page

    def press(self, key) -> None:
        self.page.keys.append(key)
        if key == "Enter" and self.page.content and self.page.send_works:
            self.page.content = ""
            self.page.chips = 0

    def insert_text(self, text) -> None:
        self.page.content += text


class _FakeMouse:
    def click(self, _x, _y) -> None:
        pass


class _FakePage:
    """Playwright page 的最小替身：够跑 `attach_images` / `_send` / `clear_stuck_stop`。

    用它把「新版桌面端没有 input[type=file]、只有拖放能挂图」「回合结束后停止按钮还留着，
    发送按钮因此不出现」这两个实测行为固化成回归测试，不用真的开 ChatGPT。
    """

    def __init__(self, *, has_file_input=False, generating=False, content="",
                 send_works=True, stop_click_unsticks=True):
        self.has_file_input = has_file_input
        self.generating = generating
        self.content = content
        self.send_works = send_works
        self.stop_click_unsticks = stop_click_unsticks
        self.chips = 0
        self.attached_files: list[str] = []
        self.cdp_calls: list[tuple[str, dict]] = []
        self.keys: list[str] = []
        self.stop_clicks = 0
        self.send_clicks = 0
        self.context = _FakeContext(self)
        self.keyboard = _FakeKeyboard(self)
        self.mouse = _FakeMouse()

    # ---- page 接口 ----
    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self, selector)

    def wait_for_timeout(self, _ms) -> None:
        pass

    def evaluate(self, script, arg=None):
        if "sels.some(s => !!document.querySelector(s))" in script:      # _generating_now
            return self.generating
        if "b.click(); return true;" in script:                            # _click_stop
            self.stop_clicks += 1
            if self.stop_click_unsticks:
                self.generating = False
            return True
        if '"移除"' in script or "移除" in script and "indexOf" in script:   # attachment_names
            return [f"移除“{Path(p).name}”" for p in self.attached_files]
        if 'data-testid="send-button"' in script and "b.click()" in script:  # _send 里的 JS click
            self.send_clicks += 1
            if self.send_works:
                self.content = ""
                self.chips = 0
            return True
        if 'button[aria-label*="发送"]' in script and "getBoundingClientRect" in script:
            # _JS_SEND_BUTTON：有内容且不在生成中才有发送按钮（与新版实测一致）
            if self.generating or not (self.content or self.chips):
                return None
            return {"disabled": False, "x": 1059.9, "y": 682.0}
        if "window.innerHeight" in script:
            return 900
        if "return {x: r.x" in script or "box" in script:
            return {"x": 300, "y": 640}
        if "activeElement" in script:
            return True
        return None


class CoverCdpNewShellTests(unittest.TestCase):
    """2026-09-17：桌面端换新外壳后封面链路的适配（用户：「启动了怎么没有效果」「到抖音图片了就不发送了」）。

    三个实测根因，每个都留一条测试：
      1. 新版没有 `input[type=file]`，「+ → 文件和文件夹」是原生对话框 → 只能走 CDP 拖放挂图；
      2. 生成图不再只是 blob，落在 `~/.codex/generated_images/<线程>/` → 必须同时等落盘目录；
      3. 回合结束后「停止」按钮还留在输入框里 → 发送按钮不出现，第二条消息永远发不出去
         （旧实现还把「有停止按钮」当成「已发送」）。
    """

    def test_attach_falls_back_to_drag_and_drop_when_there_is_no_file_input(self):
        """没有 file input 时必须改走 CDP 拖放（新版外壳唯一能自动化的挂图方式）。"""
        from backend import chatgpt_cdp as cdp

        with tempfile.TemporaryDirectory() as folder:
            img = Path(folder) / "人物图.png"
            img.write_bytes(b"x")

            old_shell = _FakePage(has_file_input=True)
            self.assertEqual(cdp.attach_images(old_shell, [img]), "input")
            self.assertEqual(old_shell.attached_files, [str(img)])
            self.assertEqual(old_shell.cdp_calls, [], "有 file input 就不该去拖放")

            new_shell = _FakePage(has_file_input=False)
            self.assertEqual(cdp.attach_images(new_shell, [img]), "dragdrop")
            types = [p["type"] for m, p in new_shell.cdp_calls if m == "Input.dispatchDragEvent"]
            self.assertEqual(types, ["dragEnter", "dragOver", "drop"])
            self.assertEqual(new_shell.attached_files, [str(img)])

    def test_send_waits_out_a_stuck_stop_button_instead_of_faking_success(self):
        """「停止」按钮一直在 ≠ 已发送：旧实现 `if _generating_now(page): return True` 会假成功。

        实测（rollout 日志里 `task_complete` 之后 5 分钟按钮还挂着）：新版外壳回合结束后仍留着
        停止按钮，这时输入框里的内容**永远发不出去** —— 用户看到的就是「到抖音图片了就不发送了」。
        现在必须等按钮消失或点掉它，只有输入框真空了才算发出去。
        """
        from backend import chatgpt_cdp as cdp

        page = _FakePage(generating=True, content="抖音 3:4 封面指令", stop_click_unsticks=True)
        box = page.locator("div.ProseMirror")

        with patch.object(
            cdp, "_env_float",
            side_effect=lambda name, default: {
                "H3_CHATGPT_SEND_TIMEOUT": 5.0, "H3_CHATGPT_STUCK_GRACE": 0.0,
            }.get(name, default),
        ):
            ok = cdp._send(page, box, timeout=5.0, what="抖音3x4.png")

        self.assertTrue(ok, "点掉卡住的停止按钮之后应该发得出去")
        self.assertGreaterEqual(page.stop_clicks, 1, "必须点掉那个卡住的停止按钮")
        self.assertEqual(page.content, "", "只有输入框真的空了才算发送成功")

    def test_send_reports_failure_when_the_composer_never_empties(self):
        """发不出去就必须**返回 False**（旧实现会因为页面上有停止按钮而返回 True）。"""
        from backend import chatgpt_cdp as cdp

        page = _FakePage(generating=True, content="发不出去的内容", stop_click_unsticks=False)
        box = page.locator("div.ProseMirror")
        with patch.object(cdp, "_env_float",
                          side_effect=lambda name, default: 0.2 if name == "H3_CHATGPT_SEND_TIMEOUT"
                          else (0.0 if name == "H3_CHATGPT_STUCK_GRACE" else default)):
            ok = cdp._send(page, box, timeout=0.2, what="抖音3x4.png")
        self.assertFalse(ok)
        self.assertEqual(page.content, "发不出去的内容")
        # 源码级硬约束：绝不许把「页面上有停止按钮」当成发送成功
        source = (Path(__file__).resolve().parents[1] / "backend" / "chatgpt_cdp.py").read_text(
            encoding="utf-8")
        self.assertNotIn("if _generating_now(page):\n            return True", source)

    def test_generated_images_are_picked_up_from_the_disk_folder(self):
        """新版外壳把出图写到 `~/.codex/generated_images/<线程>/`，必须能从那里取回。"""
        import time as time_module

        from backend import chatgpt_cdp as cdp

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old = root / "old.png"
            old.write_bytes(b"a" * 60000)
            with patch.object(cdp, "GENERATED_IMAGES_DIR", root):
                before = cdp.snapshot_generated_images()
                since = time_module.time()
                time_module.sleep(0.02)
                fresh = root / "exec-new.png"
                fresh.write_bytes(b"b" * 60000)
                tiny = root / "too-small.png"
                tiny.write_bytes(b"c" * 10)
                found = cdp.new_generated_images(before, since)
            self.assertEqual([Path(p).name for p in found], ["exec-new.png"],
                             "只认发送之后新出现、且体积像真图的文件")

    def test_timeouts_are_relaxed_as_the_user_asked(self):
        """用户 2026-09-17：「发送和等待生成图片的时间可以久一点无所谓」。"""
        from backend import chatgpt_cdp as cdp

        self.assertGreaterEqual(cdp.DEFAULT_SEND_TIMEOUT, 300.0)
        self.assertGreaterEqual(cdp.DEFAULT_UPLOAD_TIMEOUT, 600.0)
        self.assertGreaterEqual(cdp.DEFAULT_IMAGE_TIMEOUT, 900.0)
        source = (Path(__file__).resolve().parents[1] / "scripts" / "make_covers_via_cdp.py").read_text(
            encoding="utf-8")
        self.assertIn("default=1200.0", source)
        self.assertIn('parser.add_argument("--retries"', source)
        self.assertIn("range(max(1, retries + 1))", source)

    def test_generate_uses_drag_drop_and_disk_images(self):
        """`generate()` 的两条新链路要真的接上（源码级断言，避免以后被改回去）。"""
        from backend import chatgpt_cdp as cdp
        import inspect

        source = inspect.getsource(cdp.generate)
        self.assertIn("attach_images(page, images", source)
        self.assertIn("snapshot_generated_images()", source)
        self.assertIn("new_generated_images(", source)
        self.assertIn("wait_until_idle(page", source)
        self.assertNotIn('locator(\'input[type="file"]\').first.set_input_files', source,
                         "挂图要统一走 attach_images（内部才决定 file input 还是拖放）")


class _StubBatchStore:
    """`batch_store` 的最小替身：只记日志与改条目（监听模块用到的两个方法）。"""

    def __init__(self, items: dict[str, dict]):
        self.items = items
        self.logs: list[tuple[str, str, str]] = []

    def add_item_log(self, batch_id, item_id, message):
        self.logs.append((batch_id, item_id, message))
        return {}

    def mutate_item(self, batch_id, item_id, mutate):
        mutate(self.items[item_id])
        return self.items[item_id]


class _StubResponse:
    def __init__(self, status_code: int = 200, text: str = "{}"):
        self.status_code = status_code
        self.text = text


class _StubClient:
    """httpx.AsyncClient 的替身：记下每次 POST 的 URL，按脚本返回状态码。"""

    def __init__(self, codes: list[int] | None = None):
        self.codes = list(codes or [])
        self.calls: list[str] = []

    async def post(self, url, files=None):
        self.calls.append(url)
        code = self.codes.pop(0) if self.codes else 200
        return _StubResponse(code)


class BatchSingleVideoQueueTests(unittest.TestCase):
    """出片必须**看得见的单链路**（2026-09-17 用户：「怎么我第一个视频还没有完成你就开始第二个视频了
    这是不对的 是单链路一个一个视频来生成处理」）。

    真的并发出片并没有发生（runner 只持有一个 `video_task`，提交前还要过
    `_wait_for_free_pipeline`），但旧代码是**先把条目切成「出片中」再去排队等流水线**，
    于是整个等待期间页面上同时有两条「出片中」，第二条其实只是在等 GPU。现在等待期间它
    保持「已确认」，轮到它了才切状态；排队期间被取消的条目**绝不许再提交出片**。
    """

    class _Stub:
        def __init__(self, state: dict):
            self.state = state
            self.logs: list[tuple[str, str]] = []

        def get(self, _batch_id):
            return self.state

        def update(self, _batch_id, **changes):
            self.state.update(changes)
            return self.state

        def mutate_item(self, _batch_id, item_id, mutator):
            for item in self.state["items"]:
                if item["id"] == item_id:
                    mutator(item)
            return self.state

        def set_item_milestone(self, _batch_id, item_id, milestone_id, **changes):
            for item in self.state["items"]:
                if item["id"] == item_id:
                    for milestone in item.get("milestones") or []:
                        if milestone["id"] == milestone_id:
                            milestone.update(changes)
            return self.state

        def add_item_log(self, _batch_id, item_id, message):
            self.logs.append((item_id, message))
            for item in self.state["items"]:
                if item["id"] == item_id:
                    item.setdefault("logs", []).append({"time": "t", "message": message})
            return self.state

    def _state(self) -> dict:
        first = {
            "id": "i1", "index": 1, "kind": "singing", "status": "confirmed", "stage": "confirmed",
            "sourcePath": __file__, "ai": {"reference_image_path": __file__},
            "milestones": [{"id": "video", "status": "pending"}],
        }
        second = dict(first, id="i2", index=2)
        return {"id": "b1", "status": "running", "items": [first, second]}

    def test_a_queued_video_stays_confirmed_until_its_turn(self):
        from backend import batch_worker

        state = self._state()
        stub = self._Stub(state)
        seen: dict = {}

        async def fake_wait(_batch_id, item_id):
            seen["status_while_waiting"] = next(
                item["status"] for item in state["items"] if item["id"] == item_id
            )

        class _Stop(Exception):
            pass

        async def fake_submit(_batch_id, item_id):
            seen["status_when_submitting"] = next(
                item["status"] for item in state["items"] if item["id"] == item_id
            )
            raise _Stop

        async def noop(*_args, **_kwargs):
            return None

        async def scenario():
            with patch.object(batch_worker, "batch_store", stub), \
                 patch.object(batch_worker, "_wait_for_free_pipeline", fake_wait), \
                 patch.object(batch_worker, "_post_video_job", fake_submit), \
                 patch.object(batch_worker, "ensure_publish_images", noop):
                with self.assertRaises(_Stop):
                    await batch_worker._process_confirmed("b1", "i2")

        asyncio.run(scenario())
        self.assertEqual(seen["status_while_waiting"], "confirmed",
                         "排队等流水线时必须还是「已确认」，不能提前显示成「出片中」")
        self.assertEqual(seen["status_when_submitting"], "running",
                         "轮到它了、要提交出片任务了，才切成「出片中」")

    def test_a_video_cancelled_while_queued_is_never_submitted(self):
        """排队期间被「取消 / 跳过」的条目：绝不许还去提交出片任务。"""
        from backend import batch_worker

        state = self._state()
        stub = self._Stub(state)
        submitted: list[str] = []

        async def cancel_while_waiting(_batch_id, item_id):
            for item in state["items"]:
                if item["id"] == item_id:
                    item["status"] = "skipped"
                    item["skipRequested"] = True

        async def fake_submit(_batch_id, item_id):
            submitted.append(item_id)
            return {"id": "job-1"}

        async def noop(*_args, **_kwargs):
            return None

        async def scenario():
            with patch.object(batch_worker, "batch_store", stub), \
                 patch.object(batch_worker, "_wait_for_free_pipeline", cancel_while_waiting), \
                 patch.object(batch_worker, "_post_video_job", fake_submit), \
                 patch.object(batch_worker, "ensure_publish_images", noop):
                await batch_worker._process_confirmed("b1", "i2")

        asyncio.run(scenario())
        self.assertEqual(submitted, [], "取消过的条目不许再提交出片")
        self.assertTrue(any("已被取消/跳过" in message for _, message in stub.logs), stub.logs)

    def test_the_wait_happens_before_the_item_is_marked_running(self):
        """源码级顺序断言：先排队等空，再标「出片中」（别把顺序改回去）。"""
        import inspect

        from backend import batch_worker

        source = inspect.getsource(batch_worker._process_confirmed)
        wait_at = source.index("_wait_for_free_pipeline(batch_id, item_id)")
        mark_at = source.index('status="running", stage="video"')
        self.assertLess(wait_at, mark_at, "必须**先等流水线空出来**再切「出片中」")

    def test_video_gate_covers_generation_through_final_delivery(self):
        """即使误起了两代 runner，第二条也必须等第一条最终交付后才能提交视频任务。"""
        from backend import batch_worker

        state = self._state()
        stub = self._Stub(state)
        first_reached_watch = asyncio.Event()
        release_first = asyncio.Event()
        order: list[str] = []

        async def noop(*_args, **_kwargs):
            return None

        async def fake_submit(_batch_id, item_id):
            order.append(f"submit-{item_id}")
            return {"id": f"job-{item_id}"}

        async def fake_watch(_batch_id, item_id, job_id, _milestone):
            order.append(f"watch-{item_id}")
            if item_id == "i1":
                first_reached_watch.set()
                await release_first.wait()
            return {"id": job_id, "finalOutput": __file__}

        async def fake_deliver(_batch_id, item_id, _video_job):
            order.append(f"deliver-{item_id}")
            return {"folder": f"folder-{item_id}", "videoFinal": __file__}

        async def fake_mark(_batch_id, item_id, _outputs, *, note):
            del note
            order.append(f"complete-{item_id}")
            for item in state["items"]:
                if item["id"] == item_id:
                    item["status"] = "completed"

        async def scenario():
            batch_worker._BATCH_VIDEO_LOCKS.pop("b1", None)
            with patch.object(batch_worker, "batch_store", stub), \
                 patch.object(batch_worker, "_wait_for_free_pipeline", noop), \
                 patch.object(batch_worker, "_post_video_job", fake_submit), \
                 patch.object(batch_worker, "_watch_child", fake_watch), \
                 patch.object(batch_worker, "_deliver", fake_deliver), \
                 patch.object(batch_worker, "_mark_delivered", fake_mark), \
                 patch.object(batch_worker, "ensure_publish_images", noop):
                first = asyncio.create_task(batch_worker._process_confirmed("b1", "i1"))
                await asyncio.wait_for(first_reached_watch.wait(), timeout=2)
                second = asyncio.create_task(batch_worker._process_confirmed("b1", "i2"))
                await asyncio.sleep(0.05)

                self.assertEqual(order, ["submit-i1", "watch-i1"])
                self.assertEqual(state["items"][1]["status"], "confirmed")

                release_first.set()
                await asyncio.wait_for(asyncio.gather(first, second), timeout=2)

        try:
            asyncio.run(scenario())
        finally:
            batch_worker._BATCH_VIDEO_LOCKS.pop("b1", None)

        self.assertLess(order.index("complete-i1"), order.index("submit-i2"), order)
        self.assertEqual(state["items"][0]["status"], "completed")
        self.assertEqual(state["items"][1]["status"], "completed")

    def test_confirming_another_item_does_not_replace_a_live_runner(self):
        """连续确认只把条目放进现有队列，不能换代再启动第二个 runner。"""
        from backend import app as app_module

        state = {"id": "b1", "status": "running", "runnerActive": True}

        class StubStore:
            def update(self, _batch_id, **changes):
                state.update(changes)
                return state

        with patch.object(app_module, "batch_store", StubStore()), \
             patch.object(app_module, "batch_run_alive", return_value=True), \
             patch.object(app_module, "take_over_batch_run") as take_over, \
             patch.object(app_module, "spawn") as spawn:
            result = app_module._resume_batch("b1", "第二条已确认，排队等待。")

        self.assertEqual(result["notice"], "第二条已确认，排队等待。")
        take_over.assert_not_called()
        spawn.assert_not_called()

    def test_action_response_carries_the_zone_fields(self):
        """「重新开始」的响应必须带 `prepReady`：少了它页面会先显示回备料区、过几秒才跳回出片区。

        2026-10-18 用户报的：「以跳过的任务重新开始为什么回调到备料区然后才跳转到出片区」。
        根因是 `_resume_batch()` 返回的是原始 state，`prepReady` 只在 GET 里由
        `_with_publish_chain()` 现算；前端拿到没有这个字段的响应就把条目归到备料区。
        """
        import tempfile
        from pathlib import Path

        from backend import app as app_module
        from backend import batch_worker

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "dance" / "027_跳舞_测试"
            folder.mkdir(parents=True)
            (folder / "人物图.png").write_bytes(b"person")
            (folder / "发布文案.txt").write_text("标题：测试", encoding="utf-8")
            state = {
                "id": "b1",
                "status": "skipped",
                "updatedAt": "2026-10-18T00:00:00+00:00",
                "items": [
                    {
                        "id": "i1", "index": 1, "status": "confirmed", "stage": "confirmed",
                        "kind": "dance", "outputs": {"folder": str(folder)},
                    }
                ],
            }

            class StubStore:
                def update(self, _batch_id, **changes):
                    state.update(changes)
                    return state

            with patch.object(app_module, "batch_store", StubStore()), \
                 patch.object(batch_worker, "BATCH_OUTPUT_ROOT", root), \
                 patch.object(batch_worker, "full_auto_enabled", return_value=True), \
                 patch.object(app_module, "batch_run_alive", return_value=True), \
                 patch.object(app_module, "spawn"):
                result = app_module._resume_batch("b1", "已重试")

        self.assertIn("prepReady", result["items"][0])
        self.assertTrue(result["items"][0]["prepReady"])

    def test_realtime_push_carries_the_zone_fields(self):
        """实时推送同样要带 `prepReady`：状态一变（confirmed → running）前端就不沿用旧值。"""
        import inspect

        from backend import app as app_module

        source = inspect.getsource(app_module.batch_websocket)
        self.assertIn("_with_publish_chain", source)


class CancelGateTests(unittest.TestCase):
    """2026-09-17 事故回归：**被取消的任务绝不许自己复活**。

    用户报的原话：「comfyui在运行 批量制作里的状态却是错误的而且不会同步进度」。
    还原出来的真实链路：批量里点「取消」→ 出片子任务当时正卡在 `pipeline_lock` 上排队，
    取消把状态写成 `cancelled` → 它拿到锁后**无条件**把 `status` 写回 `running`
    （覆盖掉取消）→ 自己把 ComfyUI 拉起来跑了几十分钟；而批次里那一条早就「已跳过」，
    `videoJobId` 又被「重新开始」换成了另一个永远排队的空任务，页面于是盯着一个
    永远不动的进度。下面每条测试钉住这条链路上的一环，改代码前先看它们。
    """

    @staticmethod
    def _job_store(state: dict):
        class _Store:
            def get(self, _job_id):
                return state

            def update(self, _job_id, **changes):
                state.update(changes)
                return state

            def add_log(self, _job_id, message):
                state.setdefault("logs", []).append({"time": "t", "message": message})
                return state

            def set_milestone(self, _job_id, milestone_id, **changes):
                for milestone in state.get("milestones") or []:
                    if milestone["id"] == milestone_id:
                        milestone.update(changes)
                return state

            def mutate(self, _job_id, mutator):
                mutator(state)
                return state

        return _Store()

    def test_a_job_cancelled_while_queued_never_starts_comfyui(self):
        """等锁期间被取消的迁移任务：拿到锁也不许启动 ComfyUI、不许把状态翻回 running。"""
        from backend import pipeline

        state = {
            "id": "j1", "kind": "migrate", "status": "cancelled", "cancelRequested": True,
            "milestones": [{"id": "migrate", "status": "running"}],
        }
        comfy_calls: list[str] = []

        async def fake_ensure(*_args, **_kwargs):
            comfy_calls.append("ensure")

        async def scenario():
            with patch.object(pipeline, "store", self._job_store(state)), \
                 patch.object(pipeline, "pipeline_lock", asyncio.Lock()), \
                 patch.object(pipeline.resources, "ensure_comfy", new=fake_ensure):
                await pipeline.run_migrate_pipeline("j1")

        asyncio.run(scenario())
        self.assertEqual(comfy_calls, [], "被取消的迁移任务绝不许再去启动 ComfyUI")
        self.assertEqual(state["status"], "cancelled", "取消状态不许被入口覆盖成 running")
        self.assertEqual(state["stage"], "cancelled")

    def test_a_live_job_still_enters_the_migrate_pipeline(self):
        """反向断言：没被取消的任务照旧开跑（别把闸门修成一律拒绝）。"""
        from backend import pipeline

        state = {"id": "j2", "kind": "migrate", "status": "queued",
                 "milestones": [{"id": "migrate", "status": "pending"}]}
        seen: list[str] = []

        async def fake_ensure(*_args, **_kwargs):
            seen.append("ensure")
            raise pipeline.PipelineError("停在这里", "测试：不再往后跑工作流")

        async def fake_health():
            return None

        async def scenario():
            with patch.object(pipeline, "store", self._job_store(state)), \
                 patch.object(pipeline, "pipeline_lock", asyncio.Lock()), \
                 patch.object(pipeline, "comfy_health", new=fake_health), \
                 patch.object(pipeline.resources, "ensure_comfy", new=fake_ensure):
                await pipeline.run_migrate_pipeline("j2")

        asyncio.run(scenario())
        self.assertEqual(seen, ["ensure"], "没被取消的任务必须真的进入链路")
        self.assertEqual(state["status"], "failed", "链路内抛错照旧按失败收尾")

    def test_a_cancel_stamp_survives_a_status_overwrite(self):
        """取消印记 `cancelRequested` 必须压过被覆盖回来的 running 状态。"""
        from backend import pipeline

        state = {"id": "j3", "status": "running", "cancelRequested": True}
        with patch.object(pipeline, "store", self._job_store(state)):
            self.assertTrue(pipeline.is_job_cancelled("j3"),
                            "状态被翻回 running 也要认得出来这是已取消的任务")

    def test_every_pipeline_entry_walks_through_the_cancel_gate(self):
        """源码级断言：所有入口都要过 `claim_job_run`（`run_migrate_pipeline` 就栽在这）。"""
        import inspect

        from backend import pipeline

        for name in (
            "run_pipeline", "run_migrate_pipeline", "run_upscale_job", "run_rvc_job",
            "resume_singing_pipeline", "resume_migrate_pipeline",
        ):
            source = inspect.getsource(getattr(pipeline, name))
            self.assertIn("claim_job_run", source, f"{name} 必须过取消闸门 claim_job_run")

    def test_skipping_a_running_item_cancels_its_child_job(self):
        """跳过一条正在出片的条目：必须**真的取消子任务**，不能只写个标记。"""
        from backend import app as app_module

        item = {"id": "i1", "status": "running", "videoJobId": "job-1",
                "childJob": {"id": "job-1"}, "milestones": [{"id": "video", "status": "running"}]}
        state = {"id": "b1", "items": [item], "status": "running"}
        cancelled: list[str] = []

        class _BatchStore:
            def get(self, _batch_id):
                return state

            def mutate_item(self, _batch_id, _item_id, mutator):
                mutator(item)
                return state

            def update(self, _batch_id, **changes):
                state.update(changes)
                return state

        async def fake_cancel(job_id):
            cancelled.append(job_id)
            return {}

        async def scenario():
            with patch.object(app_module, "_batch_item_or_404", return_value=item), \
                 patch.object(app_module, "batch_store", _BatchStore()), \
                 patch.object(app_module, "cancel_item_work", return_value=False), \
                 patch.object(app_module, "cancel_job", new=fake_cancel), \
                 patch.object(app_module, "_wake_batch", lambda *_a, **_k: state):
                await app_module.skip_batch_item("b1", "i1")

        asyncio.run(scenario())
        self.assertEqual(cancelled, ["job-1"], "跳过正在出片的一条，必须真的取消子任务")
        self.assertTrue(item["skipRequested"])

    def test_restarting_an_item_cancels_a_leftover_child_job(self):
        """「重新开始」前必须掐掉上一轮还活着的子任务，否则它会变成烧显卡的孤儿。"""
        from backend import batch_worker

        item = {"id": "i1", "status": "skipped", "videoJobId": "job-9",
                "childJob": {"id": "job-9"}, "outputs": {},
                "milestones": [{"id": "video", "status": "running"}]}
        state = {"id": "b1", "items": [item]}
        cancelled: list[str] = []

        class _BatchStore:
            def get(self, _batch_id):
                return state

            def mutate_item(self, _batch_id, _item_id, mutator):
                mutator(item)
                return state

        async def fake_cancel(child_id, _batch_id, _item_id):
            cancelled.append(child_id)

        async def scenario():
            with patch.object(batch_worker, "batch_store", _BatchStore()), \
                 patch.object(batch_worker.store, "get", lambda _job_id: {"status": "running"}), \
                 patch.object(batch_worker, "_cancel_child_job_quietly", new=fake_cancel):
                batch_worker.restart_item_to_run("b1", "i1")
                await asyncio.sleep(0.05)

        asyncio.run(scenario())
        self.assertEqual(cancelled, ["job-9"], "重新开始前必须掐掉还活着的旧子任务")
        self.assertIsNone(item["videoJobId"], "重置照旧清掉子任务指针")

    def test_post_video_job_refuses_after_the_item_was_skipped(self):
        """等流水线期间被跳过的条目：绝不许再提交出片任务。"""
        from backend import batch_worker

        item = {"id": "i1", "index": 1, "kind": "dance", "status": "skipped",
                "skipRequested": True, "ratio": "9:16",
                "sourcePath": __file__, "ai": {"reference_image_path": __file__}}
        state = {"id": "b1", "items": [item]}
        posted: list[str] = []

        class _BatchStore:
            def get(self, _batch_id):
                return state

            def mutate_item(self, _batch_id, _item_id, mutator):
                mutator(item)
                return state

            def add_item_log(self, _batch_id, _item_id, message):
                item.setdefault("logs", []).append({"time": "t", "message": message})
                return state

        async def fake_wait(*_args, **_kwargs):
            return None

        async def scenario():
            with patch.object(batch_worker, "batch_store", _BatchStore()), \
                 patch.object(batch_worker, "_wait_for_free_pipeline", fake_wait):
                with self.assertRaises(asyncio.CancelledError):
                    await batch_worker._post_video_job("b1", "i1")
                posted.append("done")

        asyncio.run(scenario())
        self.assertEqual(posted, ["done"], "被跳过的条目必须当场中止提交")
        self.assertTrue(
            any("已被取消/跳过" in entry["message"] for entry in item.get("logs") or []),
            item.get("logs"),
        )

    def test_terminal_items_lose_their_running_milestone(self):
        """已结束的条目上不许留着「进行中」的里程碑（否则那一步永远转圈）。"""
        from backend import batch_worker

        state = {"id": "b1", "items": [
            {"id": "i1", "status": "skipped", "milestones": [{"id": "video", "status": "running"}]},
            {"id": "i2", "status": "deleted", "milestones": [{"id": "video", "status": "running"}]},
            {"id": "i3", "status": "running", "milestones": [{"id": "video", "status": "running"}]},
            {"id": "i4", "status": "awaiting_review", "milestones": [{"id": "video", "status": "pending"}]},
        ]}
        written: list[tuple[str, str]] = []

        class _BatchStore:
            def set_item_milestone(self, _batch_id, item_id, milestone_id, **changes):
                written.append((item_id, changes["status"]))
                return state

        with patch.object(batch_worker, "batch_store", _BatchStore()):
            changed = batch_worker.heal_terminal_item_milestones(state)

        self.assertTrue(changed)
        self.assertEqual(written, [("i1", "skipped"), ("i2", "skipped")],
                         "只收尾已结束的条目，正在跑 / 等确认的一律不碰")


class PortraitAdoptionTests(unittest.TestCase):
    """「谁写图谁登记」：`/images` 出图或上传成功后**当场**把人物图登记成条目的候选图。

    2026-09-20 用户选「乙」：删掉 `backend/publish_watcher.py`（每 5 秒扫发布目录的中间人），
    由写图的那一方直接登记。规则沿用 watcher 当年的口径（同一张图只驱动一次；只碰
    `awaiting_review` / `confirmed` / `failed`），这里逐条钉住。
    """

    AWEME = "7685308023212056420"

    def _fixture(
        self,
        root: Path,
        *,
        status: str = "awaiting_review",
        candidate: bytes | None = b"old-candidate",
        marker_sha: str = "",
        aweme: str = "",
        source: bytes | None = None,
    ) -> tuple:
        """真批次（临时库）+ 一个发布目录 → (store, batch_id, item_id, folder, old_candidate)。"""
        from backend import batch_store as batch_store_module
        from backend.batch_store import BatchStore

        folder = root / "发布成品" / "dance" / f"001_跳舞_测试标题_{aweme or self.AWEME}"
        folder.mkdir(parents=True, exist_ok=True)
        old_candidate: Path | None = None
        ai: dict = {"title": "测试标题"}
        if candidate is not None:
            old_candidate = root / "data" / "batches" / "b1" / "it1" / "candidate.png"
            old_candidate.parent.mkdir(parents=True, exist_ok=True)
            old_candidate.write_bytes(candidate)
            ai["reference_image_path"] = str(old_candidate)
        if marker_sha:
            ai["portrait_watch"] = {"sha": marker_sha, "at": "2026-09-20T00:00:00"}
        state = new_batch_state([], ["https://v.douyin.com/x"])
        item = state["items"][0]
        updates: dict = {
            "status": status,
            "awemeId": aweme or self.AWEME,
            "ai": ai,
            "outputs": {"folder": str(folder)},
        }
        if source is not None:
            source_path = root / "source.mp4"
            source_path.write_bytes(source)
            updates["sourcePath"] = str(source_path)
        item.update(**updates)
        state["status"] = "running"

        original = batch_store_module.DB_PATH
        batch_store_module.DB_PATH = root / "queue.db"
        store = BatchStore()
        store.create(state)
        self.addCleanup(setattr, batch_store_module, "DB_PATH", original)
        return store, state["id"], item["id"], folder, old_candidate

    def _portrait(self, folder: Path, payload: bytes) -> Path:
        path = folder / "人物图.png"
        path.write_bytes(payload)
        return path

    @staticmethod
    def _sha(payload: bytes) -> str:
        import hashlib

        return hashlib.sha256(payload).hexdigest()

    def test_new_portrait_is_registered_and_releases_the_review_point(self) -> None:
        """审核点 + 目录里出现新图 → 登记成候选图 + 自动放行（等同点「确认并出片」）。"""
        from backend import batch_worker

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            store, batch_id, item_id, publish, _ = self._fixture(root)
            image = self._portrait(publish, b"user-portrait-A")

            with patch.object(batch_worker, "batch_store", store), patch.object(
                batch_worker, "DATA_DIR", root / "data"
            ):
                outcome = batch_worker.adopt_published_portrait(publish)

            self.assertIsNotNone(outcome)
            self.assertEqual(outcome["action"], "confirm")
            self.assertTrue(outcome["needsWake"])
            row = next(it for it in store.get(batch_id)["items"] if it["id"] == item_id)
            self.assertEqual(row["status"], "confirmed")
            self.assertTrue(row["reviewApproved"])
            adopted = Path(row["ai"]["reference_image_path"])
            self.assertTrue(adopted.is_file(), "应当把发布目录那张复制成条目候选图")
            self.assertEqual(adopted.read_bytes(), image.read_bytes())
            self.assertEqual(row["ai"]["portrait_watch"]["sha"], self._sha(b"user-portrait-A"))
            self.assertTrue(
                any("候选人物图" in log["message"] for log in row.get("logs") or []),
                row.get("logs"),
            )

    def test_the_same_image_never_drives_the_item_twice(self) -> None:
        """同一张图只驱动一次（重启后也不会重复跑）—— watcher 当年踩过的坑。"""
        from backend import batch_worker

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            payload = b"user-portrait-A"
            store, batch_id, item_id, publish, _ = self._fixture(root)
            self._portrait(publish, payload)

            with patch.object(batch_worker, "batch_store", store), patch.object(
                batch_worker, "DATA_DIR", root / "data"
            ):
                first = batch_worker.adopt_published_portrait(publish)
                second = batch_worker.adopt_published_portrait(publish)

            self.assertIsNotNone(first)
            self.assertIsNone(second, "同一张图不许驱动第二次")
            row = next(it for it in store.get(batch_id)["items"] if it["id"] == item_id)
            self.assertEqual(row["ai"]["portrait_watch"]["sha"], self._sha(payload))

    def test_confirmed_item_only_gets_the_new_image(self) -> None:
        """已经放行（confirmed）的条目：只换候选图、**不重复确认**。"""
        from backend import batch_worker

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            store, batch_id, item_id, publish, _ = self._fixture(root, status="confirmed")
            self._portrait(publish, b"user-portrait-B")

            with patch.object(batch_worker, "batch_store", store), patch.object(
                batch_worker, "DATA_DIR", root / "data"
            ):
                outcome = batch_worker.adopt_published_portrait(publish)

            self.assertEqual(outcome["action"], "upload")
            self.assertTrue(outcome["needsWake"], "换图后要唤醒备料区补封面")
            row = next(it for it in store.get(batch_id)["items"] if it["id"] == item_id)
            self.assertEqual(row["status"], "confirmed")
            self.assertEqual(Path(row["ai"]["reference_image_path"]).read_bytes(), b"user-portrait-B")

    def test_failed_item_is_rerun_with_the_new_image(self) -> None:
        """失败的条目：挂上新图并把这一条重新排上（源视频还在时直接续跑出片）。"""
        from backend import batch_worker

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            store, batch_id, item_id, publish, _ = self._fixture(
                root, status="failed", source=b"fake-source"
            )
            self._portrait(publish, b"user-portrait-C")
            pending = root / "data" / "batches" / batch_id / item_id / "candidate_r0_attempt1.png.request-pending"
            pending.parent.mkdir(parents=True, exist_ok=True)
            pending.write_text("submitted", encoding="utf-8")

            with patch.object(batch_worker, "batch_store", store), patch.object(
                batch_worker, "DATA_DIR", root / "data"
            ):
                outcome = batch_worker.adopt_published_portrait(publish)

            self.assertEqual(outcome["action"], "retry")
            self.assertTrue(outcome["needsWake"])
            row = next(it for it in store.get(batch_id)["items"] if it["id"] == item_id)
            self.assertNotEqual(row["status"], "failed")
            self.assertEqual(Path(row["ai"]["reference_image_path"]).read_bytes(), b"user-portrait-C")
            self.assertTrue(pending.exists())

    def test_last_manual_cover_releases_failed_item_without_new_image_request(self) -> None:
        from backend import batch_worker

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            store, batch_id, item_id, publish, _ = self._fixture(
                root, status="failed", source=b"fake-source", candidate=b"user-portrait"
            )
            self._portrait(publish, b"user-portrait")
            (publish / "发布文案.txt").write_text("标题：测试", encoding="utf-8")
            (publish / "B站4x3.png").write_bytes(b"cover")
            pending = root / "data" / "batches" / batch_id / item_id / "candidate_r0_attempt1.png.request-pending"
            pending.parent.mkdir(parents=True, exist_ok=True)
            pending.write_text("submitted", encoding="utf-8")
            with patch.object(batch_worker, "batch_store", store), patch.object(
                batch_worker, "DATA_DIR", root / "data"
            ), patch.object(
                batch_worker, "BATCH_OUTPUT_ROOT", root / "发布成品"
            ):
                self.assertIsNone(batch_worker.resume_failed_item_with_complete_images(publish))
                (publish / "抖音3x4.png").write_bytes(b"cover")
                outcome = batch_worker.resume_failed_item_with_complete_images(publish)
            self.assertEqual(outcome, {"batchId": batch_id, "itemId": item_id})
            self.assertEqual(store.get(batch_id)["items"][0]["status"], "confirmed")
            self.assertTrue(pending.exists())

    def test_busy_and_unprepared_items_are_never_touched(self) -> None:
        """pending / revising / running 一律不碰（正在跑的活换图会打架）。

        ⚠️ 2026-09-20 起 `skipped` / `completed` **不再**属于这一类：用户要求「我替换之后
        你需要自动帮我换成我替换的图片」，跑完/跳过的条目换了人图也要真的用上（见下一条）。
        """
        from backend import batch_worker

        for status in ("pending", "revising", "running"):
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
                root = Path(folder)
                store, batch_id, item_id, publish, old = self._fixture(root, status=status)
                self._portrait(publish, b"user-portrait-D")

                with patch.object(batch_worker, "batch_store", store), patch.object(
                    batch_worker, "DATA_DIR", root / "data"
                ):
                    outcome = batch_worker.adopt_published_portrait(publish)

                self.assertIsNone(outcome, status)
                row = next(it for it in store.get(batch_id)["items"] if it["id"] == item_id)
                self.assertEqual(row["status"], status)
                self.assertEqual(Path(row["ai"]["reference_image_path"]), old)

    def test_finished_item_swaps_the_portrait_and_reruns(self) -> None:
        """已跳过 / 已出片的条目换人物图 → 换成新图 + 这一条重新排出片。

        用户 2026-09-20：「我替换之后你需要自动帮我换成我替换的图片」——以前这两类状态
        什么都不做，"替换"就只改了发布目录里那个文件，成片还是旧图。
        """
        from backend import batch_worker

        for status in ("skipped", "completed"):
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
                root = Path(folder)
                store, batch_id, item_id, publish, _ = self._fixture(
                    root, status=status, source=b"fake-source"
                )
                self._portrait(publish, b"user-portrait-E")

                with patch.object(batch_worker, "batch_store", store), patch.object(
                    batch_worker, "DATA_DIR", root / "data"
                ), patch.object(batch_worker, "spawn_api_covers", lambda *a, **k: False):
                    outcome = batch_worker.adopt_published_portrait(publish)

                self.assertIsNotNone(outcome, status)
                self.assertEqual(outcome["action"], "rerun")
                self.assertTrue(outcome["needsWake"])
                row = next(it for it in store.get(batch_id)["items"] if it["id"] == item_id)
                # 源视频还在 → 直接续跑出片（confirmed），而不是退回备料
                self.assertEqual(row["status"], "confirmed")
                self.assertEqual(
                    Path(row["ai"]["reference_image_path"]).read_bytes(), b"user-portrait-E"
                )
                self.assertEqual(row["ai"]["portrait_watch"]["sha"], self._sha(b"user-portrait-E"))

    def test_swapping_the_portrait_moves_old_covers_back_to_prep(self) -> None:
        """换图后旧封面失效，runner 补缺；不另起可能与出片并发的收费任务。"""
        from backend import batch_worker

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            store, batch_id, item_id, publish, _ = self._fixture(root, status="completed")
            self._portrait(publish, b"user-portrait-F")
            for name in ("B站4x3.png", "抖音3x4.png"):
                (publish / name).write_bytes(b"old-cover")

            with patch.object(batch_worker, "batch_store", store), patch.object(
                batch_worker, "DATA_DIR", root / "data"
            ), patch.object(batch_worker, "restart_item_to_run", lambda *a: True):
                outcome = batch_worker.adopt_published_portrait(publish)

            self.assertTrue(outcome["needsWake"])
            self.assertFalse((publish / "B站4x3.png").exists())
            self.assertFalse((publish / "抖音3x4.png").exists())
            archived = list((publish / ".previous-covers").rglob("*.png"))
            self.assertEqual(len(archived), 2)
            self.assertTrue(all(path.read_bytes() == b"old-cover" for path in archived))

    def test_fallback_frame_candidate_is_left_alone(self) -> None:
        """目录里那张就是备料留下的**兜底取帧图**（sha 与候选图相同）→ 不算你的成图，不动。"""
        from backend import batch_worker

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            frame = root / "data" / "batches" / "b1" / "it1" / "candidate_r0_frame.png"
            store, batch_id, item_id, publish, _ = self._fixture(root, candidate=None)
            frame.parent.mkdir(parents=True, exist_ok=True)
            frame.write_bytes(b"fallback-frame")
            with patch.object(batch_worker, "batch_store", store):
                store.mutate_item(
                    batch_id, item_id,
                    lambda row: row["ai"].update(reference_image_path=str(frame)),
                )
            self._portrait(publish, b"fallback-frame")       # 与候选图 sha 相同

            with patch.object(batch_worker, "batch_store", store), patch.object(
                batch_worker, "DATA_DIR", root / "data"
            ):
                outcome = batch_worker.adopt_published_portrait(publish)

            self.assertIsNone(outcome)
            row = next(it for it in store.get(batch_id)["items"] if it["id"] == item_id)
            self.assertEqual(row["status"], "awaiting_review")

    def test_folder_without_a_matching_item_is_ignored(self) -> None:
        """匹配不到条目（历史目录 / 别的作品号）→ 什么都不做。"""
        from backend import batch_worker

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            root = Path(folder)
            store, batch_id, _, publish, _ = self._fixture(root)
            stranger = root / "发布成品" / "dance" / "009_跳舞_别人的作品_1111111111111111111"
            stranger.mkdir(parents=True, exist_ok=True)
            self._portrait(stranger, b"someone-else")

            with patch.object(batch_worker, "batch_store", store), patch.object(
                batch_worker, "DATA_DIR", root / "data"
            ):
                self.assertIsNone(batch_worker.adopt_published_portrait(stranger))
                # 没有人物图的目录同样什么都不做
                empty = root / "发布成品" / "dance" / f"002_跳舞_空的_{self.AWEME}"
                empty.mkdir(parents=True, exist_ok=True)
                self.assertIsNone(batch_worker.adopt_published_portrait(empty))

    def test_the_watcher_is_gone_and_images_registers_instead(self) -> None:
        """源码级：watcher 模块与三个接口都没了；登记改在 `/images` 写图那一处。"""
        root = Path(__file__).resolve().parents[1]
        self.assertFalse((root / "backend" / "publish_watcher.py").exists())
        app_source = (root / "backend" / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("publish_watcher", app_source)
        self.assertNotIn("/api/watch/publish-portrait", app_source)
        workbench = (root / "backend" / "image_workbench.py").read_text(encoding="utf-8")
        self.assertIn("_register_published_portrait(folder)", workbench)
        self.assertIn("adopt_published_portrait", workbench)


class SceneFramePickerTests(unittest.TestCase):
    """抽帧挑图（2026-09-20 用户：「要不要我再加一道 要」）。

    背景：`_frame_detail_score` 只看**整幅**清晰度，跳舞时整幅清楚、两只手糊成一片的帧
    照样会赢，模型再把那份运动模糊照抄进人物图（013 实测）。所以备料这条路在打分之上
    再让视觉模型挑「手最清楚」的一张；模型失败 / 挑不出来 / 序号越界都退回分数最高的那张。
    """

    @staticmethod
    def _frame(color: str, size: tuple[int, int] = (60, 90)) -> bytes:
        import io

        from PIL import Image

        output = io.BytesIO()
        Image.new("RGB", size, color).save(output, format="PNG")
        return output.getvalue()

    def test_the_models_choice_beats_the_global_sharpness_score(self) -> None:
        """模型挑中的那一张必须真的被写成 `scene-frame.jpg`（哪怕它不是打分最高的）。"""
        chosen = self._frame("#ff0000", (40, 40))
        payloads = [self._frame("#101010"), self._frame("#808080"), chosen]

        async def pick(_frames):
            return 3

        written = _run_with(self, pick, [0.1, 0.9, 0.5], payloads)
        self.assertEqual(written, chosen)

    def test_a_failed_picker_falls_back_to_the_sharpest_frame(self) -> None:
        """判据失败 → 退回整幅最清晰的那张（第 2 张，0.9），绝不抛错、绝不卡住备料。"""
        payloads = [self._frame("#101010"), self._frame("#808080"), self._frame("#f0f0f0")]

        async def pick(_frames):
            raise RuntimeError("端点挂了")

        written = _run_with(self, pick, [0.1, 0.9, 0.5], payloads)
        self.assertEqual(written, payloads[1])

    def test_none_and_out_of_range_index_fall_back(self) -> None:
        for answer in (None, 0, 9):
            payloads = [self._frame("#101010"), self._frame("#808080"), self._frame("#f0f0f0")]

            async def pick(_frames, value=answer):
                return value

            written = _run_with(self, pick, [0.1, 0.9, 0.5], payloads)
            self.assertEqual(written, payloads[1], answer)

    def test_the_sheet_numbers_the_candidates_from_left_to_right(self) -> None:
        """对比图必须按候选顺序横排、宽度随张数增长（模型靠"从左到右"数序号）。"""
        import io

        from PIL import Image

        from backend import batch_ai

        one = batch_ai.frame_candidates_sheet([self._frame("#101010")])
        three = batch_ai.frame_candidates_sheet(
            [self._frame("#101010"), self._frame("#808080"), self._frame("#f0f0f0")]
        )
        with Image.open(io.BytesIO(one)) as small, Image.open(io.BytesIO(three)) as wide:
            self.assertEqual(small.height, wide.height)
            self.assertGreater(wide.width, small.width * 2)


class FrameCandidateBreadthTests(unittest.TestCase):
    """抽帧改成「全帧扫描、取评分最高」（2026-10-18 用户：去掉首帧规则、全部帧抽出来）。"""

    @staticmethod
    def _frame(color: str, size: tuple[int, int] = (60, 90)) -> bytes:
        import io

        from PIL import Image

        output = io.BytesIO()
        Image.new("RGB", size, color).save(output, format="PNG")
        return output.getvalue()

    def test_full_frame_scan_picks_the_highest_score(self) -> None:
        from backend import batch_worker

        scan = [(10.0, b"low"), (31.5, b"best"), (22.0, b"mid")]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            video = root / "source.mp4"
            video.write_bytes(b"fake")
            target = root / "scene-frame.jpg"
            with patch.object(batch_worker, "_scan_all_frames", lambda _s, _d, _st: scan), patch.object(
                batch_worker, "_video_duration_seconds", lambda _p: 10.0
            ):
                result = asyncio.run(batch_worker.extract_scene_frame_with_hands(video, target))
            self.assertEqual(result.read_bytes(), b"best")

    def test_a_failed_scan_falls_back_to_sampling(self) -> None:
        """视频损坏/ffmpeg 报错时不能卡住备料，退回按时刻采样。"""
        from backend import batch_worker

        frames: list[bytes] = []

        def extract(*_args, **_kwargs):
            frame = self._frame(f"#{len(frames):02x}1020", (60, 90))
            frames.append(frame)
            return frame

        scores = [float(index) for index in range(200)]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            video = root / "source.mp4"
            video.write_bytes(b"fake")
            target = root / "scene-frame.jpg"
            with patch.object(batch_worker, "_scan_all_frames", lambda _s, _d, _st: None), patch.object(
                batch_worker, "_extract_frame_bytes", extract
            ), patch.object(
                batch_worker, "_video_duration_seconds", lambda _p: 10.0
            ), patch.object(batch_worker, "_frame_detail_score", lambda data: scores.pop(0)):
                result = asyncio.run(batch_worker.extract_scene_frame_with_hands(video, target))
            self.assertGreaterEqual(len(frames), 20)          # 退回采样也要够多
            self.assertEqual(result.read_bytes(), frames[-1])

    def test_scan_stride_keeps_long_videos_affordable(self) -> None:
        from backend import batch_worker

        self.assertEqual(batch_worker._frame_scan_stride(10.0), 1)    # 040 这类 10 秒片全扫
        self.assertEqual(batch_worker._frame_scan_stride(40.0), 1)    # 跳舞上限也全扫
        self.assertEqual(batch_worker._frame_scan_stride(60.0), 2)
        self.assertEqual(batch_worker._frame_scan_stride(180.0), 3)

    def test_the_first_frame_rule_is_gone(self) -> None:
        from backend import batch_worker

        for name in ("first_frame_preferred", "_first_frame_with_person", "_use_first_frame"):
            self.assertFalse(hasattr(batch_worker, name), f"{name} 应该已经删掉")


def _run_with(case: SceneFramePickerTests, pick, scores, payloads) -> bytes:
    """跑一次 `extract_scene_frame_with_hands()`：抽帧与打分都是桩，模型挑选用 `pick` 替换。"""
    from backend import batch_ai, batch_worker

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        video = root / "source.mp4"
        video.write_bytes(b"fake")
        target = root / "scene-frame.jpg"
        queue = list(payloads)
        with patch.dict(os.environ, {"H3_BATCH_AI_FRAME_PICK": "1"}, clear=False), patch.object(
            batch_worker, "_video_duration_seconds", lambda _p: 10.0
        ), patch.object(
            batch_worker, "_scan_all_frames", lambda _s, _d, _st: None,   # 关掉全帧扫描，测候选打分/挑图
        ), patch.object(
            batch_worker, "_extract_frame_bytes",
            lambda *a, **k: queue.pop(0) if queue else None,   # 抽完就当成抽不到帧
        ), patch.object(batch_worker, "_frame_detail_score", lambda data: scores.pop(0)), patch.object(
            batch_ai, "choose_sharp_hands_frame", pick
        ):
            return asyncio.run(
                batch_worker.extract_scene_frame_with_hands(video, target)
            ).read_bytes()


class RvcBatchTests(unittest.TestCase):
    """一键批量转换音色（2026-09-20 step 5）：`最终成片.mp4` → `最终成片_音色版.mp4`。

    链条是用户定的：原声母版 → 音色版（本入口，**并存**）→ 字幕版（发布版）。
    判据与字幕批量同源：扫发布目录、已存在的不重复、单条失败不阻断、暂停在两条之间生效。
    """

    def _publish(self, root: Path, *, kind: str, number: int, label: str,
                 clean: bool = True, voice: bool = False) -> Path:
        folder = root / "发布成品" / kind / f"{number:03d}_{label}"
        folder.mkdir(parents=True, exist_ok=True)
        if clean:
            (folder / "最终成片.mp4").write_bytes(b"clean-video")
        if voice:
            (folder / "最终成片_音色版.mp4").write_bytes(b"already-converted")
        return folder

    @contextmanager
    def _temp_jobs(self, folder: Path):
        from backend import store as store_module

        original = store_module.DB_PATH
        store_module.DB_PATH = Path(folder) / "jobs.db"
        store_module.store._init_db()
        try:
            yield store_module.store
        finally:
            store_module.DB_PATH = original
            store_module.store._init_db()

    def _batch_job(self, job_store, queue: list[dict]) -> str:
        import uuid

        from backend.rvc_batch import BATCH_MILESTONES

        job_id = uuid.uuid4().hex
        created = now_iso()
        job_store.create({
            "id": job_id, "kind": "rvc_batch", "status": "queued", "stage": "voice",
            "createdAt": created, "updatedAt": created,
            "sourceName": f"发布成品批量（{len(queue)} 条）", "rvcQueue": queue,
            "rvcIndex": 0, "rvcDone": 0, "rvcFailed": 0, "rvcStarted": False,
            "milestones": BATCH_MILESTONES, "logs": [],
        })
        return job_id

    def test_scan_only_lists_singing_videos_missing_the_voice_version(self) -> None:
        """只扫 `singing/`：有母版、没音色版的才排队；已有音色版 / 没有母版 / 跳舞目录都跳过。"""
        from backend import rvc_batch

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            self._publish(root, kind="singing", number=1, label="唱歌_待转换")
            self._publish(root, kind="singing", number=2, label="唱歌_已有音色版", voice=True)
            self._publish(root, kind="singing", number=3, label="唱歌_没母版", clean=False)
            self._publish(root, kind="dance", number=1, label="跳舞_不动")

            with patch.object(rvc_batch, "BATCH_OUTPUT_ROOT", root / "发布成品"), patch.object(
                rvc_batch, "_find_item_for_folder", lambda _folder: None
            ):
                queue = rvc_batch.scan_singing_videos_missing_voice()

            self.assertEqual([row["label"] for row in queue], ["001_唱歌_待转换"])
            self.assertTrue(queue[0]["source"].endswith("最终成片.mp4"))
            self.assertTrue(queue[0]["out"].endswith("最终成片_音色版.mp4"))

    def test_batch_writes_the_voice_version_beside_the_master(self) -> None:
        """逐条转换，产物落回各自发布目录（**原声母版并存**），结束后任务标完成。"""
        from backend import rvc_batch

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            folder = self._publish(root, kind="singing", number=1, label="唱歌_待转换")
            with self._temp_jobs(root) as job_store, patch.object(
                rvc_batch, "BATCH_OUTPUT_ROOT", root / "发布成品"
            ), patch.object(rvc_batch, "DATA_DIR", root / "data"), patch.object(
                rvc_batch, "_find_item_for_folder", lambda _folder: None
            ), patch.object(rvc_batch, "_batch_busy", lambda: ""), patch.object(
                rvc_batch, "comfy_health", _async_false
            ), patch.object(rvc_batch.resources, "stop_comfy", _async_none), patch.object(
                rvc_batch, "run_rvc", _fake_converter
            ):
                job_id = self._batch_job(job_store, [])
                asyncio.run(rvc_batch.run_rvc_publish_batch(job_id))
                state = job_store.get(job_id)

            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["rvcDone"], 1)
            self.assertEqual(state["rvcFailed"], 0)
            self.assertTrue((folder / "最终成片_音色版.mp4").is_file())
            self.assertEqual((folder / "最终成片_音色版.mp4").read_bytes(), b"converted-voice")
            # 原声母版**并存**，一个字都没被改
            self.assertEqual((folder / "最终成片.mp4").read_bytes(), b"clean-video")

    def test_pause_takes_effect_between_two_items(self) -> None:
        """暂停在**下一条开始之前**生效：第一条转完就停，第二条原样留着等「继续」。"""
        from backend import rvc_batch

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            first = self._publish(root, kind="singing", number=1, label="唱歌_一")
            second = self._publish(root, kind="singing", number=2, label="唱歌_二")
            box: dict = {}

            async def converter_then_pause(job_id, staged, audio_from=None):
                out = staged.with_name(f"{staged.stem}_mogoo_v1.mp4")
                out.write_bytes(b"converted-voice")
                # 第一条跑完时用户点了暂停
                from backend.store import store as job_store

                job_store.update(job_id, pauseRequested=True)
                return out

            with self._temp_jobs(root) as job_store, patch.object(
                rvc_batch, "BATCH_OUTPUT_ROOT", root / "发布成品"
            ), patch.object(rvc_batch, "DATA_DIR", root / "data"), patch.object(
                rvc_batch, "_find_item_for_folder", lambda _folder: None
            ), patch.object(rvc_batch, "_batch_busy", lambda: ""), patch.object(
                rvc_batch, "comfy_health", _async_false
            ), patch.object(rvc_batch.resources, "stop_comfy", _async_none), patch.object(
                rvc_batch, "run_rvc", converter_then_pause
            ):
                job_id = self._batch_job(job_store, [])
                asyncio.run(rvc_batch.run_rvc_publish_batch(job_id))
                state = job_store.get(job_id)
                box["state"] = state

            self.assertEqual(state["status"], "paused")
            self.assertEqual(state["rvcDone"], 1)
            self.assertTrue((first / "最终成片_音色版.mp4").is_file())
            self.assertFalse((second / "最终成片_音色版.mp4").exists(), "暂停后不许再动第二条")

    def test_one_failure_does_not_stop_the_batch(self) -> None:
        """单条失败只记日志、继续下一条（与字幕批量同一条纪律）。"""
        from backend import rvc_batch

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            first = self._publish(root, kind="singing", number=1, label="唱歌_会失败")
            second = self._publish(root, kind="singing", number=2, label="唱歌_会成功")
            calls = {"n": 0}

            async def flaky(job_id, staged, audio_from=None):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("RVC 崩了")
                out = staged.with_name(f"{staged.stem}_mogoo_v1.mp4")
                out.write_bytes(b"converted-voice")
                return out

            with self._temp_jobs(root) as job_store, patch.object(
                rvc_batch, "BATCH_OUTPUT_ROOT", root / "发布成品"
            ), patch.object(rvc_batch, "DATA_DIR", root / "data"), patch.object(
                rvc_batch, "_find_item_for_folder", lambda _folder: None
            ), patch.object(rvc_batch, "_batch_busy", lambda: ""), patch.object(
                rvc_batch, "comfy_health", _async_false
            ), patch.object(rvc_batch.resources, "stop_comfy", _async_none), patch.object(
                rvc_batch, "run_rvc", flaky
            ):
                job_id = self._batch_job(job_store, [])
                asyncio.run(rvc_batch.run_rvc_publish_batch(job_id))
                state = job_store.get(job_id)

            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["rvcDone"], 1)
            self.assertEqual(state["rvcFailed"], 1)
            self.assertFalse((first / "最终成片_音色版.mp4").exists())
            self.assertTrue((second / "最终成片_音色版.mp4").is_file())

    def test_waits_for_the_render_slot_instead_of_fighting_for_the_gpu(self) -> None:
        """有出片在跑就**等**（RVC 与出片抢同一张卡），等到了才关 ComfyUI 开工。"""
        from backend import rvc_batch

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            self._publish(root, kind="singing", number=1, label="唱歌_待转换")
            busy = {"n": 0}
            stopped: list = []

            def busy_twice() -> str:
                busy["n"] += 1
                return "批量第 3 条正在出片" if busy["n"] <= 2 else ""

            async def fast_sleep(_seconds):
                return None

            with self._temp_jobs(root) as job_store, patch.object(
                rvc_batch, "BATCH_OUTPUT_ROOT", root / "发布成品"
            ), patch.object(rvc_batch, "DATA_DIR", root / "data"), patch.object(
                rvc_batch, "_find_item_for_folder", lambda _folder: None
            ), patch.object(rvc_batch, "_batch_busy", busy_twice), patch.object(
                rvc_batch.asyncio, "sleep", fast_sleep
            ), patch.object(rvc_batch, "comfy_health", _async_true), patch.object(
                rvc_batch.resources, "stop_comfy", lambda job_id=None: stopped.append(job_id) or _noop()
            ), patch.object(rvc_batch, "run_rvc", _fake_converter):
                job_id = self._batch_job(job_store, [])
                asyncio.run(rvc_batch.run_rvc_publish_batch(job_id))

            self.assertGreaterEqual(busy["n"], 3, "应当一直等到出片空档")
            self.assertTrue(stopped, "等到空档后要关掉 ComfyUI 再转音色")

    def test_the_voice_batch_never_waits_for_itself(self) -> None:
        """回归（2026-09-22 用户：「批量音色转换开始了吗 怎么好像没有效果我点击了」）。

        任务刚建好时，它自己就是任务表里最新的 `queued` 条目 —— 旧的 `_batch_busy()` 用
        `store.active()` 会**看到自己**、报「还有一条 rvc_batch 任务在跑」，于是永远等下去，
        一条都不会转（页面看着就是「点了没反应」）。判据换成 `pipeline.gpu_holder()` 后必须
        立刻放行。这条把两个判据的差别一起钉住，别再改回 `store.active()`。
        """
        from backend import rvc_batch
        from backend.pipeline import gpu_holder

        class _NoBatches:
            """批次库隔离：这条测的是任务表判据，别去读用户真实的批次。"""

            def active(self):
                return None

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            with self._temp_jobs(root) as job_store, patch.object(rvc_batch, "batch_store", _NoBatches()):
                job_id = self._batch_job(job_store, [])
                # 旧判据踩的坑：`store.active()` 看到的就是这条任务自己
                self.assertEqual((job_store.active() or {}).get("id"), job_id)
                self.assertIsNone(gpu_holder(), "还在等空档的 rvc_batch 不算占显卡")
                self.assertEqual(rvc_batch._batch_busy(), "", "不许等到自己头上")

    def test_gpu_holder_ignores_cpu_only_jobs_and_the_newest_waiting_batch(self) -> None:
        """`gpu_holder()` 是「谁真的占着显卡」的唯一判据：**跳过纯 CPU 的字幕**与**还没抢到卡**
        的 rvc_batch，而不是无脑取 `store.active()` 的最新那条。"""
        from backend.pipeline import gpu_holder

        def job(job_id: str, kind: str, status: str, created: str) -> dict:
            return {
                "id": job_id, "kind": kind, "status": status, "stage": kind,
                "createdAt": created, "updatedAt": created, "milestones": [], "logs": [],
            }

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            with self._temp_jobs(Path(raw)) as job_store:
                job_store.create(job("singing-1", "singing", "queued", "2026-01-01T00:00:01+00:00"))
                job_store.create(job("subs-1", "subtitle_batch", "running", "2026-01-01T00:00:02+00:00"))
                job_store.create(job("voice-waiting", "rvc_batch", "queued", "2026-01-01T00:00:03+00:00"))
                self.assertEqual(gpu_holder()["id"], "singing-1", "字幕与等空档的批量都不算占卡")

                job_store.create(job("voice-running", "rvc_batch", "running", "2026-01-01T00:00:04+00:00"))
                self.assertEqual(gpu_holder()["id"], "voice-running", "真在转音色仍然算占卡")

                job_store.create(job("subs-2", "subtitle", "running", "2026-01-01T00:00:05+00:00"))
                self.assertEqual(gpu_holder()["id"], "voice-running", "最新的字幕不许顶掉真的占卡任务")

    def test_a_running_batch_item_still_makes_the_voice_batch_wait(self) -> None:
        """出片真的在跑（条目 running）时仍然要等 —— 修死锁不许把这条纪律一起改掉。"""
        from backend import rvc_batch

        class _BusyBatch:
            def active(self):
                return {"id": "b1", "items": [{"index": 3, "status": "running"}]}

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            with self._temp_jobs(Path(raw)), patch.object(rvc_batch, "batch_store", _BusyBatch()):
                self.assertEqual(rvc_batch._batch_busy(), "批量第 3 条正在出片")

    def test_the_page_restores_a_voice_batch_after_a_refresh(self) -> None:
        """源码级：页面必须**同时**问 `kind=rvc` 与 `kind=rvc_batch`。

        后端 `latest` 是精确匹配，只查 `rvc` 拿不到批量任务 —— 用户点了「一键批量转换音色」
        之后一刷新页面，批量卡片整个消失、按钮又变回可点，看着就是「点击没效果」（2026-09-22）。
        """
        root = Path(__file__).resolve().parents[1]
        page = (root / "src" / "RvcRoute.tsx").read_text(encoding="utf-8")
        self.assertIn('"/api/jobs/latest?kind=rvc"', page)
        self.assertIn("/api/jobs/latest?kind=rvc_batch", page)

    def test_the_button_and_the_endpoint_are_wired(self) -> None:
        """源码级：后端有 `/api/jobs/rvc/publish-batch`，页面有「一键批量转换音色」。"""
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "backend" / "app.py").read_text(encoding="utf-8")
        self.assertIn("/api/jobs/rvc/publish-batch", app_source)
        self.assertIn("run_rvc_publish_batch", app_source)
        page = (root / "src" / "RvcRoute.tsx").read_text(encoding="utf-8")
        self.assertIn("一键批量转换音色", page)
        self.assertIn("/api/jobs/rvc/publish-batch", page)
        self.assertIn("/pause", page)
        self.assertIn("/resume", page)


async def _async_false():
    return False


async def _async_true():
    return True


async def _async_none(*_args, **_kwargs):
    return None


async def _noop():
    return None


async def _fake_converter(_job_id, staged, audio_from=None):
    """假装 RVC：在输入旁边写出转换结果（真实脚本就是这个落点）。"""
    out = staged.with_name(f"{staged.stem}_mogoo_v1.mp4")
    out.write_bytes(b"converted-voice")
    return out


class SubtitleVoiceChainTests(unittest.TestCase):
    """字幕链路的音色版规则（2026-09-20 step 6）。

    用户定的链条：`最终成片.mp4`（原声）→ `最终成片_音色版.mp4` → **`最终成片_字幕版.mp4`（发布版）**。
    字幕要**优先烧音色版**；如果字幕版比输入旧（先烧字幕、后转音色）→ 判过期并重烧，
    否则会拿旧音频的字幕版去发布。
    """

    def _folder(self, root: Path, *, clean: bool = True, voice: bool = False,
                subtitle: bool = False, voice_newer: bool = False,
                subtitle_newer: bool = False) -> Path:
        import os
        import time

        folder = root / "singing" / "001_唱歌_测试歌_7685308023212056420"
        folder.mkdir(parents=True, exist_ok=True)
        now = time.time()
        if clean:
            (folder / "最终成片.mp4").write_bytes(b"clean")
            os.utime(folder / "最终成片.mp4", (now - 300, now - 300))
        if voice:
            (folder / "最终成片_音色版.mp4").write_bytes(b"voice")
            stamp = now - 10 if voice_newer else now - 200
            os.utime(folder / "最终成片_音色版.mp4", (stamp, stamp))
        if subtitle:
            (folder / "最终成片_字幕版.mp4").write_bytes(b"subtitle")
            stamp = now - 5 if subtitle_newer else now - 250
            os.utime(folder / "最终成片_字幕版.mp4", (stamp, stamp))
        return folder

    def test_scan_prefers_the_voice_version_as_input(self) -> None:
        """有音色版就拿它当输入（不是原声母版）。"""
        from backend import subtitle_step

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            self._folder(root, clean=True, voice=True, voice_newer=True)
            with patch.object(subtitle_step, "batch_store", _NoItems()):
                queue = subtitle_step.scan_singing_publish_videos(root=root / "singing")

            self.assertEqual(len(queue), 1)
            self.assertTrue(queue[0]["source"].endswith("最终成片_音色版.mp4"))
            self.assertFalse(queue[0]["stale"])

    def test_scan_falls_back_to_the_master_when_there_is_no_voice_version(self) -> None:
        """还没转音色 → 用原声母版烧（链条可以分两步走）。"""
        from backend import subtitle_step

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            self._folder(root, clean=True, voice=False)
            with patch.object(subtitle_step, "batch_store", _NoItems()):
                queue = subtitle_step.scan_singing_publish_videos(root=root / "singing")

            self.assertEqual(len(queue), 1)
            self.assertTrue(queue[0]["source"].endswith("最终成片.mp4"))

    def test_fresh_subtitle_version_is_not_redone(self) -> None:
        """字幕版比输入新 → 已经是最新的，不再重烧。"""
        from backend import subtitle_step

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            self._folder(root, clean=True, voice=True, voice_newer=False,
                         subtitle=True, subtitle_newer=True)
            with patch.object(subtitle_step, "batch_store", _NoItems()):
                queue = subtitle_step.scan_singing_publish_videos(root=root / "singing")
            self.assertEqual(queue, [])

    def test_stale_subtitle_version_is_reburned(self) -> None:
        """先烧字幕、后转音色 → 字幕版是旧音频的，判过期、重烧。"""
        from backend import subtitle_step

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            self._folder(root, clean=True, voice=True, voice_newer=True,
                         subtitle=True, subtitle_newer=False)
            with patch.object(subtitle_step, "batch_store", _NoItems()):
                queue = subtitle_step.scan_singing_publish_videos(root=root / "singing")

            self.assertEqual(len(queue), 1, "字幕版已过期就必须回到队列里")
            self.assertTrue(queue[0]["stale"])
            self.assertTrue(queue[0]["source"].endswith("最终成片_音色版.mp4"))

    def test_publish_chain_reports_the_version_to_publish(self) -> None:
        """批量页的「发布链条」状态：原声/音色/字幕三格 + 该发哪个文件。"""
        from backend import batch_worker

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            folder = self._folder(root, clean=True, voice=True, voice_newer=True,
                                  subtitle=True, subtitle_newer=True)
            item = {"id": "i1", "kind": "singing", "awemeId": "7685308023212056420",
                    "ai": {"title": "测试"}, "outputs": {"folder": str(folder)}}
            with patch.object(batch_worker, "BATCH_OUTPUT_ROOT", root):
                chain = batch_worker.publish_chain(item)

            self.assertTrue(chain["clean"] and chain["voice"] and chain["subtitle"])
            self.assertFalse(chain["subtitleStale"])
            self.assertTrue(chain["needsVoice"])
            self.assertEqual(chain["publishFile"], "最终成片_字幕版.mp4")
            self.assertIn("字幕版", chain["publishHint"])

            # 字幕版过期 → 发布版退回音色版，并提示重烧
            self._folder(root, clean=True, voice=True, voice_newer=True,
                         subtitle=True, subtitle_newer=False)
            with patch.object(batch_worker, "BATCH_OUTPUT_ROOT", root):
                stale = batch_worker.publish_chain(item)
            self.assertTrue(stale["subtitleStale"])
            self.assertEqual(stale["publishFile"], "最终成片_音色版.mp4")
            self.assertIn("过期", stale["publishHint"])

            # 跳舞：没有音色这一步，发布版就是 `最终成片.mp4`（哪怕目录里还躺着音色版）
            dance_item = dict(item, kind="dance")
            with patch.object(batch_worker, "BATCH_OUTPUT_ROOT", root):
                dance = batch_worker.publish_chain(dance_item)
            self.assertFalse(dance["needsVoice"])
            self.assertEqual(dance["publishFile"], "最终成片.mp4")
            self.assertIn("最终成片", dance["publishHint"])

    def test_latest_batch_carries_the_chain_state(self) -> None:
        """`GET /api/batches/latest` 要给条目补上 `publishChain`（页面直接显示）。"""
        import asyncio

        from backend import app as app_module

        state = {"id": "b1", "status": "completed", "items": [
            {"id": "i1", "index": 1, "status": "completed", "kind": "singing"},
            {"id": "i2", "index": 2, "status": "pending", "kind": "singing"},
        ]}
        enriched = app_module._with_publish_chain(state)
        self.assertIn("publishChain", enriched["items"][0])
        self.assertNotIn("publishChain", enriched["items"][1], "还没出片的条目不用查磁盘")


class _NoItems:
    """`batch_store.items_for_aweme` 的最小白板（字幕扫描只用它查歌名）。"""

    def items_for_aweme(self, _aweme_id):
        return []


class SourceVideoIntegrityTests(unittest.TestCase):
    """源视频完整性闸门：半截下载不许进 ComfyUI（2026-09-19 条目 012 事故）。

    012 的驱动视频 moov 里声明 343 帧、mdat 只够 209 帧（ffprobe：`stream 1, offset
    0x1c20ac: partial file`）。Mie 循环按 343 帧排了 5 段，跑到第 3 段
    （`skip_first_frames=228`）时 VHS 解出 0 帧、只回一句 `No frames generated`：
    15 分钟 GPU 白跑；更糟的是 `sourcePath` 已经登记了这个坏文件，之后每次
    「重新开始」都复用它、原地复现同一个失败。
    """

    def setUp(self):
        from backend import video_integrity

        video_integrity._VALIDATED.clear()
        self.addCleanup(video_integrity._VALIDATED.clear)

    @staticmethod
    def _probe(claimed, decoded, stderr=""):
        return patch(
            "backend.video_integrity.probe_frame_counts",
            return_value=(claimed, decoded, stderr),
        )

    def _write_video(self, folder: str, name: str = "012.mp4") -> Path:
        video = Path(folder) / name
        video.write_bytes(b"x" * 4096)
        return video

    def test_a_truncated_download_is_reported_with_both_counts(self):
        """容器声明多少帧、实际只能解出多少帧 —— 两边的数字都要出现在原因里。"""
        from backend import video_integrity

        with tempfile.TemporaryDirectory() as folder:
            video = self._write_video(folder)
            with self._probe(343, 209, "[mov,mp4] stream 1, offset 0x1c20ac: partial file"):
                reason = video_integrity.truncation_reason(video)
        self.assertIsNotNone(reason, "343 声明帧 / 209 实际帧必须判为截断")
        self.assertIn("343", reason)
        self.assertIn("209", reason)

    def test_a_complete_download_passes_and_is_cached(self):
        from backend import video_integrity

        with tempfile.TemporaryDirectory() as folder:
            video = self._write_video(folder)
            with self._probe(343, 343) as probe:
                self.assertIsNone(video_integrity.truncation_reason(video))
                # 第二次再问同一份文件不该再整条解码一遍（下载校验 + 提交前校验会各问一次）。
                self.assertIsNone(video_integrity.truncation_reason(video))
                self.assertEqual(probe.call_count, 1)

    def test_an_unreadable_file_never_blocks_the_run(self):
        """这条判据只管「声明帧数 > 实际帧数」；读不出来的文件交给 ComfyUI 自己报。"""
        from backend import video_integrity

        with tempfile.TemporaryDirectory() as folder:
            video = self._write_video(folder)
            for claimed, decoded in ((None, None), (343, None), (None, 209)):
                with self._probe(claimed, decoded, "Invalid data found when processing input"):
                    self.assertIsNone(
                        video_integrity.truncation_reason(video),
                        f"({claimed}, {decoded}) 拿不到正面证据时不许拦",
                    )

    def test_a_truncated_file_is_parked_so_the_retry_redownloads(self):
        """坏文件必须改名挪走：留原地的话「重新开始」会再次复用它。"""
        from backend import batch_worker

        with tempfile.TemporaryDirectory() as folder:
            video = self._write_video(folder)
            with patch.object(batch_worker, "truncation_reason", return_value="343 帧只解出 209 帧"):
                reason = batch_worker._park_truncated_source(video)
            self.assertEqual(reason, "343 帧只解出 209 帧")
            self.assertFalse(video.exists(), "坏文件不许留在原路径")
            self.assertTrue(
                video.with_name("012.truncated.mp4").is_file(),
                "坏文件应该改名成 .truncated.mp4 留着（不静默删除）",
            )
            # 完整文件一律不动它。
            keep = self._write_video(folder, "013.mp4")
            with patch.object(batch_worker, "truncation_reason", return_value=None):
                self.assertIsNone(batch_worker._park_truncated_source(keep))
            self.assertTrue(keep.is_file())

    def test_parked_files_are_never_reused_as_cache(self):
        """`.truncated.mp4` 不许再被当成「本地已有这条作品」捡回来。"""
        from backend import batch_worker

        aweme = "7684126327402778850"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / f"你在哪_{aweme}.truncated.mp4").write_bytes(b"x" * 4096)
            with patch.object(batch_worker, "DOUYIN_OUTPUT", root), \
                 patch.object(batch_worker, "MANIFEST_PATH", root / "download_manifest.jsonl"), \
                 patch.object(batch_worker, "_manifest_metadata", return_value={}):
                self.assertIsNone(
                    batch_worker.cached_download_path(f"https://www.douyin.com/video/{aweme}")
                )

    def test_a_truncated_source_never_starts_comfyui(self):
        """端到端：源视频是半截文件时，迁移任务**一开局**就失败，绝不拉起 ComfyUI。"""
        from backend import pipeline

        comfy_calls: list[str] = []

        class _Store:
            def get(self, _job_id):
                return state

            def update(self, _job_id, **changes):
                state.update(changes)
                return state

            def add_log(self, _job_id, message):
                state.setdefault("logs", []).append({"time": "t", "message": message})
                return state

            def set_milestone(self, _job_id, milestone_id, **changes):
                for milestone in state.get("milestones") or []:
                    if milestone["id"] == milestone_id:
                        milestone.update(changes)
                return state

        async def fake_ensure(*_args, **_kwargs):
            comfy_calls.append("ensure")

        async def fake_health():
            return None

        with tempfile.TemporaryDirectory() as folder:
            source = self._write_video(folder)
            state = {
                "id": "j-bad", "kind": "migrate", "status": "queued",
                "sourcePath": str(source),
                "milestones": [{"id": "migrate", "status": "pending"}],
            }

            async def scenario():
                with patch.object(pipeline, "store", _Store()), \
                     patch.object(pipeline, "pipeline_lock", asyncio.Lock()), \
                     patch.object(pipeline, "comfy_health", new=fake_health), \
                     patch("backend.pipeline.truncation_reason",
                           return_value="容器声明 343 帧，实际只能解出 209 帧"), \
                     patch.object(pipeline.resources, "ensure_comfy", new=fake_ensure):
                    await pipeline.run_migrate_pipeline("j-bad")

            asyncio.run(scenario())
        self.assertEqual(comfy_calls, [], "半截源视频绝不许拉起 ComfyUI 白跑十几分钟")
        self.assertEqual(state["status"], "failed")
        self.assertIn("不完整", str(state.get("errorSummary") or ""))
        self.assertIn(
            "343",
            json.dumps(state.get("logs") or [], ensure_ascii=False),
            "原因要写进任务日志，页面才看得到到底是哪一半截了",
        )

    def test_both_pipeline_entries_walk_through_the_source_gate(self):
        """源码级断言：唱歌与迁移两条链路都要过 `_guard_source_video`。"""
        import inspect

        from backend import pipeline

        for name in ("run_pipeline", "run_migrate_pipeline"):
            source = inspect.getsource(getattr(pipeline, name))
            self.assertIn("_guard_source_video", source, f"{name} 必须过源视频完整性闸门")


class _StubCdnStream:
    """httpx 的 `client.stream(...)` 上下文替身：按脚本吐字节，可以在中途断流。"""

    def __init__(self, status_code: int, headers: dict, body: bytes, error: Exception | None = None):
        self.status_code = status_code
        self.headers = headers
        self.body = body
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def iter_bytes(self, _size: int):
        if self.body:
            yield self.body
        if self.error is not None:
            raise self.error


class _StubCdnClient:
    """直连下载用的 httpx.Client 替身：按脚本返回 response，并记下每次请求的头。"""

    def __init__(self, streams: list[_StubCdnStream]):
        self.streams = list(streams)
        self.headers_seen: list[dict | None] = []

    def stream(self, _method, _url, follow_redirects=True, headers=None):
        self.headers_seen.append(headers)
        if not self.streams:
            raise AssertionError("替身没有更多响应了（请求次数超出预期）")
        return self.streams.pop(0)


class DirectDownloadResumeTests(unittest.TestCase):
    """直连 CDN 断流必须**续传**，不许把半截文件交出去（2026-09-19 用户：

    「怎么保证视频一定是下载成功的感觉直连下载先经常失败要不要用下载器 REST 下载不要直连了」）。

    实测这一批 61 次直连只失败 1 次，但那次的代价是：直连一断流就交给 REST，REST 交出来的
    又是另一个半截文件（还报「下载成功」）—— 就是 012 事故的源头。所以直连要自己扛住断流。
    """

    def setUp(self):
        self.sleep = patch("time.sleep").start()
        # ⚠️ **不要写 `self.addCleanup(patch.stopall)`**：那会把整个模块的安全补丁
        # （发布根重定向 + 关机桩）一起停掉，排在后面的测试类就会写进用户真实的发布目录
        # —— 2026-09-20 实测就是这样多了 14 个垃圾目录。只停自己起的这一处。
        self.addCleanup(self.sleep.stop)

    def _call(self, client, dest: Path, **kwargs):
        from backend.douyin_direct import download_cdn_file

        return download_cdn_file(client, "https://cdn.example/v.mp4", dest, **kwargs)

    def test_a_dropped_connection_resumes_instead_of_restarting(self):
        """第一次传了 200000/300000 就断 → 第二次带 Range 续传，最终拿到完整文件。"""
        head, tail = b"a" * 200_000, b"b" * 100_000
        from backend import douyin_direct as D

        client = _StubCdnClient([
            _StubCdnStream(200, {"content-length": "300000"}, head, D.httpx.ReadError("peer closed")),
            _StubCdnStream(206, {"content-range": "bytes 200000-299999/300000"}, tail),
        ])
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder) / "v.mp4"
            written = self._call(client, dest)
            self.assertEqual(written, 300_000)
            self.assertEqual(dest.read_bytes(), head + tail, "续传拼出来的必须是完整文件")
        self.assertIsNone(client.headers_seen[0], "第一次不该带 Range")
        self.assertEqual(client.headers_seen[1], {"Range": "bytes=200000-"})

    def test_a_wrong_range_is_never_appended(self):
        """服务器给的范围不是我们要的那一段 → 丢弃重下，绝不拼出一份「长度对、内容错」的视频。"""
        full = b"c" * 300_000
        from backend import douyin_direct as D

        client = _StubCdnClient([
            _StubCdnStream(200, {"content-length": "300000"}, b"a" * 200_000, D.httpx.ReadError("cut")),
            _StubCdnStream(206, {"content-range": "bytes 50-299999/300000"}, b"b" * 250_000),
            _StubCdnStream(200, {"content-length": "300000"}, full),
        ])
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder) / "v.mp4"
            self.assertEqual(self._call(client, dest), 300_000)
            self.assertEqual(dest.read_bytes(), full)
        self.assertIsNone(client.headers_seen[2], "丢弃后第三次是整份重下，不该再带 Range")

    def test_giving_up_deletes_the_half_file(self):
        """重试用尽仍失败：抛错，而且半截文件必须消失（留着会被当成「本地已有」捡回去）。"""
        from backend import douyin_direct as D

        client = _StubCdnClient([
            _StubCdnStream(200, {"content-length": "300000"}, b"a" * 200_000, D.httpx.ReadError("cut")),
            _StubCdnStream(200, {"content-length": "300000"}, b"a" * 200_000, D.httpx.ReadError("cut")),
            _StubCdnStream(200, {"content-length": "300000"}, b"a" * 200_000, D.httpx.ReadError("cut")),
        ])
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder) / "v.mp4"
            with self.assertRaises(D.DouyinDirectError):
                self._call(client, dest)
            self.assertFalse(dest.exists(), "半截文件不许留在下载目录里")

    def test_a_tiny_partial_file_is_not_resumed(self):
        """只拿到 1KB（多半是错误页）：不值得续传，直接从头重下并覆盖。"""
        from backend import douyin_direct as D

        client = _StubCdnClient([
            _StubCdnStream(200, {"content-length": "300000"}, b"x" * 1024, D.httpx.ReadError("cut")),
            _StubCdnStream(200, {"content-length": "300000"}, b"y" * 300_000),
        ])
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder) / "v.mp4"
            self.assertEqual(self._call(client, dest), 300_000)
            self.assertEqual(dest.read_bytes(), b"y" * 300_000)
        self.assertIsNone(client.headers_seen[1], "太短的内容不值得续传，第二次仍要整份重下")


class _StubHttpxClient:
    """`httpx.Client` 的替身（只当上下文管理器用，请求都被 mock 掉了）。"""

    def __init__(self, *_args, **_kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class DownloadDurationGateTests(unittest.TestCase):
    """下载完还要跟**分享页声明的时长**对一次：字节数对得上 ≠ 内容对。

    012 那种「moov 完整、内容少一半」由帧数校验抓住；但如果 CDN 给的是一份**自洽的短文件**
    （容器和内容都是短的），帧数校验查不出来 —— 只有跟平台自己声明的时长对照才发现少了一截。
    """

    def test_the_duration_is_read_from_the_video_object_only(self):
        """页面里 `"video":{` 不止一处，音乐那边还有以「秒」为单位的 duration，别拿错。"""
        from backend import douyin_direct as D

        html = (
            '{"music":{"duration":11,"cover":{"width":720}},'
            '"video":{"chunkIds":["8242","3361"],"bitRate":null},'
            '"aweme":{"video":{"play_addr":{"uri":"v1e00fgi0000d9aqe1nog65rjkh964cg"},'
            '"height":3378,"width":1900,"bit_rate":null,"duration":11434,"big_thumbs":null}}}'
        )
        self.assertEqual(D.extract_video_duration_ms(html), 11434)

    def test_a_page_without_a_video_object_gives_nothing(self):
        from backend import douyin_direct as D

        self.assertIsNone(D.extract_video_duration_ms('{"music":{"duration":11}}'))

    def test_a_short_file_is_flagged_and_a_close_one_is_not(self):
        from backend.video_integrity import duration_mismatch_reason

        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "v.mp4"
            video.write_bytes(b"x" * 4096)
            with patch("backend.video_integrity.probe_duration_seconds", return_value=7.0):
                reason = duration_mismatch_reason(video, 11.434)
            self.assertIsNotNone(reason, "只有 7 秒、平台说 11.43 秒，必须判为不完整")
            self.assertIn("11.43", reason)
            with patch("backend.video_integrity.probe_duration_seconds", return_value=11.0):
                self.assertIsNone(
                    duration_mismatch_reason(video, 11.434), "1 秒容差内的抖动不算不完整"
                )
            with patch("backend.video_integrity.probe_duration_seconds", return_value=3.0):
                self.assertIsNone(
                    duration_mismatch_reason(video, None), "没有官方时长时不许拦"
                )

    def test_fetch_rejects_a_file_shorter_than_the_share_page_says(self):
        """走一遍 `fetch`：时长对不上就删掉文件并抛错（再去试 REST 兜底）。"""
        from backend import douyin_direct as D

        written: list[Path] = []

        def fake_download(_client, _cdn, dest, on_progress=None):
            dest.write_bytes(b"x" * 20_000)
            written.append(dest)
            return 20_000

        with tempfile.TemporaryDirectory() as folder:
            payload = {
                "desc": "测试作品", "tags": [], "nickname": "n",
                "videoId": "v1e00fgi0000d9aqe1nog65rjkh964cg", "durationMs": 11434,
            }
            with patch.object(D, "load_cookies", return_value={}), \
                 patch.object(D.httpx, "Client", _StubHttpxClient), \
                 patch.object(D, "resolve_aweme_id", return_value="7662214161216508389"), \
                 patch.object(D, "share_payload", return_value=payload), \
                 patch.object(D, "play_url", return_value=("https://cdn.example/v.mp4", "1080p")), \
                 patch.object(D, "download_cdn_file", side_effect=fake_download), \
                 patch.object(D, "duration_mismatch_reason",
                              return_value="平台说 11.43 秒，下载到的只有 7.00 秒"):
                with self.assertRaises(D.DouyinDirectError):
                    D.fetch("https://www.douyin.com/video/7662214161216508389", Path(folder))
            self.assertTrue(written, "应该真的走到下载这一步")
            self.assertFalse(written[0].exists(), "时长短一截的文件不许留在下载目录里")


class NetProxyRoutingTests(unittest.TestCase):
    """出图走哪条出网路线：**直连优先，直连不通才自动改走代理**（2026-09-22）。

    那天的故障：用户 VPN 断了，中转站 `api.teamorouter.com` 直连全部 `ConnectError`，
    出图失败后 `batch_worker` 按设计退回"源视频取帧"——用户看到的就是
    「怎么生成的人物图完成不像就是抽帧图啊」。所以这条口径必须钉住：
    直连能用就别绕代理；直连不通要能自己切过去；本机地址（ComfyUI、自调用）永远直连。
    """

    PROXY_KEYS = ("H3_IMAGE_PROXY", "H3_BATCH_IMAGE_PROXY", "H3_NO_PROXY",
                  "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
                  "HTTP_PROXY", "http_proxy")

    def setUp(self):
        from backend import netproxy

        self.netproxy = netproxy
        saved = {key: os.environ.get(key) for key in self.PROXY_KEYS}
        self.addCleanup(self._restore_env, saved)
        for key in self.PROXY_KEYS:
            os.environ.pop(key, None)
        with patch.object(netproxy, "_user_env", return_value=""):
            netproxy.note_failure()

    def _restore_env(self, saved):
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.netproxy.note_failure()

    def test_loopback_and_self_calls_never_go_through_a_proxy(self) -> None:
        self.assertIsNone(self.netproxy.for_url("http://127.0.0.1:15188/system_stats"))
        self.assertIsNone(self.netproxy.for_url("http://localhost:15111/api/batches/latest"))
        self.assertIsNone(self.netproxy.for_url("http://[::1]:15111/api/config"))
        self.assertTrue(self.netproxy.is_local("http://127.0.0.1:15111/"))

    def test_direct_is_preferred_when_it_works(self) -> None:
        """直连能通就不绕代理（实测中转站直连 0.3s、走代理 0.4s，多一跳没意义）。"""
        calls: list[str] = []

        def fake_probe(url, proxy, timeout):
            calls.append(proxy)
            return proxy == ""          # 只有直连能通

        with patch.object(self.netproxy, "_auto_proxy", return_value="http://127.0.0.1:7897"):
            with patch.object(self.netproxy, "_probe", side_effect=fake_probe):
                proxy, note = self.netproxy.probe_route("https://api.example/v1/models")

        self.assertEqual(proxy, "")
        self.assertIn("直连可用", note)
        self.assertEqual(calls, [""])   # 判定成功就不再试代理

    def test_direct_failure_falls_back_to_the_local_proxy(self) -> None:
        def fake_probe(url, proxy, timeout):
            return proxy == "http://127.0.0.1:7897"   # 只有代理能通

        with patch.object(self.netproxy, "_auto_proxy", return_value="http://127.0.0.1:7897"):
            with patch.object(self.netproxy, "_probe", side_effect=fake_probe):
                proxy, note = self.netproxy.probe_route("https://api.example/v1/models")

        self.assertEqual(proxy, "http://127.0.0.1:7897")
        self.assertIn("自动改走代理", note)

    def test_the_route_is_rechecked_after_a_real_request_fails(self) -> None:
        """VPN 断掉的那一刻，正在跑的那次请求失败 → 下次必须重新判定，不能一直用坏路线。"""
        with patch.object(self.netproxy, "_auto_proxy", return_value="http://127.0.0.1:7897"):
            with patch.object(self.netproxy, "_probe", side_effect=lambda url, p, t: p == ""):
                self.netproxy.probe_route("https://api.example/v1/models")
            with patch.object(self.netproxy, "_probe", side_effect=lambda url, p, t: p != ""):
                cached, _ = self.netproxy.probe_route("https://api.example/v1/models")
                self.assertEqual(cached, "")            # 缓存里还是直连
                self.netproxy.note_failure()
                rechecked, note = self.netproxy.probe_route("https://api.example/v1/models")
        self.assertEqual(rechecked, "http://127.0.0.1:7897")
        self.assertIn("自动改走代理", note)

    def test_an_explicit_proxy_setting_wins_over_probing(self) -> None:
        """用户手动设了 H3_IMAGE_PROXY 就听他的：不许偷偷改回直连，也不许拿探测覆盖。"""
        os.environ["H3_IMAGE_PROXY"] = "http://127.0.0.1:7897"
        with patch.object(self.netproxy, "_probe", side_effect=AssertionError("不该探测")):
            proxy, note = self.netproxy.probe_route("https://api.example/v1/models")
        self.assertEqual(proxy, "http://127.0.0.1:7897")
        self.assertIn("H3_IMAGE_PROXY", note)

        os.environ["H3_IMAGE_PROXY"] = "direct"      # 显式要求直连
        proxy, note = self.netproxy.probe_route("https://api.example/v1/models")
        self.assertEqual(proxy, "")
        self.assertIn("强制直连", note)


class CoverSingleReferenceFallbackTests(unittest.IsolatedAsyncioTestCase):
    """封面只发**一张**参考图，`composite`（拼合）方案对它天然不适用 —— 必须换下一套方案。

    2026-09-22 实测：人物图 132 秒正常出好，两张封面各 42 秒报
    「composite 模式必须同时给场景图与身份图」全都不出（`_build_request` 在 composite 下
    要求两张图，而封面按口径只能给一张 —— 给两张会把两个人一起画进封面）。
    原来那个 RuntimeError 是在 `try` **外面**抛的，直接炸穿整个请求，`multi`（单图编辑）
    根本没机会试；现在把它当成「这一套不适用」继续往下试。
    """

    def setUp(self):
        from backend import batch_image

        self.batch_image = batch_image
        self.root = Path(tempfile.mkdtemp(prefix="h3-cover-fallback-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    async def _run(self, scene: Path, identity: Path | None, *, multi_ok: bool = True) -> Path:
        from backend import batch_image

        plans: list[tuple[str, str]] = []
        self.plans = plans
        real_build = batch_image._build_request

        def build(plan, field, **kwargs):
            plans.append((plan, field))
            if plan == "composite" and kwargs.get("identity_image") is None:
                raise RuntimeError("composite 模式必须同时给场景图与身份图")
            return real_build(plan, field, **kwargs)

        class StubResponse:
            def __init__(self, error: bool) -> None:
                self.is_error = error
                self.status_code = 400
                self.text = '{"error": {"message": "multi 不支持"}}'

            def json(self) -> dict:
                return {"data": [{"b64_json": base64.b64encode(scene.read_bytes()).decode()}]}

        async def fake_post(data, files):
            # `plans` 的最后一个就是这次要发的那一套方案
            plan = plans[-1][0]
            if plan == "multi" and not multi_ok:
                return StubResponse(True)
            return StubResponse(False)

        async def no_probe():
            return "直连可用"

        out = self.root / "cover.png"
        with patch.object(batch_image, "_build_request", side_effect=build):
            with patch.object(batch_image, "_post", side_effect=fake_post):
                with patch.object(batch_image, "_prepare_route", side_effect=no_probe):
                    with patch.object(batch_image, "_upscale_after_relay", new_callable=AsyncMock):
                        await batch_image.generate_candidate_image(
                            scene_image=scene, identity_image=identity,
                            prompt="封面提示词", ratio="3:4", output_path=out,
                        )
        return out

    def _png(self, name: str, *, size: tuple[int, int] = (60, 45)) -> Path:
        """写一张**真** PNG：composite 方案会真的把两张图拼起来（`compose_reference`）。"""
        path = self.root / name
        Image.new("RGB", size, (120, 140, 160)).save(path)
        return path

    async def test_a_single_reference_cover_skips_the_composite_plan(self) -> None:
        scene = self._png("person.png")
        out = await self._run(scene, None)
        self.assertTrue(out.is_file())
        # composite 不适用 → 必须退到 multi（第一套就是 multi，这里验证它没被炸掉）
        self.assertTrue(self.plans, "至少应该真的试过一套方案")
        self.assertIn("multi", [plan for plan, _ in self.plans])

    async def test_two_references_do_not_fall_back_after_a_billed_request(self) -> None:
        """multi 请求失败后不再自动发 composite，避免一次操作被扣两笔。"""
        scene = self._png("scene.png")
        identity = self._png("identity.png", size=(40, 30))
        with self.assertRaisesRegex(RuntimeError, "没有自动改格式或换线路重试"):
            await self._run(scene, identity, multi_ok=False)
        self.assertEqual([plan for plan, _ in self.plans][0], "multi")
        self.assertEqual(len(self.plans), 1)


class NoSubtitleMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_true_request_creates_a_direct_migration(self) -> None:
        """旧客户端要求去字幕时，新任务仍直接使用源视频。"""
        from io import BytesIO
        from starlette.datastructures import UploadFile
        from backend import app as app_module

        with tempfile.TemporaryDirectory() as raw:
            video = UploadFile(BytesIO(b"video"), filename="source.mp4")
            reference = UploadFile(BytesIO(b"image"), filename="person.png")
            with patch.object(app_module, "COMFY_INPUT", Path(raw)), \
                 patch.object(app_module, "gpu_holder", return_value=None), \
                 patch.object(app_module, "media_metadata", new=AsyncMock(return_value={
                     "fps": 30, "frames": 90, "duration": 3,
                 })), \
                 patch.object(app_module.store, "create"), \
                 patch.object(app_module, "spawn", side_effect=lambda task: task.close()):
                state = await app_module.create_migrate_job(
                    video=video, reference_image=reference, ratio="9:16",
                    remove_subtitles="1", mode="animation", content_prompt="",
                    video_prompt="", image_prompt="", use_upscale="0",
                )
        self.assertFalse(state["removeSubtitles"])
        self.assertFalse(any(step["id"] == "paint" for step in state["milestones"]))


    async def test_saved_legacy_true_job_skips_propainter(self) -> None:
        """旧任务里保存的 true 也不得在执行时触发去字幕。"""
        from unittest.mock import MagicMock
        from backend import pipeline

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "person.png").write_bytes(b"image")
            draft = root / "draft.mp4"
            draft.write_bytes(b"video")
            state = {
                "status": "queued", "canvas": "9:16", "migrateMode": "animation",
                "sourceInputName": "source.mp4", "referenceInputName": "person.png",
                "removeSubtitles": True, "useUpscale": False,
            }
            fake_store = MagicMock()
            fake_store.get.return_value = state
            with patch.object(pipeline, "store", fake_store), \
                 patch.object(pipeline, "COMFY_INPUT", root), \
                 patch.object(pipeline, "claim_job_run", new=AsyncMock(return_value=True)), \
                 patch.object(pipeline, "_guard_source_video", new=AsyncMock()), \
                 patch.object(pipeline.resources, "ensure_comfy", new=AsyncMock()), \
                 patch.object(pipeline, "raise_if_cancelled"), \
                 patch.object(pipeline, "prepare_migrate_workflow", return_value={}), \
                 patch.object(pipeline, "run_clean_workflow", new=AsyncMock()) as clean, \
                 patch.object(pipeline, "run_comfy_workflow", new=AsyncMock(return_value=draft)), \
                 patch.object(pipeline, "media_metadata", new=AsyncMock(return_value={})):
                await pipeline.run_migrate_pipeline("legacy-job")
            clean.assert_not_awaited()
            self.assertTrue(any("直接使用上传的原视频" in call.args[1]
                                for call in fake_store.add_log.call_args_list))



if __name__ == "__main__":
    unittest.main()
