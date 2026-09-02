from pathlib import Path
import unittest

from backend.settings import SINGING_WORKFLOW, UPSCALE_WORKFLOW, required_paths
from backend.store import format_elapsed
from backend.workflows import (
    node_by_id,
    prepare_singing_workflow,
    prepare_upscale_workflow,
)


class WorkflowPreparationTests(unittest.TestCase):
    def test_elapsed_format_matches_ui(self) -> None:
        self.assertEqual(format_elapsed("2026-09-03T00:00:00+00:00", "2026-09-03T01:02:03+00:00"), "01:02:03")

    def test_required_local_resources_exist(self) -> None:
        missing = {name: str(path) for name, path in required_paths().items() if not path.is_file()}
        self.assertEqual(missing, {})

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


if __name__ == "__main__":
    unittest.main()
