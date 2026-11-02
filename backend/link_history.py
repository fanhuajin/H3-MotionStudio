"""长期保存已接收的抖音链接；批次清理不影响此记录。"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qs, urlsplit

from .settings import DATA_DIR


def history_key(url: str) -> str:
    """明确作品链接按作品号判重；短链接仍按原文，不联网解析。"""
    text = url.strip()
    try:
        parsed = urlsplit(text)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"douyin.com", "www.douyin.com"}:
            return text
        modal_ids = parse_qs(parsed.query).get("modal_id", [])
        path = re.fullmatch(r"/video/([0-9]{15,22})/?", parsed.path)
        ident = modal_ids[0] if modal_ids else path.group(1) if path else ""
        if re.fullmatch(r"[0-9]{15,22}", ident):
            return f"https://www.douyin.com/video/{ident}"
    except ValueError:
        pass
    return text


def duplicate_details(
    singing: list[str], dance: list[str], known: set[str], items: list[dict] | None = None,
) -> list[dict]:
    """按实际过滤规则返回原输入的位置和原因，供添加窗口逐条展示。"""
    from .batch_worker import url_key

    known_keys = {history_key(url) for url in known}
    current = {
        url_key(str(item.get("url") or ""))
        for item in items or [] if item.get("status") not in {"deleted", "skipped"}
    }
    overlap = {url_key(url) for url in singing if history_key(url) not in known_keys} & {
        url_key(url) for url in dance if history_key(url) not in known_keys
    }
    seen: set[str] = set()
    duplicates = []
    for kind, urls in (("singing", singing), ("dance", dance)):
        for index, url in enumerate(urls, 1):
            key = url_key(url)
            if history_key(url) in known_keys:
                reason = "历史已有"
            elif key in overlap:
                reason = "唱歌与跳舞重复，两边均未添加"
            elif key in current:
                reason = "当前队列已有"
            elif key in seen:
                reason = "本次输入重复，已保留前一条"
            else:
                seen.add(key)
                continue
            duplicates.append({"kind": kind, "inputIndex": index, "url": url, "reason": reason})
    return duplicates


def _existing_urls(data_dir: Path) -> set[str]:
    urls: set[str] = set()
    database = data_dir / "motionstudio.db"
    if database.is_file():
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as source:
            for (raw,) in source.execute("SELECT state_json FROM batches"):
                state = json.loads(raw)
                urls.update(str(item.get("url") or "").strip() for item in state.get("items") or [])
    mirror = data_dir / "douyin-jobs.json"
    if mirror.is_file():
        jobs = json.loads(mirror.read_text(encoding="utf-8"))
        urls.update(str(value.get("job", {}).get("url") or "").strip() for value in jobs.values())
    urls.discard("")
    return urls


@contextmanager
def transaction(data_dir: Path | None = None) -> Iterator[set[str]]:
    """串行判重与登记；调用方只把成功入队的原链接加入集合，异常不登记。"""
    root = data_dir if data_dir is not None else DATA_DIR
    root.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(root / "douyin-link-history.db", timeout=30)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("CREATE TABLE IF NOT EXISTS links (url TEXT PRIMARY KEY)")
        if connection.execute("PRAGMA user_version").fetchone()[0] == 0:
            connection.executemany(
                "INSERT OR IGNORE INTO links VALUES (?)", ((url,) for url in _existing_urls(root))
            )
            connection.execute("PRAGMA user_version = 1")
        original = {row[0] for row in connection.execute("SELECT url FROM links")}
        known = set(original)
        yield known
        connection.executemany(
            "INSERT OR IGNORE INTO links VALUES (?)", ((url,) for url in known - original)
        )
