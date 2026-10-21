"""不触发输入的离线检查。"""

from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch, Mock
from subprocess import CompletedProcess
from datetime import datetime, timedelta

from PIL import Image, ImageDraw
from native_mouse import pointer_path

from fill_helper import (
    candidate_stable, candidate_visible, detect_platform, read_copy,
    discard_pending_hotkeys, validate_keyboard_request, write_keyboard_state,
)
from focus_detection import classify_field, focused_field
from bilibili_auto import (
    BilibiliDraft, CHINA_TIME, ControlSnapshot, category_from_folder, choose_control, cover_from_folder,
    is_upload_url, parse_publish_time, prepare_files,
)


def main():
    start, target = (-800, 100), (1700, 900)
    path = pointer_path(start, target)
    assert path[-1] == target and pointer_path(target, target) == []
    assert all(
        (b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2 <= 14 ** 2
        for a, b in zip([start] + path, path)
    )
    motion = object.__new__(BilibiliDraft)
    motion.check = Mock()
    motion.uia = SimpleNamespace(GetCursorPos=lambda: (0, 0))
    with patch("bilibili_auto.mouse_event") as event, patch("bilibili_auto.time.sleep"):
        motion.click_at((24, 0))
        assert event.call_args_list[0].kwargs == {"position": (12, 0)}
        assert [call.args[0] for call in event.call_args_list] == [1, 1, 2, 4]
        event.reset_mock()
        motion.scroll_at("down", 2)
        assert event.call_count == 2
        assert all(call.kwargs == {"wheel": -120} for call in event.call_args_list)
    with patch("focus_detection.subprocess.run", return_value=CompletedProcess(
        [], 0, '{"window_handle":123,"controls":[{"focused":true,"name":"作品标题"}]}', ""
    )) as probe:
        assert focused_field("douyin", 123) == "title"
        command = probe.call_args.args[0]
        assert command[command.index("-ExecutionPolicy") + 1] == "Bypass"
    with patch("focus_detection.subprocess.run", return_value=CompletedProcess(
        [], 1, "", "probe failed"
    )):
        try:
            focused_field("douyin", 123)
        except RuntimeError as error:
            assert "probe failed" in str(error)
        else:
            raise AssertionError("脚本错误必须保留具体原因")
    request = {"id": "测试请求", "window": 123, "field": "title"}
    validate_keyboard_request(request, 123, "title")
    for window, field in ((124, "title"), (123, "description"), (123, "unknown")):
        try:
            validate_keyboard_request(request, window, field)
        except ValueError:
            pass
        else:
            raise AssertionError("自动化请求不得填写到其他窗口或字段")
    with tempfile.TemporaryDirectory() as directory:
        state = Path(directory) / "状态.json"
        write_keyboard_state(state, request)
        assert "测试请求" in state.read_text(encoding="utf-8")
        assert not state.with_suffix(".tmp").exists()
    source = SimpleNamespace(
        Name="上传完成", ControlTypeName="TextControl",
        BoundingRectangle=object(), IsOffscreen=False,
    )
    snapshot = ControlSnapshot(source)
    source.Name = "页面已刷新"
    assert snapshot.Name == "上传完成"
    # 控件寻找只向下滚动；正确声明和分区不得发送点击或滚动。
    draft = object.__new__(BilibiliDraft)
    target = object()
    observations = iter((None, None, target))
    scrolls = []
    draft.locate = lambda name, editable=False: next(observations)
    draft.scroll_page = lambda direction, amount: scrolls.append(direction)
    assert draft.find("标题") is target
    assert scrolls == ["down"]
    schedule_draft = object.__new__(BilibiliDraft)
    schedule_draft.window = SimpleNamespace(BoundingRectangle=SimpleNamespace(bottom=1000))
    schedule_positions = iter((950, 650))
    schedule_draft.find = lambda name: SimpleNamespace(BoundingRectangle=SimpleNamespace(bottom=next(schedule_positions)))
    schedule_scrolls = []
    schedule_draft.scroll_page = lambda direction, amount: schedule_scrolls.append((direction, amount))
    assert schedule_draft.reveal_schedule().BoundingRectangle.bottom == 650
    assert schedule_scrolls == [("down", 2)]
    correct = SimpleNamespace(GetValuePattern=lambda: SimpleNamespace(Value="含AI生成内容"))
    draft.find = lambda name, editable=False: correct
    draft.click = lambda control: (_ for _ in ()).throw(AssertionError("正确声明不应点击"))
    draft.declaration()
    label = SimpleNamespace(BoundingRectangle=SimpleNamespace(top=300))
    draft.find = lambda name, editable=False: label
    label.Name="分区"
    label.ControlTypeName="TextControl"
    label.BoundingRectangle=SimpleNamespace(top=300,left=30,right=60,bottom=320)
    draft.controls = lambda: [label,SimpleNamespace(Name="舞蹈",ControlTypeName="TextControl",
        BoundingRectangle=SimpleNamespace(top=300,left=100,right=130,bottom=320))]
    draft.input_column = lambda: 100
    draft.visible = lambda control: True
    draft.category("舞蹈")
    tag_draft = object.__new__(BilibiliDraft)
    tag_draft.find = lambda *args, **kwargs: object()
    tag_draft.click = lambda control: None
    tag_draft.keys = lambda keys: None
    tag_draft.keyboard_field = lambda field: None
    tag_states = iter((["还可以添加10个标签"], ["还可以添加9个标签", "舞蹈"]))
    tag_draft.control_names = lambda: next(tag_states)
    assert tag_draft.fill_tags(["舞蹈", "未接受标签"]) is False
    concurrent = object.__new__(BilibiliDraft)
    order = []
    for operation in ("select_browser", "start_keyboard_helper", "upload", "scroll_page", "wait_name",
                      "input_column", "cover", "keyboard_fill", "declaration", "category",
                      "fill_tags", "description", "schedule", "wait_uploaded", "find", "click", "check", "on_submit"):
        setattr(concurrent, operation, lambda *args, op=operation, **kwargs: order.append(op))
    concurrent.locate = lambda name: None
    concurrent.wait_bilibili_form = lambda: None
    concurrent.reveal_bilibili_cover = lambda: None
    concurrent.bilibili_cover_is_confirmed = lambda *args: False
    concurrent.bilibili_tags_match = lambda *args: False
    concurrent.bilibili_description_matches = lambda *args: False
    concurrent.validate_bilibili_draft = lambda *args: None
    concurrent.run(Path("作品/最终成片.mp4"), "标题", "简介", ["标签"], Path("封面.png"), "舞蹈", None)
    assert order.index("cover") < order.index("wait_uploaded")
    assert order.index("schedule") < order.index("wait_uploaded") < order.index("click")
    assert order.index("wait_uploaded") < order.index("on_submit") < order.index("click")
    from publish_queue import PublishQueue
    queue = object.__new__(PublishQueue)
    queue.running = queue.closed = False
    queue.items = {"one": {"folder": Path("one"), "scheduled": False, "date": "", "hour": ""},
                   "two": {"folder": Path("two"), "scheduled": False, "date": "", "hour": ""}}
    queue.completed = []
    queue.root = Mock()
    queue.start_button = Mock()
    queue.tree = Mock()
    queue.notice = Mock()
    queue.status_file = Mock()
    queue.prepare = lambda folder: (folder / "最终成片.mp4", "标题", "简介", ["标签"])
    queue.cover = lambda folder: folder / "B站4x3.png"
    queue.category = lambda folder: "舞蹈"
    first, second = Mock(), Mock()
    first.actual_publish_at = second.actual_publish_at = None
    second.run.side_effect = RuntimeError("模拟下一条失败")
    queue.draft_class = Mock(side_effect=[first, second])
    queue.start()
    assert list(queue.items) == ["two"]
    assert len(queue.completed) == 1
    first.next_upload.assert_called_once()
    second.next_upload.assert_not_called()
    queue.tree.delete.assert_called_once_with("one")
    paused = object.__new__(BilibiliDraft)
    paused.window = None
    paused.on_tick = None
    paused.on_pause = Mock()
    paused.paused_seconds = 0.0
    with patch("bilibili_auto.ctypes.windll.user32.GetAsyncKeyState", side_effect=[0x8000,0x8000,0]):
        paused.check()
    paused.on_pause.assert_called_once()
    now = datetime(2026, 10, 9, 12, 0, tzinfo=CHINA_TIME)
    assert parse_publish_time("2026-10-10 03:39", now, require_five_minutes=False) == datetime(2026, 10, 10, 3, 39, tzinfo=CHINA_TIME)
    assert parse_publish_time("", now) is None
    assert parse_publish_time("2026-10-09 12:05", now) == now + timedelta(minutes=5)
    assert parse_publish_time("2026-10-24 12:00", now) == now + timedelta(days=15)
    for value in (
        "2026-10-09 12:04", "2026-10-09 13:01", "2026-10-24 12:01",
        "2026-02-30 12:00", "下午六点",
    ):
        try:
            parse_publish_time(value, now)
        except ValueError:
            pass
        else:
            raise AssertionError("应拒绝格式错误或超出范围的定时时间")
    assert category_from_folder(Path("028_跳舞_测试")) == "舞蹈"
    assert category_from_folder(Path("028_唱歌_测试")) == "音乐"
    for folder_name in ("未知", "028_跳舞_唱歌"):
        try:
            category_from_folder(Path(folder_name))
        except ValueError:
            pass
        else:
            raise AssertionError("不应猜测分区")
    with tempfile.TemporaryDirectory() as folder:
        try:
            cover_from_folder(Path(folder))
        except ValueError:
            pass
        else:
            raise AssertionError("缺少封面必须停止")
        for name in ("封面.jpg", "封面.png", "B站4x3.png"):
            (Path(folder) / name).touch()
            assert cover_from_folder(Path(folder)).name == name
        path = Path(folder) / "文案.txt"
        path.write_text("标题：\n测试\n简介：\n第一行💃\n第二行\n标签：\n#舞蹈 #穿搭 #舞蹈", encoding="utf-8")
        title, description, tags = read_copy(path)
        assert title == "测试" and "💃" in description and "\n" in description
        assert tags == ["舞蹈", "穿搭"]
        (Path(folder) / "发布文案.txt").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        (Path(folder) / "最终成片.mp4").touch()
        video, auto_title, auto_description, auto_tags = prepare_files(Path(folder))
        assert video.name == "最终成片.mp4" and auto_title == title
        assert auto_description == description and auto_tags == tags
        (Path(folder) / "最终成片.mp4").unlink()
        for name in ("一.mp4", "二.mp4"):
            (Path(folder) / name).touch()
        try:
            prepare_files(Path(folder))
        except ValueError:
            pass
        else:
            raise AssertionError("应拒绝不明确的视频选择")
        path.write_text("标题：只有标题", encoding="utf-8")
        try:
            read_copy(path)
        except ValueError:
            pass
        else:
            raise AssertionError("应拒绝缺失章节的文案")
    assert detect_platform("创作中心 - 哔哩哔哩 - Microsoft Edge") == "bilibili"
    assert is_upload_url("https://member.bilibili.com/platform/upload/video/frame")
    assert not is_upload_url("https://member.bilibili.com.evil.test/platform/upload/video")
    assert not is_upload_url("https://member.bilibili.com/platform/home")
    def node(kind, left, top, width=30, height=20):
        box = SimpleNamespace(
            left=left, top=top, right=left + width, bottom=top + height,
            width=lambda: width, height=lambda: height,
        )
        return SimpleNamespace(ControlTypeName=kind, BoundingRectangle=box)

    label = node("TextControl", 100, 200)
    container = node("GroupControl", 100, 200, 600, 160)
    nested_label = node("TextControl", 102, 202)
    assert choose_control([container, label, nested_label]) is label
    assert choose_control([]) is None
    try:
        choose_control([label, node("TextControl", 100, 500)])
    except RuntimeError:
        pass
    else:
        raise AssertionError("不能合并不同位置的同名控件")
    assert detect_platform("抖音创作者中心") == "douyin"
    assert detect_platform("小红书创作服务平台") == "xiaohongshu"
    for title in ["其他网页", "抖音 - 小红书"]:
        try:
            detect_platform(title)
        except ValueError:
            pass
        else:
            raise AssertionError("应拒绝未知或歧义平台")
    before = Image.new("RGB", (400, 200), "white")
    after = before.copy()
    ImageDraw.Draw(after).rectangle((10, 10, 300, 100), fill=(245, 245, 245))
    assert candidate_visible(before, after)
    assert not candidate_visible(before, before)
    assert candidate_stable(after, after)
    assert not candidate_stable(before, after)
    def observation(name="", labels=(), focused=True):
        return {"controls": [{"name": name, "focused": focused}], "nearby_labels": list(labels)}

    assert classify_field("xiaohongshu", observation("填写标题会有更多赞哦")) == "title"
    assert classify_field("xiaohongshu", observation("输入正文描述，真诚有价值的分享予人温暖")) == "description"
    assert classify_field("douyin", observation("添加作品描述")) == "description"
    assert classify_field("bilibili", observation("按回车键Enter创建标签")) == "tags"
    assert classify_field("bilibili", observation(labels=["* 标题"])) == "title"
    assert classify_field("bilibili", observation(labels=["简介"])) == "description"
    assert classify_field("xiaohongshu", observation(labels=["输入正文描述，真诚有价值的分享予人温暖"])) == "description"
    assert classify_field("bilibili", observation("填写标题", labels=["简介"])) == "title"
    assert classify_field("douyin", observation("添加作品简介")) == "description"
    assert classify_field("bilibili", observation("请输入稿件标题")) == "title"
    rich_editor = {"controls": [{
        "focus_origin": True, "focused": False, "control_type": "ControlType.Group",
        "text_read_only": False,
    }]}
    assert classify_field("douyin", rich_editor) == "description"
    douyin_editor = {"controls": [{
        "focus_origin": True, "control_type": "ControlType.Group",
        "class_name": "zone-container editor-kit-container editor editor-comp-publish notranslate chrome window chrome88",
    }]}
    assert classify_field("douyin", douyin_editor) == "description"
    for platform in ("bilibili", "xiaohongshu"):
        try:
            classify_field(platform, douyin_editor)
        except ValueError:
            pass
        else:
            raise AssertionError("抖音编辑器类名不能用于识别其他平台")
    for control in [
        {"focus_origin": True, "automation_id": "RootWebArea", "text_read_only": False},
        {"focus_origin": True, "control_type": "ControlType.Group", "text_read_only": True},
        {"focus_origin": True, "control_type": "ControlType.Edit", "text_read_only": False},
    ]:
        try:
            classify_field("douyin", {"controls": [control]})
        except ValueError:
            pass
        else:
            raise AssertionError("整页、只读控件和未知单行输入框不能视为正文")
    for invalid in [observation(), observation(labels=["标题", "简介"]), observation("标题", focused=False)]:
        try:
            classify_field("bilibili", invalid)
        except ValueError:
            pass
        else:
            raise AssertionError("未知、歧义或未聚焦控件必须拒绝填写")
    class PendingMessages:
        def __init__(self, actions):
            self.actions = list(actions)

        def PeekMessageW(self, pointer, window, minimum, maximum, remove):
            assert minimum == maximum == 0x312 and remove == 1
            if not self.actions:
                return 0
            pointer._obj.wParam = self.actions.pop(0)
            return 1

    assert discard_pending_hotkeys(PendingMessages([1, 1, 5])) == (3, False)
    assert discard_pending_hotkeys(PendingMessages([1, 4, 1])) == (2, True)
    assert discard_pending_hotkeys(PendingMessages([])) == (0, False)
    print("文案、平台、焦点、候选检测及重复热键队列检查通过")


if __name__ == "__main__":
    main()
