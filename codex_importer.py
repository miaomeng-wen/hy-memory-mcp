"""会话增量导入器：把 Codex / DSH / ZCode 的历史会话导入 Hy-Memory。

设计要点（对应 AC-01～AC-08、AC-11）：

- 状态按「轮次」记录，不按文件。一条轮次一条记录：
  ``ok`` / ``failed`` / ``skipped`` / ``pending``，各自带规则版本与原因。
  文件里只要还有非 ok 的轮次，下次就会继续处理，不会因为「有一轮成功」
  就把整份文件标成完成。
- 轮次键（turn_key）由来源适配器给出稳定标识（会话 id + 消息 id / uuid），
  文件追加后旧轮次仍得到同一个键，成功的轮次不会被重提。
- 助手回答没有明确完成事件时状态是 ``pending`` 而非失败，下次文件变化继续判断。
- 每轮先查状态再写入；写入成功后立刻更新状态并落盘，崩溃窗口最多重放一轮。
- 单次运行有「最多写入条数」「软预算时长」两道预算：软预算到达后的下一个
  检查点不再**启动**新写入（调度策略）；已开始的同步写入无法取消，可能在
  超过软预算后才自然返回，其结果仍会被正常记录。
- 同一份状态库同一时间只允许一个实例（文件锁），避免计划任务重叠时重复写入。

用法::

    from codex_importer import Importer
    Importer().sync()          # 增量导入，带预算

命令行::

    python codex_importer.py [--dry-run] [--backfill] [--source codex,dsh,zcode]
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from session_sources import (
    MAX_MESSAGE_CHARS,
    ParsedFile,
    QUIET_WINDOW_SEC,
    RawTurn,
    SourceSpec,
    default_sources,
    digest,
    has_secret,
    parse_timestamp,
    redact_preview,
    truncate,
)

log = logging.getLogger("importer")

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "data" / "import_state_v2.json"
LOCK_FILE = BASE_DIR / "data" / "import.lock"

# 写入返回值契约：与 memory_server.py 共用同一份判定（见 result_contract.py）
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
import result_contract as rt  # noqa: E402
from result_contract import classify_write_result  # noqa: E402

# 规则版本：解析、过滤、写入规则发生不兼容变化时递增，
# 旧版本记录的轮次会被重新评估（skipped 记录会带上它）。
RULES_VERSION = 3

# 单次运行预算
MAX_WRITES_PER_RUN = int(os.getenv("HYIMPORT_MAX_WRITES", "50"))
MAX_SECONDS_PER_RUN = int(os.getenv("HYIMPORT_MAX_SECONDS", "120"))
DAILY_WRITE_LIMIT = int(os.getenv("HYIMPORT_DAILY_LIMIT", "500"))
# 日常增量扫描窗口（小时）；首次回填不受此限制
INCREMENTAL_LOOKBACK_HOURS = int(os.getenv("HYIMPORT_LOOKBACK_HOURS", "48"))
# 连续失败多少轮后停止本批
MAX_CONSECUTIVE_FAILURES = 3

# ---------------------------------------------------------------- 写入超时（软预算）
#
# hy_memory 的 embedding 请求超时与重试来自 config.py:285-296，读的是下面三个
# 环境变量（默认 15s / 2 次 / 0.3s）。它同时在 core/embed_service.py:346-376
# 把 AsyncOpenAI 的 max_retries 显式设为 0，所以没有嵌套重试放大，一次 add
# 涉及的 HTTP 等待时间量级可以用下式**估算**：
#
#     timeout × (max_retries + 1) + retry_delay × max_retries
#
# 默认值算出来 = 15 × 3 + 0.3 × 2 = 45.6 秒。
#
# ⚠️ 这是**估算值，不是严格上界**。HTTPX 的 read timeout 限制的是"等待下一块
# 数据"的时间，不是整个请求的总时长（https://www.python-httpx.org/advanced/timeouts/）；
# 服务端持续缓慢分块返回、请求排队、本地处理都可能让单次写入超过该估算。
# 它唯一的用途是**准入预留**：批次软预算（max_seconds）装不下这个估算时，
# 不再启动新的写入（调度策略，不是硬保证）。已开始的同步写入无法取消，
# 可能超出软预算后自然返回。
_EMBED_TIMEOUT = int(os.getenv("MEMORY_EMBEDDER_TIMEOUT", "15"))
_EMBED_RETRIES = max(0, int(os.getenv("MEMORY_EMBEDDER_MAX_RETRIES", "2")))
_EMBED_RETRY_DELAY = float(os.getenv("MEMORY_EMBEDDER_RETRY_DELAY", "0.3"))
# 单轮写入的估算等待（准入预留用）。旧名 WRITE_WAIT_BOUND_SEC 保留为 deprecated 别名，
# 值相同；注意它从来不是、也不是现在的"严格等待上界"。
WRITE_ADMISSION_ESTIMATE_SEC = (
    _EMBED_TIMEOUT * (_EMBED_RETRIES + 1) + _EMBED_RETRY_DELAY * _EMBED_RETRIES
)
WRITE_WAIT_BOUND_SEC = WRITE_ADMISSION_ESTIMATE_SEC  # deprecated 别名，勿在新代码使用

# 参考阈值（仅日志提示用）：估算等待超过它时打一条 warning，提醒配置可能失衡。
# 它不参与任何硬性判断——本导入器不宣称任何"严格总时限"。
WRITE_TIMEOUT_SEC = int(os.getenv("HYIMPORT_WRITE_TIMEOUT", "60"))
# 结果不确定（超时）后的冷却时间：服务端可能已经写进去了，不能立刻重投同一轮
UNCERTAIN_COOLDOWN_SEC = int(os.getenv("HYIMPORT_UNCERTAIN_COOLDOWN", "600"))

# 轮次的终态：走到这些状态就不再占用待办、不再参与重扫。
TERMINAL_TURN_STATES = ("ok", "skipped", "dropped")

# 卡住的 pending 收敛（见 Importer._reap_stale_pending）。
# pending 只在文件变化时才会被重新判定，于是有两种永远推进不了的情形：
#   1. 源文件被删除/轮转 —— 解析器再也看不到它，pending 无限期挂着（确定性）；
#   2. 文件还在但长期不再追加 —— "等明确完成事件"的 pending 等不到事件（概率性）。
# 前者立即收敛；后者给一个足够长的宽限期，避免把"暂停几天后又续写"的会话误判成废弃。
PENDING_TTL_HOURS = int(os.getenv("HYIMPORT_PENDING_TTL_HOURS", "168"))
# 源文件从"读不到"到判定为"已消失"的宽限期：避免撞上写入方"先删后写/重命名"的瞬时状态，
# 也避免把一次临时的权限/占用错误当成文件消失。
PENDING_GONE_GRACE_SEC = int(os.getenv("HYIMPORT_PENDING_GONE_GRACE", "3600"))

if WRITE_ADMISSION_ESTIMATE_SEC > WRITE_TIMEOUT_SEC:
    log.warning(
        "SDK 写入估算等待 %.1fs 超过参考阈值 %ss（仅提示；该估算是准入预留，"
        "不是严格上界）",
        WRITE_ADMISSION_ESTIMATE_SEC, WRITE_TIMEOUT_SEC,
    )


# 超时类异常：请求可能已经在服务端生效，不能当作"没写进去"直接重投
_TIMEOUT_HINTS = ("timeout", "timed out", "deadline exceeded")


def _is_timeout_error(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return True
    text = str(exc).lower()
    return any(h in text for h in _TIMEOUT_HINTS)



# ---------------------------------------------------------------- 状态


def file_signature(path: Path) -> str:
    """文件指纹：大小 + mtime，用于快速判断文件是否变化。"""
    try:
        st = path.stat()
    except OSError:
        return ""
    raw = f"{path}|{st.st_size}|{int(st.st_mtime)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def default_state() -> dict[str, Any]:
    return {
        "version": 2,
        "rules_version": RULES_VERSION,
        "files": {},      # path -> {sig, size, mtime, session_id, source, scanned_at}
        "turns": {},      # turn_key -> {st, at, reason, source, session_id, file, preview}
        "budget": {},     # day -> count
        "stats": {"ok": 0, "failed": 0, "skipped": 0, "pending": 0},
        "first_run_done": False,
        "last_sync": None,
    }


def load_state(state_file: Path) -> dict[str, Any]:
    if not state_file.exists():
        return default_state()
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        log.warning("状态文件不可用（%s），按空状态处理: %s", exc, state_file)
        return default_state()
    if not isinstance(state, dict):
        return default_state()
    base = default_state()
    base.update(state)
    for key in ("files", "turns", "budget"):
        if not isinstance(base.get(key), dict):
            base[key] = {}
    return base


def save_state(state_file: Path, state: dict[str, Any]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, state_file)


class InstanceLock:
    """跨进程互斥：同一状态文件同时只允许一个导入实例（AC-11）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: Any = None

    def __enter__(self) -> "InstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - 本项目仅 Windows
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._fh.close()
            self._fh = None
            raise RuntimeError("已有导入实例在运行") from exc
        try:
            self._fh.seek(0)
            self._fh.truncate()
            self._fh.write(f"{os.getpid()} {datetime.now(timezone.utc).isoformat()}\n")
            self._fh.flush()
        except OSError:
            pass
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self._fh.close()
        self._fh = None


# ---------------------------------------------------------------- 统计


@dataclass
class SyncStats:
    scanned: int = 0
    skipped_unchanged: int = 0
    parsed: int = 0
    turns_seen: int = 0
    added: int = 0
    failed: int = 0
    uncertain: int = 0
    skipped: int = 0
    pending: int = 0
    already_done: int = 0
    secret_skipped: int = 0
    # 卡死的 pending 被收敛为终态（dropped）的轮次数：源文件已消失或长期不再变动。
    dropped: int = 0
    budget_hit: str = ""
    errors: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "files_scanned": self.scanned,
            "files_unchanged": self.skipped_unchanged,
            "files_parsed": self.parsed,
            "turns_seen": self.turns_seen,
            "added": self.added,
            "failed": self.failed,
            "uncertain": self.uncertain,
            "skipped": self.skipped,
            "pending": self.pending,
            "already_done": self.already_done,
            "secret_skipped": self.secret_skipped,
            "dropped": self.dropped,
            "budget_hit": self.budget_hit,
            "write_admission_estimate_sec": round(WRITE_ADMISSION_ESTIMATE_SEC, 1),
            # deprecated 别名（= write_admission_estimate_sec）。历史字段，值是估算
            # 等待而非严格上界；保留是为了不破坏已有客户端，新代码请用上面的字段。
            "write_wait_bound_sec": round(WRITE_ADMISSION_ESTIMATE_SEC, 1),
            "errors": self.errors[:8],
        }


# ---------------------------------------------------------------- 导入器


class Importer:
    """按轮次状态增量导入多个来源的会话。"""

    def __init__(
        self,
        client: Any = None,
        state_file: Path | None = None,
        user_id: str | None = None,
        sources: list[SourceSpec] | None = None,
        max_writes: int | None = None,
        max_seconds: int | None = None,
        daily_limit: int | None = None,
    ) -> None:
        self._client = client
        self.state_file = Path(state_file or STATE_FILE)
        self.user_id = user_id or os.getenv("MEMORY_DEFAULT_USER_ID", "default")
        self.sources = sources if sources is not None else default_sources()
        self.max_writes = MAX_WRITES_PER_RUN if max_writes is None else max_writes
        self.max_seconds = MAX_SECONDS_PER_RUN if max_seconds is None else max_seconds
        self.daily_limit = DAILY_WRITE_LIMIT if daily_limit is None else daily_limit

    @property
    def client(self) -> Any:
        if self._client is None:
            from hy_memory import HyMemoryClient

            self._client = HyMemoryClient(mode=os.getenv("MEMORY_MODE", "lite"))
        return self._client

    # ------------------------------------------------------------ 候选文件

    def _candidate_files(self, backfill: bool, open_files: set[str] | None = None) -> list[tuple[SourceSpec, Path]]:
        """挑出本次要解析的文件，按"先待办、后新近"排序。

        增量模式下只取最近 ``INCREMENTAL_LOOKBACK_HOURS`` 小时内变动过的文件，
        但**带待办的文件不受时间窗限制**：一次失败或"回答还没出现"的轮次，
        不管文件多久没动都必须还能被重试，否则待办会随 lookback 过期而永久消失。

        ``open_files`` 必须同时包含两类待办：
          - 已登记的非终态轮次（pending / failed）所在文件；
          - **被预算截断（cut）的文件** —— 截断那一刻后面的轮次还没遍历到、
            根本没登记，只靠轮次反推会漏掉它们，文件一旦超出 lookback 就永久漏导。

        多个来源要**合在一起排序**，不能按来源依次排：Codex 有 130 个文件，
        单单它就能吃满每轮 50 条的写入预算，DSH / ZCode 会永远轮不到
        （实测前两轮调度只导入了 codex，DSH/ZCode 记录数为 0）。
        """
        cutoff = time.time() - INCREMENTAL_LOOKBACK_HOURS * 3600
        pending = open_files or set()
        picked: list[tuple[SourceSpec, Path]] = []
        for spec in self.sources:
            root = Path(spec.root)
            if not root.exists():
                continue
            try:
                files = [p for p in root.rglob(spec.pattern) if p.is_file()]
            except OSError as exc:
                log.warning("扫描失败 %s: %s", root, exc)
                continue
            if not backfill:
                files = [p for p in files if p.stat().st_mtime >= cutoff or str(p) in pending]
            picked.extend((spec, p) for p in files)

        def order(item: tuple[SourceSpec, Path]) -> tuple[int, float]:
            mtime = item[1].stat().st_mtime
            has_todo = str(item[1]) in pending
            # 待办优先；增量取最新在前（近期活动更值得先入库），回填取最早在前（按时间顺序）
            return (0 if has_todo else 1, -mtime if not backfill else mtime)

        picked.sort(key=order)
        return picked

    # ------------------------------------------------------------ 预算

    def _today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _budget_used(self, state: dict[str, Any]) -> int:
        return int(state.get("budget", {}).get(self._today(), 0))

    def _budget_add(self, state: dict[str, Any], n: int = 1) -> None:
        budget = state.setdefault("budget", {})
        day = self._today()
        budget[day] = int(budget.get(day, 0)) + n
        # 只保留最近 14 天
        if len(budget) > 14:
            for key in sorted(budget)[:-14]:
                budget.pop(key, None)

    # ------------------------------------------------------------ 主流程

    def _stale_pending(self, state: dict[str, Any], now_ts: float | None = None) -> list[tuple[str, str]]:
        """扫描"永远推进不了"的 pending 轮次，返回 ``[(turn_key, reason)]``。

        纯读，不改状态；由调用方决定是否落盘（dry_run 只统计）。两种情形：

        ``source_gone``    —— 源文件已经不在了。解析器再也看不到这份文件，
                              这些轮次**确定**不会再被判定，pending 只是永久占位。
        ``stale_no_activity`` —— 文件还在，但已经超过 ``PENDING_TTL_HOURS`` 没有任何
                              追加。它们的 pending 语义是"等一个明确完成事件"，事件
                              永远不来了。

        收敛为终态不是"遗忘"：``dropped`` 不是 ``ok``，若文件日后重新出现或又被追加，
        解析器会重新看到这些轮次并照常判定（解析出的 incomplete 会重新记为 pending）。
        """
        turns_state = state.get("turns") or {}
        now_ts = time.time() if now_ts is None else now_ts
        ttl_sec = max(0, PENDING_TTL_HOURS) * 3600
        stale: list[tuple[str, str]] = []
        for key, rec in turns_state.items():
            if rec.get("st") != "pending":
                continue
            fk = rec.get("fk") or ""
            if not fk:
                stale.append((key, "source_gone"))
                continue
            try:
                last_mtime = Path(fk).stat().st_mtime
            except OSError:
                # 文件读不到。给一段宽限期再判定"已消失"，避免撞上写入方的
                # "先删后写/重命名"或一次临时的占用错误。
                age = now_ts - self._record_at_ts(rec)
                if age >= PENDING_GONE_GRACE_SEC:
                    stale.append((key, "source_gone"))
                continue
            if now_ts - last_mtime >= ttl_sec:
                stale.append((key, "stale_no_activity"))
        return stale

    @staticmethod
    def _record_at_ts(rec: dict[str, Any]) -> float:
        """轮次记录 ``at`` 的 epoch 秒；无法解析时返回 0（= 很老，允许收敛）。"""
        raw = rec.get("at") or ""
        try:
            at = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return 0.0
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return at.timestamp()

    def _reap_stale_pending(self, state: dict[str, Any], dry_run: bool) -> int:
        """把卡死的 pending 收敛为 ``dropped``，返回收敛条数。"""
        stale = self._stale_pending(state)
        if not stale:
            return 0
        by_reason: dict[str, int] = {}
        for key, reason in stale:
            by_reason[reason] = by_reason.get(reason, 0) + 1
            if dry_run:
                continue
            rec = state["turns"][key]
            rec["st"] = "dropped"
            rec["reason"] = reason
            rec["at"] = datetime.now(timezone.utc).isoformat()
        if not dry_run:
            # 源文件已消失的文件级记录一并清掉（还有待办轮次的除外），
            # 否则 files_state 会无限累积早已不存在的路径。
            live_files = {
                r.get("fk") for r in state["turns"].values()
                if r.get("fk") and r.get("st") not in TERMINAL_TURN_STATES
            }
            for gone in {state["turns"][k].get("fk") for k, _ in stale}:
                if gone and gone not in live_files:
                    state.get("files", {}).pop(gone, None)
        detail = "、".join(f"{k}={v}" for k, v in sorted(by_reason.items()))
        log.info(
            "收敛卡死的 pending %s 条（%s）%s",
            len(stale), detail, "（dry_run 预览）" if dry_run else "",
        )
        return len(stale)

    def _reap_orphaned_pending(
        self,
        state: dict[str, Any],
        file_key: str,
        seen_keys: set[str],
        parsed: ParsedFile,
        dry_run: bool,
    ) -> int:
        """把「解析器这次不再产出」的 pending 收敛为 ``dropped``。

        触发场景：解析规则更新后，旧规则写下的轮次键不再出现在新结果里。
        这类记录永远等不到重新判定（它连轮次都不存在了），会一直占着待办位，
        让所在文件每轮都被优先重扫。把 pending 误判成 dropped 的代价很小
        （少数几轮不导入），而漏收敛的代价是永久占用预算。

        ``tail_partial`` 时不动：尾行只是**暂时**没写完，那一轮下一轮还会回来。
        """
        if parsed.tail_partial:
            return 0
        orphans = [
            k for k, r in state["turns"].items()
            if r.get("fk") == file_key and r.get("st") == "pending" and k not in seen_keys
        ]
        if not orphans:
            return 0
        if not dry_run:
            now = datetime.now(timezone.utc).isoformat()
            for k in orphans:
                rec = state["turns"][k]
                rec["st"] = "dropped"
                rec["reason"] = "orphaned_by_reparse"
                rec["at"] = now
        log.info(
            "收敛被解析器淘汰的 pending %s 条（%s）%s",
            len(orphans), Path(file_key).name, "（dry_run 预览）" if dry_run else "",
        )
        return len(orphans)

    def sync(self, dry_run: bool = False, backfill: bool | None = None, sources: list[str] | None = None) -> SyncStats:
        """扫描并增量导入。dry_run 只统计不写入。"""
        stats = SyncStats()
        state = load_state(self.state_file)
        if backfill is None:
            backfill = not bool(state.get("first_run_done"))
        if sources:
            wanted = {s.strip() for s in sources if s.strip()}
            self.sources = [s for s in self.sources if s.name in wanted]

        files_state: dict[str, Any] = state.setdefault("files", {})
        turns_state: dict[str, Any] = state.setdefault("turns", {})
        # 先把"永远推进不了"的 pending 收敛掉，再据此反推待办文件：
        # 死文件的 pending 会一直挂在 open_files 里，让它们所在的路径每轮都被优先选中。
        stats.dropped = self._reap_stale_pending(state, dry_run)
        # 由轮次记录反推两类待办（不依赖文件级 dirty 标志，崩溃在两次落盘之间时标志可能没写）：
        #   open_files  —— 所有非终态轮次所在的文件，用于让它们不受 lookback 时间窗限制
        #   retry_files —— 只有"写入失败"的文件，即使文件没变也要重试（失败可能是临时的）
        # pending（回答还没出现）不在此列：同一份没变过的文件重新解析结果必然相同，
        # 反复重扫只会把每轮预算耗在少数几个文件上，其余文件永远轮不到。
        open_files = {
            r["fk"] for r in turns_state.values()
            if r.get("fk") and r.get("st") not in TERMINAL_TURN_STATES
        }
        # 预算截断（cut）的文件必须并进来：截断时剩余的轮次还没遍历到、因此没有轮次记录，
        # 只靠上面的轮次反推会漏掉它们，文件一旦超出 lookback 窗口就永久漏导（问题 A）。
        open_files |= {key for key, info in files_state.items() if info.get("cut")}
        retry_files = {
            r["fk"] for r in turns_state.values()
            if r.get("fk") and r.get("st") == "failed"
        }
        # 冷却期已过的 uncertain 轮次同样要重试（服务端可能没写进去）；冷却期内的
        # 由轮次循环自己挡掉，不用把它们放进来反复重扫文件。
        now_ts = time.time()
        retry_files |= {
            r["fk"] for r in turns_state.values()
            if r.get("fk") and r.get("st") == "uncertain"
            and now_ts >= float(r.get("retry_after") or 0)
        }
        consecutive_failures = 0
        started = time.monotonic()

        # 今日预算已经用满就直接收工：再进循环只会把 cut 文件重解析一遍、
        # 撞在第一轮上又标回 cut，每 5 分钟空转一次。预算态本身持久化在 state 里。
        candidates: list[tuple[Any, Any]] = []
        if not dry_run and self._budget_used(state) >= self.daily_limit:
            stats.budget_hit = "daily_limit"
            log.info(
                "今日写入已达上限 %s（已用 %s），本次不处理任何文件",
                self.daily_limit, self._budget_used(state),
            )
        else:
            candidates = self._candidate_files(backfill, open_files)

        for spec, path in candidates:
            stats.scanned += 1
            key = str(path)
            sig = file_signature(path)
            known = files_state.get(key) or {}

            # 文件没变 且 没有被预算截断 且 没有待重试的失败轮次 -> 整份跳过。
            # 不用 known["dirty"] 判断：pending（等回答）和 open（统计用）都会把它置 True，
            # 这两种情况重解析同一份没变的文件得不到任何新结果。
            # 例外：
            #   - ``recheck_at``：旧格式的"等静默窗口"pending，文件没变但时间到了，
            #     解析结论会变（incomplete -> complete），必须重新解析一次（问题 B）；
            #   - ``rule_stale``：解析规则版本落后。规则改动会改变同一份文件的结论
            #     （例如 zcode 从「按行」改成「按 turnId 分组」），不重扫就永远不生效。
            if (
                known.get("sig") == sig
                and not known.get("cut")
                and key not in retry_files
                and not (known.get("recheck_at") and time.time() >= float(known["recheck_at"]))
                and known.get("rules_version") == RULES_VERSION
            ):
                stats.skipped_unchanged += 1
                # 整份跳过时也要如实报出"还在冷却期"的 uncertain 轮次，
                # 否则调用方会以为没有待办（实际是待办被冷却挡住了）。
                stats.uncertain += sum(
                    1 for r in turns_state.values()
                    if r.get("fk") == key and r.get("st") == "uncertain"
                    and time.time() < float(r.get("retry_after") or 0)
                )
                continue

            try:
                parsed: ParsedFile = spec.parser(path)
            except Exception as exc:  # noqa: BLE001
                stats.failed += 1
                stats.errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
                log.warning("解析失败 %s: %s", path.name, exc)
                continue

            stats.parsed += 1
            stats.turns_seen += len(parsed.turns)
            dirty = False
            cut = False  # 本文件是否被预算中途截断（截断就必须下次重扫，否则丢轮次）

            # 解析规则改动会让旧轮次键凭空消失（例如 zcode 从「按行」改成「按 turnId 分组」）。
            # 这些键不会再被任何解析结果命中，对应的 pending 必须就地收敛，否则永远卡着。
            stats.dropped += self._reap_orphaned_pending(
                state, key, {t.turn_key for t in parsed.turns}, parsed, dry_run
            )

            for turn in parsed.turns:
                rec = turns_state.get(turn.turn_key) or {}
                # 已经成功写入过的轮次一律不重写。rv（规则版本）只做记录，
                # 不拿它当"重写"条件：重写会产生重复条目，而 SDK 的 request_id
                # 不是幂等键（见 PROGRESS.md 环境陷阱 3）。
                # 规则改进只影响后续新轮次；已写入的旧轮次需要人工决定是否修复。
                if rec.get("st") == "ok":
                    stats.already_done += 1
                    continue
                # 超时（uncertain）的轮次在冷却期内不重投：服务端可能已经写进去了，
                # 立刻重试会造出两条记忆。
                if rec.get("st") == "uncertain":
                    retry_after = float(rec.get("retry_after") or 0)
                    if time.time() < retry_after:
                        stats.uncertain += 1
                        dirty = True
                        continue
                    log.info("uncertain 轮次冷却结束，重试: %s", turn.turn_key)
                if turn.status == "skipped":
                    stats.skipped += 1
                    if turn.reason == "secret":
                        stats.secret_skipped += 1
                    if not dry_run:
                        turns_state[turn.turn_key] = self._record(turn, key, "skipped", turn.reason)
                    continue
                if turn.status == "incomplete":
                    dirty = True
                    stats.pending += 1
                    if not dry_run:
                        prev = turns_state.get(turn.turn_key) or {}
                        if prev.get("st") != "ok":
                            turns_state[turn.turn_key] = self._record(turn, key, "pending", turn.reason)
                    continue

                # status == complete
                if dry_run:
                    stats.added += 1
                    continue

                if stats.added >= self.max_writes:
                    stats.budget_hit = "max_writes"
                    dirty = cut = True
                    break
                if time.monotonic() - started > self.max_seconds:
                    stats.budget_hit = "max_seconds"
                    dirty = cut = True
                    break
                if self._budget_used(state) >= self.daily_limit:
                    stats.budget_hit = "daily_limit"
                    dirty = cut = True
                    break

                # 准入预留：单轮写入的估算等待塞不进剩余软预算就不启动新写入。
                # 这是调度策略而不是硬保证：估算基于请求层 timeout×重试数（见
                # WRITE_ADMISSION_ESTIMATE_SEC），httpx 读超时限制的是"等待下一块
                # 数据"的时间，不是请求总时长，实际单轮可能更久。
                remaining = self.max_seconds - (time.monotonic() - started)
                if WRITE_ADMISSION_ESTIMATE_SEC > remaining:
                    stats.budget_hit = "max_seconds"
                    dirty = cut = True
                    break

                outcome, detail = self._write_turn(turn)
                if outcome == "ok":
                    stats.added += 1
                    consecutive_failures = 0
                    turns_state[turn.turn_key] = self._record(turn, key, "ok", "")
                    state["stats"]["ok"] = int(state["stats"].get("ok", 0)) + 1
                    self._budget_add(state)
                elif outcome == "uncertain":
                    # 服务端可能已经写入。既不标成功，也不立即重投：记 uncertain 并带冷却期，
                    # 冷却过后才允许重试（重复风险写明在 README / PROGRESS）。
                    stats.uncertain += 1
                    stats.errors.append(f"{path.name}: {detail}")
                    dirty = True
                    rec = self._record(turn, key, "uncertain", detail)
                    rec["retry_after"] = time.time() + UNCERTAIN_COOLDOWN_SEC
                    turns_state[turn.turn_key] = rec
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        stats.budget_hit = "consecutive_failures"
                        dirty = cut = True
                        break
                else:
                    stats.failed += 1
                    consecutive_failures += 1
                    dirty = True
                    turns_state[turn.turn_key] = self._record(turn, key, "failed", detail)
                    stats.errors.append(f"{path.name}: {detail}")
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        stats.budget_hit = "consecutive_failures"
                        dirty = cut = True
                        break

                # 每写一条立刻落盘，缩小崩溃窗口
                save_state(self.state_file, state)

            if not dry_run:
                # 文件里还有非终态轮次（pending/failed）就记一笔 open，用于统计与排查。
                # "文件没变是否要重扫"由 cut / retry_files 决定，不看这个标志：
                #   - pending（回答还没出现）：文件没变则解析结果一样，重扫无意义
                #   - 预算中途截断（cut）：剩下的轮次还没写过，文件没变也必须继续
                open_left = cut or any(
                    (turns_state.get(t.turn_key) or {}).get("st") in ("pending", "failed")
                    for t in parsed.turns
                )
                # "等静默窗口"的 pending 需要在一个具体的时刻被重新判定一次；
                # "等明确完成事件"的 pending 只等文件变化，不写 recheck_at（否则每轮都重扫）。
                quiet_turn = next(
                    (
                        t for t in parsed.turns
                        if t.status == "incomplete" and t.waiting_on == "quiet"
                        and (turns_state.get(t.turn_key) or {}).get("st") != "ok"
                    ),
                    None,
                )
                recheck_at = None
                if quiet_turn is not None:
                    try:
                        recheck_at = parsed.mtime + QUIET_WINDOW_SEC
                    except OSError:
                        recheck_at = None
                files_state[key] = {
                    "sig": sig,
                    "size": parsed.size,
                    "mtime": int(parsed.mtime),
                    "session_id": parsed.session_id,
                    "source": spec.name,
                    "bad_lines": parsed.bad_lines,
                    "tail_partial": parsed.tail_partial,
                    "scanned_at": datetime.now(timezone.utc).isoformat(),
                    "dirty": dirty or open_left,
                    "open": open_left,
                    "cut": cut,
                    "recheck_at": recheck_at,
                    "rules_version": RULES_VERSION,
                }
                if open_left:
                    open_files.add(key)
                else:
                    open_files.discard(key)
                save_state(self.state_file, state)

            if stats.budget_hit:
                break

        if not dry_run:
            state["rules_version"] = RULES_VERSION
            if backfill:
                # 回填被预算截断时不能标记完成：一旦 first_run_done=True，后续就走增量，
                # 还没轮到的老文件（mtime 早于 lookback 窗口）会永远扫不到 = 静默漏导。
                state["first_run_done"] = not stats.budget_hit
            # 增量运行不碰这个标志：它只说明「历史回填是否跑完」，增量跑多少次都不能代替
            # 一次完整回填。此前这里无条件写 True，运维手动跑一次 --incremental 就会把
            # 未完成状态洗白，剩下的老文件从此永久漏导。
            state["last_sync"] = datetime.now(timezone.utc).isoformat()
            state["stats"] = {
                "ok": sum(1 for r in state.get("turns", {}).values() if r.get("st") == "ok"),
                "failed": sum(1 for r in state.get("turns", {}).values() if r.get("st") == "failed"),
                "skipped": sum(1 for r in state.get("turns", {}).values() if r.get("st") == "skipped"),
                "pending": sum(1 for r in state.get("turns", {}).values() if r.get("st") == "pending"),
                "dropped": sum(1 for r in state.get("turns", {}).values() if r.get("st") == "dropped"),
                "uncertain": sum(1 for r in state.get("turns", {}).values() if r.get("st") == "uncertain"),
            }
            state["last_run"] = {
                "at": datetime.now(timezone.utc).isoformat(),
                "result": stats.summary(),
                "backfill": backfill,
            }
            save_state(self.state_file, state)
        return stats

    # ------------------------------------------------------------ 写入

    def _record(self, turn: RawTurn, file_key: str, st: str, reason: str) -> dict[str, Any]:
        return {
            "st": st,
            "rv": RULES_VERSION,
            "at": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "source": turn.source,
            "session_id": turn.session_id,
            "fk": file_key,
            "file": Path(file_key).name,
            "preview": redact_preview(turn.user_text),
        }

    def _write_turn(self, turn: RawTurn) -> tuple[str, str]:
        """写一轮，返回 ``(结果, 详情)``。

        结果三态：
          - ``"ok"``        —— SDK 明确返回成功，**且有符合契约的持久化 id**
                               （或可证明的合法 no-op，见 result_contract）；
          - ``"failed"``    —— 明确失败（异常、success=False 且非部分生效），可重试；
          - ``"uncertain"`` —— 结果不确定：超时、**部分生效**（已有条目落地）或
                               返回值不足以判断（None / 非 dict / 缺 success 标志 /
                               success=True 但没有任何 id）。既不能标成功，也不能
                               立刻重投同一轮（request_id 不是幂等键，会造成重复），
                               按冷却期延后重试。

        超时由 SDK 的 httpx 读超时 + 有限重试在**请求层**约束（见
        WRITE_ADMISSION_ESTIMATE_SEC 的估算口径），但那不是对单次写入总时长的
        严格上界，这里也**不做线程级中断**：同步调用无法安全取消，用不可取消
        的后台线程包装同步调用再假装"已经中断"，只会让调用方在请求还在写库时
        重投同一轮。超预算的写入会自然返回，其结果按上面三态正常记录。
        """
        messages = turn.messages()
        if not messages:
            return "ok", "empty"

        request_id = digest("turn", turn.turn_key)
        kwargs: dict[str, Any] = {
            "user_id": self.user_id,
            "agent_id": turn.agent_id,
            "session_id": turn.session_id,
            "source": turn.source,
            "request_id": request_id,
        }
        if turn.timestamp is not None:
            kwargs["memory_at"] = turn.timestamp  # SDK 要求 Optional[datetime]

        started = time.monotonic()
        try:
            result = self.client.add(messages, **kwargs)
        except TypeError as exc:
            # 真实 SDK 签名不匹配属于编码错误，必须暴露而不是静默降级
            return "failed", f"add_type_error: {exc}"
        except Exception as exc:  # noqa: BLE001
            if _is_timeout_error(exc):
                elapsed = time.monotonic() - started
                return "uncertain", (
                    f"add_timeout after {elapsed:.1f}s (估算等待 {WRITE_ADMISSION_ESTIMATE_SEC:.1f}s，"
                    f"非严格上界): {type(exc).__name__}: {str(exc)[:120]}"
                )
            return "failed", f"add_exception: {type(exc).__name__}: {exc}"
        elapsed = time.monotonic() - started

        if isinstance(result, dict):
            if result.get("success") is False:
                code = result.get("error_code") or "unknown"
                msg = str(result.get("error_message") or "")[:160]
                # SDK 明确失败：可以重试；但若是超时类错误码，服务端可能已写入
                if any(h in f"{code} {msg}".lower() for h in _TIMEOUT_HINTS):
                    return "uncertain", f"add_failed_timeout: {code}: {msg}"

        # 统一契约判定：只看 success 会漏掉 None / {} / 字符串 / 无 id 空结果 /
        # 部分生效（见 result_contract）。这里把"能不能整批重投"翻译成三态。
        verdict = classify_write_result(result)
        if verdict.status == rt.CONFIRMED:
            pass  # 有真 id，按成功继续
        elif verdict.status == rt.NOOP:
            # 明确合法"无事可做"（有可识别依据），不算失败也不重复写。
            log.info(
                "导入 %s/%s 无需写入 (%s) %.1fs",
                turn.source, turn.agent_id, verdict.reason, elapsed,
            )
            return "ok", f"noop: {verdict.reason}"
        elif verdict.status == rt.FAILED:
            return "failed", f"add_failed: {verdict.error_code or 'unknown'}: {verdict.error_message[:160]}"
        elif verdict.status == rt.PARTIAL:
            # 已有条目落地：立刻重投会造重复，按 uncertain 走冷却期延后再试。
            return "uncertain", (
                "add_partial_persist: ids="
                f"{verdict.memory_ids[:3]} unconfirmed={verdict.unconfirmed_ids[:3]} "
                f"err={verdict.error_message[:120]}"
            )
        else:  # UNKNOWN：返回值不足以判断是否落地
            return "uncertain", (
                f"add_unknown_result: {verdict.reason} "
                f"type={verdict.result_type} err={verdict.error_message[:120]}"
            )

        log.info(
            "导入 %s/%s session=%s %.1fs preview=%r",
            turn.source, turn.agent_id, (turn.session_id or "")[:24], elapsed,
            redact_preview(turn.user_text),
        )
        return "ok", ""


# ---------------------------------------------------------------- 兼容层


class CodexImporter(Importer):
    """旧名字的薄包装，只导入 Codex，供已有调用方继续使用。"""

    def __init__(self, client: Any = None, sessions_dir: Path | None = None, **kw: Any) -> None:
        super().__init__(client=client, **kw)
        if sessions_dir is not None:
            for spec in self.sources:
                if spec.name == "codex":
                    spec.root = Path(sessions_dir)
            self.sources = [s for s in self.sources if s.name == "codex"]

    @property
    def sessions_dir(self) -> Path:
        for spec in self.sources:
            if spec.name == "codex":
                return Path(spec.root)
        return Path.home() / ".codex" / "sessions"


# ---------------------------------------------------------------- CLI


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        env_file = BASE_DIR / ".env"
        if env_file.exists():
            load_dotenv(env_file, override=False)
            print(f"配置: {env_file}")
    except Exception:  # noqa: BLE001
        pass
    _recompute_wait_bound()


def _recompute_wait_bound() -> None:
    """按当前环境重算写入估算等待（准入预留）。

    模块级常量在 import 时就求值了，而 .env 要到 _load_env() 才加载；不重算的话
    CLI 直跑会拿默认值算估算，和实际 SDK 配置对不上。
    """
    global _EMBED_TIMEOUT, _EMBED_RETRIES, _EMBED_RETRY_DELAY
    global WRITE_ADMISSION_ESTIMATE_SEC, WRITE_WAIT_BOUND_SEC
    global WRITE_TIMEOUT_SEC, UNCERTAIN_COOLDOWN_SEC
    _EMBED_TIMEOUT = int(os.getenv("MEMORY_EMBEDDER_TIMEOUT", "15"))
    _EMBED_RETRIES = max(0, int(os.getenv("MEMORY_EMBEDDER_MAX_RETRIES", "2")))
    _EMBED_RETRY_DELAY = float(os.getenv("MEMORY_EMBEDDER_RETRY_DELAY", "0.3"))
    WRITE_ADMISSION_ESTIMATE_SEC = (
        _EMBED_TIMEOUT * (_EMBED_RETRIES + 1) + _EMBED_RETRY_DELAY * _EMBED_RETRIES
    )
    WRITE_WAIT_BOUND_SEC = WRITE_ADMISSION_ESTIMATE_SEC  # deprecated 别名同步更新
    WRITE_TIMEOUT_SEC = int(os.getenv("HYIMPORT_WRITE_TIMEOUT", "60"))
    UNCERTAIN_COOLDOWN_SEC = int(os.getenv("HYIMPORT_UNCERTAIN_COOLDOWN", "600"))
    if WRITE_ADMISSION_ESTIMATE_SEC > WRITE_TIMEOUT_SEC:
        log.warning(
            "SDK 写入估算等待 %.1fs 超过参考阈值 %ss（仅提示；该估算是准入预留，"
            "不是严格上界）",
            WRITE_ADMISSION_ESTIMATE_SEC, WRITE_TIMEOUT_SEC,
        )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(
        level=os.getenv("HYIMPORT_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    _load_env()

    dry = "--dry-run" in argv
    backfill = True if "--backfill" in argv else (False if "--incremental" in argv else None)
    only: list[str] | None = None
    for i, a in enumerate(argv):
        if a == "--source" and i + 1 < len(argv):
            only = argv[i + 1].split(",")

    importer = Importer()
    if dry:
        stats = importer.sync(dry_run=True, backfill=backfill, sources=only)
    else:
        try:
            with InstanceLock(LOCK_FILE):
                stats = importer.sync(backfill=backfill, sources=only)
        except RuntimeError as exc:
            print(json.dumps({"ok": False, "skipped": str(exc)}, ensure_ascii=False))
            return 2

    print(json.dumps(stats.summary(), ensure_ascii=False, indent=2))
    if stats.errors:
        print("\n错误详情：")
        for e in stats.errors[:10]:
            print(f"  - {e}")
    return 0 if not stats.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
