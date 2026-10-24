import asyncio
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from backend import app, batch_worker, link_history


class LinkHistoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        history = patch.object(link_history, "DATA_DIR", self.root)
        history.start()
        self.addCleanup(history.stop)

    def test_backfills_current_and_download_urls_once_and_retains_deleted_history(self):
        database = self.root / "motionstudio.db"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("CREATE TABLE batches (state_json TEXT)")
            connection.execute("INSERT INTO batches VALUES (?)", (json.dumps({"items": [
                {"url": "https://v.douyin.com/old", "status": "deleted"}
            ]}),))
        mirror = self.root / "douyin-jobs.json"
        mirror.write_text(json.dumps({"job": {"job": {"url": "https://v.douyin.com/download"}}}), encoding="utf-8")
        with link_history.transaction() as known:
            self.assertEqual(len(known), 2)
            known.add("https://v.douyin.com/new")
        database.unlink()
        mirror.unlink()
        with link_history.transaction() as known:
            self.assertEqual(len(known), 3)
            self.assertIn("https://v.douyin.com/old", known)

    def test_failed_add_does_not_reserve_link(self):
        with self.assertRaises(RuntimeError):
            with link_history.transaction() as known:
                known.add("https://v.douyin.com/retry")
                raise RuntimeError("save failed")
        with link_history.transaction() as known:
            self.assertNotIn("https://v.douyin.com/retry", known)

    def test_concurrent_reservations_only_accept_once(self):
        def accept(_):
            with link_history.transaction() as known:
                url = "https://v.douyin.com/once"
                accepted = url not in known
                known.add(url)
                return accepted
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sum(executor.map(accept, range(2))), 1)

    def test_create_filters_history_and_rejects_all_repeated_without_saving(self):
        old = "https://v.douyin.com/old"
        fresh = "https://v.douyin.com/new"
        with link_history.transaction() as known:
            known.add(old)
        with patch.object(app, "batch_store") as store:
            store.active.return_value = None
            store.update.side_effect = lambda key, **values: values
            response = asyncio.run(app.create_batch(app.BatchCreateRequest(danceUrls=[old, fresh])))
            self.assertIn("过滤 1 条历史重复", response["notice"])
            self.assertEqual(response["submission"], {
                "added": 1,
                "duplicates": [{"kind": "dance", "inputIndex": 1, "url": old, "reason": "历史已有"}],
            })
            self.assertEqual([item["url"] for item in store.create.call_args.args[0]["items"]], [fresh])
            store.create.reset_mock()
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(app.create_batch(app.BatchCreateRequest(singingUrls=[fresh])))
            self.assertEqual(caught.exception.status_code, 400)
            self.assertEqual(caught.exception.detail["submission"]["added"], 0)
            self.assertEqual(caught.exception.detail["submission"]["duplicates"][0]["url"], fresh)
            store.create.assert_not_called()

    def test_append_blocks_deleted_link_but_accepts_distinct_short_link(self):
        old = "https://v.douyin.com/old"
        fresh = "https://v.douyin.com/different"
        with link_history.transaction() as known:
            known.add(old)
        state = batch_worker.new_batch_state([], [])
        state["status"] = "completed"

        class Store:
            def get(self, key):
                return state

            def mutate(self, key, change):
                change(state)
                return state

            def update(self, key, **changes):
                state.update(changes)
                return state

        store = Store()
        with patch.object(app, "batch_store", store), patch.object(batch_worker, "batch_store", store):
            response = asyncio.run(app.append_batch_items_endpoint(
                state["id"], app.BatchAppendRequest(danceUrls=["  " + old + "  ", fresh])
            ))
            self.assertEqual([item["url"] for item in response["items"]], [fresh])
            self.assertIn("过滤 1 条重复", response["notice"])
            self.assertEqual(response["submission"]["duplicates"][0]["url"], old)
            state["items"].clear()
            response = asyncio.run(app.append_batch_items_endpoint(state["id"], app.BatchAppendRequest(danceUrls=[fresh])))
            self.assertEqual(state["items"], [])
            self.assertEqual(response["submission"]["added"], 0)
            self.assertEqual(response["submission"]["duplicates"][0]["url"], fresh)

    def test_cross_kind_rejection_does_not_remember_unaccepted_link(self):
        url = "https://v.douyin.com/shared"
        with patch.object(app, "batch_store") as store:
            store.active.return_value = None
            with self.assertRaises(HTTPException):
                asyncio.run(app.create_batch(app.BatchCreateRequest(singingUrls=[url], danceUrls=[url])))
        with link_history.transaction() as known:
            self.assertNotIn(url, known)

    def test_create_save_failure_does_not_remember_link(self):
        url = "https://v.douyin.com/not-saved"
        with patch.object(app, "batch_store") as store:
            store.active.return_value = None
            store.create.side_effect = RuntimeError("save failed")
            with self.assertRaises(RuntimeError):
                asyncio.run(app.create_batch(app.BatchCreateRequest(danceUrls=[url])))
        with link_history.transaction() as known:
            self.assertNotIn(url, known)

    def test_details_identify_input_positions_and_all_duplicate_reasons(self):
        old = "https://v.douyin.com/old"
        shared = "https://v.douyin.com/shared"
        current = "https://v.douyin.com/current"
        fresh = "https://v.douyin.com/new"
        details = link_history.duplicate_details(
            [old, shared, fresh, fresh + "/?from=share", current], [shared], {old},
            [{"url": current, "status": "confirmed"}],
        )
        self.assertEqual(
            [(item["kind"], item["inputIndex"], item["url"], item["reason"]) for item in details],
            [
                ("singing", 1, old, "历史已有"),
                ("singing", 2, shared, "唱歌与跳舞重复，两边均未添加"),
                ("singing", 4, fresh + "/?from=share", "本次输入重复，已保留前一条"),
                ("singing", 5, current, "当前队列已有"),
                ("dance", 1, shared, "唱歌与跳舞重复，两边均未添加"),
            ],
        )
