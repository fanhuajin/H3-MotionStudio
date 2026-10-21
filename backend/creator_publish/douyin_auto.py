"""抖音批量发布：分别上传横竖封面，核对后发布。"""
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit
from bilibili_auto import BilibiliDraft, prepare_files


class DouyinDraft(BilibiliDraft):
    page_title = "抖音创作者中心"
    def select_browser(self):
        windows = [w for w in self.uia.GetRootControl().GetChildren()
                   if "抖音创作者中心" in w.Name and any(b in w.Name for b in ("Edge", "Chrome", "Firefox"))]
        if len(windows) != 1:
            raise RuntimeError("请打开一个抖音创作者中心浏览器窗口。")
        self.window = windows[0]
        self.window.SetFocus()
        time.sleep(0.3)
        from browser_page import read_browser_url
        url = urlsplit(read_browser_url(self.uia, self.window, self.page_title))
        if url.hostname != "creator.douyin.com" or not url.path.startswith("/creator-micro/content/"):
            raise RuntimeError("当前页面不是抖音创作者投稿页面。")

    @staticmethod
    def transient_control_error(error):
        code = getattr(error, "hresult", None)
        if code is None and error.args and isinstance(error.args[0], int):
            code = error.args[0]
        return code in (-2147220991, -2147220992, -2147417848, -2147023174, -2146233079) or (
            isinstance(error, RuntimeError) and str(error) == "未找到抖音页面控件根节点。")

    def controls(self):
        # 页面跳转时仅重试只读控件捕获，不重发发布或点击操作。
        for attempt in range(8):
            try:
                return self.read_page_controls()
            except Exception as error:
                if not self.transient_control_error(error) or attempt == 7:
                    raise
                time.sleep(0.3)
        raise RuntimeError("抖音页面尚未就绪。")

    def read_page_controls(self):
        """仅遍历抖音页面，避免时间弹层触发整个浏览器控件数量限制。"""
        from bilibili_auto import ControlSnapshot, COMError
        self.check()
        document = None
        for control, _ in self.uia.WalkControl(self.window, maxDepth=20):
            if control.ControlTypeName == "DocumentControl" and control.Name == self.page_title:
                document = control
                break
        if document is None:
            raise RuntimeError("未找到抖音页面控件根节点。")
        result = []
        for control, _ in self.uia.WalkControl(document, maxDepth=35):
            result.append(ControlSnapshot(control))
            if len(result) > 12000:
                raise RuntimeError("抖音页面控件异常增多，停止读取。")
        return result

    def dump(self):
        data = []
        for item in self.controls():
            r = item.BoundingRectangle
            data.append({"name": item.Name, "type": item.ControlTypeName,
                         "visible": self.visible(item), "rect": [r.left,r.top,r.right,r.bottom]})
        Path(__file__).with_name("douyin_controls.json").write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")

    def find(self, name, editable=False):
        """根据目标在视口上方或下方逐格滚动，避免跳过设置后只向下找。"""
        from bilibili_auto import choose_control
        for _ in range(60):
            matches = [c for c in self.controls() if c.Name == name
                       and (not editable or c.ControlTypeName == "EditControl")]
            visible = [c for c in matches if self.visible(c)]
            if visible:
                return choose_control(visible, editable)
            if not matches:
                raise RuntimeError(f"抖音页面中没有“{name}”控件，无法确定滚动方向。")
            window = self.window.BoundingRectangle
            target = min(matches, key=lambda c:abs(
                (c.BoundingRectangle.top+c.BoundingRectangle.bottom)/2
                - (window.top+window.bottom)/2))
            rect = target.BoundingRectangle
            if rect.width() <= 0 or rect.height() <= 0:
                raise RuntimeError(f"“{name}”没有有效位置，请关闭遮挡表单的弹层。")
            direction = "up" if (rect.top+rect.bottom)/2 < window.top+100 else "down"
            # 在实际目标所在表单列滚动，避免滚到右侧预览或侧栏。
            x = (rect.left+rect.right)//2
            y = max(window.top+140, min((rect.top+rect.bottom)//2, window.bottom-80))
            if not window.left < x < window.right:
                raise RuntimeError(f"“{name}”所在表单列不在窗口内。")
            self.scroll_at(direction, 1, (x, y))
            time.sleep(0.15)
        raise RuntimeError(f"滚动后仍未找到可见的“{name}”，未继续发布。")

    def horizontal_cover(self, path):
        target = self.locate("设置横封面")
        if target:
            self.click(target)
        else:
            label = self.find("横封面4:3")
            rect = label.BoundingRectangle
            self.click_at(((rect.left + rect.right) // 2, rect.top - 65))
        self.click(self.wait_name("上传封面"))
        self.choose_file(path)
        self.click(self.wait_cover_ready("完成"))
        for _ in range(20):
            if not self.locate("横封面预览（4:3）"):
                return
            time.sleep(0.2)
        raise RuntimeError("横版封面编辑器未关闭。")

    def wait_cover_ready(self, button_name, timeout=90):
        """图片上传期间等待按钮可用，不把按钮已经出现当作封面加载完成。"""
        deadline = self.now() + timeout
        ready = 0
        while self.now() < deadline:
            self.check()
            controls = self.controls()
            from bilibili_auto import choose_control
            button = choose_control([c for c in controls if c.Name == button_name and self.visible(c)])
            # 视频也在后台上传，只把封面弹窗内的状态作为封面加载证据。
            dialogs = []
            if button is not None:
                b = button.BoundingRectangle
                x, y = (b.left + b.right) / 2, (b.top + b.bottom) / 2
                for c in controls:
                    r = c.BoundingRectangle
                    if (c.ControlTypeName in ("GroupControl", "WindowControl", "CustomControl")
                            and any(word in getattr(c, "ClassName", "").lower() for word in ("modal", "dialog"))
                            and self.visible(c) and r.left <= x <= r.right and r.top <= y <= r.bottom
                            and r.width() > 250 and r.height() > 100):
                        dialogs.append(c)
            popup = min(dialogs, key=lambda c:c.BoundingRectangle.width()*c.BoundingRectangle.height()) if dialogs else None
            def cover_status(c):
                if "封面" in c.Name or "图片" in c.Name:
                    return True
                if popup is None:
                    return False
                r, p = c.BoundingRectangle, popup.BoundingRectangle
                return p.left <= (r.left+r.right)/2 <= p.right and p.top <= (r.top+r.bottom)/2 <= p.bottom
            if any("上传失败" in c.Name and cover_status(c) for c in controls):
                raise RuntimeError("抖音封面上传失败，未继续发布。")
            uploading = any(("上传中" in c.Name or "正在上传" in c.Name) and cover_status(c) for c in controls)
            if button is not None and button.IsEnabled and not uploading:
                ready += 1
                if ready >= 2:
                    return button
            else:
                ready = 0
            time.sleep(0.2)
        raise RuntimeError(f"抖音封面加载或“{button_name}”按钮等待超时，已保留当前稿件。")

    def description_editor(self, placeholder, description):
        """按稳定编辑器类名恢复正文，已有话题或部分文案不影响定位。"""
        from bilibili_auto import choose_control
        for _ in range(20):
            self.check()
            empty = self.locate(placeholder)
            # 小红书沿用原定位；抖音提示文字在第二条上传刷新时可能失效。
            if empty is not None and self.page_title != "抖音创作者中心":
                return empty
            controls = self.controls()
            editors = []
            for c in controls:
                classes = getattr(c, "ClassName", "").lower().split()
                known = any(name in classes for name in ("ql-editor", "prosemirror", "tiptap"))
                if self.page_title == "抖音创作者中心":
                    known = known or {"zone-container", "editor-kit-container", "editor-comp-publish"}.issubset(classes)
                if (known and c.ControlTypeName in ("EditControl", "GroupControl", "CustomControl")
                        and getattr(c, "AutomationId", "") != "RootWebArea"):
                    editors.append(c)
            editor = choose_control(editors)
            if editor is None:
                if empty is not None:
                    return empty
                # 保留旧版页面单独暴露简介文字的情况，输入前仍核验真实焦点。
                editor = choose_control([c for c in controls if self.visible(c)
                    and c.ControlTypeName == "TextControl" and c.Name.strip() == description.strip()])
                if editor is None:
                    raise RuntimeError("未找到可确认的正文编辑区，未清空或输入正文。")
                return editor
            if self.visible(editor):
                return editor
            rect = editor.BoundingRectangle
            window = self.window.BoundingRectangle
            direction = "up" if (rect.top + rect.bottom) / 2 < window.top + 100 else "down"
            if hasattr(self, "scroll_form"):
                self.scroll_form(direction, 1)
            else:
                x = (rect.left + rect.right) // 2
                y = max(window.top + 140, min((rect.top + rect.bottom) // 2, window.bottom - 80))
                if not window.left < x < window.right or rect.width() <= 0 or rect.height() <= 0:
                    raise RuntimeError("正文编辑器没有有效的表单位置，未清空或输入正文。")
                self.scroll_at(direction, 1, (x, y))
                time.sleep(0.15)
        raise RuntimeError("滚动后正文编辑器仍不可见，未清空或输入正文。")

    def focus_description(self, body, description=None):
        from focus_detection import focused_field
        for _ in range(3):
            if description is not None:
                body = self.description_editor("添加作品简介", description)
            try:
                self.click(body)
            except RuntimeError as error:
                if description is None or str(error) != "目标控件已离开可见区域，停止输入。":
                    raise
                time.sleep(0.2)
                continue
            try:
                field = focused_field("douyin", self.window.NativeWindowHandle)
            except ValueError:
                time.sleep(0.2)
                continue
            if field == "description":
                return
            raise RuntimeError(f"抖音正文点击后焦点为{field}，未清空或输入正文。")
        raise RuntimeError("抖音正文未获得可确认的编辑焦点，未清空或输入正文。")

    def declaration(self):
        if self.locate("内容由AI生成"):
            return
        self.click(self.find("请选择自主声明"))
        self.click(self.wait_name("内容由AI生成"))
        self.click(self.find("确定"))
        deadline=self.now()+5
        while self.now()<deadline:
            if self.locate("内容由AI生成") and self.locate("确定") is None:
                return
            time.sleep(0.15)
        raise RuntimeError("未确认抖音AI声明保存并关闭弹窗，未继续发布。")

    def douyin_topic_control(self, tag, accepted=False):
        """候选只从话题弹层读取；已关联话题只从正文编辑区读取。"""
        from bilibili_auto import choose_control
        controls = self.controls()
        containers = []
        for c in controls:
            classes = getattr(c, "ClassName", "").lower().split()
            matches = ({"zone-container", "editor-kit-container", "editor-comp-publish"}.issubset(classes)
                       if accepted else "mention-suggest-mount-dom" in classes)
            if matches and self.visible(c):
                containers.append(c.BoundingRectangle)
        found = []
        for c in controls:
            # 原始输入为“#标签”；成功关联后单独暴露“标签”文字。
            if c.Name.strip() != tag or c.ControlTypeName != "TextControl" or not self.visible(c):
                continue
            r = c.BoundingRectangle
            if any(b.left <= r.left < r.right <= b.right and b.top <= r.top < r.bottom <= b.bottom
                   for b in containers):
                found.append(c)
        return choose_control(found)

    def fill_douyin_description(self, description, tags):
        """抖音独立填写正文和话题，不使用共用助手的截图候选检测。"""
        from focus_detection import focused_field
        def check_focus():
            self.check()
            if focused_field("douyin", self.window.NativeWindowHandle) != "description":
                raise RuntimeError("抖音正文焦点已改变，未继续输入话题。")
        check_focus()
        self.keys("{Ctrl}a")
        self.paste(description + " ")
        self.verify_text(description)
        self.keys("{Ctrl}{End}")
        for tag in tags:
            check_focus()
            self.keys("#")
            self.paste(tag)
            deadline = self.now() + 8
            stable = 0
            while self.now() < deadline:
                self.check()
                candidate = self.douyin_topic_control(tag)
                stable = stable + 1 if candidate is not None else 0
                if stable >= 3:
                    break
                time.sleep(0.2)
            else:
                raise RuntimeError(f"抖音话题 #{tag} 未找到同名候选，未继续发布。")
            self.click(candidate)
            deadline = self.now() + 5
            while self.now() < deadline:
                self.check()
                if self.douyin_topic_control(tag, accepted=True) is not None:
                    break
                time.sleep(0.2)
            else:
                raise RuntimeError(f"抖音话题 #{tag} 未确认关联成功，未继续发布。")
            check_focus()
            self.keys("{Ctrl}{End}")

    def disable_saving(self):
        self.select_option("不允许")

    def select_option(self, name):
        # 同名文字和复选框并存；只点击真实选项，已选中时不再切换。
        self.find(name)
        def option():
            from bilibili_auto import choose_control
            return choose_control([c for c in self.controls()
                                   if c.Name == name and self.visible(c)
                                   and c.ControlTypeName in ("CheckBoxControl", "RadioButtonControl")])
        def selected(control):
            if control is None:
                return False
            if control.ControlTypeName == "RadioButtonControl":
                return bool(control.GetSelectionItemPattern().IsSelected)
            pattern = control.GetTogglePattern()
            return pattern is not None and int(pattern.ToggleState) == 1
        control = option()
        if control is None:
            raise RuntimeError(f"未找到“{name}”选项控件。")
        if selected(control):
            return
        self.click(control)
        deadline = self.now() + 5
        while self.now() < deadline:
            if selected(option()):
                return
            time.sleep(0.15)
        raise RuntimeError(f"未确认“{name}”已选中，未继续发布。")

    def schedule(self, selected):
        from datetime import datetime, timedelta
        from bilibili_auto import CHINA_TIME
        self.disable_saving()
        if selected is None:
            self.select_option("立即发布")
            return
        now = datetime.now(CHINA_TIME)
        if not now + timedelta(hours=2) <= selected <= now + timedelta(days=14):
            raise ValueError("抖音定时应在北京时间2小时后、14天内。")
        self.select_option("定时发布")
        entry = self.find("日期和时间", editable=True)
        stage=getattr(self,"on_stage",None) or (lambda value:None)
        stage("schedule_calendar")
        self.click(entry)
        from bilibili_auto import choose_control
        from calendar_navigation import select_calendar_month
        def read_month():
            header = choose_control([c for c in self.controls() if self.visible(c)
                and re.fullmatch(r"\d{4}年\s*\d{1,2}月", c.Name)])
            if header is None:
                return None
            numbers = re.findall(r"\d+", header.Name)
            return int(numbers[0]), int(numbers[1]), header
        def navigation(direction):
            names = ("下个月", "下一月", "Next month") if direction == "next" else ("上个月", "上一月", "Previous month")
            return choose_control([c for c in self.controls() if self.visible(c) and c.Name in names])
        _, _, header = select_calendar_month(self, selected, read_month, navigation, "抖音日历")
        r = header.BoundingRectangle
        from douyin_schedule import find_calendar_day
        from schedule_controls import scroll_select, wait_hour_readback
        stage("schedule_date")
        self.click(find_calendar_day(self,header,selected))
        clocks = [c for c in self.controls() if self.visible(c)
                  and c.ControlTypeName == "TextControl" and re.fullmatch(r"\d{2}:\d{2}", c.Name)
                  and r.left - 150 < c.BoundingRectangle.left < r.right + 180]
        clock = choose_control(clocks)
        if clock is None:
            raise RuntimeError("未确认小时选择入口。")
        stage("schedule_hour")
        self.click(clock)
        clock_rect = clock.BoundingRectangle
        scroll_select(self,lambda:self.hour_options(clock_rect),selected.hour,"抖音小时")
        self.click(self.find("发布时间"))
        stage("schedule_readback")
        parsed = wait_hour_readback(self,lambda:self.find("日期和时间",editable=True).GetValuePattern().Value,selected,"抖音")
        if not datetime.now(CHINA_TIME) + timedelta(hours=2) <= parsed <= datetime.now(CHINA_TIME) + timedelta(days=14):
            raise RuntimeError("所选日期和小时超出抖音允许的定时范围。")
        self.actual_publish_at = parsed

    def hour_options(self, clock_rect):
        from douyin_schedule import inside_time_list
        # 时间弹层的小时、分钟都使用两位数字，不能只按文字或旧偏移定位。
        options = [c for c in self.controls() if self.visible(c)
                   and c.ControlTypeName == "ListItemControl"
                   and inside_time_list(c)
                   and re.fullmatch(r"\d{2}(?:时)?", c.Name)
                   and clock_rect.top-415 < c.BoundingRectangle.top < clock_rect.top-45
                   and clock_rect.left-400 < c.BoundingRectangle.left < clock_rect.right+100]
        if not options:
            return []
        columns = []
        for control in sorted(options,key=lambda c:c.BoundingRectangle.left):
            x = control.BoundingRectangle.left
            if not columns or abs(x-columns[-1][0]) > 20:
                columns.append((x,[]))
            columns[-1][1].append(control)
        if len(columns) != 2:
            raise RuntimeError("未确认独立的小时、分钟两列，未修改时间或发布。")
        hours = columns[0][1]
        if any(int(c.Name.removesuffix("时")) > 23 for c in hours):
            raise RuntimeError("小时列包含无效小时，未修改时间或发布。")
        return hours

    def fail_calendar_day(self):
        raise RuntimeError("未找到所选日期的日历项。")

    def video_publish_entry(self):
        """侧栏和作品发布面板可同时显示“发布视频”，优先面板入口。"""
        from bilibili_auto import choose_control
        controls = self.controls()
        matches = [c for c in controls if c.Name == "发布视频" and self.visible(c)]
        sidebar = [c for c in controls if c.Name == "作品发布"
                   and c.ControlTypeName == "ButtonControl" and self.visible(c)]
        if len(sidebar) == 1:
            edge = sidebar[0].BoundingRectangle.right
            panel = [c for c in matches if c.BoundingRectangle.left > edge]
            if panel:
                return choose_control(panel)
        return choose_control(matches)

    def next_upload(self):
        """等发布后的新页面就绪，再点击一次作品发布并等待上传入口。"""
        print("等待下一条上传页面。", flush=True)
        # 跳转可能已经完成，不重复点击侧栏菜单。
        if self.locate("上传视频") or self.locate("上传 视频"):
            return
        self.click(self.wait_name("作品发布", timeout=30))
        deadline = self.now() + 30
        entered = False
        while self.now() < deadline:
            if self.locate("上传视频") or self.locate("上传 视频"):
                print("下一条上传页面已就绪。", flush=True)
                return
            video_entry = self.video_publish_entry() if not entered else None
            if video_entry and not entered:
                self.click(video_entry)
                entered = True
            time.sleep(0.2)
        raise RuntimeError("等待作品发布页面超过30秒，当前成功稿件已保留，不会重新发布。")

    def vertical_cover(self, path):
        label = self.find("竖封面3:4")
        rect = label.BoundingRectangle
        self.click_at(((rect.left + rect.right) // 2 + 15, rect.top - 65))
        self.click(self.wait_name("上传封面"))
        self.choose_file(path)
        # 先进入横版，最后的完成按钮会保存两种封面。
        self.wait_cover_ready("设置横封面")

    def wait_uploaded(self):
        deadline = self.now() + 180
        while self.now() < deadline:
            names = self.control_names()
            if any("上传失败" in name for name in names):
                raise RuntimeError("视频上传失败。")
            if "重新上传" in names and "取消上传" not in names:
                return
            time.sleep(0.5)
        raise RuntimeError("视频上传等待超时。")

    def run(self, video, title, description, tags, covers, category, publish_at):
        stage=getattr(self,"on_stage",None) or (lambda value:None)
        stage("entry")
        self.select_browser()
        hint = "填写作品标题，为作品获得更多流量"
        from bilibili_auto import choose_control
        # 设置阶段标题通常已滚出屏幕，仍须识别保留的稿件，不能误进上传页。
        existing = choose_control([c for c in self.controls()
            if c.Name == hint and c.ControlTypeName == "EditControl"], editable=True)
        if existing:
            if existing.GetValuePattern().Value != title:
                raise RuntimeError("当前抖音稿件与队列作品不一致，请返回空白上传页。")
            if getattr(self, "resume_stage", None) == "settings":
                if "内容由AI生成" not in self.control_names():
                    raise RuntimeError("当前稿件AI声明未确认，无法从发布设置续跑。")
                self.finish_publish(title, publish_at)
                return
        else:
            if not (self.locate("上传视频") or self.locate("上传 视频")):
                self.next_upload()
            self.upload(video)
        self.wait_name(hint, timeout=30)
        self.start_keyboard_helper(video.parent / "发布文案.txt")
        stage("title")
        self.keyboard_fill(self.find(hint, editable=True), "title", title)
        body = self.description_editor("添加作品简介", description)
        stage("description")
        self.focus_description(body, description)
        self.fill_douyin_description(description, tags)
        stage("cover")
        self.vertical_cover(covers[0])
        self.horizontal_cover(covers[1])
        self.wait_name("封面效果检测通过", timeout=60)
        stage("declaration")
        self.declaration()
        if getattr(self, "on_stage", None):
            self.on_stage("settings")
        self.finish_publish(title, publish_at)

    def finish_publish(self, title, publish_at):
        hint = "填写作品标题，为作品获得更多流量"
        stage = getattr(self, "on_stage", None)
        if stage:
            stage("schedule")
        self.schedule(publish_at)
        if stage:
            stage("upload_ready")
        self.wait_uploaded()
        if stage:
            stage("submit_ready")
        self.submit(self.find("发布"))
        if stage:
            stage("confirm_result")
        deadline = self.now() + 60
        while self.now() < deadline:
            controls = self.controls()
            names = [c.Name for c in controls]
            form_present = any(c.Name == hint and c.ControlTypeName == "EditControl" for c in controls)
            if not form_present and any(title in name for name in names) and "内容管理" in names:
                print(f"发布成功：{title}", flush=True)
                return
            if "发布成功" in names:
                return
            time.sleep(0.5)
        raise RuntimeError("已点击发布，但未确认成功。请先检查作品管理，避免重复发布。")

    def upload(self, video):
        names = self.control_names()
        if "作品标题" in names or "添加作品描述..." in names:
            raise RuntimeError("已有作品表单，请确认当前视频后继续，避免重复上传。")
        for name in ("上传视频", "上传 视频", "点击上传"):
            target = self.locate(name)
            if target:
                self.click(target)
                self.choose_file(video)
                break
        else:
            raise RuntimeError("未找到抖音上传按钮。")


def prepare_douyin(folder):
    video, title, description, tags = prepare_files(folder)
    if len(title) > 30 or len(description + " " + " ".join("#" + tag for tag in tags)) > 1000:
        raise ValueError("抖音标题最多30字，简介与话题合计最多1000字。")
    return video, title, description, tags


def douyin_covers(folder):
    paths = (folder / "抖音3x4.png", folder / "B站4x3.png")
    if not all(path.is_file() for path in paths):
        raise ValueError("需要抖音3x4.png和B站4x3.png两张封面。")
    return tuple(path.resolve() for path in paths)


def select_douyin_hour(date, hour):
    from datetime import datetime, timedelta
    from bilibili_auto import CHINA_TIME
    now = datetime.now(CHINA_TIME)
    start = datetime.strptime(f"{date} {hour}", "%Y-%m-%d %H").replace(tzinfo=CHINA_TIME)
    for minute in range(60):
        candidate = start.replace(minute=minute)
        if now + timedelta(hours=2) <= candidate <= now + timedelta(days=14):
            return candidate
    raise ValueError("抖音定时应在北京时间2小时后、14天内，请修改该作品日期和小时。")


def main():
    import tkinter as tk
    import ctypes
    from publish_queue import PublishQueue
    from bilibili_auto import CHINA_TIME
    root = tk.Tk()
    root.withdraw()
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    mutex = kernel.CreateMutexW(None, False, "Local\\CreatorPublishHelperDouyinDraft")
    if not mutex:
        root.destroy()
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle(mutex)
        root.destroy()
        return
    try:
        PublishQueue(root, DouyinDraft, select_douyin_hour, prepare_douyin, douyin_covers,
                     lambda folder: None, CHINA_TIME, Path(__file__).with_name("douyin_auto_status.json"),
                     platform="douyin").show()
    finally:
        kernel.CloseHandle(mutex)
        root.destroy()


if __name__ == "__main__":
    main()
