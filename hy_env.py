"""Hy-Memory 运行时环境修补。

hy_memory 内部用 httpx 建 OpenAI 客户端，httpx 会读 `no_proxy` 环境变量。
某些 Windows 代理客户端把 no_proxy 写成 `localhost,127.0.0.1,::1,[::1]`，
而 httpx 的 URLPattern 解析 `[::1]` 会抛：

    httpx.InvalidURL: Invalid port: ':1]'

后果不是报错退出，而是 embedding 客户端构造失败 → 检索永远返回空、
写入永远失败，表面看像"没有记忆"。这里在创建任何客户端之前把这种
无法解析的条目摘掉，保留其余条目。
"""

from __future__ import annotations

import os

_PROXY_KEYS = ("NO_PROXY", "no_proxy")


def _split(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _broken(entry: str) -> bool:
    """httpx 无法解析带方括号的 IPv6 字面量。"""
    return entry.startswith("[")


def sanitize_no_proxy() -> str:
    """摘掉 no_proxy 里 httpx 解析不了的条目，返回修复说明（无改动返回空串）。"""
    removed: list[str] = []
    for key in _PROXY_KEYS:
        raw = os.environ.get(key)
        if not raw:
            continue
        parts = _split(raw)
        kept = [part for part in parts if not _broken(part)]
        if len(kept) == len(parts):
            continue
        removed.extend(part for part in parts if _broken(part))
        os.environ[key] = ",".join(kept)
    if not removed:
        return ""
    unique = sorted(set(removed))
    return f"已从 no_proxy 移除 httpx 无法解析的条目: {', '.join(unique)}"
