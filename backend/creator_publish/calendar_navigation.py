"""按实际年月导航；每次点击后只读等待，避免动画期间重复切月。"""
import time


def select_calendar_month(draft, selected, read_month, navigation, description,
                          *, timeout=5, max_changes=24):
    """read_month 返回 (年, 月, 定位节点)，navigation 只返回可见按钮。"""
    target = selected.year * 12 + selected.month - 1
    deadline = draft.now() + timeout
    current = None
    while draft.now() < deadline:
        draft.check()
        current = read_month()
        if current is not None:
            break
        time.sleep(0.15)
    if current is None:
        raise RuntimeError(f"{description}年月未就绪，未选择日期或发布。")

    for _ in range(max_changes + 1):
        index = current[0] * 12 + current[1] - 1
        if index == target:
            return current
        if _ == max_changes:
            break
        direction = "next" if index < target else "previous"
        button = navigation(direction)
        if button is None:
            label = "下月" if direction == "next" else "上月"
            raise RuntimeError(f"未确认{description}{label}按钮，未选择日期或发布。")
        draft.click(button)
        expected = index + (1 if direction == "next" else -1)
        deadline = draft.now() + timeout
        observed = None
        while draft.now() < deadline:
            draft.check()
            observed = read_month()
            if observed is not None and observed[0] * 12 + observed[1] - 1 == expected:
                current = observed
                break
            time.sleep(0.15)
        else:
            value = f"{observed[0]}年{observed[1]}月" if observed else "未读取到年月"
            raise RuntimeError(f"{description}切月后未确认更新（实际{value}），未重复点击或发布。")
    raise RuntimeError(f"{description}月份与所选月份距离过大，未选择日期或发布。")
