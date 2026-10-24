import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import batch_worker as worker


def row(key, kind="dance", **values):
    return {"id": key, "kind": kind, "status": "confirmed", "index": 10, "kindIndex": 10, **values}


class RenderNumberTests(unittest.TestCase):
    def assign(self, state, selected):
        with patch.object(worker.batch_store, "mutate", side_effect=lambda b, change: change(state)), \
             patch.object(worker, "_existing_publish_folder", return_value=None):
            worker.assign_render_numbers("batch", selected)

    def test_only_scheduled_items_take_numbers_and_each_kind_starts_at_one(self):
        items = [row("failed", status="failed"), row("waiting"), row("d1"), row("d2"), row("s1", "singing")]
        state = {"items": items}
        self.assign(state, ["d1", "d2", "s1"])
        self.assertEqual([item.get("renderIndex") for item in items], [None, None, 1, 2, 1])
        self.assertTrue(worker._ideal_publish_folder(items[2]).name.startswith("001_"))
        self.assertTrue(worker._ideal_publish_folder(items[3]).name.startswith("002_"))

    def test_next_schedule_continues_without_renumbering_or_reusing_deleted_number(self):
        state = {"items": [row("later"), row("first")]}
        self.assign(state, ["first"])
        self.assign(state, ["later", "first"])
        self.assertEqual([item["renderIndex"] for item in state["items"]], [2, 1])
        for item in state["items"]:
            item["renderQueued"] = True
        self.assertEqual(worker._next_confirmed(state)["id"], "first")
        state["items"] = [row("new")]
        self.assign(state, ["new"])
        self.assertEqual(state["items"][0]["renderIndex"], 3)

    def test_legacy_queued_and_completed_numbers_remain_and_new_numbers_continue(self):
        state = {"items": [row("queued", renderQueued=True, kindIndex=7), row("done", status="completed", kindIndex=9), row("new", kindIndex=30)]}
        self.assign(state, ["new"])
        self.assertEqual([item["renderIndex"] for item in state["items"]], [7, 9, 10])

    def test_existing_folder_number_is_preserved_when_scheduled_again(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "005_跳舞_旧作品"
            folder.mkdir()
            state = {"items": [row("old", outputs={"folder": str(folder)}), row("new")]}
            with patch.object(worker, "BATCH_OUTPUT_ROOT", Path(directory)):
                self.assign(state, ["old", "new"])
            self.assertEqual([item["renderIndex"] for item in state["items"]], [5, 6])
            self.assertTrue(folder.is_dir())
