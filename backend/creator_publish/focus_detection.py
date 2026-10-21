"""读取 Windows 焦点控件并识别字段；不改变焦点、不依靠固定坐标。"""

import json
from pathlib import Path
import subprocess


def classify_field(platform, observation):
    """按输入提示、关联标签、同一行字段标签识别；歧义或无证据时拒绝输入。"""
    controls = observation.get("controls", [])
    # 浏览器整页 Document 不是正文编辑器，必须有真实的焦点输入控件。
    focused = [control for control in controls if control.get("focused") or control.get("focus_origin")]
    if not focused:
        raise ValueError("没有识别到已聚焦的输入框，请先点击目标输入框。")
    if all(
        control.get("control_type") == "ControlType.Pane"
        and control.get("class_name") == "View"
        and not any(control.get(key) for key in ("name", "help", "label"))
        for control in focused
    ):
        raise ValueError(
            "浏览器未向 Windows 暴露网页输入框。\n"
            "Edge 请打开 edge://accessibility，Chrome 请打开 chrome://accessibility，"
            "开启 Native accessibility（原生可访问性），然后回到投稿页点击输入框再按 F6。"
        )

    def matches(text):
        text = str(text or "").strip().strip("*＊：: ").lower()
        result = set()
        if text in ("标题", "作品标题") or any(
            hint in text for hint in ("填写标题", "填写作品标题", "输入标题", "请输入稿件标题", "写标题", "标题会有更多")
        ):
            result.add("title")
        if text in ("简介", "正文", "内容简介", "作品描述") or any(
            hint in text for hint in ("输入正文", "填写简介", "输入简介", "填写更全面的相关信息", "正文描述", "添加作品描述", "添加作品简介")
        ):
            result.add("description")
        if text in ("标签", "话题输入") or any(
            hint in text for hint in ("创建标签", "输入标签", "添加标签", "输入话题")
        ):
            result.add("tags")
        return result

    # 优先使用焦点控件自身属性；不从现有正文、字段高度或鼠标坐标猜类型。
    result = set()
    for control in focused:
        for key in ("name", "help", "label"):
            result.update(matches(control.get(key)))
    if not result:
        for label in observation.get("nearby_labels", []):
            result.update(matches(label))
    if not result:
        # contenteditable 未声明 role=textbox 时，Chrome 可能暴露为 Group/Custom。
        # 只接受当前焦点的可写富文本证据；整页 RootWebArea 和普通 Edit 不降级猜测。
        for control in focused:
            if control.get("automation_id") == "RootWebArea":
                continue
            class_name = control.get("class_name", "").lower()
            known_editor = any(name in class_name.split() for name in ("ql-editor", "prosemirror", "tiptap"))
            if control.get("control_type") == "ControlType.Edit" and not known_editor:
                continue
            # 抖音 Slate 正文的稳定类名；不使用带构建哈希的外层容器类名。
            if platform == "douyin" and control.get("focus_origin"):
                known_editor = known_editor or {
                    "zone-container", "editor-kit-container", "editor-comp-publish"
                }.issubset(class_name.split())
            writable = control.get("text_read_only") is False
            if known_editor or writable:
                result.add("description")
    if len(result) != 1:
        raise ValueError("无法明确识别标题、简介或标签框。为避免填错，本次未输入。")
    field = result.pop()
    if platform != "bilibili" and field == "tags":
        raise ValueError("该平台请点击正文编辑区，F6 会一并填写简介和话题。")
    return field


def focused_field(platform, expected_window):
    """通过系统 UI Automation 只读探测，超时或窗口改变时停止，不操作浏览器。"""
    process = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-File",
         str(Path(__file__).with_name("focus_probe.ps1"))],
        capture_output=True,
        encoding="utf-8-sig",
        errors="replace",
        timeout=8,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if process.returncode:
        detail = (process.stderr or process.stdout).strip()
        raise RuntimeError("浏览器焦点检测脚本执行失败：\n" + (detail or f"退出码 {process.returncode}"))
    observation = json.loads(process.stdout)
    handle = observation.get("window_handle")
    if not handle or handle != expected_window:
        raise RuntimeError("焦点窗口与当前浏览器不一致，本次未输入。")
    try:
        return classify_field(platform, observation)
    except ValueError:
        # 仅失败时落本地诊断，便于确认平台是否暴露输入提示；不上传诊断。
        Path(__file__).with_name("focus_diagnostic.json").write_text(
            json.dumps(observation, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        raise
