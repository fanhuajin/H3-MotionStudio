from contextlib import contextmanager
from pathlib import Path
import json
import os
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

                    with self.assertRaises(ValueError):
                        batch_worker.set_item_ratio("b1", "it2", "4:3")   # 已确认出片 → 锁定
                    with self.assertRaises(ValueError):
                        batch_worker.set_item_ratio("b1", "it1", "16:9")  # 非法比例
            finally:
                batch_worker.batch_store, batch_worker.batch_ai.compose_image_prompt = originals
        self.assertEqual(stub.state["items"][1]["ratio"], "9:16")

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

        # 1) 链接指纹：大小写、结尾斜杠、分享查询串都不算新任务
        self.assertEqual(url_key("HTTPS://V.Douyin.com/AbC/?vid=1#x"), url_key("https://v.douyin.com/AbC"))
        self.assertEqual(item_key("singing", "https://v.douyin.com/a/"), item_key("singing", "https://v.douyin.com/a"))
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
        for entry in run_state["items"]:
            entry["startRequested"] = True   # 两条都由用户点过「启动这一条」
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

    def test_queue_only_runs_after_explicit_start(self) -> None:
        """先把任务排进队列、点「启动」才跑流程（2026-09-10 用户要求）。"""
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
            # 新建批次：只入队、状态仍是 queued、没有起 runner
            created = asyncio.run(
                app_module.create_batch(
                    app_module.BatchCreateRequest(singingUrls=["https://v.douyin.com/a"])
                )
            )
            self.assertEqual(created["status"], "queued")
            self.assertIn("点「启动」后开始处理", created["notice"])
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

            # 点「启动这一条」才真的跑这一条，并且顺手把 ComfyUI 拉起来预热
            stub.state["items"][1]["status"] = "pending"
            started = asyncio.run(app_module.start_batch_item("b1", stub.state["items"][1]["id"]))
            self.assertEqual(started["status"], "running")
            self.assertEqual(len(spawned), 2)   # run_batch + ComfyUI 预热
            self.assertIn("已启动这一条", started["notice"])
            self.assertTrue(stub.state["items"][1]["startRequested"])
            self.assertFalse(stub.state["items"][0].get("startRequested", False))  # 别的条目不受影响

            # 已经启动过的条目不能重复启动
            stub.state["items"][1]["status"] = "awaiting_review"
            with self.assertRaises(app_module.HTTPException):
                asyncio.run(app_module.start_batch_item("b1", stub.state["items"][1]["id"]))

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
        """只有点过「启动这一条」的条目才会跑；`awaiting_review` 与没启动的排队条目都不跑。"""
        from backend.batch_worker import _next_work

        state = {
            "items": [
                {"id": "a", "status": "awaiting_review"},
                {"id": "b", "status": "pending"},
                {"id": "c", "status": "confirmed"},
            ]
        }
        self.assertEqual(_next_work(state)["id"], "c")      # 用户确认出片 → 确认本身就是开始
        state["items"][2]["status"] = "completed"
        self.assertIsNone(_next_work(state))                # 没点「启动这一条」→ 谁都不跑
        state["items"][1]["startRequested"] = True
        self.assertEqual(_next_work(state)["id"], "b")      # 点过启动 → 轮到它备料
        state["items"][1]["status"] = "revising"
        self.assertEqual(_next_work(state)["id"], "b")      # 按意见重做同样由启动标记驱动
        state["items"][1]["startRequested"] = False
        self.assertIsNone(_next_work(state))                # 消费掉标记后不再重复跑

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
        """文案必须看着最终候选图写：图文一致是硬要求。"""
        prompt = batch_ai.copy_prompt(
            song_name="爱如潮水", song_mood="抒情慢板，克制的失恋感", description="原作品描述"
        )
        self.assertIn("第一张图就是本条最终要发布的人物图", prompt)
        self.assertIn("必须和画面里**实际出现**", prompt)
        self.assertIn("画面里没有的东西一律不要写", prompt)
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

    def test_batch_ai_fallback_and_action_plan_keep_item_runnable(self) -> None:
        """模型降级时条目仍可继续：文案退到源作品信息，动作/运镜按时长铺满。"""
        result = batch_ai.fallback_result(
            kind="singing",
            description="粉色限定 #爱如潮水remix",
            tags=["爱如潮水", "#翻唱"],
        )
        self.assertEqual(result["style_source"], "video")
        self.assertEqual(result["title"], "粉色限定")
        self.assertEqual(result["tags"], ["爱如潮水", "翻唱"])
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

    def test_batch_deliver_no_longer_renders_covers(self) -> None:
        """双封面改由用户自己在 GPT 聊天里出，交付阶段只留成片与发布文案。"""
        source = (Path(__file__).parents[1] / "backend" / "batch_worker.py").read_text(encoding="utf-8")
        self.assertNotIn("render_covers", source)
        self.assertNotIn("coverBilibili", source)
        self.assertNotIn("coverDouyin", source)
        # 交付仍然要写发布文案
        self.assertIn("发布文案.txt", source)

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
