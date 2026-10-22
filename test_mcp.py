"""测试 memory_server.py 的 MCP 端点：列工具、健康检查、写/搜记忆。

用法（先启动 memory_server.py）：
    .venv\\Scripts\\python.exe test_mcp.py            # 含写入测试（user_id=mcp_test）
    .venv\\Scripts\\python.exe test_mcp.py --no-write # 只读，不向记忆库写入任何内容
    .venv\\Scripts\\python.exe test_mcp.py --import-writes 5  # 额外跑一次最多写 5 条的导入

说明：
  * 走真实 HTTP，需要服务在跑。
  * 直连本机时绕过系统代理：某些代理客户端把 no_proxy 写成含 "[::1]" 的形式，
    httpx 解析会抛 InvalidURL，导致连不上本机端口。
"""

import argparse
import asyncio
import json
import os
import sys

# Windows 控制台默认 cp1252/gbk，中文会抛 UnicodeEncodeError
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

URL = os.getenv("MEMORY_MCP_URL", "http://127.0.0.1:19527/mcp")
TEST_USER = "mcp_test"


def _clean_no_proxy() -> str:
    """摘掉 httpx 解析不了的 no_proxy 条目（[::1] 之类）。"""
    removed = []
    for key in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(key)
        if not raw:
            continue
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        kept = [p for p in parts if not p.startswith("[")]
        if len(kept) != len(parts):
            removed.extend(p for p in parts if p.startswith("["))
            os.environ[key] = ",".join(kept)
    return ", ".join(sorted(set(removed)))


async def main(no_write: bool, do_import: int = 0) -> int:
    removed = _clean_no_proxy()
    if removed:
        print(f"[i] 已从 no_proxy 移除 httpx 无法解析的条目: {removed}")

    import httpx2

    failures: list[str] = []
    print(f"连接 {URL} ...")
    # trust_env=False：不让系统代理设置干扰本机回环连接
    async with httpx2.AsyncClient(trust_env=False, timeout=30.0) as hc:
        async with streamable_http_client(URL, http_client=hc) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                print("初始化成功\n")

                tools = await session.list_tools()
                names = sorted(t.name for t in tools.tools)
                print(f"可用工具：{', '.join(names)}\n")
                expected = {
                    "memory_add",
                    "memory_search",
                    "memory_profile",
                    "memory_health",
                    "memory_import",
                    "memory_import_status",
                }
                missing = expected - set(names)
                if missing:
                    failures.append(f"缺少工具: {sorted(missing)}")

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

                health = await call("memory_health")
                print("[memory_health]")
                print(json.dumps(health, ensure_ascii=False, indent=2))
                print()
                if not (isinstance(health, dict) and health.get("ok")):
                    failures.append("memory_health.ok 不为 true")

                deep = await call("memory_health", deep=True)
                print("[memory_health deep]")
                print(json.dumps(deep, ensure_ascii=False, indent=2))
                print()
                checks = (deep or {}).get("checks") or {}
                if not checks.get("search_path"):
                    failures.append("深度检查失败：检索链路不可用")
                else:
                    # 失败分支与真空结果的区分必须能从返回值看出来
                    print(f"[i] 库内 {TEST_USER} 之外的用户 {checks.get('stored_total')} 条（total 为该用户计数）")

                if no_write:
                    print("[i] --no-write：跳过写入测试")
                else:
                    add = await call(
                        "memory_add",
                        content="我喜欢看科幻电影，最喜欢的导演是诺兰",
                        user_id=TEST_USER,
                        source="mcp_test",
                        session_id="test_mcp_smoke",
                    )
                    print(f"[memory_add raw] {json.dumps(add, ensure_ascii=False)}")
                    if not (isinstance(add, dict) and add.get("ok")):
                        failures.append(f"raw 写入失败: {add}")

                    facts = await call(
                        "memory_add",
                        content="测试用事实：mcp_test 用户偏好科幻电影，导演偏好诺兰。",
                        user_id=TEST_USER,
                        source="mcp_test",
                        session_id="test_mcp_smoke",
                        mode="facts",
                    )
                    print(f"[memory_add facts] {json.dumps(facts, ensure_ascii=False)}")
                    if not (isinstance(facts, dict) and facts.get("ok")):
                        failures.append(f"facts 写入失败: {facts}")

                    search = await call(
                        "memory_search",
                        query="我喜欢什么类型的电影",
                        user_id=TEST_USER,
                        limit=5,
                    )
                    print("[memory_search]")
                    print(json.dumps(search, ensure_ascii=False, indent=2))
                    if not (isinstance(search, dict) and search.get("ok")):
                        failures.append(f"检索失败: {(search or {}).get('error')}")
                    elif not search.get("memories"):
                        failures.append("检索返回 0 条（写入后应能召回）")

                # 用明显无意义的串验证"真空"与"失败"可区分；真实库可能有噪声召回，
                # 只要求 ok=true（链路成功），不强制 count==0。
                empty = await call("memory_search", query="qqzzxx-不存在-9f3a2b1c", user_id=TEST_USER, limit=3)
                if isinstance(empty, dict):
                    print(f"[i] 无答案查询: ok={empty.get('ok')} count={empty.get('count')}")
                    if empty.get("ok") is not True:
                        failures.append(f"空结果区分异常: {empty}")

                # 导入是只读的状态查询；真正跑导入需要写库，--no-write 时跳过
                status = await call("memory_import_status")
                if isinstance(status, dict):
                    print("[memory_import_status]")
                    print(json.dumps(status, ensure_ascii=False, indent=2))
                    print()
                    if not status.get("ok"):
                        failures.append(f"导入状态读取失败: {status}")
                else:
                    failures.append(f"memory_import_status 返回异常: {status}")

                if do_import:
                    imp = await call("memory_import", max_writes=do_import)
                    print(f"[memory_import max_writes={do_import}]")
                    print(json.dumps(imp, ensure_ascii=False, indent=2))
                    print()
                    if not (isinstance(imp, dict) and imp.get("ok")):
                        failures.append(f"导入失败: {imp}")
                    status2 = await call("memory_import_status")
                    if isinstance(status2, dict) and not status2.get("ok"):
                        failures.append(f"导入后状态读取失败: {status2}")

    if failures:
        print("\n[FAIL]")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("\n[PASS] 全部检查通过")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MCP 端点冒烟测试")
    parser.add_argument("--no-write", action="store_true", help="只读模式：不写入任何记忆")
    parser.add_argument(
        "--import-writes", type=int, default=0,
        help="额外跑一次 memory_import（最多写 N 条），默认 0 表示不跑",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.no_write, args.import_writes)))
