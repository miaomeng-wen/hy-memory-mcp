"""会话来源解析适配：Codex / ZCode / DSH。

只负责「读文件 -> 标准轮次」，不接触记忆库，便于离线测试。
每个适配器返回 ParsedFile(session_id, turns, ...)，其中 RawTurn.status 取：

- ``complete``   轮次已结束且拿到了最终助手回答，可以写入
- ``incomplete`` 轮次未结束（回答还在追加 / 没有完成事件 / 模型报错），不写入，
                 状态保留为 pending，下次扫描继续判断
- ``skipped``    明确不导入（环境注入、空消息、纯进度播报、含秘密），记录原因

轮次键（turn_key）必须稳定：同一轮在文件追加后仍得到同一个键，
这样「已成功」的轮次不会被重复提交。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("session-sources")

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# ---------------------------------------------------------------- 通用限制

# 单条消息最大字符数，超长截断（避免 embedding 报错和浪费 token）
MAX_MESSAGE_CHARS = 4000
# 助手回答没有明确完成事件时，文件静默多久后可视为回答已定型
QUIET_WINDOW_SEC = int(os.getenv("HYIMPORT_QUIET_WINDOW_SEC", "120"))

# ---------------------------------------------------------------- 脱敏规则

# 命中即整体丢弃该消息（而不是打码）——宁可少存，不存敏感内容。
# 必须在截断前对原文执行，见 has_secret 调用点。
SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),          # OpenAI / 千问 / Buddy Switch
    re.compile(r"\bsk-[a-z]+-[A-Za-z0-9_\-]{16,}"),    # sk-wb- / sk-qoder-
    re.compile(r"\bBearer\s+[A-Za-z0-9_\-\.]{20,}"),
    re.compile(r"(?i)\b(password|passwd|pwd|secret|token)\s*[:=]\s*\S{8,}"),
    re.compile(r"(?i)\bapi[_-]?key\s*[:=]\s*\S{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]


def has_secret(text: str) -> bool:
    return any(p.search(text) for p in SECRET_PATTERNS)


def redact_preview(text: str, limit: int = 60) -> str:
    one = " ".join(text.split())
    return one[:limit]


def truncate(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n\n…（已截断）"


def digest(*parts: str) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update((p or "").encode("utf-8", "replace"))
        h.update(b"\x1f")
    return h.hexdigest()[:20]


# ---------------------------------------------------------------- 环境注入过滤

# 客户端会把系统提示、审批上下文、环境信息塞进 role=user 的消息里。
# 这些不是用户说的话，不能进长期记忆（AC-08）。
INJECT_PREFIXES = (
    "# AGENTS.md instructions for ",
    "<environment_context>",
    "<external_codex_apps",
    "<external_codex_apps_open_page>",
    "<turn_aborted>",
    "<send_user_message_question_reply>",
    "<in-app-browser-context>",
    "<user_instructions>",
    "<permissions instructions>",
    "<system-reminder>",
    "<attached_files>",
    "The following is the Codex agent history added sin",
    "The following is the Codex agent history whose request",
    "The following is the Codex agent history",
    # ZCode 在上下文用尽后会把压缩摘要当作 role=user 消息塞回来（18661 字符），
    # 里面全是助手自己写的历史总结，不是用户说的话。
    "This session is being continued from a previous conversation",
    # 同样是 ZCode 注入的「本轮只准输出文本」控制指令。
    "CRITICAL: Respond with TEXT ONLY",
)
# 旧名字保留，避免外部引用失效
CODEX_INJECT_PREFIXES = INJECT_PREFIXES

# 前缀里的标签可能带属性：写法是 `<in-app-browser-context>`，实际出现的是
# `<in-app-browser-context source="ambient-ui-state">`，字面量 startswith 永远不中。
# 这里按「标签名」判定，属性的有无都不影响。
_INJECT_TAG_RE = re.compile(r"^<\s*([A-Za-z][\w\-]*)")
_INJECT_TAG_NAMES = frozenset(
    m.group(1).lower() for m in (_INJECT_TAG_RE.match(p) for p in INJECT_PREFIXES) if m
)


def _is_inject_head(head: str) -> bool:
    """这条 user 消息是不是环境注入（按标签名 + 字面前缀双重判定）。"""
    m = _INJECT_TAG_RE.match(head)
    if m and m.group(1).lower() in _INJECT_TAG_NAMES:
        return True
    return any(head.startswith(p) for p in INJECT_PREFIXES)

# 打断标记：Codex 在 role=developer（多数）或 role=user（少数）的消息里写这一行，
# 表示该轮被主动打断，之后不会再有 final_answer。它既不是用户发言，也不该开出新的一轮。
_TURN_ABORTED_MARK = "<turn_aborted>"

# "# Files mentioned by the user:" / "# Files pasted by the user:" 前缀后面
# 用 "## My request:" 分隔真实请求。
_MY_REQUEST_RE = re.compile(r"^#{1,6}\s*My request:\s*$", re.MULTILINE)

# 用户贴图时，Codex 会把本机临时文件路径塞进正文：
#   <image name=[Image #1] path="C:\Users\...\Temp\codex-clipboard-xxx.png"></image>
# 路径属于本机环境细节，不该进记忆库；但"用户发了张图"这件事本身有意义
# （助手的回答常常就是针对图的），所以替换成占位符而不是整轮丢弃。
_IMAGE_TAG_RE = re.compile(r"<image\b[^>]*>(?:\s*</image>)?", re.IGNORECASE)
_IMAGE_META_RE = re.compile(r"^Image attachment:\s*true\s*$", re.MULTILINE)
_ATTACH_PLACEHOLDER = "[图片]"

# Codex 有时把整段对话历史（带 "[72] assistant:" / "[44] tool exec call:" 这类
# 序号标记）塞进一条用户消息。记忆只应保留用户真正说的那句，历史回放属于
# 环境注入，且会把本机路径和大段助手输出一起带进来。
_REPLAY_MARK_RE = re.compile(r"^\[(\d+)\]\s+(assistant|user|tool exec call|tool \w+)\s*:", re.MULTILINE)


def strip_replay(text: str) -> str:
    """截掉对话历史回放，只留首个回放标记之前的用户原话。"""
    m = _REPLAY_MARK_RE.search(text or "")
    if not m:
        return text or ""
    return text[: m.start()].strip()


def strip_attachments(text: str) -> str:
    """把贴图标签换成占位符，去掉附件元数据行。"""
    out = _IMAGE_TAG_RE.sub(_ATTACH_PLACEHOLDER, text or "")
    out = _IMAGE_META_RE.sub("", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


# DSH 里只有 source.kind=="user" 才是真实输入，其余全是注入。
DSH_USER_KINDS = {"user"}


def clean_user_text(text: str) -> tuple[str, str]:
    """返回 (清洗后文本, 跳过原因)。原因为空串表示可用。"""
    text = (text or "").strip()
    if not text:
        return "", "empty"
    text = strip_replay(text)
    if not text:
        return "", "injection"
    head = text.lstrip()
    if _is_inject_head(head):
        m = _MY_REQUEST_RE.search(head)
        if m:
            return strip_attachments(head[m.end():]), ""
        return "", "injection"
    m = _MY_REQUEST_RE.search(head)
    if m:
        head = head[m.end():]
    head = strip_attachments(head)
    if not head:
        return "", "empty"
    return head, ""


# ---------------------------------------------------------------- 数据结构


@dataclass
class RawTurn:
    """来源无关的一轮对话。"""

    source: str                 # codex / zcode / dsh
    agent_id: str               # 写入记忆库的 agent_id
    session_id: str             # 真实会话 id（不编造）
    turn_key: str               # 稳定轮次键
    user_text: str
    assistant_text: str
    timestamp: datetime | None = None
    status: str = "complete"    # complete / incomplete / skipped
    reason: str = ""
    # 未完成时"还差什么"：
    #   "quiet" —— 只缺一个静默窗口（旧格式没有明确完成事件），窗口到期后结论会变；
    #   "event" —— 必须等明确的完成标记或文件追加，时间过去不会改变解析结果。
    waiting_on: str = ""

    def messages(self) -> list[dict[str, str]]:
        msgs = [{"role": "user", "content": truncate(self.user_text)}]
        if self.assistant_text:
            msgs.append({"role": "assistant", "content": truncate(self.assistant_text)})
        return msgs


@dataclass
class ParsedFile:
    source: str
    path: Path
    session_id: str = ""
    turns: list[RawTurn] = field(default_factory=list)
    bad_lines: int = 0
    tail_partial: bool = False
    error: str = ""

    @property
    def size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    @property
    def mtime(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0


# ---------------------------------------------------------------- 时间处理


def parse_timestamp(value: Any) -> datetime | None:
    """把 ISO8601 字符串或 epoch 秒/毫秒转成带时区的 datetime。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e11:  # 毫秒
            v /= 1000.0
        if v <= 0:
            return None
        return datetime.fromtimestamp(v, tz=timezone.utc)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return None


# ---------------------------------------------------------------- Codex


def _iter_jsonl(path: Path) -> tuple[Iterable[dict[str, Any]], int, bool]:
    """逐行读 JSONL；返回 (对象迭代器, 坏行数, 尾行是否不完整)。

    尾行不完整（例如进程被杀）时把这一行当成坏行跳过，不把它当作成功：
    文件下次变化仍会重新解析，未引入的轮次不会丢（AC-07）。
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"读取失败: {exc}") from exc

    lines = raw.split("\n")
    ends_with_newline = raw.endswith("\n")
    if ends_with_newline:
        lines = lines[:-1]

    objs: list[dict[str, Any]] = []
    bad = 0
    tail_partial = False
    last_idx = len(lines) - 1
    for i, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            bad += 1
            if i == last_idx and not ends_with_newline:
                tail_partial = True
            continue
        if isinstance(obj, dict):
            objs.append(obj)
    return objs, bad, tail_partial


def parse_codex(path: Path, quiet_window: int = QUIET_WINDOW_SEC) -> ParsedFile:
    """解析 Codex rollout JSONL。

    完成信号优先级：
    1. ``event_msg`` 的 ``item_completed`` 中 item.type=="AgentMessage"
       且 item.phase=="final_answer"；或
    2. ``response_item.payload.phase == "final_answer"`` 且 status=="completed"；或
    3. 文件里完全没有 phase 信息（旧格式）时，用静默窗口兜底。

    ``phase == "commentary"`` 的助手消息是中间播报（纯进度），不写入。

    另外两种"这轮已经结束、但不会再有任何输出"的终态信号：

    * ``<turn_aborted>`` —— 用户或系统主动打断。它出现在 ``response_item`` 里，
      但 role 是 ``developer``（多数）或 ``user``（少数），旧代码按 role 过滤时
      会把它们整条丢掉，导致被打断的轮次永远停在 pending。
    * 轮次已被下一条用户消息"关闭"（桶不再增长）且只剩 commentary —— 模型已经
      开始下一轮了，上一轮不可能再补 final_answer。
    """
    objs, bad, tail_partial = _iter_jsonl(path)
    result = ParsedFile(source="codex", path=path, bad_lines=bad, tail_partial=tail_partial)

    session_id = ""
    final_ids: set[str] = set()
    # event 里的 final_answer 文本：有些 final_answer 只以 event_msg 的形式交付
    # （``delivery=async`` 的 request_user_input 之类，item.id 是 call_xxx，
    # response_item 里没有对应的 message），旧代码只认 response_item 的 id，
    # 这些回答会被整条丢弃、轮次永远 pending。
    # 归属方式：事件顺序与消息顺序一致（实测 1318 个 final 事件里 1208 个都落在
    # 下一轮用户消息之前），所以记到「事件到达时正打开的那个桶」上。
    event_final_by_bucket: dict[int, str] = {}
    has_phase_info = False
    user_msgs: list[tuple[str, str, Any]] = []           # (msg_id, text, ts)
    buckets: list[list[tuple[str, str, Any, str]]] = []  # 每轮的用户消息及其后续助手消息
    aborted_buckets: set[int] = set()                    # 桶内出现过 <turn_aborted> 的下标

    for obj in objs:
        otype = obj.get("type")
        ts = obj.get("timestamp")

        if otype == "session_meta":
            payload = obj.get("payload") or {}
            session_id = session_id or str(payload.get("session_id") or "")
            continue

        if otype == "event_msg":
            payload = obj.get("payload") or {}
            if payload.get("type") != "item_completed":
                continue
            item = payload.get("item") or {}
            if item.get("type") == "AgentMessage":
                phase = item.get("phase")
                if phase is not None:
                    has_phase_info = True
                if phase == "final_answer" and item.get("id"):
                    iid = str(item["id"])
                    final_ids.add(iid)
                    text = _codex_text(item.get("content") or [])
                    if text and buckets:
                        # 同一桶可能有多个 event；只保留第一个非空文本，避免把
                        # 后续轮次的答案拼到前一轮上。
                        event_final_by_bucket.setdefault(len(buckets) - 1, text)
            continue

        if otype != "response_item":
            continue
        payload = obj.get("payload") or {}
        if payload.get("type") != "message":
            continue
        role = payload.get("role")

        # 打断标记在 role=developer / role=user 的消息里，必须先于下面的
        # role 过滤处理，否则这条信号看不到。
        if role not in ("user", "assistant"):
            raw_text = _codex_text(payload.get("content") or [])
            if _TURN_ABORTED_MARK in raw_text and buckets:
                aborted_buckets.add(len(buckets) - 1)
            continue

        text = _codex_text(payload.get("content") or [])
        if not text:
            continue
        if role == "user" and _TURN_ABORTED_MARK in text:
            # 少数打断标记走 role=user。它不是用户发言，不能开新桶，否则会多出
            # 一轮永远没有回答的 pending。
            if buckets:
                aborted_buckets.add(len(buckets) - 1)
            continue
        mid = str(payload.get("id") or "")
        phase = payload.get("phase")
        if role == "user":
            user_msgs.append((mid, text, ts))
            buckets.append([])
            continue
        if phase is not None:
            has_phase_info = True
        if phase == "final_answer" and payload.get("status") in (None, "completed") and mid:
            final_ids.add(mid)
        if buckets:  # 第一个用户消息之前的助手消息不属于任何一轮
            buckets[-1].append((mid, text, ts, str(phase or "")))

    if not session_id:
        session_id = path.stem  # 只在完全没有 session_meta 时退回文件名
    result.session_id = session_id

    file_age = None
    try:
        file_age = datetime.now().timestamp() - path.stat().st_mtime
    except OSError:
        file_age = None

    last_index = len(user_msgs) - 1
    for i, (uid, utext, uts) in enumerate(user_msgs):
        bucket = buckets[i] if i < len(buckets) else []
        finals = [t for (mid, t, _ts, _p) in bucket if mid and mid in final_ids]
        waiting_on = "event"
        if finals:
            assistant = "\n".join(finals)
            complete = True
        elif i in event_final_by_bucket:
            # 这轮的 final_answer 只以 event_msg 交付（item.id 是 call_xxx，桶里
            # 没有对应的 response_item message）。旧代码只认同一个 id，这类回答
            # 会被整条丢弃、轮次永远 pending。
            assistant = event_final_by_bucket[i]
            complete = True
        elif has_phase_info:
            assistant = "\n".join(t for (mid, t, _ts, ph) in bucket if ph == "final_answer")
            complete = bool(assistant)
        else:
            assistant = "\n".join(t for (_mid, t, _ts, _p) in bucket)
            # 旧格式没有明确的完成事件，最后一轮的结论**会随文件变老而改变**，
            # 所以要标记 waiting_on="quiet"，让导入器在窗口到期后即使文件指纹
            # 没变也重新解析一次（问题 B）。
            fresh_last = i == last_index and file_age is not None and file_age < quiet_window
            if fresh_last:
                complete = False
                waiting_on = "quiet"
            elif assistant:
                complete = True
            else:
                # 窗口已过还完全没有助手文本 = 这轮就是没有回答。
                # 交给 _make_turn 归为 skipped/no_answer（不能靠时间把它当完成写进去）。
                complete = True

        if not complete and i != last_index:
            # 桶已经被下一条用户消息关闭：模型已经开了下一轮，这一轮不可能再补
            # final_answer。只剩 commentary（进度播报）或什么都没有，都属于「没有
            # 可用回答」，交给 _make_turn 归为 skipped/no_answer —— 不能永远挂
            # 在 pending 里（问题 D：85 条 pending 中 79 条是这种）。
            assistant = ""
            complete = True
        elif not complete and i in aborted_buckets:
            # <turn_aborted> 是这个桶的终态：用户/系统主动打断，之后不会再有输出。
            # 没有 final_answer 就不该写入，明确记为 skipped/turn_aborted。
            skip_reason = "turn_aborted"
            result.turns.append(
                _make_turn(
                    source="codex",
                    agent_id="codex",
                    session_id=session_id,
                    turn_key=digest("codex", session_id, uid) if uid else digest(
                        "codex", session_id, str(i),
                        hashlib.sha1(utext.encode("utf-8", "replace")).hexdigest(),
                    ),
                    user_text=utext,
                    assistant_text="",
                    timestamp=parse_timestamp(uts),
                    complete=True,
                    incomplete_reason="answer_pending",
                    skip_reason=skip_reason,
                )
            )
            continue

        key = digest("codex", session_id, uid) if uid else digest(
            "codex", session_id, str(i), hashlib.sha1(utext.encode("utf-8", "replace")).hexdigest()
        )
        result.turns.append(
            _make_turn(
                source="codex",
                agent_id="codex",
                session_id=session_id,
                turn_key=key,
                user_text=utext,
                assistant_text=assistant,
                timestamp=parse_timestamp(uts),
                complete=complete,
                incomplete_reason="answer_pending",
                waiting_on=waiting_on,
            )
        )
    return result


def _codex_text(content: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") in ("input_text", "output_text", "text"):
            t = block.get("text")
            if isinstance(t, str) and t.strip():
                parts.append(t)
    return "\n".join(parts).strip()


# ---------------------------------------------------------------- 轮次成形


def _make_turn(
    *,
    source: str,
    agent_id: str,
    session_id: str,
    turn_key: str,
    user_text: str,
    assistant_text: str,
    timestamp: datetime | None,
    complete: bool,
    incomplete_reason: str,
    skip_reason: str = "",
    waiting_on: str = "event",
) -> RawTurn:
    """统一处理注入过滤、空消息、秘密与未完成判定。"""
    cleaned, skip = clean_user_text(user_text)
    if skip:
        return RawTurn(source, agent_id, session_id, turn_key, "", "", timestamp, "skipped", skip)
    if skip_reason:
        # 明确跳过的轮次保留用户文本摘要便于核对，但不写入记忆库；
        # 含秘密的情况连摘要都不留。
        safe = "" if skip_reason == "secret" else cleaned
        return RawTurn(source, agent_id, session_id, turn_key, safe, "", timestamp, "skipped", skip_reason)
    if not complete:
        return RawTurn(
            source, agent_id, session_id, turn_key, cleaned, "", timestamp,
            "incomplete", incomplete_reason, waiting_on,
        )
    assistant = (assistant_text or "").strip()
    if not assistant:
        return RawTurn(source, agent_id, session_id, turn_key, cleaned, "", timestamp, "skipped", "no_answer")
    # 脱敏预检必须在截断之前对原文执行
    if has_secret(cleaned) or has_secret(assistant):
        return RawTurn(source, agent_id, session_id, turn_key, "", "", timestamp, "skipped", "secret")
    return RawTurn(source, agent_id, session_id, turn_key, cleaned, assistant, timestamp, "complete")


# ---------------------------------------------------------------- ZCode


def _zcode_content_text(content: Any) -> str:
    """ZCode 的 content 可能是字符串，也可能是内容块列表。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(b.get("text") or "").strip()
            for b in content
            if isinstance(b, dict) and str(b.get("text") or "").strip()
        )
    return ""


def _msg_fp(msg: dict) -> str:
    return f"{msg.get('role')}\x1f{_zcode_content_text(msg.get('content'))}"


def _align_new_messages(canonical: list[dict], msgs: list[dict]) -> int:
    """求出 ``msgs`` 里属于"新消息"的起点下标。

    ZCode 三种快照形态混用：``full``（完整对话）、``delta``（只有本次新增）、
    ``tail``（末尾窗口，前面被裁掉）。旧实现只在 ``full`` 时按长度切片，其余
    一律把整份当新增，于是 tail 行会把窗口里所有历史用户消息再算一遍。

    这里不猜形态，直接对齐：找最长的 k，使 ``canonical[-k:] == msgs[:k]``，
    即「已知对话的尾巴」与「本行开头」重叠多少，重叠之后的部分才是新增。
    """
    limit = min(len(canonical), len(msgs))
    for k in range(limit, 0, -1):
        if [_msg_fp(x) for x in canonical[-k:]] == [_msg_fp(x) for x in msgs[:k]]:
            return k
    return 0


def _zcode_user_text(new_msgs: list[dict]) -> str:
    """从新增消息里取用户本轮输入。

    逐条过滤注入，而不是像 ``clean_user_text`` 那样只看整体头部：ZCode 会把
    ambient-ui-state / system-reminder 当成**独立的** role=user 消息插在中间，
    拼起来之后注入就不在头部了。
    """
    parts: list[str] = []
    for m in new_msgs:
        if m.get("role") != "user":
            continue
        text = _zcode_content_text(m.get("content")).strip()
        if not text:
            continue
        if _is_inject_head(text.lstrip()):
            continue
        parts.append(text)
    return "\n".join(parts).strip()


def parse_zcode(path: Path) -> ParsedFile:
    """解析 ZCode 的 model-io JSONL。

    每行 = 一次模型调用。同一轮对话里模型每调用一次工具就多一行，只有最后
    ``finishReason == "stop"`` 的那行才带最终答复。因此**不能按行当轮次**：

    1. 逐行求「新增消息」（对齐式，兼容 full / delta / tail 三种快照）；
    2. 按 ``turnId`` 把连续的行并成一组 = 一轮真实对话；
    3. 组内首个带真实用户输入的行为轮次键（与旧实现一致，保住已写入记录）；
    4. 组内存在 stop 行才 complete，否则看是否文件最后一组决定
       incomplete（还在等）还是 skipped/no_answer（已被下一轮关闭）。

    没有 ``turnId`` 的老数据退化为逐行语义。
    """
    objs, bad, tail_partial = _iter_jsonl(path)
    result = ParsedFile(source="zcode", path=path, bad_lines=bad, tail_partial=tail_partial)

    session_id = ""
    for obj in objs:
        sid = str(obj.get("sessionId") or "")
        if sid:
            session_id = sid
            break
    has_real_session = bool(session_id)
    session_id = session_id or path.stem

    canonical: list[dict] = []
    rows: list[dict[str, Any]] = []
    last_ts: datetime | None = None

    for obj in objs:
        request = obj.get("request") or {}
        messages = request.get("messages") or []
        if not isinstance(messages, list):
            messages = []
        k = _align_new_messages(canonical, messages)
        new_msgs = messages[k:]
        if len(messages) >= len(canonical):
            canonical = messages

        ts = parse_timestamp(obj.get("startedAt")) or parse_timestamp(obj.get("completedAt")) or last_ts
        if ts:
            last_ts = ts

        response = obj.get("response") or {}
        rows.append(
            {
                "turn_id": str(obj.get("turnId") or ""),
                "ts": ts,
                "user_text": _zcode_user_text(new_msgs),
                "answer": str(response.get("text") or "").strip(),
                "finish": str(response.get("finishReason") or ""),
                "error": bool(obj.get("error")),
                # 键必须跨次解析稳定：优先 responseId，兜底 traceId/requestId。
                # 全量 284 行里 282 行有 responseId、2 行有 traceId，摘要兜底实际不触发。
                "key": digest(
                    "zcode",
                    session_id,
                    str(response.get("responseId") or "")
                    or str(obj.get("traceId") or "")
                    or str(obj.get("requestId") or "")
                    or digest("zcode", session_id, json.dumps(new_msgs, ensure_ascii=False)[:2000]),
                ),
            }
        )

    # 按 turnId 连续分组；无 turnId 的行各自成组（保持逐行旧语义）。
    groups: list[list[dict[str, Any]]] = []
    for row in rows:
        if row["turn_id"] and groups and groups[-1][0]["turn_id"] == row["turn_id"]:
            groups[-1].append(row)
        else:
            groups.append([row])

    last_group = len(groups) - 1
    for gi, items in enumerate(groups):
        user_rows = [r for r in items if r["user_text"]]
        if not user_rows:
            # 整组都没有真实用户输入（只有注入/工具上下文），不是一轮对话
            continue
        user_text = user_rows[0]["user_text"]
        key = user_rows[0]["key"]
        ts = next((r["ts"] for r in user_rows if r["ts"]), items[0]["ts"])
        grouped = bool(items[0]["turn_id"])

        answer = ""
        for row in items:
            if row["finish"] == "stop" and row["answer"]:
                answer = row["answer"]
        has_error = any(r["error"] for r in items)

        if answer:
            result.turns.append(
                _make_turn(
                    source="zcode",
                    agent_id="zcode",
                    session_id=session_id,
                    turn_key=key,
                    user_text=user_text,
                    assistant_text=answer,
                    timestamp=ts,
                    complete=True,
                    incomplete_reason="answer_pending",
                )
            )
            continue
        if has_error:
            result.turns.append(
                RawTurn("zcode", "zcode", session_id, key, "", "", ts, "incomplete", "provider_error")
            )
            continue
        if grouped and gi != last_group:
            # 后面还有别的轮次 = 这组早被下一轮关闭，不会再有最终答复了
            result.turns.append(
                _make_turn(
                    source="zcode",
                    agent_id="zcode",
                    session_id=session_id,
                    turn_key=key,
                    user_text=user_text,
                    assistant_text="",
                    timestamp=ts,
                    complete=True,
                    incomplete_reason="answer_pending",
                )
            )
            continue
        result.turns.append(
            RawTurn(
                "zcode", "zcode", session_id, key, user_text, "", ts,
                "incomplete", "answer_pending", "event",
            )
        )

    if not has_real_session:
        result.session_id = path.stem
        # 用文件名里的 sess_<uuid> 作为真实会话 id
        m = re.search(r"(sess_[0-9a-f\-]+)", path.stem)
        if m:
            result.session_id = m.group(1)
            for t in result.turns:
                t.session_id = result.session_id
    else:
        result.session_id = session_id
    return result


# ---------------------------------------------------------------- DSH


def _node_exe() -> str | None:
    for env in ("HYIMPORT_NODE", "DSH_NODE_EXE"):
        p = os.getenv(env)
        if p and Path(p).exists():
            return p
    which = shutil.which("node")
    if which:
        return which
    for cand in (
        str(Path.home() / ".dsh" / "dsh-runtimes" / "dsh-primary-runtime" / "dependencies" / "node" / "bin" / "node.exe"),
    ):
        if Path(cand).exists():
            return cand
    return None

_ZSTD_JS = (
    "const fs=require('fs'),z=require('zlib');"
    "process.stdout.write(z.zstdDecompressSync(fs.readFileSync(process.argv[1])));"
)


def _read_zstd_text(path: Path) -> str:
    """Python venv 没有 zstd 支持，用 Node 的 zlib.zstdDecompressSync 解压。"""
    node = _node_exe()
    if not node:
        raise RuntimeError("找不到 node，无法解压 DeepSeek Harness 会话（可用 HYIMPORT_NODE 指定）")
    proc = subprocess.run(
        [node, "-e", _ZSTD_JS, str(path)],
        capture_output=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(f"node 解压失败: {err[-1] if err else proc.returncode}")
    return proc.stdout.decode("utf-8", "replace")


def parse_dsh(path: Path, quiet_window: int = QUIET_WINDOW_SEC) -> ParsedFile:
    """解析 DSH 会话（session.v4.jsonl.zstd）。

    一轮 = turn/start ... turn/end。真实用户输入是 ``user/message`` 里
    ``data.source.kind == "user"`` 的那些，其余是 agent-instructions /
    runtime-context / skill-catalog 等注入。
    最终回答取该轮最后一个含 text 块的 ``assistant/message``。
    """
    text = _read_zstd_text(path)
    result = ParsedFile(source="dsh", path=path, tail_partial=False)
    session_id = path.parent.name or path.stem
    file_age = datetime.now().timestamp() - path.stat().st_mtime

    turns_raw: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for line in text.split("\n"):
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            result.bad_lines += 1
            continue
        etype = obj.get("type")
        data = obj.get("data") or {}
        if etype == "session":
            sid = str(obj.get("id") or "")
            if sid:
                session_id = sid
        elif etype == "turn/start":
            current = {"turn": data.get("turn"), "users": [], "answers": [], "end": None, "start": obj.get("time")}
        elif etype == "user/message" and current is not None:
            source = data.get("source") or {}
            if source.get("kind") in DSH_USER_KINDS:
                content = data.get("content") or []
                buf = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        t = block.get("text")
                        if isinstance(t, str) and t.strip():
                            buf.append(t)
                if buf:
                    current["users"].append(
                        {"id": str(data.get("id") or ""), "text": "\n".join(buf), "time": obj.get("time")}
                    )
        elif etype == "assistant/message" and current is not None:
            content = (data.get("message") or {}).get("content") or []
            buf = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    t = block.get("text")
                    if isinstance(t, str) and t.strip():
                        buf.append(t)
            if buf:
                current["answers"].append({"text": "\n".join(buf), "time": obj.get("time")})
        elif etype == "turn/end" and current is not None:
            current["end"] = data.get("reason") or {}
            turns_raw.append(current)
            current = None

    if current is not None:  # 文件末尾还有未结束的轮次
        turns_raw.append(current)

    for item in turns_raw:
        users = item["users"]
        if not users:
            continue
        answer = item["answers"][-1]["text"] if item["answers"] else ""
        end = item["end"]
        uid = users[0]["id"] or str(item.get("turn"))
        key = digest("dsh", session_id, uid)
        ts = parse_timestamp((users[0].get("time") or 0) / 1000.0) if users[0].get("time") else None

        if end is None:
            if not answer:
                status, reason = "skipped", "turn_abandoned"
            elif file_age > quiet_window:
                # 没等到 turn/end，但文件长期不再变化：按最终回答写入，
                # 并记录原因，避免这轮永远卡在 pending（AC-07）。
                status, reason = "complete", "turn_abandoned"
            else:
                status, reason = "incomplete", "answer_pending"
        elif str(end.get("kind")) == "error":
            # 报错的轮次内容不完整，明确跳过并记录原因，不伪装成成功
            status, reason = "skipped", ("turn_error_partial" if answer else "turn_error")
        else:
            status, reason = "complete", ""

        turn = _make_turn(
            source="dsh",
            agent_id="dsh",
            session_id=session_id,
            turn_key=key,
            user_text="\n".join(u["text"] for u in users),
            assistant_text=answer,
            timestamp=ts,
            complete=status == "complete",
            incomplete_reason=reason,
            skip_reason=reason if status == "skipped" else "",
        )
        if status == "complete" and reason:
            turn.reason = reason
        result.turns.append(turn)
    result.session_id = session_id
    return result


# ---------------------------------------------------------------- 注册表


@dataclass
class SourceSpec:
    name: str
    pattern: str          # rglob 模式
    root: Path
    parser: Any


def default_sources() -> list[SourceSpec]:
    codex_home = Path(os.getenv("CODEX_HOME", str(Path.home() / ".codex")))
    zcode_root = Path(os.getenv("ZCODE_ROLLOUT_DIR", str(Path.home() / ".zcode" / "cli" / "rollout")))
    dsh_root = Path(os.getenv("DSH_SESSIONS_DIR", str(Path.home() / ".dsh" / "sessions")))
    return [
        SourceSpec("codex", "rollout-*.jsonl", codex_home / "sessions", parse_codex),
        SourceSpec("zcode", "model-io-*.jsonl", zcode_root, parse_zcode),
        SourceSpec("dsh", "session.v4.jsonl.zstd", dsh_root, parse_dsh),
    ]
