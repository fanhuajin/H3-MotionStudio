import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from backend import app, batch_worker


class BatchZoneTests(unittest.TestCase):
    def setUp(self):
        from backend import link_history

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        history = patch.object(link_history, "DATA_DIR", Path(directory.name))
        history.start()
        self.addCleanup(history.stop)

    def test_cross_kind_duplicate_is_removed_from_both_lists(self):
        shared = "https://v.douyin.com/shared"
        state = batch_worker.new_batch_state(
            [shared + "/?from=share", "https://v.douyin.com/song"],
            [shared, "https://v.douyin.com/dance"],
        )
        self.assertEqual(
            [(item["kind"], item["url"]) for item in state["items"]],
            [("singing", "https://v.douyin.com/song"), ("dance", "https://v.douyin.com/dance")],
        )

    def test_append_filters_cross_kind_duplicate_from_both_lists(self):
        state = batch_worker.new_batch_state(["https://v.douyin.com/original"], [])

        class Store:
            def get(self, _batch_id):
                return state

            def mutate(self, _batch_id, change):
                change(state)
                return state

        with patch.object(batch_worker, "batch_store", Store()):
            result = batch_worker.append_batch_items(
                state["id"], ["https://v.douyin.com/shared"], ["https://v.douyin.com/shared/"]
            )
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["duplicates"], 2)
        self.assertEqual(len(state["items"]), 1)

    def test_create_rejects_batch_when_every_link_is_cross_kind_duplicate(self):
        shared = "https://v.douyin.com/shared"
        with patch.object(app.batch_store, "active", return_value=None), patch.object(app.batch_store, "create") as create:
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(app.create_batch(app.BatchCreateRequest(singingUrls=[shared], danceUrls=[shared])))
        self.assertEqual(caught.exception.status_code, 400)
        create.assert_not_called()

    def test_new_batch_always_waits_for_manual_schedule(self):
        self.assertFalse(batch_worker.new_batch_state(["https://v.douyin.com/a"], [], auto_render=True)["autoRender"])
        self.assertFalse(batch_worker.new_batch_state(["https://v.douyin.com/a"], [], auto_render=False)["autoRender"])

    def test_prep_blockers_ignore_covers_and_failed_items(self):
        """2026-10-18：封面不再拦出片（出片后才从成片抽帧出封面），失败条目也不挡备料。"""
        row = {"status": "confirmed"}
        with patch.object(batch_worker, "full_auto_enabled", return_value=True), patch.object(
            batch_worker, "_covers_ready", return_value=False
        ):
            self.assertFalse(batch_worker._prep_remaining({"items": [row]}))
        row["status"] = "failed"
        self.assertFalse(batch_worker._prep_remaining({"items": [row]}))
        row["status"] = "awaiting_review"
        self.assertTrue(batch_worker._prep_remaining({"items": [row]}))

    def test_manual_render_refuses_unfinished_prep(self):
        state = {"status": "awaiting_review", "items": [{"status": "pending"}, {"status": "confirmed"}]}
        with patch.object(app, "_batch_or_404", return_value=state):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(app.start_batch_render("batch"))
        self.assertEqual(caught.exception.status_code, 409)

    def test_manual_complete_registers_existing_final_video(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "最终成片.mp4").write_bytes(b"video")
            (folder / "人物图.png").write_bytes(b"person")
            (folder / "发布文案.txt").write_text("标题：测试", encoding="utf-8")
            row = {
                "id": "item", "status": "skipped", "stage": "skipped",
                "milestones": [{"id": "video", "status": "skipped", "progress": 0}],
            }

            class Store:
                def get(self, _batch_id):
                    return {"id": "batch", "items": [row]}

                def mutate_item(self, _batch_id, _item_id, change):
                    change(row)

                def add_item_log(self, *_args, **_kwargs):
                    return None

            with patch.object(batch_worker, "_item", return_value=row), patch.object(
                batch_worker, "_recorded_publish_folder", return_value=folder
            ), patch.object(batch_worker, "batch_store", Store()):
                batch_worker.mark_item_completed("batch", "item")
            self.assertEqual(row["status"], "completed")
            self.assertEqual(row["outputs"]["videoFinal"], str(folder / "最终成片.mp4"))
            self.assertIsNone(row["warning"])

    def test_manual_complete_rejects_running_item(self):
        row = {"id": "item", "status": "running"}
        with patch.object(batch_worker, "_item", return_value=row):
            with self.assertRaises(ValueError):
                batch_worker.mark_item_completed("batch", "item")

    def test_output_falls_back_to_publish_folder_after_manual_complete(self):
        """手动标记完成时成片还没落盘 → outputs 没有 videoFinal，之后放进来也要能点开。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "dance" / "026_跳舞_测试"
            folder.mkdir(parents=True)
            video = folder / "最终成片.mp4"
            video.write_bytes(b"video")
            item = {
                "id": "item", "status": "completed", "stage": "completed",
                "kind": "dance", "outputs": {"folder": str(folder)},
            }
            with patch.object(app, "_batch_item_or_404", return_value=item), patch.object(
                app, "BATCH_OUTPUT_ROOT", root
            ), patch.object(batch_worker, "BATCH_OUTPUT_ROOT", root):
                response = asyncio.run(
                    app.batch_item_output("batch", "item", "videoFinal", download=False)
                )
                missing = None
                try:
                    asyncio.run(app.batch_item_output("batch", "item", "copy", download=False))
                except HTTPException as error:
                    missing = error
            self.assertEqual(Path(response.path).resolve(), video.resolve())
            self.assertEqual(missing.status_code, 404)

    def test_publish_output_path_returns_none_without_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "dance" / "027_跳舞_空"
            folder.mkdir(parents=True)
            item = {"outputs": {"folder": str(folder)}}
            with patch.object(batch_worker, "BATCH_OUTPUT_ROOT", root):
                self.assertIsNone(batch_worker.publish_output_path(item, "videoFinal"))

    def test_existing_covers_make_no_api_request(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for name in ("人物图.png", "发布文案.txt", "B站4x3.png", "抖音3x4.png"):
                (folder / name).write_bytes(b"existing")
            item = {"status": "confirmed"}
            with patch.object(batch_worker, "_item", return_value=item), patch.object(
                batch_worker, "_recorded_publish_folder", return_value=folder
            ), patch.object(batch_worker, "full_auto_enabled", return_value=True), patch.object(
                batch_worker, "_generate_covers_via_api", new_callable=AsyncMock
            ) as generate:
                asyncio.run(batch_worker._finish_prep_covers("batch", "item"))
            generate.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()


class WaitingQueueTests(unittest.TestCase):
    def test_rearranging_316_skipped_items_resets_them_in_one_save(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            image = Path(directory) / "person.png"
            source.write_bytes(b"source")
            image.write_bytes(b"image")
            state = {"status": "awaiting_review", "items": [
                {"id": str(index), "status": "skipped", "sourcePath": str(source),
                 "ai": {"reference_image_path": str(image)}, "skipRequested": True,
                 "revisionMode": "image", "milestones": [{"id": "video", "status": "error"}]}
                for index in range(316)
            ]}
            with patch.object(app, "_batch_or_404", return_value=state), \
                 patch.object(app.batch_store, "mutate", side_effect=lambda _, change: change(state)) as save, \
                 patch.object(batch_worker, "assign_render_numbers"), \
                 patch.object(batch_worker, "cancel_item_video_job") as cancel, \
                 patch.object(batch_worker, "uncertain_person_checkpoints", return_value=[]), \
                 patch.object(app, "_resume_batch", return_value=state):
                result = asyncio.run(app.start_batch_render("batch"))
            save.assert_called_once()
            cancel.assert_not_called()
            for row in result["items"]:
                self.assertEqual(row["status"], "confirmed")
                self.assertTrue(row["renderQueued"])
                self.assertTrue(row["forceRender"])
                self.assertFalse(row["skipRequested"])
                self.assertEqual(row["revisionMode"], "")
                self.assertEqual(row["milestones"][0]["status"], "pending")

    def test_pipeline_wait_does_not_query_http_or_comfyui(self):
        with patch("backend.pipeline.gpu_holder", return_value=None) as holder, \
             patch.object(batch_worker.httpx, "AsyncClient") as network:
            asyncio.run(batch_worker._wait_for_free_pipeline("batch", "item"))
        holder.assert_called_once()
        network.assert_not_called()

    def test_skip_during_upload_cancels_the_new_child_instead_of_leaving_it_running(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            image = Path(directory) / "person.png"
            source.write_bytes(b"source")
            image.write_bytes(b"image")
            row = {"id": "item", "kind": "dance", "status": "running", "sourcePath": str(source),
                   "ai": {"reference_image_path": str(image)}}
            client = AsyncMock()
            client.__aenter__.return_value = client
            import httpx

            client.post.return_value = httpx.Response(200, json={"id": "late-child", "status": "queued"})
            with patch.object(batch_worker, "_item", side_effect=[row, row, {**row, "status": "skipped"}]), \
                 patch.object(batch_worker, "_wait_for_free_pipeline", new_callable=AsyncMock), \
                 patch.object(batch_worker.httpx, "AsyncClient", return_value=client), \
                 patch.object(batch_worker.batch_store, "add_item_log"), \
                 patch.object(batch_worker, "_set_item") as save, \
                 patch.object(batch_worker, "_cancel_child_job_quietly", new_callable=AsyncMock) as cancel, \
                 patch.object(batch_worker, "_spawn_deferred_salvage") as salvage:
                with self.assertRaises(asyncio.CancelledError):
                    asyncio.run(batch_worker._post_video_job("batch", "item"))
            save.assert_called_once_with("batch", "item", videoJobId="late-child",
                                         childJob={"id": "late-child", "status": "queued"})
            cancel.assert_awaited_once_with("late-child", "batch", "item")
            salvage.assert_called_once_with("batch", "item", "late-child")

    def test_websocket_file_checks_do_not_block_event_loop(self):
        async def scenario():
            state = {"items": []}
            release = threading.Event()
            started = asyncio.Event()
            loop = asyncio.get_running_loop()
            checks = []

            def inspect_files(snapshot):
                loop.call_soon_threadsafe(started.set)
                checks.append(release.wait(timeout=1))
                return snapshot

            websocket = AsyncMock()
            websocket.send_json.side_effect = [None, app.WebSocketDisconnect()]
            queue = asyncio.Queue()
            queue.put_nowait(state)
            with patch.object(app.batch_store, "get", return_value=state), \
                 patch.object(app.batch_store, "subscribe", return_value=queue), \
                 patch.object(app.batch_store, "unsubscribe"), \
                 patch.object(app, "_with_publish_chain", side_effect=inspect_files):
                task = asyncio.create_task(app.batch_websocket(websocket, "batch"))
                await started.wait()
                release.set()
                await asyncio.wait_for(task, timeout=2)
            self.assertEqual(checks, [True, True])

        asyncio.run(scenario())

    def test_background_skip_really_cancels_child_job(self):
        client = AsyncMock()
        client.__aenter__.return_value = client
        with patch.object(batch_worker, "_item", return_value={"videoJobId": "child"}), \
             patch.object(batch_worker.httpx, "AsyncClient", return_value=client), \
             patch.object(batch_worker.store, "get", return_value={"status": "cancelled"}), \
             patch.object(batch_worker.asyncio, "sleep", new_callable=AsyncMock), \
             patch.object(batch_worker, "_finish_abandoned", new_callable=AsyncMock) as finish, \
             patch.object(batch_worker, "_stop_comfy_if_idle", new_callable=AsyncMock):
            asyncio.run(batch_worker._abandon_without_runner("batch", "item", deleted=False))
        client.post.assert_awaited_once_with(f"{batch_worker.BATCH_SELF_URL}/api/jobs/child/cancel")
        finish.assert_awaited_once_with("batch", "item", deleted=False)

    def test_bulk_skip_saves_once_and_only_cancels_active_rows_in_background(self):
        state = {"items": [
            {"id": str(index), "status": "confirmed", "renderQueued": True,
             "milestones": [{"id": "video", "status": "pending"}]} for index in range(350)
        ] + [{"id": "active", "status": "running"}, {"id": "done", "status": "completed"}]}
        with patch.object(batch_worker.batch_store, "mutate", side_effect=lambda _, change: change(state)) as save, \
             patch.object(batch_worker, "cancel_item_work") as cancel, \
             patch.object(batch_worker, "_spawn_orphan_abandon") as abandon:
            result = batch_worker.mark_items_skipped("batch", [row["id"] for row in state["items"]])
        save.assert_called_once()
        self.assertEqual(len(result["marked"]), 350)
        self.assertEqual(result["busy"], ["active"])
        self.assertEqual(cancel.call_count, 351)
        abandon.assert_called_once_with("batch", "active", deleted=False)
        for row in state["items"][:350]:
            self.assertEqual(row["status"], "skipped")
            self.assertFalse(row["skipRequested"])
            self.assertFalse(row["renderQueued"])
        self.assertTrue(state["items"][350]["skipRequested"])

    def test_runner_only_picks_explicitly_scheduled_items(self):
        waiting = {"id": "waiting", "status": "confirmed", "index": 1}
        queued = {"id": "queued", "status": "confirmed", "index": 2, "renderQueued": True}
        self.assertIsNone(batch_worker._next_confirmed({"items": [waiting]}))
        self.assertEqual(batch_worker._next_confirmed({"items": [waiting, queued]})["id"], "queued")

    def test_schedule_selected_waiting_and_skipped_only(self):
        state = {"status": "completed", "items": [
            {"id": "a", "status": "confirmed"},
            {"id": "b", "status": "skipped"},
            {"id": "c", "status": "confirmed"}]}
        with patch.object(app, "_batch_or_404", return_value=state), \
             patch.object(app.batch_store, "mutate", side_effect=lambda _, change: change(state)) as save, \
             patch.object(batch_worker, "prepare_item_restart", return_value=lambda row: row.update(status="confirmed")) as restart, \
             patch.object(app, "deliver_review_materials") as deliver, \
             patch.object(batch_worker, "assign_render_numbers") as number, \
             patch.object(app.batch_store, "update"), \
             patch.object(app, "_resume_batch", return_value=state):
            asyncio.run(app.start_batch_render("batch", app.BatchItemIdsRequest(itemIds=["a", "b"])))
        save.assert_called_once()
        self.assertEqual([row["id"] for row in state["items"] if row.get("renderQueued")], ["a", "b"])
        self.assertTrue(state["renderRequested"])
        self.assertFalse(state["autoRender"])
        self.assertFalse(state["shutdownAfterPrep"])
        restart.assert_called_once_with("batch", "b", state["items"][1])
        deliver.assert_not_called()
        number.assert_called_once_with("batch", ["a", "b"])

    def test_large_waiting_queue_is_saved_once(self):
        state = {"status": "awaiting_review", "items": [
            {"id": str(index), "status": "confirmed"} for index in range(350)
        ]}
        with patch.object(app, "_batch_or_404", return_value=state), \
             patch.object(batch_worker, "assign_render_numbers"), \
             patch.object(batch_worker, "_set_item") as mark, \
             patch.object(app.batch_store, "mutate", side_effect=lambda _, change: change(state)) as save, \
             patch.object(app, "_resume_batch", return_value=state):
            result = asyncio.run(app.start_batch_render("batch"))
        save.assert_called_once()
        mark.assert_not_called()
        self.assertTrue(all(row["renderQueued"] and row["updatedAt"] for row in result["items"]))

    def test_removed_auto_render_cannot_be_enabled(self):
        with patch.object(app, "_batch_or_404", return_value={}), patch.object(app.batch_store, "update") as update:
            with self.assertRaises(HTTPException):
                asyncio.run(app.set_batch_auto_render("batch", app.BatchAutoRenderRequest(enabled=True)))
        update.assert_not_called()
