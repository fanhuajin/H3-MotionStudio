import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from bilibili_auto import BilibiliDraft, CHINA_TIME
from datetime import datetime


class ScheduleAndCategoryTests(unittest.TestCase):
    def test_category_plain_text_menu_is_identified_by_column_and_multiple_options(self):
        d=object.__new__(BilibiliDraft);d.visible=lambda c:True;d.input_column=lambda:100
        root=NS(ControlTypeName="DocumentControl",ClassName="")
        def node(name,x,y):
            return NS(Name=name,ControlTypeName="TextControl",ClassName="",
                BoundingRectangle=NS(left=x,top=y,right=x+24,bottom=y+20),GetParentControl=lambda:root)
        label=node("分区",30,100);selected=node("舞蹈",110,100)
        options=[node(name,110,150+i*45) for i,name in enumerate(("音乐","舞蹈","动画"))]
        d.controls=lambda:[label,selected]+options
        self.assertIs(d.category_option("舞蹈"),options[1])
    def test_xiaohongshu_near_boundary_selects_valid_minute(self):
        from xiaohongshu_auto import select_xiaohongshu_hour
        now=datetime(2026,10,11,19,30,tzinfo=CHINA_TIME)
        self.assertEqual(select_xiaohongshu_hour("2026-10-11","20",now),
                         datetime(2026,10,11,20,30,tzinfo=CHINA_TIME))

    def test_category_menu_excludes_duplicate_form_text(self):
        d=object.__new__(BilibiliDraft); d.visible=lambda c:True
        root=NS(ControlTypeName="DocumentControl",ClassName="")
        menu=NS(ControlTypeName="MenuItemControl",ClassName="")
        rect=NS(left=10,top=20,right=40,bottom=40,width=lambda:30,height=lambda:20)
        def text(parent):
            return NS(Name="舞蹈",ControlTypeName="TextControl",ClassName="",BoundingRectangle=rect,
                      GetParentControl=lambda:parent)
        form,option=text(root),text(menu)
        d.controls=lambda:[form,option]
        self.assertIs(d.category_option("舞蹈"),option)

    def test_category_does_not_guess_without_menu_evidence(self):
        d=object.__new__(BilibiliDraft); d.visible=lambda c:True
        root=NS(ControlTypeName="DocumentControl",ClassName="")
        d.controls=lambda:[NS(Name="舞蹈",ControlTypeName="TextControl",ClassName="",
                              GetParentControl=lambda:root)]
        self.assertIsNone(d.category_option("舞蹈"))


if __name__=="__main__":
    unittest.main()
