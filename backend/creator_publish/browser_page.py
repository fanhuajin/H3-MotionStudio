"""从 Windows 可访问性控件读取网址，不点击地址栏、不改焦点或剪贴板。"""
from urllib.parse import urlsplit


def valid_url(value):
    parsed=urlsplit(value)
    return parsed.scheme in ("http","https") and bool(parsed.hostname)


def read_browser_url(uia, window, title_hint):
    address=[]
    for control,_ in uia.WalkControl(window,maxDepth=20):
        try:
            if control.IsOffscreen:
                continue
            document=control.ControlTypeName=="DocumentControl" and title_hint in control.Name
            address_bar=control.ControlTypeName=="EditControl" and (
                any(word in control.Name.lower() for word in ("地址", "address", "search or enter web", "搜索或输入"))
                or getattr(control,"AutomationId","").lower() in ("omnibox","addresseditbox"))
            if not (document or address_bar):
                continue
            pattern=control.GetValuePattern()
            value=pattern.Value.strip() if pattern else ""
            if address_bar and value and "://" not in value and "." in value.split("/")[0] and " " not in value:
                value="https://"+value
            if not valid_url(value):
                continue
            if document:
                return value
            address.append(value)
        except Exception:
            continue
    if len(set(address))==1:
        return address[0]
    raise RuntimeError("无法直接读取当前页面网址，请确认浏览器网页可访问性已开启。")
