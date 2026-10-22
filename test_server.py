"""memory_server.py 的离线测试：用假 client 验证包装层判定，不连真实服务。

用：
    .venv\\Scripts\\python.exe -m unittest test_server
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import memory_server as srv  # noqa: E402


class FakeClient:
    """记录调用，并按预设脚本返回结果。"""

    def __init__(self, add_result=None, extracted_result=None, search_result=None, raise_on=None):
        self.add_calls: list[tuple] = []
        self.extracted_calls: list[tuple] = []
        self.search_calls: list[tuple] = []
        self.list_calls: list[tuple] = []
        self._add = add_result if add_result is not None else {"success": True, "memory_id": "m1", "scene": "chat", "request_id": "r1"}
        self._extracted = extracted_result if extracted_result is not None else {"success": True, "memory_ids": ["m2"], "request_id": "r2"}
        self._search = search_result if search_result is not None else _ok_search()
        self._raise_on = raise_on or ""

    def add(self, data, **kwargs):
        self.add_calls.append((data, kwargs))
        if self._raise_on == "add":
            raise RuntimeError("boom-add")
        return self._add

    def add_extracted(self, contents, **kwargs):
        self.extracted_calls.append((contents, kwargs))
        if self._raise_on == "add_extracted":
            raise RuntimeError("boom-extracted")
        return self._extracted

    def search(self, **kwargs):
        self.search_calls.append(kwargs)
        if self._raise_on == "search":
            raise RuntimeError("boom-search")
        return self._search

    def list_memories(self, **kwargs):
        self.list_calls.append(kwargs)
        if self._raise_on == "list":
            raise RuntimeError("boom-list")
        return {"vdb": {"memories": [], "total": 7, "limit": 1, "offset": 0}, "elapsed_ms": 1.0}


def _ok_search(memories=None):
    """模拟 SDK 成功返回：必有 rewrite 键。"""
    return {
        "request_id": "req-ok",
        "memories": memories
        if memories is not None
        else {
            "normal": [
                {
                    "score": 0.8123,
                    "content": "用户在 Windows 上部署记忆服务",
                    "memory_id": "mem-a",
                    "source": "codex",
                    "agent_id": "codex",
                    "user_id": "default",
                    "layer": "l2_fact",
                    "memory_at": 1792348958,
                    "gmt_created": 1792348975,
                }
            ],
            "schema": [],
            "proactive": [],
        },
        "rewrite": {"used": False, "original_query": "q", "effective_query": "q", "created_at": None, "scene": "normal"},
        "elapsed_ms": 12.5,
    }


def _fail_search():
    """模拟 SDK 失败分支：只有 request_id/memories/elapsed_ms，没有 rewrite。"""
    return {
        "request_id": "req-fail",
        "memories": {"schema": [], "proactive": [], "normal": []},
        "elapsed_ms": 3.0,
    }


class ServerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = (srv._client, srv.DEFAULT_USER_ID)

    def tearDown(self) -> None:
        srv._client, srv.DEFAULT_USER_ID = self._saved

    def use(self, client) -> None:
        srv._client = client
        srv.DEFAULT_USER_ID = "default"


class TestAddCompatibility(ServerTestBase):
    """AC-09：raw 走 add，facts 走 add_extracted，旧调用保持兼容。"""

    def test_default_mode_is_raw_add(self) -> None:
        c = FakeClient()
        self.use(c)
        out = srv.memory_add(content="原始对话", user_id="default", source="codex")
        self.assertTrue(out["ok"])
        self.assertEqual(out["mode"], "raw")
        self.assertEqual(len(c.add_calls), 1)
        self.assertEqual(c.extracted_calls, [])
        data, kwargs = c.add_calls[0]
        self.assertEqual(data, "原始对话")
        self.assertEqual(kwargs["source"], "codex")
        self.assertNotIn("session_id", kwargs, "未传 session_id 时不应注入")

    def test_legacy_positional_call_still_works(self) -> None:
        c = FakeClient()
        self.use(c)
        # 旧签名 memory_add(content, user_id, agent_id, scene, source)
        out = srv.memory_add("文本", "default", "codex", "chat", "codex")
        self.assertTrue(out["ok"])
        _, kwargs = c.add_calls[0]
        self.assertEqual(kwargs["scene"], "chat")
        self.assertEqual(kwargs["agent_id"], "codex")

    def test_facts_uses_add_extracted(self) -> None:
        c = FakeClient()
        self.use(c)
        out = srv.memory_add(content="已整理事实：项目在 C:\\ExampleProject", user_id="default", mode="facts", source="codex", session_id="s-1")
        self.assertTrue(out["ok"])
        self.assertEqual(out["mode"], "facts")
        self.assertEqual(c.add_calls, [])
        contents, kwargs = c.extracted_calls[0]
        self.assertEqual(contents, "已整理事实：项目在 C:\\ExampleProject")
        self.assertEqual(kwargs["session_id"], "s-1")
        self.assertNotIn("scene", kwargs, "facts 模式不传 scene")
        self.assertEqual(out["memory_ids"], ["m2"])

    def test_facts_ignores_scene(self) -> None:
        c = FakeClient()
        self.use(c)
        srv.memory_add(content="事实", mode="facts", scene="coding")
        _, kwargs = c.extracted_calls[0]
        self.assertNotIn("scene", kwargs)

    def test_messages_list_accepted(self) -> None:
        c = FakeClient()
        self.use(c)
        msgs = [{"role": "user", "content": "问题"}, {"role": "assistant", "content": "答案"}]
        out = srv.memory_add(content=msgs)
        self.assertTrue(out["ok"])
        data, _ = c.add_calls[0]
        self.assertEqual(data, msgs, "raw 模式把列表原样交给 SDK")

    def test_facts_flattens_messages(self) -> None:
        c = FakeClient()
        self.use(c)
        msgs = [{"role": "user", "content": "问题"}, {"role": "assistant", "content": "答案"}]
        srv.memory_add(content=msgs, mode="facts")
        contents, _ = c.extracted_calls[0]
        self.assertIsInstance(contents, str)
        self.assertIn("问题", contents)
        self.assertIn("答案", contents)

    def test_facts_accepts_string_list(self) -> None:
        """facts 传字符串列表时应原样批量交给 add_extracted（一次 LLM 写多条）。"""
        c = FakeClient()
        self.use(c)
        out = srv.memory_add(content=["事实一", "  ", "事实二"], mode="facts", session_id="s-9")
        self.assertTrue(out["ok"])
        contents, kwargs = c.extracted_calls[0]
        self.assertEqual(contents, ["事实一", "事实二"], "空串应被剔除，其余保持列表")
        self.assertEqual(kwargs["session_id"], "s-9")

    def test_facts_string_list_all_blank_rejected(self) -> None:
        out = srv.memory_add(content=["   ", ""], mode="facts")
        self.assertFalse(out["ok"])
        self.assertIn("空", out["error"])

    def test_raw_string_list_joined(self) -> None:
        """raw 模式收到字符串列表时拼成一段文本，避免 SDK 逐条当消息解析后报 TypeError。"""
        c = FakeClient()
        self.use(c)
        out = srv.memory_add(content=["第一段", "第二段"])
        self.assertTrue(out["ok"])
        data, _ = c.add_calls[0]
        self.assertEqual(data, "第一段\n\n第二段")

    def test_empty_content_rejected(self) -> None:
        for value in ("", "   ", [], None):
            out = srv.memory_add(content=value)
            self.assertFalse(out["ok"], f"{value!r} 应被拒绝")

    def test_bad_mode_rejected(self) -> None:
        out = srv.memory_add(content="x", mode="magic")
        self.assertFalse(out["ok"])
        self.assertIn("raw", out["error"])


class TestAddFailures(ServerTestBase):
    """SDK 返回 success=False 时必须是失败，不能吞掉。"""

    def test_success_false_is_failure(self) -> None:
        c = FakeClient(add_result={"success": False, "error_code": "E_X", "error_message": "写入被拒"})
        self.use(c)
        out = srv.memory_add(content="x")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "写入被拒")
        self.assertEqual(out["error_code"], "E_X")

    def test_exception_is_failure(self) -> None:
        c = FakeClient(raise_on="add")
        self.use(c)
        out = srv.memory_add(content="x")
        self.assertFalse(out["ok"])
        self.assertIn("RuntimeError", out["error"])

    def test_facts_failure(self) -> None:
        c = FakeClient(extracted_result={"success": False, "error_message": "抽取写入失败"})
        self.use(c)
        out = srv.memory_add(content="事实", mode="facts")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "抽取写入失败")


class TestSearchDiscrimination(ServerTestBase):
    """AC-10：包装层异常 / SDK 显式失败 / 真实空结果必须能区分。"""

    def test_success_with_hits(self) -> None:
        c = FakeClient()
        self.use(c)
        out = srv.memory_search(query="记忆服务", user_id="default")
        self.assertTrue(out["ok"])
        self.assertEqual(out["count"], 1)
        mem = out["memories"][0]
        self.assertEqual(mem["memory_id"], "mem-a")
        self.assertEqual(mem["source"], "codex")
        self.assertEqual(mem["layer"], "l2_fact")
        self.assertTrue(mem["memory_at"].startswith("2026-"), mem["memory_at"])
        self.assertIn("+00:00", mem["memory_at"])
        self.assertFalse(out["degraded"])

    def test_sdk_failure_is_not_empty_result(self) -> None:
        c = FakeClient(search_result=_fail_search())
        self.use(c)
        out = srv.memory_search(query="记忆服务", user_id="default")
        self.assertFalse(out["ok"], "SDK 显式失败不能被报成真空")
        self.assertEqual(out["error_kind"], "sdk_read_failed")
        self.assertEqual(out["count"], 0)
        self.assertEqual(out["request_id"], "req-fail")
        self.assertIn("elapsed_ms", out)

    def test_real_empty_result_is_ok(self) -> None:
        c = FakeClient(search_result=_ok_search(memories={"normal": [], "schema": [], "proactive": []}))
        self.use(c)
        out = srv.memory_search(query="不存在的问题", user_id="default")
        self.assertTrue(out["ok"], "真实空结果必须是 ok=true")
        self.assertEqual(out["count"], 0)

    def test_wrapper_exception_distinguished(self) -> None:
        c = FakeClient(raise_on="search")
        self.use(c)
        out = srv.memory_search(query="q")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_kind"], "wrapper_exception")
        self.assertIn("RuntimeError", out["error"])

    def test_empty_query_rejected(self) -> None:
        out = srv.memory_search(query="  ")
        self.assertFalse(out["ok"])

    def test_min_score_passed_through(self) -> None:
        c = FakeClient()
        self.use(c)
        srv.memory_search(query="q", min_score=0.42)
        self.assertEqual(c.search_calls[0]["min_score"], 0.42)

    def test_default_min_score_unchanged(self) -> None:
        c = FakeClient()
        self.use(c)
        srv.memory_search(query="q")
        self.assertEqual(c.search_calls[0]["min_score"], 0.3, "默认 min_score 不应被改动")

    def test_results_sorted_by_score(self) -> None:
        mems = {
            "normal": [
                {"score": 0.4, "content": "low", "memory_id": "l"},
                {"score": 0.9, "content": "high", "memory_id": "h"},
            ],
            "schema": [{"score": 0.7, "content": "mid", "memory_id": "m"}],
            "proactive": [],
        }
        c = FakeClient(search_result=_ok_search(memories=mems))
        self.use(c)
        out = srv.memory_search(query="q", limit=10)
        self.assertEqual([m["memory_id"] for m in out["memories"]], ["h", "m", "l"])


class TestHealthLayers(ServerTestBase):
    """AC-06/健康检查：轻量只证明初始化，深度单独有界触发。"""

    def test_shallow_does_not_touch_store(self) -> None:
        c = FakeClient()
        self.use(c)
        out = srv.memory_health()
        self.assertTrue(out["ok"])
        self.assertEqual(out["checks"], {"client_init": True})
        self.assertEqual(c.search_calls, [], "轻量检查不检索")
        self.assertEqual(c.list_calls, [], "轻量检查不列举")
        self.assertIn("deep_hint", out)

    def test_shallow_marks_config_and_masks_keys(self) -> None:
        c = FakeClient()
        self.use(c)
        out = srv.memory_health()
        cfg = out["config"]
        self.assertIn("mode", cfg)
        for field in ("embedder_key", "llm_key"):
            value = cfg[field]
            self.assertNotEqual(value, os.getenv("MEMORY_EMBEDDER_API_KEY"), f"{field} 是明文")
            self.assertNotEqual(value, os.getenv("MEMORY_LLM_API_KEY"), f"{field} 是明文")
            self.assertTrue(value == "(未设置)" or "…" in value, f"{field} 未脱敏: {value!r}")
            if "…" in value:
                self.assertLessEqual(len(value), 14, f"{field} 露出了过多字符: {value!r}")

    def test_deep_reports_ok_without_writing(self) -> None:
        c = FakeClient()
        self.use(c)
        out = srv.memory_health(deep=True)
        self.assertTrue(out["ok"], out.get("error"))
        self.assertTrue(out["checks"]["search_path"])
        self.assertEqual(out["checks"]["stored_total"], 7)
        self.assertEqual(c.add_calls, [], "深度检查不得写入")
        self.assertEqual(c.extracted_calls, [])
        self.assertIn("只读", out["scope"])

    def test_deep_detects_broken_search(self) -> None:
        c = FakeClient(search_result=_fail_search())
        self.use(c)
        out = srv.memory_health(deep=True)
        self.assertFalse(out["ok"])
        self.assertFalse(out["checks"]["search_path"])
        self.assertEqual(out["error_kind"], "deep_check_failed")

    def test_deep_detects_list_failure(self) -> None:
        c = FakeClient(raise_on="list")
        self.use(c)
        out = srv.memory_health(deep=True)
        self.assertFalse(out["ok"])
        self.assertFalse(out["checks"]["list_memories"])
        self.assertTrue(out["checks"]["search_path"])

    def test_client_init_failure(self) -> None:
        class Boom:
            def __init__(self):
                raise RuntimeError("init failed")

        srv._client = None
        saved = srv.get_client
        srv.get_client = lambda: Boom()  # type: ignore[assignment]
        try:
            out = srv.memory_health()
        finally:
            srv.get_client = saved  # type: ignore[assignment]
        self.assertFalse(out["ok"])
        self.assertIn("init failed", out["error"])


class TestSearchLayers(ServerTestBase):
    """导入的原始对话落在 l1_raw，SDK 默认召回层不含它，必须显式传。"""

    def test_default_layers_include_l1_raw(self) -> None:
        c = FakeClient()
        self.use(c)
        srv.memory_search(query="q")
        layers = c.search_calls[0]["layers"]
        self.assertIn("l1_raw", layers, "默认召回层必须含 l1_raw，否则导入的记忆搜不到")
        self.assertIn("l2_fact", layers, "SDK 原有默认层不能丢")
        self.assertIn("l3_summary", layers)
        self.assertIn("l4_identity", layers)

    def test_explicit_layers_passed_through(self) -> None:
        c = FakeClient()
        self.use(c)
        srv.memory_search(query="q", layers=["l1_raw"])
        self.assertEqual(c.search_calls[0]["layers"], ["l1_raw"])

    def test_bad_layer_rejected(self) -> None:
        c = FakeClient()
        self.use(c)
        out = srv.memory_search(query="q", layers=["l1_raw", "l9_nope"])
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_kind"], "bad_layers")
        self.assertIn("l9_nope", out["error"])
        self.assertEqual(c.search_calls, [], "非法层名不应打到 SDK")

    def test_empty_layers_falls_back_to_default(self) -> None:
        c = FakeClient()
        self.use(c)
        srv.memory_search(query="q", layers=[])
        self.assertIn("l1_raw", c.search_calls[0]["layers"])

    def test_health_deep_uses_same_layers(self) -> None:
        c = FakeClient()
        self.use(c)
        srv.memory_health(deep=True)
        self.assertIn("l1_raw", c.search_calls[0]["layers"])


class TestImportTools(ServerTestBase):
    """memory_import / memory_import_status：进程内导入 + 只读状态。"""

    def setUp(self) -> None:
        super().setUp()
        self._saved_import = (srv._import_client, srv.get_import_client)
        self._tmp = Path(__file__).resolve().parent / "data" / "_test_state"
        self._tmp.mkdir(parents=True, exist_ok=True)
        # 生产锁文件不能出现在测试路径上：计划任务每 5 分钟就会持锁跑一次，
        # 否则测试会随机因为"已有导入实例在运行"失败。
        import codex_importer as ci

        self._saved_lock = (ci.InstanceLock, ci.LOCK_FILE)
        self._test_lock = self._tmp / "import.lock"
        ci.LOCK_FILE = self._test_lock  # type: ignore[assignment]

    def tearDown(self) -> None:
        import codex_importer as ci

        ci.InstanceLock, ci.LOCK_FILE = self._saved_lock  # type: ignore[assignment]
        srv._import_client, srv.get_import_client = self._saved_import
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)
        super().tearDown()

    def test_import_runs_in_process_with_lite_client(self) -> None:
        import codex_importer as ci

        fake = FakeClient()
        srv._import_client = fake
        seen: dict = {}

        class StubImporter:
            def __init__(self, client=None, **_kw):
                seen["client"] = client

            def sync(self, dry_run=False, backfill=None, sources=None):
                seen["dry_run"] = dry_run
                stats = ci.SyncStats()
                stats.added = 3
                return stats

        saved = ci.Importer
        ci.Importer = StubImporter  # type: ignore[assignment]
        try:
            out = srv.memory_import()
        finally:
            ci.Importer = saved  # type: ignore[assignment]

        self.assertTrue(out["ok"], out)
        self.assertIs(seen["client"], fake, "导入必须用服务进程内的客户端，不能另起进程")
        self.assertEqual(out["stats"]["added"], 3)
        self.assertFalse(seen["dry_run"])

    def test_dry_run_does_not_build_client(self) -> None:
        import codex_importer as ci

        fake = FakeClient()
        srv._import_client = fake
        seen: dict = {}

        class StubImporter:
            def __init__(self, client=None, **_kw):
                seen["client"] = client

            def sync(self, dry_run=False, **_kw):
                return ci.SyncStats()

        saved = ci.Importer
        ci.Importer = StubImporter  # type: ignore[assignment]
        try:
            srv.memory_import(dry_run=True)
        finally:
            ci.Importer = saved  # type: ignore[assignment]

        self.assertIsNone(seen["client"], "dry_run 不应建客户端")

    def test_concurrent_import_rejected(self) -> None:
        srv._import_lock.acquire()
        try:
            out = srv.memory_import()
        finally:
            srv._import_lock.release()
        self.assertFalse(out["ok"])
        self.assertIn("已有导入任务", out["skipped"])

    def test_instance_lock_conflict_reported(self) -> None:
        import codex_importer as ci

        class StubImporter:
            def __init__(self, **_kw):
                pass

            def sync(self, **_kw):
                return ci.SyncStats()

        class BusyLock:
            def __init__(self, *_a, **_kw):
                pass

            def __enter__(self):
                raise RuntimeError("已有导入实例在运行")

            def __exit__(self, *_a):
                return False

        saved = (ci.Importer, ci.InstanceLock)
        ci.Importer, ci.InstanceLock = StubImporter, BusyLock  # type: ignore[assignment]
        try:
            out = srv.memory_import()
        finally:
            ci.Importer, ci.InstanceLock = saved  # type: ignore[assignment]
        self.assertFalse(out["ok"])
        self.assertIn("已有导入实例", out["skipped"])

    def test_status_reads_state_without_importing(self) -> None:
        import json

        import codex_importer as ci

        state_file = self._tmp / "state.json"
        state_file.write_text(
            json.dumps(
                {
                    "turns": {
                        "t1": {"st": "ok", "source": "codex"},
                        "t2": {"st": "failed", "source": "codex"},
                        "t3": {"st": "pending", "source": "dsh"},
                        "t4": {"st": "uncertain", "source": "codex", "retry_after": 1e12},
                    },
                    "files": {"a": {}, "b": {}},
                    "first_run_done": True,
                    "budget": {srv.datetime.now().strftime("%Y-%m-%d"): 12},
                }
            ),
            encoding="utf-8",
        )
        saved = ci.STATE_FILE
        ci.STATE_FILE = state_file
        try:
            out = srv.memory_import_status()
        finally:
            ci.STATE_FILE = saved

        self.assertTrue(out["ok"])
        self.assertEqual(out["turns_total"], 4)
        self.assertEqual(out["by_status"]["failed"], 1)
        self.assertEqual(out["by_status"]["uncertain"], 1)
        # uncertain 必须能被外部看见：它既不是成功也不是失败
        self.assertEqual(out["uncertain"]["count"], 1)
        self.assertEqual(out["uncertain"]["cooling"], 1)
        self.assertIn("exactly-once", out["uncertain"]["note"])
        self.assertEqual(out["by_source"]["dsh"]["pending"], 1)
        self.assertEqual(out["files"], 2)
        self.assertEqual(out["budget_today"]["used"], 12)
        self.assertTrue(out["first_run_done"])


class TestHelpers(unittest.TestCase):
    def test_as_text_str(self) -> None:
        self.assertEqual(srv._as_text("abc"), "abc")

    def test_as_text_messages(self) -> None:
        out = srv._as_text([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}])
        self.assertIn("user: hi", out)
        self.assertIn("assistant: yo", out)

    def test_as_text_nested(self) -> None:
        out = srv._as_text([{"role": "user", "content": [{"type": "text", "text": "x"}]}])
        self.assertIn("x", out)

    def test_iso_seconds(self) -> None:
        self.assertEqual(srv._iso(0), "1970-01-01T00:00:00+00:00")

    def test_iso_millis(self) -> None:
        self.assertEqual(srv._iso(1800000000000), srv._iso(1800000000))

    def test_iso_passthrough(self) -> None:
        self.assertIsNone(srv._iso(None))
        self.assertEqual(srv._iso("2026-01-01"), "2026-01-01")
        self.assertEqual(srv._iso("not-a-number"), "not-a-number")

    def test_mask(self) -> None:
        self.assertEqual(srv._mask(""), "(未设置)")
        self.assertEqual(srv._mask("short"), "***")
        masked = srv._mask("sk-1234567890abcdef")
        self.assertIn("…", masked)
        self.assertNotEqual(masked, "sk-1234567890abcdef")


if __name__ == "__main__":
    unittest.main()
