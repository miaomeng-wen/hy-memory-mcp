"""SDK 写入类调用（add / add_extracted）的返回值契约校验（共享）。

背景：hy_memory 的写入接口**出错时不抛异常**，而是返回 dict。历史代码只看
``success is False``，因此下面这些返回值都会被误当成"已写入"：

- ``None`` / ``{}`` / 字符串等非 dict 值；
- ``success=True`` 但一个持久化 id 都没有（无依据空结果）；
- ``success=False, error_code=409, [PARTIAL_PERSIST]``（部分生效，不能当普通失败整批重投）。

本模块把返回值判成五态，供 MCP 包装层（memory_server.py）与导入器
（codex_importer.py）共用，避免两处各写一套：

============  ==========================================================
状态           含义
============  ==========================================================
confirmed     明确成功，且有符合契约的持久化 id
noop          明确合法"无事可做"，有可识别依据（如 agent_status=noop）
partial       部分生效 / 重试不安全（409、[PARTIAL_PERSIST]、applied=partial）
failed        明确失败（success=False 且非部分生效）
unknown       返回值不足以判断是否落地（None、非 dict、缺 success、空结果）
============  ==========================================================

判定只依赖返回值本身，不做任何 I/O，也不相信 ``request_id`` 的幂等性
（SDK 的 request_id 不是幂等键，见 PROGRESS.md 环境陷阱 3）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

CONFIRMED = "confirmed"
NOOP = "noop"
PARTIAL = "partial"
FAILED = "failed"
UNKNOWN = "unknown"

# 部分生效的判据
_PARTIAL_ERROR_CODE = 409
_PARTIAL_TAG = "[PARTIAL_PERSIST]"


@dataclass
class WriteVerdict:
    """一次写入调用的判定结果。"""

    status: str
    reason: str
    memory_ids: List[str] = field(default_factory=list)
    unconfirmed_ids: List[str] = field(default_factory=list)
    error_code: Any = None
    error_message: str = ""
    request_id: Optional[str] = None
    result_type: str = ""

    @property
    def ok(self) -> bool:
        """只有 confirmed 与 noop 算"调用方可以当成处理完毕"。"""
        return self.status in (CONFIRMED, NOOP)

    @property
    def persisted(self) -> bool:
        """是否可证明至少落了一条。"""
        return bool(self.memory_ids)

    def summary(self) -> Dict[str, Any]:
        """可直接塞进 MCP 返回值的精简视图。"""
        return {
            "status": self.status,
            "reason": self.reason,
            "memory_ids": list(self.memory_ids),
            "error_code": self.error_code,
            "error_message": self.error_message or None,
            "request_id": self.request_id,
        }


def _collect_ids(result: Dict[str, Any]) -> List[str]:
    """收集符合契约的持久化 id（非空字符串），保序去重。"""
    ids: List[str] = []
    single = result.get("memory_id")
    if isinstance(single, str) and single.strip():
        ids.append(single.strip())
    many = result.get("memory_ids")
    if isinstance(many, (list, tuple)):
        for item in many:
            if isinstance(item, str) and item.strip():
                ids.append(item.strip())
    seen = set()
    out: List[str] = []
    for mid in ids:
        if mid not in seen:
            seen.add(mid)
            out.append(mid)
    return out


def _ops_summary(result: Dict[str, Any]) -> Dict[str, Any]:
    """SDK 的 ops 账本：client.add 叫 ops_summary，add_extracted 也是 ops_summary。"""
    ops = result.get("ops_summary")
    if isinstance(ops, dict):
        return ops
    ops = result.get("reconcile_ops")
    if isinstance(ops, dict):
        return ops
    return {}


def _noop_evidence(result: Dict[str, Any]) -> str:
    """返回非空字符串表示"有依据的合法空操作"，否则空串。"""
    if result.get("agent_status") == "noop":
        return "agent_status=noop"
    ops = _ops_summary(result)
    if ops.get("applied") == "noop":
        return "ops_summary.applied=noop"
    # 计划 0 条 op 且确认 0 条：reconcile 明确判定"没有可执行变更"。
    if ops and ops.get("applied") == "all":
        try:
            planned = int(ops.get("planned") or 0)
            confirmed = int(ops.get("confirmed") or 0)
        except (TypeError, ValueError):
            return ""
        if planned == 0 and confirmed == 0:
            return "ops_summary planned=0"
    return ""


def _partial_evidence(result: Dict[str, Any]) -> str:
    ops = _ops_summary(result)
    if ops.get("applied") == "partial":
        return "ops_summary.applied=partial"
    message = str(result.get("error_message") or "")
    if _PARTIAL_TAG in message:
        return "error_message has [PARTIAL_PERSIST]"
    code = result.get("error_code")
    if code == _PARTIAL_ERROR_CODE:
        # SDK 用 409 表达"有东西已经落地，整批重放不安全"（[PARTIAL_PERSIST]、
        # [DYNAMIC_EPISODE_FINALIZE_FAILED]、L1 回滚失败等）。
        return "error_code=409"
    return ""


def _unconfirmed_ids(result: Dict[str, Any]) -> List[str]:
    ops = _ops_summary(result)
    ids = ops.get("unconfirmed_ids")
    if isinstance(ids, (list, tuple)):
        return [str(i) for i in ids if isinstance(i, str) and i]
    return []


def classify_write_result(result: Any) -> WriteVerdict:
    """把 SDK 写入返回值判成五态。永不抛异常。"""
    result_type = type(result).__name__

    if result is None:
        return WriteVerdict(
            status=UNKNOWN,
            reason="none_result",
            result_type=result_type,
            error_message="SDK 返回 None，无法确认是否写入",
        )

    if not isinstance(result, dict):
        return WriteVerdict(
            status=UNKNOWN,
            reason=f"non_dict_result:{result_type}",
            result_type=result_type,
            error_message=f"SDK 返回 {result_type}，不是可判定的 dict 结果",
        )

    request_id = result.get("request_id")
    request_id = request_id if isinstance(request_id, str) and request_id else None
    ids = _collect_ids(result)
    error_code = result.get("error_code")
    error_message = str(result.get("error_message") or "")
    success = result.get("success")

    if not result:
        return WriteVerdict(
            status=UNKNOWN,
            reason="empty_dict_result",
            result_type=result_type,
            request_id=request_id,
            error_message="SDK 返回空 dict，没有任何成功信号",
        )

    if success is False:
        partial = _partial_evidence(result)
        if partial:
            return WriteVerdict(
                status=PARTIAL,
                reason=partial,
                memory_ids=ids,
                unconfirmed_ids=_unconfirmed_ids(result),
                error_code=error_code,
                error_message=error_message or "部分生效（SDK 未给 error_message）",
                request_id=request_id,
                result_type=result_type,
            )
        return WriteVerdict(
            status=FAILED,
            reason="success_false",
            memory_ids=ids,
            error_code=error_code,
            error_message=error_message or "写入失败（SDK 未给 error_message）",
            request_id=request_id,
            result_type=result_type,
        )

    if success is not True:
        # 缺 success 或 success=None：SDK 契约里必有的字段缺失，不能当成功。
        return WriteVerdict(
            status=UNKNOWN,
            reason="missing_success_flag",
            memory_ids=ids,
            error_code=error_code,
            error_message=error_message or "SDK 返回值缺少 success 标志",
            request_id=request_id,
            result_type=result_type,
        )

    # success is True
    if ids:
        return WriteVerdict(
            status=CONFIRMED,
            reason="success_true_with_ids",
            memory_ids=ids,
            error_code=error_code,
            error_message=error_message,
            request_id=request_id,
            result_type=result_type,
        )

    noop = _noop_evidence(result)
    if noop:
        return WriteVerdict(
            status=NOOP,
            reason=noop,
            memory_ids=[],
            error_code=error_code,
            error_message=error_message,
            request_id=request_id,
            result_type=result_type,
        )

    # success=True 但没有任何 id 也没有 noop 依据：无依据空结果。
    partial = _partial_evidence(result)
    if partial:
        return WriteVerdict(
            status=PARTIAL,
            reason=partial,
            memory_ids=[],
            unconfirmed_ids=_unconfirmed_ids(result),
            error_code=error_code,
            error_message=error_message or "部分生效但没有已确认 id",
            request_id=request_id,
            result_type=result_type,
        )
    return WriteVerdict(
        status=UNKNOWN,
        reason="success_true_without_ids",
        memory_ids=[],
        error_code=error_code,
        error_message=error_message or "SDK 报 success 但没有任何持久化 id",
        request_id=request_id,
        result_type=result_type,
    )
