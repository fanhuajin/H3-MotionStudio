"""automation.h3_client — 直调 H3 项目后端 API，完成批量制作的自动化操作。

不识别屏幕、不点坐标：直接调 backend/app.py 已暴露的 /api/batches/* 接口。
覆盖：建批次 / 追加任务 / 读取批次状态（待确认检测）/ 确认出片 / 上传候选图 / 打开发布目录。

依赖：httpx（项目 .venv 已装）。
"""
from __future__ import annotations

import time
from typing import Any

import httpx

# 条目状态（backend/batch_worker 语义）
STATUS_PENDING = "pending"          # 排队中
STATUS_AWAITING_REVIEW = "awaiting_review"  # 待确认
STATUS_CONFIRMED = "confirmed"      # 已确认放行
STATUS_RUNNING = "running"          # 出片中
STATUS_REVISING = "revising"        # 重新备料
STATUS_COMPLETED = "completed"      # 已完成
STATUS_FAILED = "failed"            # 失败
STATUS_SKIPPED = "skipped"          # 跳过
STATUS_DELETED = "deleted"          # 已删除

VALID_ITEM_STATUSES = {
    STATUS_PENDING, STATUS_AWAITING_REVIEW, STATUS_CONFIRMED, STATUS_RUNNING,
    STATUS_REVISING, STATUS_COMPLETED, STATUS_FAILED, STATUS_SKIPPED, STATUS_DELETED,
}


class H3ClientError(RuntimeError):
    """后端返回非 2xx 或网络错误时抛出，带具体端点与原因。"""


class H3Client:
    def __init__(self, base_url: str, timeout: float = 30.0, poll: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.poll = poll
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout)

    # ---------- 底层 ----------
    def _url(self, path: str) -> str:
        return self.base_url + path

    def _check(self, resp: httpx.Response, method: str, path: str) -> Any:
        if resp.status_code >= 400:
            detail = _safe_detail(resp)
            raise H3ClientError(
                f"HTTP {resp.status_code}：{detail}　〔{method} {path}〕"
            )
        if resp.status_code == 204:
            return None
        try:
            return resp.json()
        except ValueError:
            raise H3ClientError(
                f"服务返回的不是 JSON（状态 {resp.status_code}）　〔{method} {path}〕"
            )

    def get(self, path: str) -> Any:
        resp = self._http.get(path)
        return self._check(resp, "GET", path)

    def post(self, path: str, json: dict | None = None) -> Any:
        resp = self._http.post(path, json=json or {})
        return self._check(resp, "POST", path)

    def upload_image(self, path: str, image_path: str) -> Any:
        with open(image_path, "rb") as fh:
            files = {"file": (image_path, fh, "image/png")}
            resp = self._http.post(path, files=files)
        return self._check(resp, "POST", path)

    # ---------- 批次 ----------
    def create_batch(
        self,
        singing_urls: list[str] | None = None,
        dance_urls: list[str] | None = None,
        *,
        auto_start: bool = True,
        shutdown_on_complete: bool = False,
        singing_ratio: str | None = None,
        dance_ratio: str | None = None,
    ) -> dict:
        """新建批次并（默认）立即开跑。返回批次 state。"""
        body: dict[str, Any] = {
            "singingUrls": singing_urls or [],
            "danceUrls": dance_urls or [],
            "autoStart": auto_start,
            "shutdownOnComplete": shutdown_on_complete,
        }
        if singing_ratio:
            body["singingRatio"] = singing_ratio
        if dance_ratio:
            body["danceRatio"] = dance_ratio
        return self.post("/api/batches", body)

    def latest_batch(self) -> dict | None:
        """最近一个批次；没有则 None。"""
        return self.get("/api/batches/latest")

    def get_batch(self, batch_id: str) -> dict:
        return self.get(f"/api/batches/{batch_id}")

    def append_items(
        self,
        batch_id: str,
        singing_urls: list[str] | None = None,
        dance_urls: list[str] | None = None,
        *,
        auto_start: bool = True,
    ) -> dict:
        """向已有批次追加任务（多次提供的链接归到同一批次）。"""
        return self.post(f"/api/batches/{batch_id}/items", {
            "singingUrls": singing_urls or [],
            "danceUrls": dance_urls or [],
            "autoStart": auto_start,
        })

    def start_batch(self, batch_id: str) -> dict:
        """让队列开跑（若还没跑）。"""
        return self.post(f"/api/batches/{batch_id}/start")

    # ---------- 条目操作 ----------
    def confirm_item(self, batch_id: str, item_id: str) -> dict:
        """确认出片（仅 awaiting_review 且已有候选图才放行）。"""
        return self.post(f"/api/batches/{batch_id}/items/{item_id}/confirm")

    def skip_item(self, batch_id: str, item_id: str) -> dict:
        return self.post(f"/api/batches/{batch_id}/items/{item_id}/skip")

    def retry_item(self, batch_id: str, item_id: str) -> dict:
        return self.post(f"/api/batches/{batch_id}/items/{item_id}/retry")

    def upload_candidate_image(self, batch_id: str, item_id: str, image_path: str) -> dict:
        """把桌面端生成的成图作为候选人物图上传回去。"""
        return self.upload_image(
            f"/api/batches/{batch_id}/items/{item_id}/image", image_path
        )

    def open_output(self, batch_id: str, item_id: str) -> dict:
        return self.post(f"/api/batches/{batch_id}/items/{item_id}/open-output")

    # ---------- 状态查询辅助 ----------
    def items(self, batch_id: str) -> list[dict]:
        state = self.get_batch(batch_id)
        return state.get("items") or []

    def items_by_status(self, batch_id: str, statuses: set[str]) -> list[dict]:
        return [it for it in self.items(batch_id) if it.get("status") in statuses]

    def awaiting_review_items(self, batch_id: str) -> list[dict]:
        """所有「待确认」条目。"""
        return self.items_by_status(batch_id, {STATUS_AWAITING_REVIEW})

    def wait_for_status(
        self,
        batch_id: str,
        statuses: set[str],
        *,
        item_ids: list[str] | None = None,
        timeout: float = 1800.0,
        on_tick=None,
    ) -> dict:
        """轮询直到任意条目进入指定状态；返回该条目。

        item_ids 为空时匹配批次里任意条目；否则只匹配给定 id。
        超时抛 H3ClientError。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            for it in self.items(batch_id):
                if item_ids and it.get("id") not in item_ids:
                    continue
                if it.get("status") in statuses:
                    return it
            if on_tick:
                on_tick()
            time.sleep(self.poll)
        raise H3ClientError(
            f"等待超时（{timeout:.0f}s）：条目未进入 {statuses}"
        )

    def wait_batch_done(self, batch_id: str, timeout: float = 10800.0) -> dict:
        """等待批次所有条目走到终态（completed/skipped/failed/deleted）。"""
        from . import settings as _s
        cfg = _s.load()
        poll = cfg.get("poll_seconds", self.poll)
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.get_batch(batch_id)
            items = state.get("items") or []
            if items and all(
                it.get("status") in {STATUS_COMPLETED, STATUS_SKIPPED,
                                     STATUS_FAILED, STATUS_DELETED}
                for it in items
            ):
                return state
            time.sleep(poll)
        raise H3ClientError(f"批次等待完成超时（{timeout:.0f}s）")


def _safe_detail(resp: httpx.Response) -> str:
    """尽量从 FastAPI 错误响应里取出可读 detail。"""
    try:
        data = resp.json()
        if isinstance(data, dict):
            d = data.get("detail")
            if isinstance(d, str) and d:
                return d
            if isinstance(d, list):  # pydantic 校验错误
                return "; ".join(
                    f"{e.get('loc', '')}: {e.get('msg', '')}" for e in d
                )
    except ValueError:
        pass
    return (resp.text or "")[:200]


# 便捷：单次请求版（不持有连接）
def quick_create(
    singing_urls=None, dance_urls=None, *, base_url="http://127.0.0.1:8111", **kw
) -> dict:
    c = H3Client(base_url)
    try:
        return c.create_batch(singing_urls, dance_urls, **kw)
    finally:
        c._http.close()
