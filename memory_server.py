"""通用记忆 MCP 服务（Windows 版）。

用 Hy-Memory 2.0 给所有 AI 智能体提供共享长期记忆。

启动：
    run_server.bat
或：
    .venv\\Scripts\\python.exe memory_server.py

MCP 端点：
    http://127.0.0.1:19527/mcp

配置来自同目录的 .env（由 config_ui.py 生成）。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "server.log"

# Windows 控制台默认 cp1252/gbk，日志里的中文会抛 UnicodeEncodeError
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# 尽早加载 .env，让 hy_memory 在初始化前就能读到配置
try:
    from dotenv import load_dotenv

    _env_file = BASE_DIR / ".env"
    if _env_file.exists():
        load_dotenv(_env_file, override=False)
        _LOADED = str(_env_file)
    else:
        _LOADED = None
except Exception:  # noqa: BLE001
    _LOADED = None

# no_proxy 里的 "[::1]" 会让 httpx 抛 InvalidURL，必须先摘掉再建客户端
from hy_env import sanitize_no_proxy  # noqa: E402

_NO_PROXY_FIX = sanitize_no_proxy()

from mcp.server.mcpserver import MCPServer  # noqa: E402

# 写入返回值契约（与 codex_importer 共用同一份判定，避免两处各写一套）
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
import result_contract as rt  # noqa: E402
from result_contract import classify_write_result  # noqa: E402

HOST = os.getenv("MEMORY_MCP_HOST", "127.0.0.1")
PORT = int(os.getenv("MEMORY_MCP_PORT", "19527"))
DEFAULT_USER_ID = os.getenv("MEMORY_DEFAULT_USER_ID", "default")
DEFAULT_AGENT_ID = os.getenv("MEMORY_DEFAULT_AGENT_ID", "default_agent")

# search 命中条目里 SDK 真实会返回的元数据键（src: client.py search 组装）
_EPOCH_KEYS = ("memory_at", "gmt_created", "gmt_modified")

# SDK 支持的全部记忆层（src: hy_memory/models/memory.py:25-56 MemoryLayer）
_ALL_LAYERS = (
    "l0_basic_info",
    "l1_raw",
    "l2_fact",
    "l3_summary",
    "l4_identity",
    "l5_knowledge",
    "l6_schema",
    "l7_intention",
)

# 默认召回层 = SDK 自己的默认召回层 + l1_raw。SDK 默认是
# [l2_fact, l3_summary, l4_identity]（src: reader_base.py:143-148 _VDB_RECALL_LAYERS，
# 注释写明"不含 L1_RAW（原始对话，append-only 不召回）"），而导入器零 LLM 写入的
# 原始对话正好落在 l1_raw，不显式带上就永远搜不到。
# 实测：显式带上 l1_raw 后同一批查询的期望来源 top5 命中从 5/30 升到 24/30。
DEFAULT_SEARCH_LAYERS = ("l1_raw", "l2_fact", "l3_summary", "l4_identity")


def _setup_logging() -> None:
    """控制台 + 滚动文件双输出。

    后台运行时没有控制台，文件日志是唯一排查手段。
    """
    level = os.getenv("MEMORY_LOG_LEVEL", "INFO").upper()
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_FILE,
            maxBytes=5 * 1024 * 1024,  # 5 MB
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError as exc:
        root.warning("无法写日志文件 %s: %s（仅控制台输出）", LOG_FILE, exc)


_setup_logging()
log = logging.getLogger("memory-mcp")
if _NO_PROXY_FIX:
    log.warning("%s（否则 embedding 客户端会构造失败）", _NO_PROXY_FIX)


_client: Any = None


def get_client() -> Any:
    """返回全局 HyMemoryClient 单例。"""
    global _client
    if _client is None:
        from hy_memory import HyMemoryClient

        mode = os.getenv("MEMORY_MODE", "lite")
        log.info("初始化 HyMemoryClient (mode=%s)", mode)
        _client = HyMemoryClient(mode=mode)
        log.info("HyMemoryClient 就绪")
    return _client


def _mask(value: str) -> str:
    if not value:
        return "(未设置)"
    if len(value) <= 10:
        return "***"
    return f"{value[:6]}…{value[-4:]}"


def _iso(value: Any) -> Any:
    """把 SDK 的 epoch 秒/毫秒转成带时区的 ISO8601，转不了就原样返回。"""
    if value is None or isinstance(value, str):
        return value
    try:
        num = float(value)
    except (TypeError, ValueError):
        return value
    if num > 1e11:  # 毫秒
        num /= 1000.0
    try:
        return datetime.fromtimestamp(num, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return value


def _as_text(content: Any) -> str:
    """把 content 归一成字符串。

    支持纯文本、OpenAI 消息列表（[{role, content}]）、内容块列表
    （[{"type": "text", "text": ...}]）。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if "role" in item:
                    role = item.get("role") or "user"
                    parts.append(f"{role}: {_as_text(item.get('content') or '')}")
                elif "text" in item:
                    parts.append(str(item.get("text") or ""))
                elif item.get("content") is not None:
                    parts.append(_as_text(item["content"]))
            elif item is not None:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        if "text" in content:
            return str(content.get("text") or "")
        if "content" in content:
            return _as_text(content["content"])
        return str(content)
    return "" if content is None else str(content)


mcp = MCPServer(
    name="memory",
    title="通用记忆服务",
    description=(
        "基于 Hy-Memory 的跨智能体共享长期记忆。"
        "memory_add 写入对话或结论，memory_search 按语义检索相关记忆。"
    ),
    instructions=(
        "在开始新任务前，先调用 memory_search 查看是否已有相关历史；"
        "在完成一段有价值的对话或得出结论后，调用 memory_add 把它记下来。"
    ),
)


@mcp.tool(
    name="memory_add",
    description=(
        "把内容写入长期记忆。"
        "mode='raw'（默认）用于原始对话片段，交给服务端按当前模式加工；"
        "mode='facts' 用于已经整理好的结论/事实，跳过抽取直接入库。"
        "content 可以是纯文本、OpenAI 格式的消息列表（[{role, content}]），"
        "或字符串列表（facts 模式下批量写入，一批只花一次 LLM 调用）。"
    ),
)
def memory_add(
    content: str | list[dict[str, Any]] | list[str],
    user_id: str | None = None,
    agent_id: str | None = None,
    scene: str | None = None,
    source: str | None = None,
    session_id: str | None = None,
    mode: str = "raw",
) -> dict[str, Any]:
    """写入一条记忆。

    Args:
        content: 要记住的内容。纯文本、[{role, content}] 消息列表，或字符串列表。
        user_id: 记忆归属的用户，默认取配置里的 default。
        agent_id: 写入方智能体标识。
        scene: chat / working / coding，留空自动判断（facts 模式忽略）。
        source: 来源应用标识，如 cursor、codex。
        session_id: 会话标识，留空用 SDK 默认值。
        mode: raw=原始对话（默认，走 add）；facts=已整理事实（走 add_extracted）。
    """
    if content is None or (isinstance(content, str) and not content.strip()):
        return {"ok": False, "error": "content 不能为空"}
    if isinstance(content, list) and not content:
        return {"ok": False, "error": "content 不能为空"}

    mode_norm = (mode or "raw").strip().lower()
    if mode_norm not in ("raw", "facts"):
        return {"ok": False, "error": f"mode 只能是 raw 或 facts，收到 {mode!r}"}

    kwargs: dict[str, Any] = {
        "user_id": user_id or DEFAULT_USER_ID,
        "agent_id": agent_id or DEFAULT_AGENT_ID,
    }
    if source:
        kwargs["source"] = source
    if session_id:
        kwargs["session_id"] = session_id

    # facts 只接收已整理文本，source/scene 之类不参与；raw 保留原有 scene 语义
    payload: Any = content
    if mode_norm == "facts":
        # 字符串列表是「批量事实」，原样交给 add_extracted；消息列表要打平成文本
        if isinstance(content, list) and all(isinstance(item, str) for item in content):
            payload = [item for item in content if item.strip()]
            if not payload:
                return {"ok": False, "error": "facts 模式下 content 解析后为空"}
        else:
            payload = _as_text(content)
            if not payload.strip():
                return {"ok": False, "error": "facts 模式下 content 解析后为空"}
    else:
        # raw 模式下 SDK 只接受 str 或 [{role, content}]；纯字符串列表拼成一段对话
        if isinstance(content, list) and all(isinstance(item, str) for item in content):
            payload = "\n\n".join(item for item in content if item.strip())
            if not payload.strip():
                return {"ok": False, "error": "content 不能为空"}
        if scene:
            kwargs["scene"] = scene

    preview = _as_text(content).strip().replace("\n", " ")[:60]
    log.info(
        "add mode=%s user=%s agent=%s session=%s source=%s len=%d preview=%r",
        mode_norm, kwargs["user_id"], kwargs["agent_id"],
        kwargs.get("session_id", "-"), source or "-", len(preview), preview,
    )

    try:
        client = get_client()
        if mode_norm == "facts":
            result = client.add_extracted(payload, **kwargs)
        else:
            result = client.add(payload, **kwargs)
    except Exception as exc:  # noqa: BLE001
        log.exception("memory_add 抛出异常")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # hy_memory 出错时不抛异常，而是返回 dict。只认 success 会漏掉 None / {} /
    # 字符串 / 无 id 空结果 / 部分生效，统一交给共享契约判定（result_contract）。
    verdict = classify_write_result(result)

    _res = result if isinstance(result, dict) else {}
    # SDK 的诊断信息要透传：调用方靠它判断"部分成功 / 降级 / 未知"，
    # 丢掉这些字段会让上层只剩 success 一个布尔量可用。
    _ops = _res.get("ops_summary") if isinstance(_res.get("ops_summary"), dict) else None
    if verdict.status == rt.PARTIAL:
        # 部分生效：已经有条目落地，整批重放会造重复（request_id 不是幂等键）。
        log.warning(
            "add partial user=%s code=%s unconfirmed=%s err=%s",
            kwargs["user_id"], verdict.error_code,
            verdict.unconfirmed_ids, verdict.error_message[:200],
        )
        return {
            "ok": False,
            "mode": mode_norm,
            "status": rt.PARTIAL,
            "error": verdict.error_message,
            "error_code": verdict.error_code,
            "request_id": verdict.request_id,
            "memory_ids": verdict.memory_ids,
            "unconfirmed_ids": verdict.unconfirmed_ids,
            "ops_summary": _ops,
            "retry_hint": "只补写 unconfirmed_ids，不要重放整批",
        }

    if verdict.status in (rt.FAILED, rt.UNKNOWN):
        log.warning(
            "add %s user=%s code=%s reason=%s err=%s",
            verdict.status, kwargs["user_id"], verdict.error_code,
            verdict.reason, verdict.error_message[:200],
        )
        return {
            "ok": False,
            "mode": mode_norm,
            "status": verdict.status,
            "error": verdict.error_message or "写入失败（未知原因）",
            "error_code": verdict.error_code,
            "request_id": verdict.request_id,
            # 已确认落地的 id 仍然如实给（失败也可能有一部分成功）
            "memory_ids": verdict.memory_ids or None,
            "ops_summary": _ops,
            "retry_safe": verdict.status == rt.FAILED,
        }

    log.info(
        "add ok mode=%s user=%s status=%s scene=%s",
        mode_norm, kwargs["user_id"], verdict.status, _res.get("scene"),
    )
    return {
        "ok": True,
        "mode": mode_norm,
        "status": verdict.status,
        # noop 是"确实没有新东西可写"，此时没有 memory_id 属正常
        "memory_id": verdict.memory_ids[0] if verdict.memory_ids else None,
        "memory_ids": verdict.memory_ids or None,
        "scene": _res.get("scene"),
        "request_id": verdict.request_id,
        "reason": verdict.reason,
        "ops_summary": _ops,
    }


@mcp.tool(
    name="memory_search",
    description=(
        "按语义检索长期记忆，返回与查询最相关的若干条。"
        "回答用户问题前如果涉及历史偏好、之前做过的决定，应先调用本工具。"
        "默认召回层已包含 l1_raw（导入的原始对话），一般不需要传 layers。"
        "ok=false 且 error_kind='sdk_read_failed' 表示检索链路失败（不是没有记忆）；"
        "ok=true 且 count=0 才是真的没检索到。"
    ),
)
def memory_search(
    query: str,
    user_id: str | None = None,
    limit: int = 10,
    min_score: float = 0.3,
    layers: list[str] | None = None,
) -> dict[str, Any]:
    """检索记忆。

    Args:
        query: 自然语言查询。
        user_id: 检索哪个用户的记忆。
        limit: 最多返回条数。
        min_score: 相似度下限，低于该值的丢弃。
        layers: 可选，指定召回哪些记忆层（如 ["l1_raw","l2_fact"]）。
            留空用服务端默认层（含 l1_raw）；lite 模式下 SDK 会强制只用 l1_raw。
    """
    if not query or not query.strip():
        return {"ok": False, "error": "query 不能为空"}

    uid = user_id or DEFAULT_USER_ID

    # 层名白名单校验：非法层名直接报错，避免被 SDK 静默忽略后误判"没有记忆"
    wanted_layers: list[str] | None = None
    if layers:
        cleaned = [str(x).strip() for x in layers if str(x).strip()]
        invalid = [x for x in cleaned if x not in _ALL_LAYERS]
        if invalid:
            return {
                "ok": False,
                "error": f"layers 含非法层名 {invalid}，可用值: {list(_ALL_LAYERS)}",
                "error_kind": "bad_layers",
            }
        wanted_layers = cleaned
    if wanted_layers is None:
        wanted_layers = list(DEFAULT_SEARCH_LAYERS)

    log.info(
        "search user=%s limit=%d layers=%s query=%r",
        uid, limit, ",".join(wanted_layers) if wanted_layers else "sdk-default", query.strip()[:80],
    )

    try:
        result = get_client().search(
            query=query,
            user_ids=[uid],
            limit=limit,
            min_score=min_score,
            layers=wanted_layers,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("memory_search 抛出异常")
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "error_kind": "wrapper_exception",
        }

    if not isinstance(result, dict):
        return {"ok": False, "error": f"SDK 返回了 {type(result).__name__}", "error_kind": "sdk_unexpected"}

    # SDK 失败分支（client.py:3463-3469）只返回 request_id/memories/elapsed_ms，
    # 不含 "rewrite"；成功分支（client.py:3621-3646）必定带 "rewrite"。
    # 这是 2.0.3 里唯一稳定的判别字段，用它区分"检索失败"与"确实没有结果"。
    if "rewrite" not in result:
        log.warning(
            "search SDK 检索失败 user=%s request_id=%s elapsed_ms=%s",
            uid, result.get("request_id"), result.get("elapsed_ms"),
        )
        return {
            "ok": False,
            "error": "检索链路失败（SDK 未返回成功结果，具体原因未透出）",
            "error_kind": "sdk_read_failed",
            "detail": "SDK 失败分支不返回 error_message，只记录 request_id 便于查日志",
            "request_id": result.get("request_id"),
            "elapsed_ms": result.get("elapsed_ms"),
            "count": 0,
            "memories": [],
        }

    buckets = result.get("memories") or {}
    items: list[dict[str, Any]] = []
    for channel in ("normal", "schema", "proactive"):
        for mem in buckets.get(channel) or []:
            item: dict[str, Any] = {
                "channel": channel,
                "score": round(float(mem.get("score") or 0), 4),
                "content": mem.get("content"),
                "memory_id": mem.get("memory_id") or mem.get("id"),
            }
            # 来源/会话/时间按 SDK 真实返回的元数据填，不臆造
            for key in ("source", "agent_id", "user_id", "layer", "session_id"):
                if mem.get(key):
                    item[key] = mem[key]
            for key in _EPOCH_KEYS:
                if mem.get(key) is not None:
                    item[key] = _iso(mem[key])
            items.append(item)

    items.sort(key=lambda m: m["score"], reverse=True)
    rewrite = result.get("rewrite") or {}
    log.info("search ok user=%s hits=%d elapsed_ms=%s", uid, len(items), result.get("elapsed_ms"))
    return {
        "ok": True,
        "count": len(items),
        "memories": items[:limit],
        "request_id": result.get("request_id"),
        "elapsed_ms": result.get("elapsed_ms"),
        "degraded": False,
        "query_used": rewrite.get("effective_query") if isinstance(rewrite, dict) else None,
    }


# ---------------------------------------------------------------- 导入

_import_lock = threading.Lock()
_import_client: Any = None


def get_import_client() -> Any:
    """导入专用客户端（lite 模式，零 LLM 写入）。

    导入必须发生在**本进程内**：hy_memory 的 chromadb 客户端有进程内 system
    缓存，外部进程写入后本进程 search 看不到，而 refresh（clear_system_cache
    + 重建客户端）会泄漏线程、close 后再重建甚至让进程崩溃（实测 0xC0000005）。
    同进程内 lite 客户端写、pro 客户端读立即可见，这是唯一可靠的做法。

    用 lite 而不是 pro：pro 的 raw 写入要过 EXTRACT（本机模型 30 条失败 21 条、
    单条 26–50 秒），lite 零 LLM、单条约 0.3 秒。
    """
    global _import_client
    if _import_client is None:
        from hy_memory import HyMemoryClient

        log.info("初始化导入客户端 (mode=lite, 数据目录同服务)")
        _import_client = HyMemoryClient(mode="lite")
        log.info("导入客户端就绪")
    return _import_client


@mcp.tool(
    name="memory_import",
    description=(
        "把 Codex / DeepSeek Harness / ZCode 的历史会话增量导入记忆库。"
        "在服务进程内执行（写入对检索立即可见），默认走 lite 零 LLM 路径。"
        "有写入条数/软预算时长/每日上限三道预算：软预算到达后的下一个检查点"
        "不再启动新写入（是调度策略，不是硬时限——已开始的同步写入无法取消，"
        "可能在超过软预算后自然返回，其结果照常记录）。达到任一预算就保存待办退出。"
        "默认增量扫描最近 48 小时变化的文件；backfill=true 才做首次历史回填。"
    ),
)
def memory_import(
    backfill: bool | None = None,
    max_writes: int | None = None,
    max_seconds: int | None = None,
    source: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """在服务进程内跑一次增量导入。

    Args:
        backfill: True=首次历史回填（扫描全部文件），False=只扫最近变化，
            留空由状态文件决定（首次自动回填）。
        max_writes: 本次最多写入条数，留空用配置默认值。
        max_seconds: 本次软预算时长（秒）：开始计时后，下一个可检查的边界
            不再启动新写入；已开始的写入可能超过它。留空用配置默认值。
        source: 只处理某个来源，逗号分隔（codex,dsh,zcode），留空全处理。
        dry_run: True 只统计不写入。
    """
    if not _import_lock.acquire(blocking=False):
        return {"ok": False, "skipped": "已有导入任务在运行（本进程）"}
    try:
        from codex_importer import Importer, InstanceLock, LOCK_FILE

        sources = [s.strip() for s in source.split(",")] if source else None
        try:
            with InstanceLock(LOCK_FILE):
                importer = Importer(
                    client=None if dry_run else get_import_client(),
                    max_writes=max_writes,
                    max_seconds=max_seconds,
                    sources=None,
                )
                stats = importer.sync(dry_run=dry_run, backfill=backfill, sources=sources)
        except RuntimeError as exc:
            log.warning("导入跳过: %s", exc)
            return {"ok": False, "skipped": str(exc)}
        except Exception as exc:  # noqa: BLE001
            log.exception("导入失败")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        summary = stats.summary()
        log.info("导入完成 dry_run=%s %s", dry_run, summary)
        return {"ok": True, "dry_run": dry_run, "stats": summary}
    finally:
        _import_lock.release()


@mcp.tool(
    name="memory_import_status",
    description="查看导入器状态：各来源已导入/失败/跳过/待办轮次数与当日写入预算用量。只读。",
)
def memory_import_status() -> dict[str, Any]:
    """读取导入状态文件，不触发任何导入。"""
    try:
        from codex_importer import STATE_FILE, load_state, RULES_VERSION
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    state = load_state(STATE_FILE)
    turns = state.get("turns") or {}
    by_status: dict[str, int] = {}
    by_source: dict[str, dict[str, int]] = {}
    for rec in turns.values():
        st = rec.get("st") or "unknown"
        by_status[st] = by_status.get(st, 0) + 1
        src = rec.get("source") or "unknown"
        slot = by_source.setdefault(src, {})
        slot[st] = slot.get(st, 0) + 1

    today = datetime.now().strftime("%Y-%m-%d")
    uncertain = [r for r in turns.values() if (r.get("st") or "") == "uncertain"]
    now_ts = time.time()
    return {
        "ok": True,
        "state_file": str(STATE_FILE),
        "rules_version": RULES_VERSION,
        "first_run_done": bool(state.get("first_run_done")),
        "last_sync": state.get("last_sync"),
        "files": len(state.get("files") or {}),
        "turns_total": len(turns),
        "by_status": by_status,
        "by_source": by_source,
        "budget_today": {"date": today, "used": int((state.get("budget") or {}).get(today, 0))},
        "last_run": state.get("last_run"),
        "uncertain": {
            "count": len(uncertain),
            "cooling": sum(1 for r in uncertain if now_ts < float(r.get("retry_after") or 0)),
            "note": "uncertain=写入请求超时，服务端可能已写入也可能没有；冷却期过后会重试。"
                    "没有服务端幂等保证时，重复写入次数不能严格限定（不保证 exactly-once）",
        },
        "pending_note": "pending=助手回答尚未出现完成信号，下次文件变化会重新判断；"
                        "failed=写入失败，会自动重试；"
                        "dropped=卡死的 pending 已收敛（源文件消失、长期不再变动，"
                        "或解析规则升级后该轮次不再产出），源文件日后重新变化时会被重新判定",
    }

@mcp.tool(name="memory_profile", description="读取某个用户的长期画像（偏好、习惯等汇总）。")
def memory_profile(user_id: str | None = None) -> dict[str, Any]:
    """读取用户画像。"""
    try:
        result = get_client().get_user_profile(user_id=user_id or DEFAULT_USER_ID)
    except Exception as exc:  # noqa: BLE001
        log.exception("memory_profile 抛出异常")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    profile = (result or {}).get("profile")
    if not profile:
        return {"ok": True, "profile": None, "note": "该用户暂无画像"}
    return {
        "ok": True,
        "profile": {
            "content": profile.get("content"),
            "basic_info": profile.get("basic_info"),
            "layer": profile.get("layer"),
        },
    }


@mcp.tool(
    name="memory_health",
    description=(
        "检查记忆服务是否正常。默认只做低成本的客户端初始化检查（不证明链路可用）；"
        "deep=true 会额外做一次有界只读探测（列举 + 单条检索），会消耗一次 embedding 请求，"
        "但不会写入任何测试数据。"
    ),
)
def memory_health(deep: bool = False) -> dict[str, Any]:
    """健康检查。

    轻量检查只证明「客户端能初始化」，不证明读写链路可用；
    深度检查用一次只读检索验证 embedding + 向量库链路，不写数据。
    """
    info = {
        "mode": os.getenv("MEMORY_MODE", "lite"),
        "embedder_model": os.getenv("MEMORY_EMBEDDER_MODEL", "(unset)"),
        "embedder_base": os.getenv("MEMORY_EMBEDDER_BASE_URL", "(unset)"),
        "embedding_dims": os.getenv("MEMORY_EMBEDDING_DIMS", "(unset)"),
        "embedder_key": _mask(os.getenv("MEMORY_EMBEDDER_API_KEY", "")),
        "llm_model": os.getenv("MEMORY_LLM_MODEL", "(unset)"),
        "llm_base": os.getenv("MEMORY_LLM_BASE_URL", "(unset)"),
        "llm_key": _mask(os.getenv("MEMORY_LLM_API_KEY", "")),
        "data_dir": os.getenv("MEMORY_DATA_DIR", "(unset)"),
        "default_user_id": DEFAULT_USER_ID,
        "env_file": _LOADED or "(未找到 .env)",
    }

    try:
        client = get_client()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "config": info}

    out: dict[str, Any] = {
        "ok": True,
        "checks": {"client_init": True},
        "scope": "轻量检查：仅验证客户端初始化，不代表读写链路可用",
        "config": info,
    }
    if _NO_PROXY_FIX:
        out["no_proxy_fix"] = _NO_PROXY_FIX

    if not deep:
        out["deep_hint"] = "调用 memory_health(deep=true) 可做一次只读链路探测"
        return out

    checks: dict[str, Any] = {"client_init": True}
    errors: list[str] = []
    t0 = datetime.now(timezone.utc)

    try:
        listed = client.list_memories(user_id=DEFAULT_USER_ID, limit=1)
        vdb = (listed or {}).get("vdb") or {}
        checks["list_memories"] = True
        checks["stored_total"] = vdb.get("total")
    except Exception as exc:  # noqa: BLE001
        checks["list_memories"] = False
        errors.append(f"list_memories: {type(exc).__name__}: {exc}")

    try:
        probe = client.search(
            query="记忆服务健康检查",
            user_ids=[DEFAULT_USER_ID],
            limit=1,
            min_score=0.0,
            layers=list(DEFAULT_SEARCH_LAYERS),  # 与 memory_search 用同一组召回层
        )
        # 与 memory_search 同一判据：有 rewrite 键才算检索链路成功
        checks["search_path"] = isinstance(probe, dict) and "rewrite" in probe
        if not checks["search_path"]:
            errors.append("search: SDK 未返回成功结果（检索链路失败）")
    except Exception as exc:  # noqa: BLE001
        checks["search_path"] = False
        errors.append(f"search: {type(exc).__name__}: {exc}")

    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
    out.update(
        {
            "ok": not errors,
            "checks": checks,
            "elapsed_sec": round(elapsed, 3),
            "scope": "深度检查：只读探测 list_memories + search，不写入任何数据",
        }
    )
    if errors:
        out["error"] = "; ".join(errors)[:500]
        out["error_kind"] = "deep_check_failed"
    return out


def main() -> None:
    log.info("启动通用记忆 MCP 服务: http://%s:%s/mcp", HOST, PORT)
    log.info("配置文件: %s", _LOADED or "未找到 .env（将使用环境变量/默认值）")
    mcp.run(transport="streamable-http", host=HOST, port=PORT)


if __name__ == "__main__":
    main()
