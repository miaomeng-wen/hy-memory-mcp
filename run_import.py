"""后台采集入口：通过 MCP 调用服务进程内的 memory_import，然后退出。

为什么不用 `python codex_importer.py` 直接跑：
  hy_memory 的向量库读快照是**进程内**的。跨进程写入后，常驻服务看不到
  （实测 stale / 重建客户端都拿不到，只有 clear_system_cache + 重建才行，
  而那会导致线程泄漏并在反复 close 后以 0xC0000005 崩进程）。
  所以写入必须发生在 memory_server 进程内，本脚本只负责按周期触发它。

用法：
    .venv\\Scripts\\python.exe run_import.py                 # 默认增量
    .venv\\Scripts\\python.exe run_import.py --backfill      # 首次历史回填
    .venv\\Scripts\\python.exe run_import.py --max-writes 10
    .venv\\Scripts\\python.exe run_import.py --status        # 只看状态，不导入

退出码：0 正常（含"无事可做"）；2 服务未运行；3 导入被跳过（已有实例/预算到）；
4 本轮有写入超时（结果不确定，需人工确认，不重试）；1 出错。

注意区分两层超时：
  - 本脚本 --timeout 是**客户端 HTTP 等待**上限。脚本因它超时退出，只说明脚本
    没等到响应，**不代表服务内的导入已经结束**——服务端会继续把本轮跑完。
  - 服务内导入自己受软预算约束（max_seconds）：软预算到达后的下一个检查点
    不再启动新写入；已开始的同步写入无法取消，可能超过软预算后自然返回。
  - 即使脚本超时退出，后续调度也不会重叠执行：服务进程内互斥锁 + 跨进程
    文件锁会挡住并发的第二轮导入。
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "import_task.log"
URL = os.getenv("MEMORY_MCP_URL", "http://127.0.0.1:19527/mcp")
MAX_WRITES = int(os.getenv("HYIMPORT_MAX_WRITES", "50"))
MAX_SECONDS = int(os.getenv("HYIMPORT_MAX_SECONDS", "120"))


def _log(payload: dict) -> None:
    """计划任务里没有控制台，结果必须自己落文件。"""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"at": datetime.now(timezone.utc).isoformat(), **payload}, ensure_ascii=False)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _clean_no_proxy() -> None:
    """摘掉 httpx 解析不了的 no_proxy 条目（[::1] 之类），否则连不上本机端口。"""
    for key in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(key)
        if not raw:
            continue
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        kept = [p for p in parts if not p.startswith("[")]
        if len(kept) != len(parts):
            os.environ[key] = ",".join(kept)


async def _run(args) -> int:
    _clean_no_proxy()
    import httpx2

    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    try:
        hc = httpx2.AsyncClient(trust_env=False, timeout=float(args.timeout))
        async with hc:
            async with streamable_http_client(URL, http_client=hc) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()

                    async def call(name, **kwargs):
                        res = await session.call_tool(name, kwargs)
                        for block in res.content:
                            text = getattr(block, "text", None)
                            if text:
                                try:
                                    return json.loads(text)
                                except json.JSONDecodeError:
                                    return text
                        return None

                    if args.status:
                        out = await call("memory_import_status")
                        print(json.dumps(out, ensure_ascii=False))
                        _log({"mode": "status", "out": out})
                        return 0 if (isinstance(out, dict) and out.get("ok")) else 1

                    kwargs = {"max_writes": args.max_writes, "max_seconds": args.max_seconds}
                    if args.backfill:
                        kwargs["backfill"] = True
                    if args.incremental:
                        kwargs["backfill"] = False
                    out = await call("memory_import", **kwargs)
                    print(json.dumps(out, ensure_ascii=False))
                    _log({"mode": "import", "args": kwargs, "out": out})
                    if not isinstance(out, dict):
                        return 1
                    if out.get("skipped"):
                        return 3
                    if not out.get("ok"):
                        return 1
                    # 本轮有写入超时（结果不确定）时单独给退出码 4：
                    # 既不算成功也不算失败，便于从任务日志里筛出来人工确认。
                    uncertain = (out.get("stats") or {}).get("uncertain") or 0
                    return 4 if uncertain else 0
    except (ConnectionError, OSError) as exc:
        msg = {"ok": False, "error": f"服务不可用: {exc}"}
        print(json.dumps(msg, ensure_ascii=False))
        _log({"mode": "import", "out": msg})
        return 2
    except Exception as exc:  # noqa: BLE001 - 后台任务需要明确退出码而不是堆栈
        msg = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(msg, ensure_ascii=False))
        _log({"mode": "import", "out": msg})
        return 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="按周期触发服务内导入")
    p.add_argument("--backfill", action="store_true", help="强制历史回填（忽略增量时间窗）")
    p.add_argument("--incremental", action="store_true", help="强制只处理最近变化的文件")
    p.add_argument("--max-writes", type=int, default=MAX_WRITES, help=f"本次最多写多少条（默认 {MAX_WRITES}）")
    p.add_argument("--max-seconds", type=int, default=MAX_SECONDS, help=f"本次软预算秒数：到达后的下一个检查点不再启动新写入（默认 {MAX_SECONDS}）")
    p.add_argument("--timeout", type=float, default=180.0, help="客户端 HTTP 等待上限（秒）；超时退出不代表服务内导入已结束")
    p.add_argument("--status", action="store_true", help="只打印导入状态，不导入")
    p.add_argument("--max-budget", type=int, default=0, help="今日预算上限；达到则本次不导入（0=用服务默认）")
    args = p.parse_args(argv)

    if not (args.backfill or args.incremental or args.status):
        # 默认按状态决定：回填没跑完（first_run_done=false）就继续回填，
        # 否则走增量。否则旧文件（早于 lookback 窗口）永远轮不到 = 静默漏导。
        try:
            state = json.loads(
                (BASE_DIR / "data" / "import_state_v2.json").read_text(encoding="utf-8")
            )
            args.backfill = not bool(state.get("first_run_done"))
        except (OSError, ValueError):
            args.backfill = True

    if args.max_budget > 0:
        used = _today_budget()
        if used >= args.max_budget:
            msg = {"ok": True, "skipped": "daily budget reached", "used": used, "limit": args.max_budget}
            print(json.dumps(msg, ensure_ascii=False))
            _log({"mode": "import", "out": msg})
            return 3

    return asyncio.run(_run(args))


def _today_budget() -> int:
    """读状态文件里的今日写入计数（计划任务据此提前收工）。"""
    state = BASE_DIR / "data" / "import_state_v2.json"
    try:
        data = json.loads(state.read_text(encoding="utf-8"))
        return int((data.get("budget") or {}).get(datetime.now().strftime("%Y-%m-%d"), 0))
    except (OSError, ValueError, TypeError):
        return 0


if __name__ == "__main__":
    sys.exit(main())
