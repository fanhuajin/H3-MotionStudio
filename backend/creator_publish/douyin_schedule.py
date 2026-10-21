"""抖音日期弹层识别；只读取节点，不输入日期/时间文本。"""
import calendar
import json
import time
from datetime import datetime
from pathlib import Path
from statistics import median


def center(control):
    r=control.BoundingRectangle
    return (r.left+r.right)/2,(r.top+r.bottom)/2


def inside_time_list(control):
    """滚轮会暴露被列表裁切的循环副本，窗口内可见不足以证明可点击。"""
    node=getattr(control,"control",control)
    parent=getattr(node,"GetParentControl",None)
    if parent is None:
        return True
    r=control.BoundingRectangle
    for _ in range(10):
        node=parent()
        if node is None:
            return False
        kind=getattr(node,"ControlTypeName","")
        if kind in ("DocumentControl","WindowControl"):
            return False
        bounds=node.BoundingRectangle
        if "semi-scrolllist-list-outer" in getattr(node,"ClassName","").split():
            return (bounds.left<=r.left<r.right<=bounds.right
                    and bounds.top<=r.top<r.bottom<=bounds.bottom)
        parent=getattr(node,"GetParentControl",None)
        if parent is None:
            return False
    return False


def calendar_day(controls, year, month, day):
    from schedule_controls import month_calendar_day
    return month_calendar_day(controls, year, month, day, "抖音")


def date_candidates(draft, header, controls):
    """优先按日期面板范围取节点，避免短月份标题裁掉边缘日期列。"""
    r=header.BoundingRectangle
    node=getattr(header,"control",header)
    # 面板类名仅用来缩小读取范围，最终仍必须通过月份序列和几何验证。
    for _ in range(8):
        if node is None or getattr(node,"ControlTypeName","") in ("DocumentControl","WindowControl"):
            break
        class_name=getattr(node,"ClassName","").lower()
        bounds=node.BoundingRectangle
        if (any(word in class_name for word in ("datepicker","calendar"))
                and 180<=bounds.width()<=800 and 120<=bounds.height()<=650
                and bounds.left<=r.left<r.right<=bounds.right
                and bounds.top<=r.top<r.bottom<=bounds.bottom):
            return [c for c in controls if draft.visible(c) and c.Name.isdigit()
                    and bounds.left<center(c)[0]<bounds.right
                    and r.bottom<center(c)[1]<bounds.bottom]
        get_parent=getattr(node,"GetParentControl",None)
        node=get_parent() if get_parent else None
    # UIA未暴露面板类名时扩大标题两侧范围；不假定所有行都有七个数字。
    return [c for c in controls if draft.visible(c) and c.Name.isdigit()
            and r.left-220<center(c)[0]<r.right+220
            and r.bottom+25<center(c)[1]<r.bottom+360]


def find_calendar_day(draft, header, selected):
    deadline=draft.now()+5
    days=[]
    last_error=RuntimeError("抖音日历日期网格未就绪，未选择日期。")
    while draft.now()<deadline:
        draft.check()
        days=date_candidates(draft,header,draft.controls())
        try:
            return calendar_day(days,selected.year,selected.month,selected.day)
        except RuntimeError as error:
            last_error=error
        # 等待弹层渲染，只重读；不重新打开弹层或重复点击日期。
        time.sleep(0.15)
    # 仅保存最后一次已读取的候选数字，不另行抓全页、改焦点或重发输入。
    def record(c):
        r=c.BoundingRectangle
        return {"name":c.Name,"type":c.ControlTypeName,"rect":[r.left,r.top,r.right,r.bottom]}
    diagnostic={"captured_at":datetime.now(selected.tzinfo).isoformat(timespec="seconds"),
                "target":str(selected),"header":record(header),"error":str(last_error),
                "candidates":[record(c) for c in days]}
    path=Path(__file__).with_name("douyin_datepicker_diagnostic.json")
    try:
        path.write_text(json.dumps(diagnostic,ensure_ascii=False,indent=2),encoding="utf-8")
    except OSError:
        raise last_error
    raise RuntimeError(f"{last_error} 日期控件诊断已保存至{path.name}。") from last_error
