"""result_contract 的五态判定，以及两处调用方（MCP / 导入器）的接入回归测试。

覆盖 T001 AC-01（返回值校验）与 AC-03 的"部分生效透传到调用方"部分。
不连真实记忆库、不调 LLM，全部用假 client。

运行：
    .venv\\Scripts\\python.exe -m unittest test_contract -v
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import codex_importer  # noqa: E402
import memory_server as srv  # noqa: E402
import result_contract as rt  # noqa: E402
from result_contract import classify_write_result  # noqa: E402
from session_sources import RawTurn  # noqa: E402


# ---------------------------------------------------------------- 夹具


class ScriptedClient:
    """按脚本返回任意写入返回值（含 None / 字符串 / 空字典）。"""

    def __init__(self, add_result=None, extracted_result=None) -> None:
        self.add_calls: list[tuple] = []
        self.extracted_calls: list[tuple] = []
        self._add = add_result
        self._extracted = extracted_result

    def add(self, data, **kwargs):  # noqa: ANN001, ANN003
        self.add_calls.append((data, kwargs))
        return self._add

    def add_extracted(self, contents, **kwargs):  # noqa: ANN001, ANN003
        self.extracted_calls.append((contents, kwargs))
        return self._extracted


def make_turn(key: str = "turn-1") -> RawTurn:
    return RawTurn(
        source="codex",
        agent_id="codex",
        session_id="sess-1",
        turn_key=key,
        user_text="用户在 Windows 上部署记忆服务",
        assistant_text="好的，先确认 Python 环境。",
        timestamp=datetime(2026, 10, 19, 6, 0, 0),
    )


# ---------------------------------------------------------------- AC-01 五态判定


class TestClassifyWriteResult(unittest.TestCase):
    """classify_write_result 必须覆盖 None、空字典、字符串、明确失败、
    正常确认、无依据空结果、明确合法 no-op、部分生效、未知结果。"""

    def test_none_is_unknown(self) -> None:
        v = classify_write_result(None)
        self.assertEqual(v.status, rt.UNKNOWN)
        self.assertEqual(v.reason, "none_result")
        self.assertFalse(v.ok)
        self.assertFalse(v.persisted)

    def test_empty_dict_is_unknown(self) -> None:
        v = classify_write_result({})
        self.assertEqual(v.status, rt.UNKNOWN)
        self.assertEqual(v.reason, "empty_dict_result")
        self.assertFalse(v.ok)

    def test_string_result_is_unknown(self) -> None:
        v = classify_write_result("ok")
        self.assertEqual(v.status, rt.UNKNOWN)
        self.assertEqual(v.reason, "non_dict_result:str")
        self.assertEqual(v.result_type, "str")
        self.assertFalse(v.ok)

    def test_missing_success_flag_is_unknown(self) -> None:
        v = classify_write_result({"memory_id": "m1", "request_id": "r"})
        self.assertEqual(v.status, rt.UNKNOWN)
        self.assertEqual(v.reason, "missing_success_flag")
        self.assertFalse(v.ok, "缺 success 标志不能当作写入成功")

    def test_explicit_failure(self) -> None:
        v = classify_write_result(
            {"success": False, "error_code": 500, "error_message": "写入被拒"}
        )
        self.assertEqual(v.status, rt.FAILED)
        self.assertEqual(v.reason, "success_false")
        self.assertEqual(v.error_code, 500)
        self.assertIn("写入被拒", v.error_message)
        self.assertFalse(v.ok)

    def test_confirmed_with_memory_id(self) -> None:
        v = classify_write_result({"success": True, "memory_id": "m1", "request_id": "r1"})
        self.assertEqual(v.status, rt.CONFIRMED)
        self.assertEqual(v.memory_ids, ["m1"])
        self.assertTrue(v.ok)
        self.assertTrue(v.persisted)
        self.assertEqual(v.request_id, "r1")

    def test_confirmed_with_memory_ids_only(self) -> None:
        v = classify_write_result({"success": True, "memory_ids": ["m2", "m3"]})
        self.assertEqual(v.status, rt.CONFIRMED)
        self.assertEqual(v.memory_ids, ["m2", "m3"])

    def test_blank_ids_are_not_ids(self) -> None:
        """空白 id 不是持久化确认。"""
        v = classify_write_result({"success": True, "memory_id": "  ", "memory_ids": ["", " "]})
        self.assertEqual(v.status, rt.UNKNOWN)
        self.assertEqual(v.reason, "success_true_without_ids")

    def test_success_without_ids_is_unknown(self) -> None:
        v = classify_write_result({"success": True, "memory_ids": []})
        self.assertEqual(v.status, rt.UNKNOWN)
        self.assertEqual(v.reason, "success_true_without_ids")
        self.assertFalse(v.ok, "没有 id 的空结果不能算已写入")

    def test_noop_with_agent_status(self) -> None:
        v = classify_write_result({"success": True, "agent_status": "noop"})
        self.assertEqual(v.status, rt.NOOP)
        self.assertEqual(v.reason, "agent_status=noop")
        self.assertTrue(v.ok)
        self.assertFalse(v.persisted, "noop 合法但没有落库 id")

    def test_noop_with_zero_planned_ops(self) -> None:
        v = classify_write_result(
            {
                "success": True,
                "ops_summary": {"applied": "all", "planned": 0, "confirmed": 0},
            }
        )
        self.assertEqual(v.status, rt.NOOP)
        self.assertEqual(v.reason, "ops_summary planned=0")
        self.assertTrue(v.ok)

    def test_noop_with_applied_noop(self) -> None:
        v = classify_write_result({"success": True, "ops_summary": {"applied": "noop"}})
        self.assertEqual(v.status, rt.NOOP)

    def test_partial_with_409(self) -> None:
        v = classify_write_result(
            {
                "success": False,
                "error_code": 409,
                "error_message": "[PARTIAL_PERSIST] 1/2 planned op(s) confirmed",
                "memory_ids": ["m1"],
                "ops_summary": {"planned": 2, "confirmed": 1, "unconfirmed_ids": ["old-1"]},
            }
        )
        self.assertEqual(v.status, rt.PARTIAL)
        self.assertEqual(v.memory_ids, ["m1"])
        self.assertEqual(v.unconfirmed_ids, ["old-1"])
        self.assertFalse(v.ok, "部分生效不能报成功")
        self.assertTrue(v.persisted)

    def test_partial_detected_from_ops_summary_alone(self) -> None:
        v = classify_write_result(
            {"success": False, "ops_summary": {"applied": "partial"}}
        )
        self.assertEqual(v.status, rt.PARTIAL)

    def test_partial_message_without_409(self) -> None:
        v = classify_write_result(
            {"success": False, "error_code": 502, "error_message": "[PARTIAL_PERSIST] x"}
        )
        self.assertEqual(v.status, rt.PARTIAL)

    def test_success_true_with_partial_evidence_is_partial(self) -> None:
        """success=True 但账本说 partial、又没有 id：不能算 unknown 之外的成功。"""
        v = classify_write_result(
            {"success": True, "ops_summary": {"applied": "partial", "planned": 2, "confirmed": 1}}
        )
        self.assertEqual(v.status, rt.PARTIAL)
        self.assertFalse(v.ok)

    def test_unexpected_type_result(self) -> None:
        v = classify_write_result(123)
        self.assertEqual(v.status, rt.UNKNOWN)
        self.assertEqual(v.reason, "non_dict_result:int")

    def test_never_raises_on_weird_payloads(self) -> None:
        for payload in ([], (), object(), {"success": True, "ops_summary": "not-a-dict"}):
            v = classify_write_result(payload)
            self.assertIn(
                v.status, (rt.CONFIRMED, rt.NOOP, rt.PARTIAL, rt.FAILED, rt.UNKNOWN)
            )


# ---------------------------------------------------------------- MCP 接入


class TestMemoryAddContract(unittest.TestCase):
    """memory_server.memory_add 必须把五态与诊断字段透传给调用方。"""

    def setUp(self) -> None:
        self._saved = (srv._client, srv.DEFAULT_USER_ID)

    def tearDown(self) -> None:
        srv._client, srv.DEFAULT_USER_ID = self._saved

    def use(self, client) -> None:
        srv._client = client
        srv.DEFAULT_USER_ID = "default"

    def test_none_result_not_reported_as_written(self) -> None:
        self.use(ScriptedClient(add_result=None))
        out = srv.memory_add(content="原始对话", user_id="default")
        self.assertFalse(out["ok"], "SDK 返回 None 不能报成功")
        self.assertEqual(out["status"], rt.UNKNOWN)
        self.assertNotIn("memory_id", out, "未知结果不能给出一个'已写入'的 id")
        self.assertIsNone(out["memory_ids"])
        self.assertFalse(out["retry_safe"], "结果未知时整批重投不安全")

    def test_empty_dict_not_reported_as_written(self) -> None:
        self.use(ScriptedClient(add_result={}))
        out = srv.memory_add(content="原始对话", user_id="default")
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], rt.UNKNOWN)

    def test_string_result_not_reported_as_written(self) -> None:
        self.use(ScriptedClient(add_result="ok"))
        out = srv.memory_add(content="原始对话", user_id="default")
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], rt.UNKNOWN)

    def test_explicit_failure_passes_error_code(self) -> None:
        self.use(
            ScriptedClient(add_result={"success": False, "error_code": "E_X", "error_message": "被拒"})
        )
        out = srv.memory_add(content="原始对话", user_id="default")
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], rt.FAILED)
        self.assertEqual(out["error_code"], "E_X")
        self.assertTrue(out["retry_safe"], "明确失败可以重试")

    def test_normal_write_still_works(self) -> None:
        self.use(
            ScriptedClient(add_result={"success": True, "memory_id": "m1", "scene": "chat", "request_id": "r1"})
        )
        out = srv.memory_add(content="原始对话", user_id="default")
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], rt.CONFIRMED)
        self.assertEqual(out["memory_id"], "m1")
        self.assertEqual(out["memory_ids"], ["m1"])
        self.assertEqual(out["scene"], "chat")
        self.assertEqual(out["request_id"], "r1")

    def test_facts_path_uses_same_contract(self) -> None:
        self.use(ScriptedClient(extracted_result={"success": True, "memory_ids": ["m2"]}))
        out = srv.memory_add(content=["事实一"], user_id="default", mode="facts")
        self.assertTrue(out["ok"])
        self.assertEqual(out["mode"], "facts")
        self.assertEqual(out["memory_ids"], ["m2"])

    def test_noop_is_ok_without_ids(self) -> None:
        self.use(ScriptedClient(add_result={"success": True, "agent_status": "noop"}))
        out = srv.memory_add(content="原始对话", user_id="default")
        self.assertTrue(out["ok"], "有依据的合法 no-op 是成功")
        self.assertEqual(out["status"], rt.NOOP)
        self.assertIsNone(out["memory_id"])
        self.assertIn("noop", out["reason"])

    def test_partial_persist_is_exposed_not_swallowed(self) -> None:
        """AC-03：部分生效必须暴露已确认与未确认项，且提示不要整批重投。"""
        self.use(
            ScriptedClient(
                extracted_result={
                    "success": False,
                    "error_code": 409,
                    "error_message": "[PARTIAL_PERSIST] 1/2 planned op(s) confirmed",
                    "memory_ids": ["m1"],
                    "ops_summary": {
                        "planned": 2,
                        "confirmed": 1,
                        "unconfirmed_ids": ["old-9"],
                        "applied": "partial",
                    },
                }
            )
        )
        out = srv.memory_add(content=["事实一", "事实二"], user_id="default", mode="facts")
        self.assertFalse(out["ok"])
        self.assertEqual(out["status"], rt.PARTIAL)
        self.assertEqual(out["memory_ids"], ["m1"], "已确认落地部分要如实给")
        self.assertEqual(out["unconfirmed_ids"], ["old-9"])
        self.assertIn("unconfirmed", out["retry_hint"])
        self.assertEqual(out["ops_summary"]["confirmed"], 1)

    def test_exception_still_fails_closed(self) -> None:
        class Boom:
            def add(self, data, **kwargs):
                raise RuntimeError("boom")

            def add_extracted(self, contents, **kwargs):
                raise RuntimeError("boom")

        self.use(Boom())
        out = srv.memory_add(content="原始对话", user_id="default")
        self.assertFalse(out["ok"])
        self.assertIn("RuntimeError", out["error"])


# ---------------------------------------------------------------- 导入器接入


class TestImporterWriteTurnContract(unittest.TestCase):
    """_write_turn 必须把五种返回值翻成三态，且 partial/unknown 不盲目全批重试。"""

    def make_importer(self, client) -> codex_importer.Importer:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        return codex_importer.Importer(
            client=client,
            state_file=root / "state.json",
            user_id="tester",
            sources=[],
            max_writes=50,
            max_seconds=120,
            daily_limit=500,
        )

    def outcome(self, result) -> tuple[str, str]:
        imp = self.make_importer(ScriptedClient(add_result=result))
        return imp._write_turn(make_turn())

    def test_confirmed_is_ok(self) -> None:
        self.assertEqual(self.outcome({"success": True, "memory_id": "m1"})[0], "ok")

    def test_none_is_uncertain(self) -> None:
        st, detail = self.outcome(None)
        self.assertEqual(st, "uncertain")
        self.assertIn("add_unknown_result", detail)
        self.assertIn("none_result", detail)

    def test_empty_dict_is_uncertain(self) -> None:
        st, detail = self.outcome({})
        self.assertEqual(st, "uncertain")
        self.assertIn("empty_dict_result", detail)

    def test_string_is_uncertain(self) -> None:
        st, detail = self.outcome("ok")
        self.assertEqual(st, "uncertain")
        self.assertIn("non_dict_result", detail)

    def test_success_without_ids_is_uncertain(self) -> None:
        st, detail = self.outcome({"success": True, "memory_ids": []})
        self.assertEqual(st, "uncertain")
        self.assertIn("success_true_without_ids", detail)

    def test_explicit_failure_is_retryable_failed(self) -> None:
        st, detail = self.outcome({"success": False, "error_code": "E_X", "error_message": "被拒"})
        self.assertEqual(st, "failed", "明确失败可以重试")
        self.assertIn("add_failed", detail)
        self.assertIn("E_X", detail)

    def test_timeout_code_still_uncertain(self) -> None:
        st, detail = self.outcome(
            {"success": False, "error_code": "timeout", "error_message": "read timed out"}
        )
        self.assertEqual(st, "uncertain")
        self.assertIn("add_failed_timeout", detail)

    def test_partial_is_uncertain_not_failed(self) -> None:
        """AC-03：部分生效不能当普通失败——立刻重投会造重复。"""
        st, detail = self.outcome(
            {
                "success": False,
                "error_code": 409,
                "error_message": "[PARTIAL_PERSIST] 1/2 confirmed",
                "memory_ids": ["m1"],
                "ops_summary": {"planned": 2, "confirmed": 1, "unconfirmed_ids": ["o1"]},
            }
        )
        self.assertEqual(st, "uncertain", "部分生效必须走冷却期，不是立刻重试")
        self.assertIn("add_partial_persist", detail)
        self.assertIn("o1", detail)

    def test_noop_is_ok(self) -> None:
        st, detail = self.outcome({"success": True, "agent_status": "noop"})
        self.assertEqual(st, "ok")
        self.assertIn("noop", detail)


# ---------------------------------------------------------------- 端到端：三态消费


class TestImporterStateMachineIntegration(unittest.TestCase):
    """partial 经 sync() 后落成 uncertain + 冷却期，冷却期内不再提交。"""

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.state = self.root / "state.json"

    def _build_session(self) -> None:
        import json

        lines = [
            json.dumps(
                {
                    "timestamp": "2026-10-01T00:00:00Z",
                    "type": "session_meta",
                    "payload": {"session_id": "sess-p1", "id": "x", "cwd": "C:\\tmp"},
                }
            ),
            json.dumps(
                {
                    "timestamp": "2026-10-01T00:01:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "id": "msg_u1",
                        "content": [{"type": "input_text", "text": "问题一"}],
                    },
                }
            ),
            json.dumps(
                {
                    "timestamp": "2026-10-01T00:02:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "id": "msg_a0",
                        "phase": "final_answer",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "回答一"}],
                    },
                }
            ),
            json.dumps(
                {
                    "timestamp": "2026-10-01T00:02:00Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "item_completed",
                        "thread_id": "t",
                        "turn_id": "u",
                        "item": {
                            "type": "AgentMessage",
                            "id": "msg_a0",
                            "phase": "final_answer",
                            "content": [],
                        },
                    },
                }
            ),
        ]
        (self.sessions / "rollout-p1.jsonl").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

    def _importer(self, client) -> codex_importer.Importer:
        from session_sources import SourceSpec, parse_codex

        return codex_importer.Importer(
            client=client,
            state_file=self.state,
            user_id="tester",
            sources=[SourceSpec("codex", "rollout-*.jsonl", self.sessions, parse_codex)],
            max_writes=50,
            max_seconds=120,
            daily_limit=500,
        )

    def test_partial_persist_becomes_uncertain_with_cooldown(self) -> None:
        self._build_session()
        codex_importer.save_state(
            self.state, {**codex_importer.load_state(self.state), "first_run_done": True}
        )
        partial = {
            "success": False,
            "error_code": 409,
            "error_message": "[PARTIAL_PERSIST] 1/2 confirmed",
            "memory_ids": ["m1"],
            "ops_summary": {"planned": 2, "confirmed": 1, "unconfirmed_ids": ["o1"]},
        }
        stats = self._importer(ScriptedClient(add_result=partial)).sync()
        self.assertEqual(stats.added, 0, "部分生效不能算 added")
        self.assertEqual(stats.failed, 0, "部分生效不是普通失败")
        self.assertEqual(stats.uncertain, 1)

        recs = list(codex_importer.load_state(self.state)["turns"].values())
        self.assertEqual(recs[0]["st"], "uncertain")
        self.assertIn("add_partial_persist", recs[0]["reason"])
        self.assertTrue(recs[0].get("retry_after"), "要带冷却期")

    def test_partial_not_resubmitted_within_cooldown(self) -> None:
        self._build_session()
        codex_importer.save_state(
            self.state, {**codex_importer.load_state(self.state), "first_run_done": True}
        )
        partial = {
            "success": False,
            "error_code": 409,
            "error_message": "[PARTIAL_PERSIST] 1/2 confirmed",
        }
        self._importer(ScriptedClient(add_result=partial)).sync()

        second = ScriptedClient(add_result={"success": True, "memory_id": "m2"})
        stats2 = self._importer(second).sync()
        self.assertEqual(len(second.add_calls), 0, "冷却期内不得重投")
        self.assertEqual(stats2.uncertain, 1)

    def test_unknown_result_not_resubmitted_within_cooldown(self) -> None:
        self._build_session()
        codex_importer.save_state(
            self.state, {**codex_importer.load_state(self.state), "first_run_done": True}
        )
        self._importer(ScriptedClient(add_result=None)).sync()
        second = ScriptedClient(add_result={"success": True, "memory_id": "m2"})
        stats2 = self._importer(second).sync()
        self.assertEqual(len(second.add_calls), 0, "结果未知时不能立刻重投")
        self.assertEqual(stats2.uncertain, 1)

    def test_explicit_failure_retried_next_run(self) -> None:
        self._build_session()
        codex_importer.save_state(
            self.state, {**codex_importer.load_state(self.state), "first_run_done": True}
        )
        stats = self._importer(
            ScriptedClient(add_result={"success": False, "error_code": "E_X", "error_message": "x"})
        ).sync()
        self.assertEqual(stats.failed, 1)

        second = ScriptedClient(add_result={"success": True, "memory_id": "m2"})
        stats2 = self._importer(second).sync()
        self.assertEqual(len(second.add_calls), 1, "明确失败下一次应当重试")
        self.assertEqual(stats2.added, 1)


if __name__ == "__main__":
    unittest.main()
