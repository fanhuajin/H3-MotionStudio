from contextlib import contextmanager
from pathlib import Path
import json
import os
import re
import tempfile
import unittest
from unittest.mock import patch

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
from backend.store import format_elapsed, migrate_milestones
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


def setUpModule() -> None:
    _PUBLISH_ROOT_PATCH.start()


def tearDownModule() -> None:
    _PUBLISH_ROOT_PATCH.stop()
    _TEST_PUBLISH_ROOT.cleanup()


@contextmanager
def process_env_only():
    """让环境读取只认进程环境。

    真实环境里 `settings.env_value` 会回读 Windows 用户级变量（本机确实配了中转站），
    不隔离的话「未配置中转站」这类测试会随开发机状态飘。
    """

    def fake(name: str, default: str = "") -> str:
        return (os.environ.get(name) or "").strip() or default

    with patch("backend.batch_image.env_value", fake), patch(
        "backend.batch_worker.env_value", fake
    ), patch("backend.batch_ai.env_value", fake):
        yield


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
        ), patch.object(batch_worker, "_prepare_review", fake_prepare):
            asyncio.run(batch_worker.run_batch("b1"))

        by_index = {item["index"]: item for item in box["state"]["items"]}
        self.assertEqual(by_index[1]["status"], "skipped")
        self.assertIn("同一个抖音作品", by_index[1]["warning"])
        self.assertEqual(by_index[2]["status"], "awaiting_review")   # 后面的条目照常跑
        self.assertNotEqual(box["state"]["status"], "failed")

    def test_batch_keeps_preparing_items_while_another_one_renders(self) -> None:
        """出片跑在后台时，后面的条目照常备料。

        用户 2026-09-13 实测：「我现在重新加入了一条…下载抖音视频、生成人物图与发布文案、
        等待你的确认…现在没有处理啊」——出片一条要十几分钟，runner 原来原地 await 出片，
        新追加的条目就卡在「排队中」拿不到候选图与文案。备料只用下载 + ffmpeg + 文本模型，
        不碰 ComfyUI，所以必须能和出片并行；出片本身仍严格一条一条来。
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
            ):
                runner = asyncio.create_task(batch_worker.run_batch("b1"))
                await asyncio.wait_for(video_started.wait(), timeout=5)
                # 出片**跑起来之后**用户才追加一条（页面上「加入队列」的行为：排到队尾）
                late = new_batch_state(["https://v.douyin.com/late"], [])["items"][0]
                late["index"] = 2
                box["state"]["items"].append(late)
                box["state"]["total"] = 2
                appended["id"] = late["id"]
                for _ in range(300):
                    if late["status"] == "awaiting_review":
                        break
                    await asyncio.sleep(0.01)
                # 出片还在后台跑，新追加的那一条已经备完料了
                self.assertEqual(late["status"], "awaiting_review")
                self.assertIn(f"download-{late['id']}", order)
                self.assertNotIn("video-end", order)
                release.set()
                await asyncio.wait_for(runner, timeout=5)

        asyncio.run(scenario())
        self.assertEqual(order[0], "video-start")
        self.assertEqual(order[-1], "video-end")
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
            self.assertEqual(len(spawned), 2)   # run_batch + ComfyUI 预热
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
            self.assertEqual(len(spawned), 2)
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

    def test_batch_table_endpoints_are_registered(self) -> None:
        """后台表格化的批量接口（重排序 + 批量确认/跳过/删除）必须都注册在 app 上。"""
        from backend.app import app

        paths = {getattr(route, "path", "") for route in app.routes}
        self.assertIn("/api/batches/{batch_id}/items/{item_id}/move", paths)
        self.assertIn("/api/batches/{batch_id}/items/confirm-many", paths)
        self.assertIn("/api/batches/{batch_id}/items/skip-many", paths)
        self.assertIn("/api/batches/{batch_id}/items/delete-many", paths)

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
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-from-env"}):
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
            self.assertEqual(batch_image.IMAGE_SIZES["4:3"], "1536x1152")
            self.assertEqual(batch_image.IMAGE_SIZES["9:16"], "1152x2048")
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
            width, height = (int(part) for part in batch_image.IMAGE_SIZES[ratio].split("x"))
            self.assertEqual(width % 16, 0, f"{ratio} 宽不是 16 的倍数")
            self.assertEqual(height % 16, 0, f"{ratio} 高不是 16 的倍数")
            self.assertLessEqual(max(width, height), 3840)
            self.assertLessEqual(max(width, height) / min(width, height), 3.0)
            pixels = width * height
            self.assertGreaterEqual(pixels, 655_360)
            self.assertLessEqual(pixels, 8_294_400)
            # 比例必须精确：不能用 1536x1024 这种 3:2 冒充 4:3
            self.assertEqual(width * expected[1], height * expected[0], f"{ratio} 比例不精确")

    def test_batch_image_request_plans_fall_back_to_singular_field(self) -> None:
        """官方多图编辑用 image[]，中转站文档只写 image：要能自动回退。"""
        with process_env_only():
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("H3_BATCH_IMAGE_FIELD", None)
                os.environ.pop("H3_BATCH_IMAGE_MODE", None)
                self.assertEqual(
                    batch_image.request_plans(),
                    [("multi", "image[]"), ("multi", "image"), ("composite", "image")],
                )
            with patch.dict(os.environ, {"H3_BATCH_IMAGE_FIELD": "image"}, clear=False):
                self.assertEqual(batch_image.request_plans(), [("multi", "image"), ("composite", "image")])
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

    def test_batch_image_falls_back_on_transport_error(self) -> None:
        """连接失败这类传输层异常也必须走回退链，并汇总每一步的失败原因。"""
        import asyncio

        import httpx as _httpx

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            scene, identity = root / "s.png", root / "i.png"
            Image.new("RGB", (320, 240), "#223344").save(scene)
            Image.new("RGB", (240, 320), "#884466").save(identity)
            calls: list[str] = []

            async def boom(data, files):
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
            self.assertIn("composite", message)  # 提示可切单图拼合
            self.assertEqual(calls, ["image[]", "image", "image[]"])

    def test_batch_image_provider_selection(self) -> None:
        """默认必须是 manual（用户自己出图）；显式配置才走 api/local/frame。"""
        with process_env_only():
            with patch.dict(os.environ, {"H3_BATCH_IMAGE_PROVIDER": "local"}, clear=False):
                self.assertEqual(_image_provider(), "local")
            with patch.dict(os.environ, {"H3_BATCH_IMAGE_PROVIDER": "api"}, clear=False):
                self.assertEqual(_image_provider(), "api")
            # 非法值回落到默认值
            with patch.dict(os.environ, {"H3_BATCH_IMAGE_PROVIDER": "没这个值"}, clear=False):
                self.assertEqual(_image_provider(), "manual")
            # 未设置时也必须是 manual：绝不能在用户没要求时自动出图
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("H3_BATCH_IMAGE_PROVIDER", None)
                self.assertEqual(_image_provider(), "manual")

    def test_batch_ai_image_prompt_carries_review_feedback(self) -> None:
        """审核修改意见必须进入出图提示词，否则「调整图片」只会改文案、图不动。"""
        base = batch_ai.compose_prompt("singing", "video")
        # 出图提示词以造型提示词开头，后面追加构图规格、歌曲与身份约束
        self.assertTrue(batch_ai.compose_image_prompt("singing", "video", "", "both").startswith(base))
        # 只调文案时不应把反馈写进出图提示词（那条路径本来就不重新出图）
        copy_prompt = batch_ai.compose_image_prompt("singing", "video", "换背景", "copy")
        self.assertTrue(copy_prompt.startswith(base))
        self.assertNotIn("换背景", copy_prompt)
        for mode in ("image", "both"):
            prompt = batch_ai.compose_image_prompt("singing", "video", "头顶留白压到 2%", mode)
            self.assertIn("头顶留白压到 2%", prompt)
            self.assertIn("本次必须优先满足的修改要求", prompt)
            self.assertTrue(prompt.startswith(base))

    def test_batch_ai_image_prompt_carries_the_song(self) -> None:
        """歌曲必须真的进入出图提示词，否则「图一给造型、歌曲给情绪」落不了地。"""
        # 1) 《歌曲名》是源文件里的字面占位符，必须被真实歌名替换
        redesign = batch_ai.compose_prompt("singing", "redesign", "爱如潮水")
        self.assertIn("《爱如潮水》", redesign)
        self.assertNotIn(batch_ai.SONG_PLACEHOLDER, redesign)

        # 2) 沿用源视频造型时：歌名 + 情绪进入提示词，并且把分工写清
        prompt = batch_ai.compose_image_prompt(
            "singing",
            "video",
            song_name="爱如潮水",
            song_mood="抒情慢板，克制的失恋感",
        )
        self.assertIn("【本次歌曲】《爱如潮水》", prompt)
        self.assertIn("抒情慢板，克制的失恋感", prompt)
        self.assertIn("图一决定人物的发型、发色、服装、配饰、场景、环境与灯光", prompt)
        self.assertIn("不要因为歌曲而改动图一已经给出的造型要素", prompt)

        # 3) 跳舞没有歌曲，不能凭空塞一个歌曲块
        self.assertNotIn("【本次歌曲】", batch_ai.compose_image_prompt("dance"))
        # 4) 没识别出歌名时也不能留一个空歌曲块
        self.assertNotIn("【本次歌曲】", batch_ai.compose_image_prompt("singing", "video", song_name=""))

    def test_batch_ai_image_prompt_carries_composition_spec(self) -> None:
        """构图规格取自用户的构图参考图实测值，且必须按画布比例分别注入。"""
        singing = batch_ai.compose_image_prompt("singing")
        self.assertIn("【构图规格 · 按用户的构图参考图实测】", singing)
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
        self.assertIn("图二是唯一的人物身份与面部来源", prompt)
        self.assertIn("禁止把两张脸融合", prompt)
        self.assertIn("一律牺牲其他要求、保住图二的脸", prompt)
        # 跳舞同样受身份约束保护
        self.assertIn("保住图二的脸", batch_ai.compose_image_prompt("dance"))

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

        with patch.dict(os.environ, {}, clear=False), patch.object(
            settings_module, "env_value", fake_env_value
        ), patch.object(batch_ai, "env_value", fake_env_value):
            os.environ.pop("H3_BATCH_TEXT_API_KEY", None)
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

        with patch.object(batch_ai, "_JSON_SCHEMA_SUPPORTED", None), patch.object(
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
        for name in ("RvcRoute.tsx", "UpScaleRoute.tsx", "LyricRoute.tsx"):
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

    def test_batch_queue_rows_show_their_own_time(self) -> None:
        """每条任务显示**自己的**时间，不显示批次总耗时。

        用户 2026-09-13 先要「当前任务队列的时间也给下」，随后明确「每一个队列里的任务都是
        独立的计算时间我不需要看总时间」——所以队列行各有各的时间，队列头部不再有批次计时。
        排队中的条目必须写「排队」而不是「已用」，否则会让人以为它已经在跑了。
        """
        source = (Path(__file__).parents[1] / "src" / "BatchRoute.tsx").read_text(encoding="utf-8")
        self.assertIn("const queueTime = (item: BatchItem)", source)
        self.assertIn("queueTime(item)", source)          # 列表里真的用上了
        for label in ('"排队"', '"耗时"', '"已用"'):
            self.assertIn(label, source)
        # 只要有条目在跑就继续跳秒（批次可能刚收尾）
        self.assertIn("const queueLive = visibleItems.some(", source)
        self.assertIn("useNowTick(batchLive || queueLive)", source)
        # 批次总耗时整块去掉（注释里提到这几个字不算）
        rendered = re.sub(r"\{/\*.*?\*/\}", "", source, flags=re.S)
        rendered = re.sub(r"^\s*//.*$", "", rendered, flags=re.M)
        self.assertNotIn("batchElapsedMs", rendered)
        self.assertNotIn("批次总耗时", rendered)

    def test_batch_selection_stays_on_the_item_you_clicked(self) -> None:
        """点开已完成 / 已跳过的条目不许自己跳走。

        用户 2026-09-13：「取消出片之后为什么点击不了了 一点就跳转到了其他的」——旧逻辑只要
        选中项的 status 是 completed/skipped/deleted，就把选中项强行改成 `currentItemId`，
        于是刚取消出片（→ skipped）的那一条根本点不开，已出片的条目也看不了。
        现在只有「选中的条目已不存在/已删除」或「本来就是自动跟随」时才跳。
        """
        source = (Path(__file__).parents[1] / "src" / "BatchRoute.tsx").read_text(encoding="utf-8")
        self.assertIn("const selectItem = (itemId: string)", source)
        self.assertIn("onClick={() => selectItem(item.id)}", source)
        self.assertIn("followedItemRef", source)
        self.assertIn('const unusable = !target || target.status === "deleted";', source)
        self.assertNotIn('["completed", "skipped", "deleted"].includes(currentSelection.status)', source)

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
                self.assertEqual(second.parent, root)
                self.assertIn("第二版标题", second.name)
                self.assertFalse(first.exists(), "旧目录应当被改名，而不是留下两个")
                self.assertTrue((second / "人物图.png").is_file())
                # 反复调用必须稳定在同一个目录
                self.assertEqual(batch_worker.publish_folder(item), second)
                self.assertEqual(len(list(root.iterdir())), 1)
                # 记录的目录已经不在了（被清掉）→ 复用同作品号已有的目录，**不新建第三个**
                item["outputs"] = {"folder": str(root / "gone")}
                item["ai"]["title"] = "第三版标题"
                self.assertEqual(batch_worker.publish_folder(item), second)
                self.assertEqual(len(list(root.iterdir())), 1)
                # 记录的目录在发布根之外（脏数据）→ 也只认发布根里那一个
                item["outputs"] = {"folder": str(root.parent / "outside")}
                self.assertEqual(batch_worker.publish_folder(item), second)
                self.assertEqual(len(list(root.iterdir())), 1)

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
                self.assertEqual(created.name, "003_全新标题_7000000000000000000")

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
                    # 已确认且源视频还在：回到 confirmed，只重跑出片，旧的发布记录清空
                    asyncio.run(app_module.retry_batch_item(state["id"], approved["id"]))
                    row = row_of(approved["id"])
                    self.assertEqual((row["status"], row["stage"]), ("confirmed", "confirmed"))
                    self.assertEqual(row["outputs"], {})
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
                    self.assertEqual(row["outputs"], {})

                    # 正在出片的条目必须先「取消出片 / 回到确认」，不能直接重来
                    store.mutate_item(
                        state["id"], approved["id"], lambda row: row.update(status="running")
                    )
                    with self.assertRaises(app_module.HTTPException):
                        asyncio.run(app_module.retry_batch_item(state["id"], approved["id"]))
            finally:
                batch_store_module.DB_PATH = original_db

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

    def test_dance_item_can_toggle_remove_subtitles_before_production(self) -> None:
        """跳舞条目的「去除字幕」用户可以自己改（2026-09-14）——没开始出片就能改，出片后拒绝。"""
        from backend import batch_store as batch_store_module
        from backend import batch_worker
        from backend.batch_store import BatchStore

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            original_db = batch_store_module.DB_PATH
            try:
                batch_store_module.DB_PATH = Path(folder) / "queue.db"
                store = BatchStore()
                state = new_batch_state([], ["https://v.douyin.com/dance"])
                item = state["items"][0]
                item.update(
                    status="awaiting_review",
                    stage="review",
                    ai={"reference_image_path": "x.png", "remove_subtitles": True},
                )
                store.create(state)

                with patch.object(batch_worker, "batch_store", store):
                    state_after = batch_worker.set_item_remove_subtitles(state["id"], item["id"], False)
                    row = state_after["items"][0]
                    self.assertFalse(row["ai"]["remove_subtitles"])
                    self.assertTrue(any("不去字幕" in log["message"] for log in row["logs"]))
                    # 还没出片（confirmed）也能改：用户要求「未开始前的任务都允许修改」
                    store.mutate_item(state["id"], item["id"], lambda r: r.update(status="confirmed"))
                    state_after = batch_worker.set_item_remove_subtitles(state["id"], item["id"], True)
                    self.assertTrue(state_after["items"][0]["ai"]["remove_subtitles"])
                    # 正在出片 / 已出片：拒绝，要先回到确认
                    for blocked in ("running", "completed"):
                        store.mutate_item(state["id"], item["id"], lambda r, s=blocked: r.update(status=s))
                        with self.assertRaises(ValueError):
                            batch_worker.set_item_remove_subtitles(state["id"], item["id"], False)

                # 歌曲条目没有这个开关
                song_state = new_batch_state(["https://v.douyin.com/song"], [])
                song_state["items"][0].update(status="awaiting_review", ai={"reference_image_path": "x.png"})
                store.create(song_state)
                with patch.object(batch_worker, "batch_store", store):
                    with self.assertRaises(ValueError):
                        batch_worker.set_item_remove_subtitles(
                            song_state["id"], song_state["items"][0]["id"], False
                        )
            finally:
                batch_store_module.DB_PATH = original_db

    def test_spa_entry_is_never_cached(self) -> None:
        """SPA 入口必须 no-store：index.html 引用带 hash 的 bundle，缓存住刷新也只是旧前端。"""
        import asyncio

        from backend import app as app_module

        for path in ("/batch", "/singing", "/migrate", "/douyin", "/upscale", "/rvc"):
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


if __name__ == "__main__":
    unittest.main()
