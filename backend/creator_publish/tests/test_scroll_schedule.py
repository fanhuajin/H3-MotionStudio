import calendar
import time
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from bilibili_auto import CHINA_TIME
from bilibili_auto import BilibiliDraft
from douyin_auto import DouyinDraft
from xiaohongshu_auto import XiaohongshuDraft
from schedule_controls import calendar_day, month_calendar_day, scroll_select, wait_hour_readback
from douyin_schedule import calendar_day as douyin_calendar_day, date_candidates, find_calendar_day


def rect(x,y,w=24,h=20):
    return NS(left=x,top=y,right=x+w,bottom=y+h,width=lambda:w,height=lambda:h)


def control(name,x=100,y=200,kind="TextControl",w=24):
    return NS(Name=name,ControlTypeName=kind,BoundingRectangle=rect(x,y,w),IsEnabled=True)


def grid(year,month,firstweekday=0):
    first=datetime(year,month,1)
    start=first-timedelta(days=(first.weekday()-firstweekday)%7)
    return [control(str((start+timedelta(days=i)).day),105+(i%7)*30,200+(i//7)*30)
            for i in range(42)]


class ScrollScheduleTests(unittest.TestCase):
    def draft(self):
        return NS(check=Mock(),click=Mock(),scroll_at=Mock(),now=time.monotonic)

    def test_rolls_up_inside_current_list_and_clicks_target(self):
        d=self.draft()
        first=[control(str(i),y=200+i*20) for i in range(12,15)]
        second=[control(str(i),y=200+i*20) for i in range(9,12)]
        third=[control(str(i),y=200+i*20) for i in range(6,9)]
        with patch("schedule_controls.time.sleep"):
            scroll_select(d,Mock(side_effect=[first,second,third]),6,"小时")
        self.assertEqual([c.args[0] for c in d.scroll_at.call_args_list],["up","up"])
        self.assertEqual(d.scroll_at.call_args_list[1].args[2],(112,410))
        d.click.assert_called_once_with(third[0])

    def test_list_disappearance_never_scrolls_whole_page(self):
        d=self.draft(); options=[control("12"),control("13",y=225)]
        with patch("schedule_controls.time.sleep"),self.assertRaisesRegex(RuntimeError,"已关闭"):
            scroll_select(d,Mock(side_effect=[options,[]]),6,"小时")
        self.assertEqual(d.scroll_at.call_count,1)
        d.click.assert_not_called()

    def test_stalled_list_has_bounded_scrolls(self):
        d=self.draft(); options=[control("12"),control("13",y=225)]
        with patch("schedule_controls.time.sleep"),self.assertRaisesRegex(RuntimeError,"未变化"):
            scroll_select(d,lambda:options,6,"小时")
        self.assertEqual(d.scroll_at.call_count,5)
        d.click.assert_not_called()

    def test_calendar_supports_both_week_starts_and_adjacent_month_duplicates(self):
        for weekstart in (0,6):
            cs=grid(2026,10,weekstart)
            chosen=calendar_day(cs,2026,10,1)
            expected=(datetime(2026,10,1).weekday()-weekstart)%7
            self.assertIs(chosen,cs[expected])
            self.assertIs(calendar_day(cs,2026,10,31),cs[expected+30])

    def test_incomplete_calendar_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError,"七列"):
            calendar_day(grid(2026,10)[:-1],2026,10,12)

    def test_bilibili_blank_edges_preserve_real_date_positions(self):
        cs=grid(2026,10,6)[4:35]
        self.assertIs(month_calendar_day(cs,2026,10,12,"B站"),cs[11])
        self.assertIs(month_calendar_day(cs,2026,10,31,"B站"),cs[30])
        with self.assertRaisesRegex(RuntimeError,"完整日期序列"):
            month_calendar_day(cs[:10]+cs[11:],2026,10,12,"B站")

    def test_calendar_merges_parent_and_text_for_same_cell(self):
        cs=grid(2026,10)
        wrappers=[control(c.Name,c.BoundingRectangle.left-2,c.BoundingRectangle.top-2,"GroupControl",28) for c in cs]
        self.assertIs(calendar_day(cs+wrappers,2026,10,12),cs[14])

    def test_douyin_accepts_missing_adjacent_month_nodes_on_edge_rows(self):
        for year,month in ((2026,10),(2027,2),(2028,2),(2026,11)):
            for weekstart in (0,6):
                cs=grid(year,month,weekstart)
                offset=(datetime(year,month,1).weekday()-weekstart)%7
                length=calendar.monthrange(year,month)[1]
                current=cs[offset:offset+length]
                self.assertIs(douyin_calendar_day(current,year,month,12),cs[offset+11])
                self.assertIs(douyin_calendar_day(cs,year,month,length),cs[offset+length-1])

    def test_douyin_missing_current_month_date_is_rejected(self):
        cs=grid(2026,10);cs.pop(13)
        with self.assertRaisesRegex(RuntimeError,"完整日期序列"):
            douyin_calendar_day(cs,2026,10,12)

    def test_douyin_text_top_variation_and_duplicate_cell_wrappers(self):
        cs=grid(2026,10)
        wrappers=[control(c.Name,c.BoundingRectangle.left-5,c.BoundingRectangle.top-10,"GroupControl",34) for c in cs]
        for w in wrappers:
            b=w.BoundingRectangle;b.bottom=b.top+40;b.height=lambda:40
        self.assertIs(douyin_calendar_day(cs+wrappers,2026,10,12),cs[14])

    def test_douyin_unrelated_number_list_cannot_be_used_as_calendar(self):
        cs=[control(str(i),100,100+i*25) for i in range(1,32)]
        with self.assertRaisesRegex(RuntimeError,"七个独立列"):
            douyin_calendar_day(cs,2026,10,12)

    def test_douyin_calendar_does_not_compress_missing_grid_positions(self):
        cs=grid(2026,10)
        # 日期数字仍连续，但整周在视觉位置上跳了一行，不能按数字盲选。
        for c in cs[14:]:
            c.BoundingRectangle.top+=30;c.BoundingRectangle.bottom+=30
        with self.assertRaisesRegex(RuntimeError,"行间距"):
            douyin_calendar_day(cs,2026,10,12)

    def test_douyin_panel_bounds_include_columns_outside_short_month_heading(self):
        d=self.draft();d.visible=lambda c:True
        header=control("2026年10月",240,100,w=35)
        panel=NS(ClassName="semi-datepicker-panel",ControlTypeName="GroupControl",BoundingRectangle=rect(50,90,360,290))
        header.GetParentControl=lambda:panel
        days=grid(2026,10)
        for c in days:
            c.BoundingRectangle.left-=40;c.BoundingRectangle.right-=40
        self.assertEqual(date_candidates(d,header,[header]+days),days)

    def test_douyin_waits_for_calendar_nodes_without_reopening_or_clicking(self):
        d=self.draft();d.visible=lambda c:True
        header=control("2026年10月",220,100);days=grid(2026,10)
        d.controls=Mock(side_effect=[days[:10],days])
        with patch("douyin_schedule.time.sleep"):
            self.assertIs(find_calendar_day(d,header,datetime(2026,10,12)),days[14])
        d.click.assert_not_called();d.scroll_at.assert_not_called()

    def test_douyin_calendar_failure_saves_only_date_candidates_and_never_clicks(self):
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory
        d=self.draft();d.visible=lambda c:True;d.now=Mock(side_effect=[0,0,6])
        header=control("2026年10月",220,100);days=grid(2026,10);days.pop(13)
        d.controls=Mock(return_value=days)
        with TemporaryDirectory() as folder,patch("douyin_schedule.Path",return_value=Path(folder)/"module.py"),patch("douyin_schedule.time.sleep"):
            with self.assertRaisesRegex(RuntimeError,"诊断已保存"):
                find_calendar_day(d,header,datetime(2026,10,12))
            diagnostic=json.loads((Path(folder)/"douyin_datepicker_diagnostic.json").read_text(encoding="utf-8"))
            self.assertEqual(diagnostic["target"],"2026-10-12 00:00:00")
            self.assertTrue(all(c["name"].isdigit() for c in diagnostic["candidates"]))
        d.click.assert_not_called();d.scroll_at.assert_not_called()

    def test_readback_waits_for_page_update(self):
        d=self.draft(); selected=datetime(2026,10,12,6,tzinfo=CHINA_TIME)
        with patch("schedule_controls.time.sleep"):
            actual=wait_hour_readback(d,Mock(side_effect=["2026-10-12 12:17","2026-10-12 06:17"]),selected,"平台")
        self.assertEqual(actual.hour,6)
        self.assertEqual(actual.minute,17)

    def test_wrong_day_prevents_success(self):
        d=self.draft(); d.now=Mock(side_effect=[0,0,6])
        with patch("schedule_controls.time.sleep"),self.assertRaisesRegex(RuntimeError,"回读不一致"):
            wait_hour_readback(d,lambda:"2026-10-13 06:17",datetime(2026,10,12,6),"平台")

    def test_xiaohongshu_complete_schedule_uses_calendar_and_multiple_scrolls(self):
        d=object.__new__(XiaohongshuDraft); selected=(datetime.now(CHINA_TIME)+timedelta(days=1)).replace(hour=6,minute=0,second=0,microsecond=0)
        d.check=Mock(); d.now=time.monotonic; d.visible=lambda c:True
        label=control("定时发布",100,50)
        e=control("",100,90,"EditControl",400); pattern=NS(Value=selected.replace(hour=12,minute=17).strftime("%Y-%m-%d %H:%M"))
        e.GetValuePattern=lambda:pattern
        year=control(f"{selected.year}年",200,110)
        month=control(f"{selected.month}月",310,110)
        days=grid(selected.year,selected.month)
        hour=control("时",300,500)
        current={"start":12,"open":False,"time":False}
        def controls():
            cs=[label,e]
            if current["open"]: cs += [year,month]+days
            if current["time"]: cs += [hour]+[control(f"{i:02d}",290,530+(i-current['start'])*25) for i in range(current['start'],current['start']+3)]
            return cs
        def click(c):
            if c is e:
                current["open"]=True
                current["time"]=True
            elif c in days: current["time"]=True
            elif c.Name=="06": pattern.Value=selected.replace(minute=17).strftime("%Y-%m-%d %H:%M")
        d.controls=controls; d.form_find=lambda name:label; d.wait_name=lambda name:hour
        d.click=Mock(side_effect=click); d.click_at=Mock()
        def scroll(direction,amount,target):
            self.assertEqual(direction,"up"); self.assertEqual(amount,1)
            self.assertGreater(target[0],250)
            current['start']-=3
        d.scroll_at=Mock(side_effect=scroll)
        d.keys=Mock(side_effect=AssertionError("不允许键盘输入时间"));d.clipboard=NS(copy=Mock(side_effect=AssertionError("不允许粘贴时间")))
        with patch("xiaohongshu_auto.time.sleep"),patch("schedule_controls.time.sleep"):
            d.schedule(selected)
        self.assertEqual(d.scroll_at.call_count,2)
        self.assertEqual(d.actual_publish_at.hour,6)
        d.keys.assert_not_called(); d.clipboard.copy.assert_not_called()

    def test_douyin_complete_schedule_scrolls_hour_column_only(self):
        d=object.__new__(DouyinDraft); selected=(datetime.now(CHINA_TIME)+timedelta(days=1)).replace(hour=6,minute=0,second=0,microsecond=0)
        d.check=Mock(); d.now=time.monotonic; d.visible=lambda c:True
        header=control(f"{selected.year}年{selected.month}月",220,100)
        clock=control("12:17",350,650)
        e=control("日期和时间",100,90,"EditControl",400); pattern=NS(Value=selected.replace(hour=12,minute=17).strftime("%Y-%m-%d %H:%M"));e.GetValuePattern=lambda:pattern
        close=control("发布时间",100,50); days=grid(selected.year,selected.month)
        current={"start":12,"time":False}
        def controls():
            cs=[header,clock]+days
            if current['time']:
                cs += [control(f"{i:02d}",100,300+(i-current['start'])*25,"ListItemControl") for i in range(current['start'],current['start']+3)]
                cs += [control(f"{i:02d}",200,300+i*25,"ListItemControl") for i in range(3)]
            return cs
        def click(c):
            if c is clock: current['time']=True
            elif c.Name=="06" and c.ControlTypeName=="ListItemControl": pattern.Value=selected.replace(minute=17).strftime("%Y-%m-%d %H:%M")
        d.controls=controls;d.find=lambda name,editable=False:e if name=="日期和时间" else close
        d.click=Mock(side_effect=click);d.disable_saving=Mock();d.select_option=Mock()
        def scroll(direction,amount,target):
            self.assertEqual(direction,"up");self.assertEqual(amount,1);self.assertEqual(target[0],112)
            current['start']-=3
        d.scroll_at=Mock(side_effect=scroll)
        d.keys=Mock(side_effect=AssertionError("不允许直接输入时间"))
        with patch("schedule_controls.time.sleep"),patch("douyin_auto.time.sleep"):
            d.schedule(selected)
        d.select_option.assert_called_once_with("定时发布")
        self.assertEqual(d.scroll_at.call_count,2)
        self.assertEqual(d.actual_publish_at.hour,6)
        d.keys.assert_not_called()

    def test_bilibili_complete_schedule_scrolls_and_reads_back(self):
        d=object.__new__(BilibiliDraft);selected=(datetime.now(CHINA_TIME)+timedelta(days=1)).replace(hour=6,minute=0,second=0,microsecond=0)
        d.check=Mock();d.now=time.monotonic;d.visible=lambda c:True
        label=control("定时发布",100,50);date=control(selected.strftime("%Y-%m-%d"));clock=control("12:17",350,650)
        header=control(f"{selected.year}年{selected.month}月",220,100);days=grid(selected.year,selected.month)
        current={"start":12,"time":False}
        def controls():
            cs=[header]+days
            if current['time']:cs += [control(f"{i:02d}",353,430+(i-current['start'])*25) for i in range(current['start'],current['start']+3)]
            return cs
        def click(c):
            if c is clock:current['time']=True
            elif c.Name=="06":clock.Name="06:17"
        d.controls=controls;d.reveal_schedule=lambda:label;d.find=lambda name:label
        d.hour_option_in_view=lambda option:True
        d.schedule_fields=lambda:(date,clock);d.click=Mock(side_effect=click)
        def scroll(direction,amount,target):
            self.assertEqual(direction,"up");self.assertEqual(amount,1);current['start']-=3
        d.scroll_at=Mock(side_effect=scroll);d.keys=Mock(side_effect=AssertionError("不允许输入时间"))
        with patch("schedule_controls.time.sleep"),patch("bilibili_auto.time.sleep"):
            d.schedule(selected)
        self.assertEqual(d.scroll_at.call_count,2)
        self.assertEqual(d.actual_publish_at.hour,6)
        d.keys.assert_not_called()


if __name__=="__main__": unittest.main()
