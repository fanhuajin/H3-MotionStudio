"""小红书小时列表定位和失败诊断；不输入日期/时间文本。"""
import json
import re
from datetime import datetime
from pathlib import Path


def hour_options(draft):
    from bilibili_auto import CHINA_TIME, choose_control
    controls=draft.controls()
    # 保存本轮已读取的时间相关节点，失败时不重新抓全页或抢回焦点。
    visible=[c for c in controls if draft.visible(c)
             and re.fullmatch(r"(?:\d{1,2}(?:时|分)?|\d{2}:\d{2}|时|分)",c.Name)]
    def record(c):
        r=c.BoundingRectangle
        return {"name":c.Name,"type":c.ControlTypeName,"rect":[r.left,r.top,r.right,r.bottom]}
    draft.xiaohongshu_hour_snapshot={"captured_at":datetime.now(CHINA_TIME).isoformat(timespec="seconds"),
                                   "candidates":[record(c) for c in visible]}
    labels=[c for c in visible if c.Name=="时"]
    if not labels:
        # 未选中的小时不会暴露“时”；用分钟列和连续垂直小时列共同确认。
        minute_labels=[c for c in visible if c.Name=="分"]
        if len(minute_labels)!=1:
            return []
        minute=minute_labels[0].BoundingRectangle
        groups={}
        for c in visible:
            r=c.BoundingRectangle
            if (re.fullmatch(r"\d{2}",c.Name)
                    and c.ControlTypeName in ("TextControl","ListItemControl")
                    and minute.left-160<r.left<minute.left-40
                    and minute.top-65<r.top<minute.top+300):
                groups.setdefault(round(r.left/8),[]).append(c)
        columns=[]
        for group in groups.values():
            group.sort(key=lambda c:c.BoundingRectangle.top)
            # 边缘裁切节点的坐标可能偏移；仅使用连续、等距的可见部分。
            runs=[]
            for c in group:
                if not 0<=int(c.Name)<=23:
                    continue
                if (not runs or int(c.Name)!=int(runs[-1][-1].Name)+1
                        or not 25<=c.BoundingRectangle.top-runs[-1][-1].BoundingRectangle.top<=65):
                    runs.append([])
                runs[-1].append(c)
            valid=[run for run in runs if len(run)>=3]
            if valid:
                columns.append(max(valid,key=len))
        return columns[0] if len(columns)==1 else []
    label=choose_control(labels)
    hr=label.BoundingRectangle
    column=[c for c in visible if re.fullmatch(r"\d{2}(?:时)?",c.Name)
             and c.ControlTypeName in ("TextControl","ListItemControl")
             and hr.left-50<c.BoundingRectangle.left<hr.right
             and hr.top-400<c.BoundingRectangle.top<hr.top+400]
    # “时”跟随选中行移动，并非固定表头。用整列顶部确定可点区域，
    # 否则选中22点时只剩22、23，滚轮会落在底部渐隐遮罩上。
    minute_labels=[c for c in visible if c.Name=="分"]
    top=hr.top
    if len(minute_labels)==1:
        ceiling=min(hr.top,minute_labels[0].BoundingRectangle.top)-65
        top=min([hr.top]+[c.BoundingRectangle.top for c in column
                              if c.BoundingRectangle.top>ceiling])
    options=[c for c in column
             # 弹层底部有渐隐遮挡；边缘数字虽暴露为控件，点击不会选中。
             # 保留完整可点行，目标在底部时交给列表滚动移入内部。
             if top-10<c.BoundingRectangle.top<top+300]
    if any(int(c.Name.removesuffix("时"))>23 for c in options):
        raise RuntimeError("小红书小时列包含无效小时，未滚动或发布。")
    return options


def choose_hour(draft, selected):
    from schedule_controls import scroll_select
    try:
        scroll_select(draft,draft.xiaohongshu_hour_options,selected.hour,"小红书小时")
    except RuntimeError as error:
        diagnostic={**getattr(draft,"xiaohongshu_hour_snapshot",{}),
                    "target":str(selected),"error":str(error)}
        path=Path(__file__).with_name("xiaohongshu_time_diagnostic.json")
        try:
            path.write_text(json.dumps(diagnostic,ensure_ascii=False,indent=2),encoding="utf-8")
        except OSError:
            raise error
        raise RuntimeError(f"{error} 时间控件诊断已保存至{path.name}。") from error
