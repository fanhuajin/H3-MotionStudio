"""小红书作品文件夹批量发布。"""
import json
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit
from douyin_auto import DouyinDraft


class XiaohongshuDraft(DouyinDraft):
    page_title = "小红书创作服务平台"

    def keyboard_fill(self, control, field, value):
        if field != "title":
            return super().keyboard_fill(control,field,value)
        current=control.GetValuePattern().Value
        if unicodedata.normalize("NFC",current).strip() == unicodedata.normalize("NFC",value).strip():
            return
        hint="填写标题会有更多赞哦"
        for _ in range(3):
            entry=self.form_find(hint,editable=True,direction="up")
            self.click(entry)
            deadline=self.now()+2
            while self.now()<deadline:
                self.check()
                focused=self.uia.GetFocusedControl()
                if (focused and focused.ControlTypeName=="EditControl"
                        and focused.Name==hint and focused.AutomationId!="RootWebArea"):
                    self.keyboard_field("title")
                    self.verify_edit_value(hint,value)
                    return
                time.sleep(0.1)
        raise RuntimeError("小红书标题框点击后未获得输入焦点，未发送F6或输入。")

    def focus_description(self, body):
        from focus_detection import focused_field
        for _ in range(3):
            self.click(body)
            try:
                field=focused_field("xiaohongshu",self.window.NativeWindowHandle)
            except ValueError:
                time.sleep(0.2)
                continue
            if field=="description":
                return
            raise RuntimeError(f"小红书正文点击后焦点为{field}，未清空或输入正文。")
        raise RuntimeError("小红书正文未获得可确认的编辑焦点，未清空或输入正文。")

    def require_control(self, controls, predicate, description):
        from bilibili_auto import choose_control
        found = [c for c in controls if predicate(c)]
        if not found:
            raise RuntimeError(f"未找到小红书{description}，当前页面尚未就绪或控件布局已变化。")
        return choose_control(found)

    def select_browser(self):
        windows = [w for w in self.uia.GetRootControl().GetChildren()
                   if any(browser in w.Name for browser in ("Edge", "Chrome"))]
        candidates = []
        for window in windows:
            for item, _ in self.uia.WalkControl(window, maxDepth=20):
                if item.ControlTypeName == "TabItemControl" and "小红书创作服务平台" in item.Name:
                    candidates.append((window, item))
        if len(candidates) != 1:
            raise RuntimeError("请只打开一个小红书创作服务平台标签页。")
        self.window, tab = candidates[0]
        self.window.SetFocus()
        time.sleep(0.4)
        r = tab.BoundingRectangle
        if r.width() <= 0:
            raise RuntimeError("小红书标签页不可见。")
        self.click_at(((r.left+r.right)//2,(r.top+r.bottom)//2))
        time.sleep(0.3)
        from browser_page import read_browser_url
        url = urlsplit(read_browser_url(self.uia, self.window, self.page_title))
        if url.hostname != "creator.xiaohongshu.com":
            raise RuntimeError("当前页面不是小红书创作服务平台。")

    def dump(self):
        data=[]
        for c in self.controls():
            r=c.BoundingRectangle
            data.append({"name":c.Name,"type":c.ControlTypeName,"visible":self.visible(c),"rect":[r.left,r.top,r.right,r.bottom]})
        Path(__file__).with_name("xiaohongshu_controls.json").write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")


    def scroll_form(self, direction, amount=5):
        button = self.require_control(self.controls(),
            lambda c:c.Name == "暂存离开" and self.visible(c), "表单滚动锚点“暂存离开”")
        r = button.BoundingRectangle
        self.move_pointer((r.left-300, r.top-150))
        self.scroll_at(direction, amount)
        time.sleep(0.4)

    def form_find(self, name, editable=False, direction="down"):
        for _ in range(20):
            self.check()
            if name=="定时发布":
                target=next((c for c in self.controls() if c.Name==name
                    and c.ControlTypeName=="TextControl" and self.visible(c)),None)
                if (target and getattr(self,"window",None)
                        and target.BoundingRectangle.top>self.window.BoundingRectangle.bottom-180):
                    target=None
            else:
                target = self.locate(name, editable)
                if target is None and name == "添加内容类型声明":
                    target = self.locate("笔记含AI合成内容")
            # 声明栏可能已进入可访问性树，却仍被固定发布底栏遮住。
            # 先滚到表单正文内，避免点击被底栏拦截后空等菜单。
            if (target and name == "添加内容类型声明" and getattr(self, "window", None)
                    and target.BoundingRectangle.bottom > self.window.BoundingRectangle.bottom-180):
                target = None
            if target:
                return target
            self.scroll_form(direction, 4)
        raise RuntimeError(f"未找到小红书控件：{name}")

    def set_cover(self, path):
        if self.locate("完成"):
            self.finish_cover()
        label = self.form_find("设置封面", direction="up")
        r = label.BoundingRectangle
        previews = [c for c in self.controls() if c.ControlTypeName == "GroupControl" and not c.Name
                    and self.visible(c) and abs(c.BoundingRectangle.left-r.left)<25
                    and r.bottom<c.BoundingRectangle.top<r.bottom+180
                    and 100<c.BoundingRectangle.width()<250 and c.BoundingRectangle.height()>150]
        if not previews:
            raise RuntimeError("未找到封面预览。")
        p = previews[0].BoundingRectangle
        self.move_pointer(((p.left+p.right)//2,p.bottom-40))
        self.click(self.wait_name("编辑封面"))
        self.click(self.cover_upload_button())
        self.choose_file(path)
        self.wait_uploaded_cover()
        self.finish_cover()
        self.wait_name("封面效果评估通过，未发现封面质量问题", timeout=60)

    def cover_upload_button(self, timeout=8):
        """模板页保留时只切换已实测的“裁剪”工具页，一次切换后只读等待。"""
        from bilibili_auto import choose_control
        deadline=self.now()+timeout
        switched=False
        while self.now()<deadline:
            self.check()
            controls=self.controls()
            upload=choose_control([c for c in controls if c.Name=="上传" and self.visible(c)])
            if upload is not None and upload.IsEnabled:
                return upload
            done=choose_control([c for c in controls if c.Name=="完成" and self.visible(c)])
            if done is not None and not switched and upload is None:
                crop=choose_control([c for c in controls if c.Name=="裁剪"
                    and c.ControlTypeName=="TextControl" and self.visible(c)])
                if crop is not None:
                    self.click(crop)
                    switched=True
                    deadline=self.now()+timeout
            time.sleep(0.15)
        if switched:
            raise RuntimeError("已切换到裁剪页，但小红书封面上传入口未出现，已保留当前稿件。")
        raise RuntimeError("小红书封面编辑器未显示上传入口或唯一的“裁剪”页签，已保留当前稿件。")

    def uploaded_cover(self):
        """上传缩略图的可访问名称可能包含子按钮，不能匹配清除按钮本身。"""
        from bilibili_auto import choose_control
        return choose_control([c for c in self.controls() if self.visible(c)
            and re.fullmatch(r"已上传封面(?:\s+清除已上传封面)?",c.Name.strip())])

    def wait_uploaded_cover(self, timeout=30):
        deadline=self.now()+timeout
        while self.now()<deadline:
            self.check()
            uploaded=self.uploaded_cover()
            if uploaded is not None:
                return uploaded
            time.sleep(0.2)
        raise RuntimeError("小红书封面上传缩略图未就绪，未完成封面或发布。")

    def finish_cover(self):
        # 文件名出现时图片仍可能加载；等待按钮可用，并确认编辑窗口确实关闭。
        deadline = self.now() + 90
        last_click = None
        while self.now() < deadline:
            self.check()
            button = self.locate("完成")
            if button is None:
                return
            uploaded = self.uploaded_cover()
            if uploaded and button.IsEnabled:
                if last_click is None or self.now()-last_click >= 5:
                    self.click(button)
                    last_click = self.now()
            time.sleep(0.2)
        raise RuntimeError("封面加载或保存超过90秒，已保留当前稿件。")

    def schedule(self, selected):
        label = self.form_find("定时发布")
        def entry():
            return next((c for c in self.controls() if c.ControlTypeName == "EditControl"
                         and self.visible(c) and c.BoundingRectangle.width()>300
                         and label.BoundingRectangle.top-20<c.BoundingRectangle.top<label.BoundingRectangle.bottom+180), None)
        e = entry()
        if bool(e) != bool(selected):
            r = label.BoundingRectangle
            toggle = self.require_control(self.controls(), lambda c:c.ControlTypeName == "GroupControl" and not c.Name
                          and self.visible(c) and abs(c.BoundingRectangle.top-r.top)<10
                          and 40<c.BoundingRectangle.width()<60 and c.BoundingRectangle.left>r.right,
                          "定时发布开关")
            self.click(toggle)
            deadline = self.now() + 5
            while self.now() < deadline:
                self.check()
                if bool(entry()) == bool(selected):
                    break
                time.sleep(0.15)
            else:
                raise RuntimeError("未确认小红书定时发布开关状态，未发布。")
        if not selected:
            if entry() is not None:
                raise RuntimeError("未确认关闭小红书定时发布，未发布。")
            return
        e = entry()
        if e is None:
            raise RuntimeError("定时时间输入框未出现。")
        from datetime import datetime,timedelta
        try:
            actual=datetime.strptime(e.GetValuePattern().Value.strip(),"%Y-%m-%d %H:%M").replace(tzinfo=selected.tzinfo)
        except ValueError:
            actual=None
        if actual and (actual.date(),actual.hour)==(selected.date(),selected.hour):
            from datetime import datetime,timedelta
            now=datetime.now(selected.tzinfo)
            if not now+timedelta(hours=1)<=actual<=now+timedelta(days=14):
                raise RuntimeError("页面时间须在1小时后、14天内。")
            self.actual_publish_at=actual
            return
        stage=getattr(self,"on_stage",None) or (lambda value:None)
        stage("schedule_calendar")
        self.click(e)
        self.select_schedule_date(selected, actual)
        from schedule_controls import wait_hour_readback
        from xiaohongshu_schedule import choose_hour
        stage("schedule_hour")
        choose_hour(self,selected)
        r=e.BoundingRectangle
        self.click_at((r.left-35,r.top-40))
        stage("schedule_readback")
        def value():
            current=entry()
            return current.GetValuePattern().Value if current else None
        actual=wait_hour_readback(self,value,selected,"小红书")
        now=datetime.now(selected.tzinfo)
        if not now+timedelta(hours=1)<=actual<=now+timedelta(days=14):
            raise RuntimeError("页面时间须在1小时后、14天内。")
        self.actual_publish_at=actual

    def select_schedule_date(self, selected, actual):
        # 小红书再次点击已选日期会清空时间并关闭定时开关。
        if actual is not None and actual.date() == selected.date():
            return
        stage=getattr(self,"on_stage",None) or (lambda value:None)
        from calendar_navigation import select_calendar_month
        from bilibili_auto import choose_control
        def read_month():
            cs = self.controls()
            year = choose_control([c for c in cs if c.Name.endswith("年") and c.Name[:-1].isdigit() and self.visible(c)])
            month = choose_control([c for c in cs if c.Name.endswith("月") and c.Name[:-1].isdigit() and self.visible(c)])
            if year is None or month is None:
                return None
            return int(year.Name[:-1]), int(month.Name[:-1]), month
        def navigation(direction):
            names = ("下个月", "下一月", "Next month") if direction == "next" else ("上个月", "上一月", "Previous month")
            return choose_control([c for c in self.controls() if self.visible(c)
                and c.ControlTypeName == "ButtonControl" and c.Name in names])
        _, _, month = select_calendar_month(self, selected, read_month, navigation, "小红书日历")
        from schedule_controls import calendar_day
        mr = month.BoundingRectangle
        days = [c for c in self.controls() if self.visible(c) and c.Name.isdigit()
                and mr.left-210<c.BoundingRectangle.left<mr.left+160
                and mr.bottom+50<c.BoundingRectangle.top<mr.bottom+340]
        stage("schedule_date")
        day=calendar_day(days,selected.year,selected.month,selected.day)
        (self.click_declaration if hasattr(day,"control") else self.click)(day)

    def xiaohongshu_hour_options(self):
        from xiaohongshu_schedule import hour_options
        return hour_options(self)

    def wait_uploaded(self):
        # 固定底栏按钮始终可见，选完时间后原地等待可提交状态，不回滚页面。
        deadline=self.now()+180
        while self.now()<deadline:
            controls=self.controls()
            if any("上传失败" in c.Name for c in controls):
                raise RuntimeError("视频上传失败。")
            uploading=any("取消上传" in c.Name or "上传中" in c.Name for c in controls)
            if not uploading and any(c.Name in ("定时发布","发布")
                    and c.ControlTypeName=="ButtonControl" and self.visible(c) and c.IsEnabled
                    for c in controls):
                return
            time.sleep(0.2)
        raise RuntimeError("发布按钮等待超时，未提交。")

    def set_declaration(self):
        label=self.form_find("添加内容类型声明")
        def selected():
            current=self.locate("添加内容类型声明") or self.locate("笔记含AI合成内容")
            if current is None:
                return False
            r=current.BoundingRectangle
            for c in self.controls():
                if c.Name!="笔记含AI合成内容" or not self.visible(c):
                    continue
                b=c.BoundingRectangle
                if r.left<=(b.left+b.right)/2<=r.right and r.top<=(b.top+b.bottom)/2<=r.bottom:
                    return True
            return False
        if selected():
            return
        self.click(label)
        self.click(self.wait_name("笔记含AI合成内容"))
        deadline=self.now()+5
        while self.now()<deadline:
            self.check()
            if selected():
                return
            time.sleep(0.15)
        raise RuntimeError("未确认小红书AI声明已出现在声明栏，未继续发布。")

    def run(self, video, title, description, tags, cover, category, publish_at):
        stage = getattr(self, "on_stage", None) or (lambda value: None)
        stage("entry")
        self.select_browser()
        if self.locate("完成"):
            self.finish_cover()
        hint="填写标题会有更多赞哦"
        existing=next((c for c in self.controls() if c.Name==hint and c.ControlTypeName=="EditControl"),None)
        if existing:
            actual_title=existing.GetValuePattern().Value
            if unicodedata.normalize("NFC",actual_title).strip() != unicodedata.normalize("NFC",title).strip():
                raise RuntimeError(f"当前小红书稿件与队列作品不一致：页面标题{actual_title!r}，队列标题{title!r}，未修改稿件。")
        else:
            self.next_upload()
            upload=self.wait_upload_button()
            self.click(upload)
            self.choose_file(video)
            self.wait_name(hint, timeout=60)
        self.start_keyboard_helper(video.parent/"发布文案.txt")
        if not (existing and getattr(self,"resume_settings",False)):
            stage("title")
            self.keyboard_fill(self.form_find(hint,editable=True,direction="up"),"title",title)
            body=self.description_editor("输入正文描述，真诚有价值的分享予人温暖", description)
            stage("description")
            self.focus_description(body)
            # 重启续跑时先清空已有正文，让原始提示恢复，按键助手才能明确识别字段。
            self.keys("{Ctrl}a{Back}")
            self.click(self.wait_name("输入正文描述，真诚有价值的分享予人温暖"))
            self.keyboard_field("description")
            stage("cover")
            self.set_cover(cover)
        stage("declaration")
        self.set_declaration()
        stage("schedule")
        self.schedule(publish_at)
        stage("upload_ready")
        self.wait_uploaded()
        name="定时发布" if publish_at else "发布"
        button=self.require_control(self.controls(),lambda c:c.Name==name and c.ControlTypeName=="ButtonControl" and self.visible(c),f"提交按钮“{name}”")
        stage("submit_ready")
        self.submit(button)
        stage("confirm_result")
        deadline=self.now()+60
        while self.now()<deadline:
            self.check()
            names=self.control_names()
            if any(n in names for n in ("发布成功", "笔记发布成功", "定时发布成功")):
                return
            form=any(c.Name==hint and c.ControlTypeName=="EditControl" for c in self.controls())
            if not form and "笔记管理" in names and any(title in n for n in names):
                return
            time.sleep(0.5)
        raise RuntimeError("已点击发布，但未确认成功；请检查笔记管理，避免重复提交。")

    def next_upload(self):
        if self.upload_button():
            return
        # 成功页会倒计时跳回发布页；这期间不要点击导航打断自动跳转。
        names = self.control_names()
        if not any("发布成功" in name or "跳转" in name for name in names):
            self.click(self.wait_name("发布笔记",timeout=30))
        self.wait_upload_button()

    def upload_button(self):
        from bilibili_auto import choose_control
        return choose_control([c for c in self.controls()
            if c.Name == "上传视频" and c.ControlTypeName == "ButtonControl"
            and self.visible(c) and c.IsEnabled])

    def wait_upload_button(self, timeout=45):
        # 顶部“上传视频”文字标签不能打开文件框，只接受上传区域的真实按钮。
        deadline = self.now() + timeout
        while self.now() < deadline:
            self.check()
            button = self.upload_button()
            if button is not None:
                return button
            time.sleep(0.2)
        raise RuntimeError("小红书跳转后上传按钮未就绪，未点击上传或选择文件。")


def prepare_xiaohongshu(folder):
    from bilibili_auto import prepare_files
    data=prepare_files(folder)
    if len(data[1])>20:
        raise ValueError("小红书标题最多20字，请修改发布文案。")
    return data


def xiaohongshu_cover(folder):
    path=folder/"抖音3x4.png"
    if not path.is_file():
        raise ValueError("作品缺少抖音3x4.png封面。")
    return path.resolve()


def select_xiaohongshu_hour(date,hour,now=None):
    from datetime import datetime,timedelta
    from bilibili_auto import CHINA_TIME
    now=now or datetime.now(CHINA_TIME)
    start=datetime.strptime(f"{date} {hour}","%Y-%m-%d %H").replace(tzinfo=CHINA_TIME)
    for minute in range(60):
        candidate=start.replace(minute=minute)
        if now+timedelta(hours=1)<=candidate<=now+timedelta(days=14):
            return candidate
    raise ValueError("小红书定时应在北京时间1小时后、14天内。")


def main():
    import tkinter as tk
    from publish_queue import PublishQueue
    from bilibili_auto import CHINA_TIME
    root=tk.Tk()
    root.withdraw()
    PublishQueue(root,XiaohongshuDraft,select_xiaohongshu_hour,prepare_xiaohongshu,xiaohongshu_cover,
                 lambda folder:None,CHINA_TIME,Path(__file__).with_name("xiaohongshu_auto_status.json"),
                 publish_at="2026-10-10 01:00",platform="xiaohongshu").show()


if __name__ == "__main__":
    main()
