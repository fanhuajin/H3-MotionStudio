"""重放三端完整控制流程及关键失败边界，所有外部输入均替换为离线桩。"""
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from bilibili_auto import BilibiliDraft
from douyin_auto import DouyinDraft
from xiaohongshu_auto import XiaohongshuDraft


class CompleteWorkflowTests(unittest.TestCase):
    def harness(self, cls, fail=None):
        draft=object.__new__(cls); events=[]
        bounds=NS(left=10,top=10,right=100,bottom=40,width=lambda:90,height=lambda:30)
        button=NS(Name="发布",ControlTypeName="ButtonControl",BoundingRectangle=bounds)
        body=NS(Name="正文",ControlTypeName="TextControl",BoundingRectangle=bounds)
        def action(name, result=None):
            def call(*args,**kwargs):
                events.append(name)
                if fail==name:raise RuntimeError(f"{name}失败")
                return result
            return call
        for name in ("select_browser","next_upload","upload","start_keyboard_helper","scroll_page","input_column",
                     "cover","set_cover","keyboard_fill","declaration","set_declaration","category","fill_tags","description",
                     "schedule","wait_uploaded","choose_file","keys","keyboard_field","focus_description",
                     "vertical_cover","horizontal_cover","fill_douyin_description"):
            setattr(draft,name,action(name,True if name=="fill_tags" else None))
        draft.on_stage=lambda stage:events.append("stage:"+stage)
        draft.on_submit=action("submitted")
        draft.click=action("click");draft.check=Mock();draft.now=time.monotonic;draft.visible=lambda c:True
        draft.find=lambda *args,**kwargs:button
        draft.form_find=lambda *args,**kwargs:body
        draft.wait_name=lambda *args,**kwargs:body
        draft.wait_upload_button=lambda *args,**kwargs:button
        draft.wait_bilibili_form=Mock()
        draft.reveal_bilibili_cover=Mock()
        draft.bilibili_cover_is_confirmed=Mock(return_value=False)
        draft.bilibili_tags_match=Mock(return_value=False)
        draft.bilibili_description_matches=Mock(return_value=False)
        draft.validate_bilibili_draft=Mock()
        draft.locate=lambda name,*args,**kwargs:body if name in ("添加作品简介","输入正文描述，真诚有价值的分享予人温暖","笔记含AI合成内容") else None
        if cls is DouyinDraft:
            draft.controls=lambda:[NS(Name="标题",ControlTypeName="TextControl"),NS(Name="内容管理",ControlTypeName="TextControl")]
        else:
            draft.controls=lambda:[button]
        draft.control_names=lambda:["发布成功"]
        return draft,events

    def run_work(self,draft):
        cover=(Path("横.png"),Path("竖.png")) if type(draft) is DouyinDraft else Path("封面.png")
        draft.run(Path("work/最终成片.mp4"),"标题","正文",["标签"],cover,"舞蹈",None)

    def test_all_platforms_complete_flow_twice_from_entry_to_success(self):
        for cls in (BilibiliDraft,DouyinDraft,XiaohongshuDraft):
            for replay in range(2):
                with self.subTest(platform=cls.__name__,replay=replay),patch("builtins.print"):
                    d,events=self.harness(cls);self.run_work(d)
                    self.assertEqual(events.count("submitted"),1)
                    self.assertLess(events.index("schedule"),events.index("wait_uploaded"))
                    self.assertLess(events.index("wait_uploaded"),events.index("submitted"))
                    self.assertLess(events.index("submitted"),events.index("stage:confirm_result"))

    def test_schedule_or_upload_failure_blocks_submission_on_every_platform(self):
        for cls in (BilibiliDraft,DouyinDraft,XiaohongshuDraft):
            for failure in ("schedule","wait_uploaded"):
                with self.subTest(platform=cls.__name__,failure=failure),patch("builtins.print"):
                    d,events=self.harness(cls,failure)
                    with self.assertRaisesRegex(RuntimeError,"失败"):self.run_work(d)
                    self.assertNotIn("submitted",events)
                    self.assertNotIn("stage:confirm_result",events)

    def test_cover_failure_blocks_settings_and_submission_on_every_platform(self):
        for cls,failure in ((BilibiliDraft,"cover"),(DouyinDraft,"vertical_cover"),(XiaohongshuDraft,"set_cover")):
            with self.subTest(platform=cls.__name__),patch("builtins.print"):
                d,events=self.harness(cls,failure)
                with self.assertRaisesRegex(RuntimeError,"失败"):self.run_work(d)
                self.assertNotIn("schedule",events)
                self.assertNotIn("submitted",events)


if __name__=="__main__":unittest.main()
