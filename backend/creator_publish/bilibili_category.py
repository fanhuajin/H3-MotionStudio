"""B站分区：读取实际行与菜单控件，限定滚动范围并等待选中值回读。"""

from _ctypes import COMError
from dataclasses import dataclass
import time


CATEGORY_NAMES = frozenset((
    "音乐", "舞蹈", "动画", "绘画", "娱乐", "游戏", "知识", "科技", "运动", "汽车",
    "美食", "生活", "影视", "时尚", "动物圈", "亲子", "健康", "情感", "vlog",
))
MENU_CLASS = "drop-list-v2-content-wrp"
PLACEHOLDERS = frozenset(("请选择分区", "请选择视频分区", "选择分区"))


def _choose(controls):
    from bilibili_auto import choose_control
    return choose_control(controls)


def _center(control):
    rect = control.BoundingRectangle
    return (rect.left + rect.right) / 2, (rect.top + rect.bottom) / 2


def _inside(control, container):
    rect, bounds = control.BoundingRectangle, container.BoundingRectangle
    return (bounds.left <= rect.left < rect.right <= bounds.right
            and bounds.top <= rect.top < rect.bottom <= bounds.bottom)


def _row_label(draft, controls):
    return _choose([c for c in controls if c.Name == "分区" and draft.visible(c)])


def _row_controls(draft, controls, label):
    if label is None:
        return []
    left = draft.input_column()
    _, top = _center(label)
    return [c for c in controls if draft.visible(c)
            and left - 15 <= c.BoundingRectangle.left < left + 600
            and abs(_center(c)[1] - top) <= 25]


def selected_category(draft, controls=None):
    """只从分区表单行回读；下拉菜单内的同名值不代表已保存。"""
    controls = draft.controls() if controls is None else controls
    label = _row_label(draft, controls)
    found = _choose([c for c in _row_controls(draft, controls, label)
                     if c.Name in CATEGORY_NAMES])
    return found.Name if found else None


def category_opener(draft, controls=None):
    """点击实际当前值/组合框，避免依赖标题列加固定偏移量。"""
    controls = draft.controls() if controls is None else controls
    row = _row_controls(draft, controls, _row_label(draft, controls))
    # 2026-10-13现场：select-container内的此箭头是真实分区开关。
    observed = [c for c in row if c.ControlTypeName in ("GroupControl", "CustomControl")
                and "select-container" in getattr(c, "ClassName", "").split()]
    container = _choose(observed)
    if container is not None:
        arrows = [c for c in row if c.ControlTypeName in ("ImageControl", "ButtonControl", "TextControl")
                  and "selebox-box-v2-drop-icon" in getattr(c, "ClassName", "").split()
                  and _inside(c, container)]
        return _choose(arrows) or container
    combos = [c for c in row if c.ControlTypeName == "ComboBoxControl"]
    if combos:
        return _choose(combos)
    values = [c for c in row if c.Name in CATEGORY_NAMES | PLACEHOLDERS]
    if values:
        return _choose(values)
    # 部分版本只暴露空名称容器；它必须是分区行内的真实下拉控件。
    containers = [c for c in row if c.ControlTypeName in ("GroupControl", "CustomControl", "ButtonControl")
                  and "drop-list-v2" in getattr(c, "ClassName", "")
                  and MENU_CLASS not in getattr(c, "ClassName", "")]
    return _choose(containers)


def _menu_ancestor(control):
    """旧父节点只影响本轮证据；不会把COM异常带入输入流程。"""
    node = getattr(control, "control", control)
    for _ in range(8):
        if node is None:
            return None
        try:
            role = node.ControlTypeName
            cls = getattr(node, "ClassName", "").lower()
            if role=="ToolBarControl" or "bookmark" in cls:
                return None
            if "select-item-cont" in cls or "select-container" in cls.split():
                return None  # 表单当前值容器，绝不是已展开的菜单项。
            if MENU_CLASS in cls or role in ("MenuControl", "ListControl"):
                return node
            if role in ("MenuItemControl", "ListItemControl") or any(
                marker in cls for marker in ("select-option", "select-dropdown-item", "drop-list-v2-item")
            ):
                return node
            if role in ("DocumentControl", "WindowControl"):
                return None
            parent = getattr(node, "GetParentControl", None)
            node = parent() if parent else None
        except COMError:
            return None
    return None


def _unique_options(controls):
    groups = {}
    for control in controls:
        groups.setdefault(control.Name, []).append(control)
    return [_choose(group) for group in groups.values()]


@dataclass
class CategoryMenu:
    container: object | None
    options: list
    all_options: list


def read_menu(draft, controls=None):
    """包装类名、实际菜单容器或多个纵向选项提供一致的就绪证据。"""
    controls = draft.controls() if controls is None else controls
    categories = [c for c in controls if c.Name in CATEGORY_NAMES
                  and "bookmark" not in getattr(c,"ClassName","").lower()]
    visible = [c for c in categories if draft.visible(c)]
    label = _row_label(draft, controls)
    containers = [c for c in controls if MENU_CLASS in getattr(c, "ClassName", "")
                  and draft.visible(c)]
    container = _choose(containers)
    if container is None:
        # UIA平铺遍历可能缺少包装节点，其真实祖先仍能提供剪裁矩形。
        from bilibili_auto import ControlSnapshot
        ancestors = []
        for option in visible:
            ancestor = _menu_ancestor(option)
            if ancestor is None:
                continue
            try:
                if (MENU_CLASS in getattr(ancestor, "ClassName", "")
                        or ancestor.ControlTypeName in ("MenuControl", "ListControl")):
                    snapshot = ControlSnapshot(ancestor)
                    if draft.visible(snapshot):
                        ancestors.append(snapshot)
            except COMError:
                continue
        container = _choose(ancestors)
    if container is None and label is not None:
        left, (_, row_y) = draft.input_column(), _center(label)
        # 缺少专用类名时仍使用UIA真实容器矩形，不造一个虚拟菜单范围。
        candidates = []
        for control in controls:
            rect = control.BoundingRectangle
            if (control.ControlTypeName not in ("GroupControl", "CustomControl", "ListControl", "MenuControl")
                    or not draft.visible(control) or not left - 20 <= rect.left <= left + 60
                    or not 100 <= rect.right - rect.left <= 600
                    or not 60 <= rect.bottom - rect.top <= 600
                    or rect.top <= row_y <= rect.bottom
                    or not abs(_center(control)[1] - row_y) < 550):
                continue
            if len({c.Name for c in visible if _inside(c, control)}) >= 3:
                candidates.append(control)
        if candidates:
            area = lambda c: ((c.BoundingRectangle.right - c.BoundingRectangle.left)
                              * (c.BoundingRectangle.bottom - c.BoundingRectangle.top))
            smallest = min(area(c) for c in candidates)
            container = _choose([c for c in candidates if area(c) == smallest])
    if container is not None:
        texts=[c for c in controls if c.Name and any(ch.isalnum() for ch in c.Name)
               and c.ControlTypeName in ("TextControl","ListItemControl","MenuItemControl")
               and "bookmark" not in getattr(c,"ClassName","").lower()]
        dedicated=MENU_CLASS in getattr(container,"ClassName","")
        categories=[c for c in texts if _menu_ancestor(c) is not None
                    or (not dedicated and c.Name in CATEGORY_NAMES)]
        options = _unique_options([c for c in categories if draft.visible(c) and _inside(c, container)])
        # 离开剪裁范围的目标必须有真实菜单祖先，不能把推荐标签当作滚动方向。
        all_options = [c for c in categories if _inside(c, container)
                       or _menu_ancestor(c) is not None]
        return CategoryMenu(container, options, all_options)
    if label is not None:
        left, (_, row_y) = draft.input_column(), _center(label)
        nearby = [c for c in visible if left <= c.BoundingRectangle.left < left + 260
                  and 35 < abs(_center(c)[1] - row_y) < 500]
        # 同一纵列的至少三种类别构成菜单证据；普通推荐标签通常横向排列。
        for anchor in nearby:
            column = _unique_options([c for c in nearby
                                      if abs(c.BoundingRectangle.left - anchor.BoundingRectangle.left) <= 12])
            column.sort(key=lambda c: _center(c)[1])
            gaps = [_center(b)[1] - _center(a)[1] for a, b in zip(column, column[1:])]
            if len(column) >= 3 and all(15 <= gap <= 70 for gap in gaps):
                return CategoryMenu(None, column, column)
    if label is not None:
        left,(_,row_y)=draft.input_column(),_center(label)
        visible=[c for c in visible if left-20<=c.BoundingRectangle.left<left+600
                 and 30<abs(_center(c)[1]-row_y)<550]
    explicit = _unique_options([c for c in visible if _menu_ancestor(c) is not None])
    return CategoryMenu(None, explicit, explicit) if explicit else None


def category_option(draft, value, controls=None):
    menu = read_menu(draft, controls)
    return _choose([c for c in menu.options if c.Name == value]) if menu else None


def category_menu(draft, controls=None):
    menu = read_menu(draft, controls)
    return menu.container if menu else None


def _make_menu_room(draft, controls):
    """未打开菜单时逐格露出下方空间，滚动后用新控件位置再定位。"""
    window = getattr(draft, "window", None)
    if window is None:
        return controls
    previous = None
    for _ in range(8):
        label = _row_label(draft, controls)
        if label is None:
            raise RuntimeError("B站分区行滚动后不可见，未点击或发布。")
        bottom = label.BoundingRectangle.bottom
        if bottom < window.BoundingRectangle.bottom - 350 or bottom == previous:
            return controls
        previous = bottom
        draft.scroll_page("down", 1)
        controls = draft.controls()
    return controls


def set_category(draft, value, *, timeout=5, max_steps=60):
    if value not in CATEGORY_NAMES:
        raise ValueError(f"不支持的B站分区：{value}")
    draft.find("分区")
    controls = draft.controls()
    if selected_category(draft, controls) == value:
        return
    menu = read_menu(draft, controls)
    if menu is None:
        controls = _make_menu_room(draft, controls)
        opener = category_opener(draft, controls)
        if opener is None:
            raise RuntimeError("无法识别B站分区下拉控件，未点击或发布。")
        draft.click(opener)
    deadline = draft.now() + timeout
    while menu is None or not menu.options:
        draft.check()
        if draft.now() >= deadline:
            raise RuntimeError("B站分区菜单未就绪，未继续点击或发布。")
        time.sleep(0.15)
        menu = read_menu(draft)
    previous, unchanged, direction, reversed_once = None, 0, "up", False
    for _ in range(max_steps):
        draft.check()
        chosen = _choose([c for c in menu.options if c.Name == value])
        if chosen is not None:
            draft.click(chosen)
            break
        signature = tuple(sorted((c.Name, *_center(c)) for c in menu.options))
        unchanged = unchanged + 1 if signature == previous else 0
        previous = signature
        if unchanged >= 4:
            if reversed_once:
                raise RuntimeError(f"B站分区菜单滚动后未变化，未找到{value}，未发布。")
            direction, reversed_once, previous, unchanged = "down", True, None, 0
        hidden = _unique_options([c for c in menu.all_options if c.Name == value])
        if hidden and menu.container is not None:
            direction = "up" if _center(hidden[0])[1] < menu.container.BoundingRectangle.top else "down"
        anchor = menu.container or sorted(menu.options, key=lambda c: _center(c)[1])[len(menu.options) // 2]
        draft.scroll_in_control(anchor, direction, 1)
        time.sleep(0.15)
        menu = read_menu(draft)
        if menu is None or not menu.options:
            raise RuntimeError("B站分区菜单滚动后消失，未继续滚动页面或发布。")
    else:
        raise RuntimeError(f"B站分区列表中没有找到{value}，未发布。")
    deadline = draft.now() + timeout
    while draft.now() < deadline:
        draft.check()
        if selected_category(draft) == value:
            return
        time.sleep(0.15)
    raise RuntimeError(f"未确认{value}分区，未继续发布。")
