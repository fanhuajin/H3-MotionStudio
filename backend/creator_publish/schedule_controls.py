"""日历与时间列表的只读定位、真实滚动和选中回读；不输入时间文本。"""
import time
from datetime import datetime


def wait_candidates(draft, read, description, timeout=5):
    deadline=draft.now()+timeout
    while draft.now()<deadline:
        draft.check()
        found=read()
        if found:
            return found
        time.sleep(0.15)
    raise RuntimeError(f"{description}未就绪，未继续点击或发布。")


def scroll_select(draft, options, target, description, *, max_steps=60):
    """只在已识别的列表内滚动，列表消失或持续不动时立即停止。"""
    from bilibili_auto import choose_control
    previous = None
    unchanged = 0
    ready_deadline = draft.now()+5
    for _ in range(max_steps):
        draft.check()
        controls = options()
        while not controls and previous is None and draft.now()<ready_deadline:
            draft.check()
            time.sleep(0.15)
            controls=options()
        if not controls:
            raise RuntimeError(f"{description}列表未出现或已关闭，未滚动页面或发布。")
        values = [int(c.Name.removesuffix("时").removesuffix("分")) for c in controls]
        chosen = choose_control([c for c,v in zip(controls,values) if v == target])
        if chosen is not None:
            if hasattr(chosen,"control"):
                draft.click_declaration(chosen)
            else:
                draft.click(chosen)
            return
        signature = tuple(sorted((c.Name,c.BoundingRectangle.left,c.BoundingRectangle.top) for c in controls))
        unchanged = unchanged + 1 if signature == previous else 0
        if unchanged >= 5:
            raise RuntimeError(f"{description}列表滚动后未变化：目标{target:02d}，可见值{values}，未发布。")
        previous = signature
        # 光标始终落在本轮列表中间项，不能在旧锚点或整页上发送滚轮。
        anchor_control = sorted(controls,key=lambda c:c.BoundingRectangle.top)[len(controls)//2]
        anchor = anchor_control.BoundingRectangle
        direction = "up" if target < min(values) else "down"
        if hasattr(anchor_control,"control"):
            live=anchor_control.control
            point=((anchor.left+anchor.right)//2,(anchor.top+anchor.bottom)//2)
            draft.move_pointer(point)
            latest=live.BoundingRectangle
            if not draft.visible(live):
                raise RuntimeError(f"{description}滚动锚点已不可见，未滚动页面或发布。")
            if not latest.left<=point[0]<latest.right or not latest.top<=point[1]<latest.bottom:
                previous=None
                continue
            draft.scroll_at(direction,1)
        else:
            draft.scroll_at(direction,1,((anchor.left+anchor.right)//2,(anchor.top+anchor.bottom)//2))
        time.sleep(0.2)
    raise RuntimeError(f"{description}列表未找到{target:02d}，未发布。")


def calendar_day(controls, year, month, day):
    """按七列日历次序匹配当月日期，避免选到相邻月份同名日。"""
    from bilibili_auto import choose_control
    groups = []
    for c in controls:
        r = c.BoundingRectangle
        for group in groups:
            other=group[0].BoundingRectangle
            if c.Name==group[0].Name and abs((r.left+r.right)-(other.left+other.right))<=16 and abs((r.top+r.bottom)-(other.top+other.bottom))<=16:
                group.append(c)
                break
        else:
            groups.append([c])
    cells = [choose_control(group) for group in groups]
    cells.sort(key=lambda c:(c.BoundingRectangle.top,c.BoundingRectangle.left))
    if not cells:
        raise RuntimeError("日历日期网格未出现，未选择日期。")
    rows = []
    for c in cells:
        if not rows or abs(c.BoundingRectangle.top-rows[-1][0])>8:
            rows.append((c.BoundingRectangle.top,[]))
        rows[-1][1].append(c)
    if any(len(row)!=7 for _,row in rows):
        raise RuntimeError("未确认完整七列日历，未选择日期。")
    cells = [c for _,row in rows for c in sorted(row,key=lambda c:c.BoundingRectangle.left)]
    # 以连续1、2、3的位置确定周起点，不假设平台按周一或周日开头。
    starts = [i for i,c in enumerate(cells[:7]) if c.Name=="1" and i+2<len(cells)
              and cells[i+1].Name=="2" and cells[i+2].Name=="3"]
    if len(starts)!=1:
        raise RuntimeError("无法确认当月1号在日历中的位置，未选择日期。")
    index = starts[0]+day-1
    if index>=len(cells) or cells[index].Name!=str(day):
        raise RuntimeError("日历日期次序与当月不一致，未选择日期。")
    # 顺序至少应覆盖当月1号至所选日。
    if any(cells[starts[0]+i-1].Name!=str(i) for i in range(1,day+1)):
        raise RuntimeError("日历当月日期不连续，未选择日期。")
    datetime(year,month,day)  # 拒绝不存在的日期。
    return cells[index]


def month_calendar_day(controls, year, month, day, platform):
    """按当月完整序列及实际七列位置验证，不要求每行暴露相邻月份。"""
    import calendar
    from statistics import median
    from bilibili_auto import choose_control
    def center(control):
        r=control.BoundingRectangle
        return (r.left+r.right)/2,(r.top+r.bottom)/2
    datetime(year,month,day)
    groups=[]
    day_names={str(i) for i in range(1,32)}
    for c in controls:
        if c.Name not in day_names:
            continue
        x,y=center(c)
        for group in groups:
            gx,gy=center(group[0])
            if c.Name==group[0].Name and abs(x-gx)<=8 and abs(y-gy)<=8:
                group.append(c)
                break
        else:
            groups.append([c])
    cells=[choose_control(group) for group in groups]
    cells.sort(key=lambda c:(center(c)[1],center(c)[0]))
    rows=[]
    for c in cells:
        if not rows or abs(center(c)[1]-rows[-1][0])>8:
            rows.append((center(c)[1],[]))
        rows[-1][1].append(c)
    cells=[c for _,row in rows for c in sorted(row,key=lambda c:center(c)[0])]
    length=calendar.monthrange(year,month)[1]
    starts=[i for i,c in enumerate(cells) if c.Name=="1" and i+length<=len(cells)
            and all(cells[i+j].Name==str(j+1) for j in range(length))]
    if len(starts)!=1:
        raise RuntimeError(f"{platform}日历未确认唯一的当月完整日期序列，未选择日期。")
    month_cells=cells[starts[0]:starts[0]+length]
    # 日期文字和外框顶边不同，行列按中心坐标识别。
    columns=[]
    for c in sorted(month_cells,key=lambda c:center(c)[0]):
        x=center(c)[0]
        if not columns or abs(x-columns[-1][0])>8:
            columns.append((x,[]))
        columns[-1][1].append(c)
    if len(columns)!=7:
        raise RuntimeError(f"{platform}日历当月日期未形成七个独立列，未选择日期。")
    xs=[median(center(c)[0] for c in col) for _,col in columns]
    steps=[b-a for a,b in zip(xs,xs[1:])]
    pitch=median(steps)
    if pitch<16 or any(abs(step-pitch)>max(8,pitch*0.2) for step in steps):
        raise RuntimeError(f"{platform}日历日期列间距不一致，未选择日期。")
    month_rows=[]
    positions=[]
    for c in month_cells:
        x,y=center(c)
        if not month_rows or abs(y-month_rows[-1])>8:
            month_rows.append(y)
        column=min(range(7),key=lambda i:abs(xs[i]-x))
        positions.append((len(month_rows)-1)*7+column)
    if any(position-positions[0]!=i for i,position in enumerate(positions)):
        raise RuntimeError(f"{platform}日历日期排列不符合每周七天，未选择日期。")
    row_steps=[b-a for a,b in zip(month_rows,month_rows[1:])]
    row_pitch=median(row_steps)
    if row_pitch<16 or any(abs(step-row_pitch)>max(8,row_pitch*0.2) for step in row_steps):
        raise RuntimeError(f"{platform}日历日期行间距不一致，未选择日期。")
    return month_cells[day-1]


def wait_hour_readback(draft, read_value, selected, description, timeout=5):
    deadline = draft.now()+timeout
    value = None
    while draft.now()<deadline:
        draft.check()
        value = read_value()
        if value:
            try:
                actual = datetime.strptime(value.strip(),"%Y-%m-%d %H:%M").replace(tzinfo=selected.tzinfo)
            except ValueError:
                actual = None
            if actual and (actual.date(),actual.hour)==(selected.date(),selected.hour):
                return actual
        time.sleep(0.15)
    raise RuntimeError(f"{description}日期/小时回读不一致：目标{selected:%Y-%m-%d %H}点，实际{value!r}，未发布。")
