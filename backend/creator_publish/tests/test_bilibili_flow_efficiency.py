import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from bilibili_auto import BilibiliDraft


def node(name, kind="TextControl", cls="", parent=None, y=100):
    return NS(Name=name,ControlTypeName=kind,ClassName=cls,
        BoundingRectangle=NS(left=100,top=y,right=130,bottom=y+20),
        GetParentControl=lambda:parent)


class BilibiliEfficiencyTests(unittest.TestCase):
    def test_cover_visible_does_not_scroll_to_top(self):
        d=object.__new__(BilibiliDraft);d.locate=Mock(return_value=object());d.scroll_page=Mock()
        d.reveal_bilibili_cover()
        d.scroll_page.assert_not_called()

    def test_cover_scroll_stops_as_soon_as_visible(self):
        d=object.__new__(BilibiliDraft);d.locate=Mock(side_effect=[None,object()]);d.scroll_page=Mock()
        d.reveal_bilibili_cover()
        d.scroll_page.assert_called_once_with("up",2)

    def test_matching_tags_exclude_recommended_same_name(self):
        d=object.__new__(BilibiliDraft)
        actual=node("",cls="label-item-v2-content")
        hot=node("",cls="hot-tag-container")
        d.controls=lambda:[node("还可以添加9个标签"),node("舞蹈",parent=actual),node("舞蹈",parent=hot)]
        self.assertTrue(d.bilibili_tags_match(["舞蹈"]))
        d.controls=lambda:[node("还可以添加9个标签"),node("舞蹈",parent=hot)]
        self.assertFalse(d.bilibili_tags_match(["舞蹈"]))

    def test_menu_option_outside_viewport_cannot_be_clicked(self):
        d=object.__new__(BilibiliDraft);d.visible=lambda c:True;d.input_column=lambda:100
        root=node("",kind="DocumentControl")
        viewport=node("",kind="GroupControl",cls="drop-list-v2-content-wrp",parent=root,y=500)
        viewport.BoundingRectangle.right=360;viewport.BoundingRectangle.bottom=800
        clipped=node("舞蹈",parent=viewport,y=100)
        visible=node("舞蹈",parent=viewport,y=550)
        d.controls=lambda:[viewport,clipped]
        self.assertIsNone(d.category_option("舞蹈"))
        d.controls=lambda:[viewport,clipped,visible]
        self.assertIs(d.category_option("舞蹈"),visible)

    def test_cover_checkpoint_requires_same_files_and_same_title(self):
        d=object.__new__(BilibiliDraft)
        with TemporaryDirectory() as directory:
            video=Path(directory)/"video.mp4";video.write_bytes(b"video")
            cover=Path(directory)/"cover.png";cover.write_bytes(b"cover")
            field=node("请输入稿件标题",kind="EditControl")
            field.GetValuePattern=lambda:NS(Value="标题")
            d.controls=lambda:[field];d.control_names=lambda:["video"]
            d.bilibili_checkpoint={"cover_signature":d.bilibili_cover_signature(video,cover)}
            self.assertTrue(d.bilibili_cover_is_confirmed(video,"标题",cover))
            self.assertFalse(d.bilibili_cover_is_confirmed(video,"另一个标题",cover))
            cover.write_bytes(b"changed")
            self.assertFalse(d.bilibili_cover_is_confirmed(video,"标题",cover))

    def test_matching_description_does_not_need_keyboard(self):
        d=object.__new__(BilibiliDraft)
        d.controls=lambda:[node("正文",kind="GroupControl")]
        self.assertTrue(d.bilibili_description_matches("正文"))
        self.assertFalse(d.bilibili_description_matches("新的正文"))

    def test_other_work_with_generic_video_filename_is_not_overwritten(self):
        d=object.__new__(BilibiliDraft)
        field=node("请输入稿件标题",kind="EditControl")
        field.GetValuePattern=lambda:NS(Value="上一条作品标题")
        d.controls=lambda:[field]
        with self.assertRaisesRegex(RuntimeError,"未覆盖当前稿件"):
            d.validate_bilibili_draft(Path("最终成片.mp4"),"本条作品标题")

    def test_retry_skips_confirmed_cover_and_matching_text(self):
        from test_complete_workflows import CompleteWorkflowTests
        fixture=CompleteWorkflowTests()
        draft,events=fixture.harness(BilibiliDraft)
        draft.bilibili_cover_is_confirmed=Mock(return_value=True)
        draft.bilibili_tags_match=Mock(return_value=True)
        draft.bilibili_description_matches=Mock(return_value=True)
        with patch("builtins.print"):
            fixture.run_work(draft)
        self.assertNotIn("cover",events)
        self.assertNotIn("fill_tags",events)
        self.assertNotIn("description",events)
        draft.reveal_bilibili_cover.assert_not_called()
        self.assertIn("schedule",events)
        self.assertEqual(events.count("submitted"),1)


if __name__ == "__main__":
    unittest.main()
