"""封面弹窗中的已上传缩略图名称与保存回读，不操作桌面。"""
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from xiaohongshu_auto import XiaohongshuDraft


def control(name,kind="ButtonControl",x=100,y=200,enabled=True,classes=""):
    return NS(Name=name,ControlTypeName=kind,ClassName=classes,IsEnabled=enabled,
        BoundingRectangle=NS(left=x,top=y,right=x+150,bottom=y+30,width=lambda:150,height=lambda:30))


class CoverTests(unittest.TestCase):
    def draft(self):
        d=object.__new__(XiaohongshuDraft);d.visible=lambda c:True
        d.check=Mock();d.now=time.monotonic;d.click=Mock()
        return d

    def test_combined_uploaded_thumbnail_is_recognized(self):
        d=self.draft();thumbnail=control("已上传封面 清除已上传封面")
        d.controls=lambda:[thumbnail,control("清除已上传封面",x=220)]
        self.assertIs(d.uploaded_cover(),thumbnail)

    def test_clear_button_alone_is_not_upload_evidence(self):
        d=self.draft();d.controls=lambda:[control("清除已上传封面")]
        self.assertIsNone(d.uploaded_cover())

    def test_exact_uploaded_label_remains_supported(self):
        d=self.draft();label=control("已上传封面",kind="TextControl")
        d.controls=lambda:[label]
        self.assertIs(d.uploaded_cover(),label)

    def test_combined_thumbnail_recovery_saves_when_done_is_enabled(self):
        d=self.draft();done=control("完成");thumbnail=control("已上传封面 清除已上传封面")
        d.locate=Mock(side_effect=[done,None]);d.controls=lambda:[thumbnail]
        with patch("xiaohongshu_auto.time.sleep"):
            d.finish_cover()
        d.click.assert_called_once_with(done)

    def test_upload_wait_reads_until_combined_thumbnail_appears(self):
        d=self.draft();thumbnail=control("已上传封面 清除已上传封面")
        d.controls=Mock(side_effect=[[control("清除已上传封面")],[thumbnail]])
        with patch("xiaohongshu_auto.time.sleep"):
            self.assertIs(d.wait_uploaded_cover(),thumbnail)
        d.click.assert_not_called()

    def test_retained_template_view_switches_observed_crop_tab_once(self):
        d=self.draft();done=control("完成");crop=control("裁剪",kind="TextControl")
        upload=control("上传")
        d.controls=Mock(side_effect=[
            [done,crop,control("模版",kind="TextControl")],
            [done,crop],
            [done,upload]])
        with patch("xiaohongshu_auto.time.sleep"):
            self.assertIs(d.cover_upload_button(),upload)
        d.click.assert_called_once_with(crop)

    def test_missing_crop_tab_stops_without_guessing_other_toolbar(self):
        d=self.draft();d.now=Mock(side_effect=[0,0,9])
        d.controls=lambda:[control("完成"),control("模版",kind="TextControl"),control("滤镜",kind="TextControl")]
        with patch("xiaohongshu_auto.time.sleep"),self.assertRaisesRegex(RuntimeError,"唯一的“裁剪”页签"):
            d.cover_upload_button()
        d.click.assert_not_called()

    def test_crop_switch_without_uploader_stops_after_single_click(self):
        d=self.draft();crop=control("裁剪",kind="TextControl")
        d.now=Mock(side_effect=[0,0,1,9]);d.controls=lambda:[control("完成"),crop]
        with patch("xiaohongshu_auto.time.sleep"),self.assertRaisesRegex(RuntimeError,"已切换到裁剪页"):
            d.cover_upload_button()
        d.click.assert_called_once_with(crop)

    def test_existing_upload_entry_does_not_change_toolbar(self):
        d=self.draft();upload=control("上传")
        d.controls=lambda:[control("完成"),control("裁剪",kind="TextControl"),upload]
        self.assertIs(d.cover_upload_button(),upload)
        d.click.assert_not_called()

    def test_crop_word_without_cover_dialog_does_not_trigger_click(self):
        d=self.draft();d.now=Mock(side_effect=[0,0,9]);d.controls=lambda:[control("裁剪",kind="TextControl")]
        with patch("xiaohongshu_auto.time.sleep"),self.assertRaisesRegex(RuntimeError,"封面编辑器"):
            d.cover_upload_button()
        d.click.assert_not_called()


if __name__=="__main__":
    unittest.main()
