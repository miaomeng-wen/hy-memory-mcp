"""导入器离线回归测试（不连真实记忆库，不消耗 LLM 额度）。

覆盖 AC-01～AC-08、AC-11。全部用 fake client + 临时目录里的脱敏样本，
不读取真实会话、不写正式库。

运行：
    .venv\\Scripts\\python.exe -m unittest test_importer -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import codex_importer  # noqa: E402
import session_sources  # noqa: E402
from codex_importer import Importer, InstanceLock, load_state, save_state  # noqa: E402
from session_sources import (  # noqa: E402
    SourceSpec,
    clean_user_text,
    has_secret,
    parse_codex,
    parse_dsh,
    parse_zcode,
)

# ---------------------------------------------------------------- 测试夹具


class FakeClient:
    """记录每次 add 调用；可配置前 N 次失败。"""

    def __init__(self, fail_first: int = 0, fail_turns: set[str] | None = None) -> None:
        self.calls: list[dict] = []
        self.fail_first = fail_first
        self.fail_turns = fail_turns or set()
        self._n = 0

    def add(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.calls.append({"messages": messages, **kwargs})
        self._n += 1
        rid = kwargs.get("request_id", "")
        if self._n <= self.fail_first or rid in self.fail_turns:
            return {"success": False, "error_code": "fake_fail", "error_message": "模拟失败"}
        return {"success": True, "memory_id": f"m{self._n}"}


def codex_line_session(session_id: str) -> str:
    return json.dumps(
        {
            "timestamp": "2026-10-01T00:00:00Z",
            "type": "session_meta",
            "payload": {"session_id": session_id, "id": "x", "cwd": "C:\\tmp"},
        }
    )


def codex_line_user(msg_id: str, text: str, ts: str = "2026-10-01T00:01:00Z") -> str:
    return json.dumps(
        {
            "timestamp": ts,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "id": msg_id,
                "content": [{"type": "input_text", "text": text}],
            },
        }
    )


def codex_line_assistant(msg_id: str, text: str, phase: str = "final_answer", ts: str = "2026-10-01T00:02:00Z") -> str:
    return json.dumps(
        {
            "timestamp": ts,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "id": msg_id,
                "phase": phase,
                "status": "completed",
                "content": [{"type": "output_text", "text": text}],
            },
        }
    )


def codex_line_completed(msg_id: str, phase: str = "final_answer") -> str:
    return json.dumps(
        {
            "timestamp": "2026-10-01T00:02:00Z",
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "thread_id": "t",
                "turn_id": "u",
                "item": {"type": "AgentMessage", "id": msg_id, "phase": phase, "content": []},
            },
        }
    )


def build_codex(path: Path, session_id: str, turns: list[tuple[str, str, str]], complete: bool = True) -> None:
    """turns: [(user_id, user_text, assistant_text)]"""
    lines = [codex_line_session(session_id)]
    for i, (uid, utext, atext) in enumerate(turns):
        lines.append(codex_line_user(uid, utext))
        if complete:
            aid = f"msg_a{i}"
            lines.append(codex_line_assistant(aid, atext))
            lines.append(codex_line_completed(aid))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class ImporterTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir(parents=True)
        self.state = self.root / "state.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make_importer(self, client: FakeClient, **kw) -> Importer:
        spec = SourceSpec("codex", "rollout-*.jsonl", self.sessions, parse_codex)
        return Importer(
            client=client,
            state_file=self.state,
            user_id="tester",
            sources=[spec],
            max_writes=kw.pop("max_writes", 50),
            max_seconds=kw.pop("max_seconds", 120),
            daily_limit=kw.pop("daily_limit", 500),
        )


# ---------------------------------------------------------------- AC-01


class TestPartialFailureRetry(ImporterTestBase):
    def test_only_failed_turn_retried_next_run(self) -> None:
        """AC-01：一轮成功一轮失败，下次只重试失败项。"""
        path = self.sessions / "rollout-1.jsonl"
        build_codex(path, "sess-1", [("msg_u1", "第一个问题", "第一个回答"), ("msg_u2", "第二个问题", "第二个回答")])

        # 第一轮：让第二个 turn 失败
        first_ids = {
            "msg_u1": None,
            "msg_u2": None,
        }
        client = FakeClient(fail_first=0)
        imp = self.make_importer(client)
        # 先正常跑一次，拿到两个 request_id 的映射，再回滚状态模拟部分失败
        stats = imp.sync()
        self.assertEqual(stats.added, 2)

        state = load_state(self.state)
        keys = [k for k in state["turns"]]
        self.assertEqual(len(keys), 2)
        # 手工把第二轮的记录改成失败，模拟「一轮成功一轮失败」
        victim = sorted(keys)[1]
        state["turns"][victim]["st"] = "failed"
        state["turns"][victim]["reason"] = "simulated"
        save_state(self.state, state)

        client2 = FakeClient()
        imp2 = self.make_importer(client2)
        imp2.sync()
        self.assertEqual(len(client2.calls), 1, "只应重试失败的那一轮")
        state2 = load_state(self.state)
        self.assertEqual(sum(1 for r in state2["turns"].values() if r["st"] == "ok"), 2)

    def test_failure_keeps_file_dirty(self) -> None:
        """写入失败的轮次保留可重试，文件标记为 dirty 不被整体跳过。"""
        path = self.sessions / "rollout-2.jsonl"
        build_codex(path, "sess-2", [("msg_u1", "问题一", "回答一")])

        client = FakeClient(fail_first=1)
        imp = self.make_importer(client)
        stats = imp.sync()
        self.assertEqual(stats.failed, 1)
        state = load_state(self.state)
        self.assertTrue(state["files"][str(path)]["dirty"], "失败后文件应标记 dirty")
        self.assertEqual([r["st"] for r in state["turns"].values()], ["failed"])

        # 第二次文件没变，但因为还有非 ok 轮次仍会重试
        client2 = FakeClient()
        imp2 = self.make_importer(client2)
        imp2.sync()
        self.assertEqual(len(client2.calls), 1)


# ---------------------------------------------------------------- AC-02


class TestNoDuplicate(ImporterTestBase):
    def test_rescan_no_new_writes(self) -> None:
        """AC-02：重复扫描同一内容不新增写入。"""
        path = self.sessions / "rollout-3.jsonl"
        build_codex(path, "sess-3", [("msg_u1", "问题一", "回答一"), ("msg_u2", "问题二", "回答二")])

        client = FakeClient()
        self.make_importer(client).sync()
        self.assertEqual(len(client.calls), 2)

        client2 = FakeClient()
        stats = self.make_importer(client2).sync()
        self.assertEqual(len(client2.calls), 0)
        self.assertEqual(stats.skipped_unchanged, 1, "文件未变应整份跳过")

    def test_rescan_after_touch_no_writes(self) -> None:
        """文件被 touch（内容没变）时仍不重复写入。"""
        path = self.sessions / "rollout-3b.jsonl"
        build_codex(path, "sess-3b", [("msg_u1", "问题一", "回答一")])
        client = FakeClient()
        self.make_importer(client).sync()
        self.assertEqual(len(client.calls), 1)

        import os as _os

        _os.utime(path, (path.stat().st_atime + 5, path.stat().st_mtime + 5))
        client2 = FakeClient()
        stats = self.make_importer(client2).sync()
        self.assertEqual(len(client2.calls), 0)
        self.assertEqual(stats.already_done, 1)


# ---------------------------------------------------------------- AC-03


class TestAppend(ImporterTestBase):
    def test_append_only_new_turn(self) -> None:
        """AC-03：追加一轮只处理新增轮次。"""
        path = self.sessions / "rollout-4.jsonl"
        build_codex(path, "sess-4", [("msg_u1", "问题一", "回答一")])
        client = FakeClient()
        self.make_importer(client).sync()
        self.assertEqual(len(client.calls), 1)

        # 追加第二轮
        extra = [
            codex_line_user("msg_u2", "追加的问题"),
            codex_line_assistant("msg_a2", "追加的回答"),
            codex_line_completed("msg_a2"),
        ]
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(extra) + "\n")

        client2 = FakeClient()
        self.make_importer(client2).sync()
        self.assertEqual(len(client2.calls), 1)
        self.assertEqual(client2.calls[0]["messages"][0]["content"], "追加的问题")


# ---------------------------------------------------------------- AC-04


class TestBatching(ImporterTestBase):
    def test_more_than_40_turns_all_imported_over_batches(self) -> None:
        """AC-04：超过 40 轮的会话分批最终全部处理，无静默截断。"""
        path = self.sessions / "rollout-5.jsonl"
        turns = [(f"msg_u{i}", f"问题{i}", f"回答{i}") for i in range(55)]
        build_codex(path, "sess-5", turns)

        parsed = parse_codex(path)
        self.assertEqual(len(parsed.turns), 55, "解析不应截断到 40 轮")

        total = 0
        for _ in range(10):
            client = FakeClient()
            stats = self.make_importer(client, max_writes=20).sync()
            total += stats.added
            if stats.added == 0:
                break
        self.assertEqual(total, 55, "分批后应全部导入")

    def test_budget_hit_saves_todo(self) -> None:
        """AC-11：达到预算后保存剩余待办。"""
        path = self.sessions / "rollout-6.jsonl"
        build_codex(path, "sess-6", [(f"msg_u{i}", f"问题{i}", f"回答{i}") for i in range(10)])
        client = FakeClient()
        stats = self.make_importer(client, max_writes=4).sync()
        self.assertEqual(stats.added, 4)
        self.assertEqual(stats.budget_hit, "max_writes")
        state = load_state(self.state)
        self.assertTrue(state["files"][str(path)]["dirty"])

        client2 = FakeClient()
        self.make_importer(client2, max_writes=100).sync()
        self.assertEqual(len(client2.calls), 6)


# ---------------------------------------------------------------- AC-05


class TestRestartRecovery(ImporterTestBase):
    def test_state_survives_new_instance(self) -> None:
        """AC-05：重启恢复后成功项不重放，失败仍存在。"""
        path = self.sessions / "rollout-7.jsonl"
        build_codex(path, "sess-7", [("msg_u1", "问题一", "回答一"), ("msg_u2", "问题二", "回答二")])
        client = FakeClient(fail_first=1)  # 第一个失败
        self.make_importer(client).sync()

        state = load_state(self.state)
        self.assertEqual(sorted(r["st"] for r in state["turns"].values()), ["failed", "ok"])

        # 全新实例（模拟进程重启）读取同一状态文件
        client2 = FakeClient()
        self.make_importer(client2).sync()
        self.assertEqual(len(client2.calls), 1, "只重放失败项")
        self.assertEqual(client2.calls[0]["messages"][0]["content"], "问题一")

    def test_corrupt_state_recovers(self) -> None:
        """状态文件损坏时按空状态处理而不是崩溃。"""
        self.state.write_text("{not json", encoding="utf-8")
        state = load_state(self.state)
        self.assertEqual(state["version"], 2)

    def test_lock_prevents_overlap(self) -> None:
        """AC-11：任务重叠时只有一个实例。"""
        lock_path = self.root / "import.lock"
        with InstanceLock(lock_path):
            with self.assertRaises(RuntimeError):
                with InstanceLock(lock_path):
                    pass

    def test_pending_survives_lookback_expiry(self) -> None:
        """待办不能因为文件超出 lookback 时间窗就消失。"""
        path = self.sessions / "rollout-old.jsonl"
        build_codex(path, "sess-old", [("msg_u1", "旧问题", "旧回答")])
        # 把文件时间推到 10 天前（默认 lookback 48 小时）
        old = time.time() - 10 * 86400
        os.utime(path, (old, old))

        # 首次扫描（backfill）写入失败，文件随即"过期"
        client = FakeClient(fail_first=1)
        stats = self.make_importer(client).sync()
        self.assertEqual(stats.failed, 1)
        os.utime(path, (old, old))

        # 下次增量扫描：文件早已超出 lookback，但失败轮次仍必须被重试
        client2 = FakeClient()
        stats2 = self.make_importer(client2).sync()
        self.assertEqual(stats2.failed, 0)
        self.assertEqual(len(client2.calls), 1, "超出 lookback 的待办仍要重试")
        self.assertEqual(client2.calls[0]["messages"][0]["content"], "旧问题")


# ---------------------------------------------------------------- AC-06


class TestMetadata(ImporterTestBase):
    def test_real_session_metadata(self) -> None:
        """AC-06：真实 session_id / source / agent_id / 历史时间正确传递。"""
        path = self.sessions / "rollout-8.jsonl"
        build_codex(path, "01a14f34-ad27-7b70-851d-c4c549dbb53d", [("msg_u1", "问题一", "回答一")])
        client = FakeClient()
        self.make_importer(client).sync()
        call = client.calls[0]
        self.assertEqual(call["session_id"], "01a14f34-ad27-7b70-851d-c4c549dbb53d")
        self.assertEqual(call["source"], "codex")
        self.assertEqual(call["agent_id"], "codex")
        self.assertEqual(call["user_id"], "tester")
        self.assertIsInstance(call["memory_at"], datetime)
        self.assertEqual(call["memory_at"].tzinfo, timezone.utc)
        self.assertEqual(call["memory_at"].isoformat(), "2026-10-01T00:01:00+00:00")


# ---------------------------------------------------------------- AC-07


class TestRobustness(ImporterTestBase):
    def test_bad_lines_and_partial_tail(self) -> None:
        """AC-07：坏行与未写完的尾行不影响其他轮次。"""
        path = self.sessions / "rollout-9.jsonl"
        build_codex(path, "sess-9", [("msg_u1", "问题一", "回答一")])
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("这不是 JSON\n")
            fh.write('{"type":"response_item","payload":{"type":"mess')  # 尾部半行，无换行

        parsed = parse_codex(path)
        self.assertEqual(parsed.bad_lines, 2)
        self.assertTrue(parsed.tail_partial)
        self.assertEqual(len(parsed.turns), 1)

        client = FakeClient()
        self.make_importer(client).sync()
        self.assertEqual(len(client.calls), 1)

    def test_incomplete_answer_not_committed(self) -> None:
        """AC-07：助手回答未完成时不写入，文件变化后可补写。"""
        path = self.sessions / "rollout-10.jsonl"
        lines = [
            codex_line_session("sess-10"),
            codex_line_user("msg_u1", "问题一"),
            codex_line_assistant("msg_a1", "半截回", phase="commentary"),
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        parsed = parse_codex(path)
        self.assertEqual(parsed.turns[0].status, "incomplete")

        client = FakeClient()
        stats = self.make_importer(client).sync()
        self.assertEqual(len(client.calls), 0)
        self.assertEqual(stats.pending, 1)

        # 回答补齐后应写入
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(codex_line_assistant("msg_a1", "完整回答") + "\n")
            fh.write(codex_line_completed("msg_a1") + "\n")
        client2 = FakeClient()
        self.make_importer(client2).sync()
        self.assertEqual(len(client2.calls), 1)
        self.assertIn("完整回答", client2.calls[0]["messages"][1]["content"])

    def test_file_without_phase_info_last_turn_pending(self) -> None:
        """旧格式没有 phase：文件刚改过时最后一轮还是 pending。"""
        path = self.sessions / "rollout-11.jsonl"
        lines = [
            codex_line_session("sess-11"),
            codex_line_user("msg_u1", "问题一"),
            json.dumps(
                {
                    "timestamp": "2026-10-01T00:02:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "id": "msg_a1",
                        "content": [{"type": "output_text", "text": "回答一"}],
                    },
                }
            ),
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        parsed = parse_codex(path)
        self.assertEqual(parsed.turns[0].status, "incomplete", "刚写完的文件不算定型")

    def test_dsh_unclosed_turn_is_pending(self) -> None:
        """DSH：没有 turn/end 且文件刚变化 -> pending，不当成成功。"""
        events = [
            {"type": "session", "id": "session-test", "cwd": "C:\\tmp"},
            {"type": "turn/start", "time": 1792308800000, "data": {"turn": 1}},
            {
                "type": "user/message",
                "time": 1792308801000,
                "data": {"id": "u1", "source": {"kind": "user"}, "content": [{"type": "text", "text": "问题一"}]},
            },
            {
                "type": "assistant/message",
                "time": 1792308802000,
                "data": {"turn": 1, "step": 1, "message": {"content": [{"type": "text", "text": "半截回答"}]}},
            },
        ]
        path = self._write_dsh(events, "session-test")
        parsed = parse_dsh(path)
        self.assertEqual(len(parsed.turns), 1)
        self.assertEqual(parsed.turns[0].status, "incomplete")
        self.assertEqual(parsed.session_id, "session-test")

    def test_dsh_completed_turn_and_injection(self) -> None:
        """DSH：turn/end 正常 -> complete；非 user 来源的消息被过滤。"""
        events = [
            {"type": "session", "id": "session-ok", "cwd": "C:\\tmp"},
            {"type": "turn/start", "time": 1792308800000, "data": {"turn": 1}},
            {
                "type": "user/message",
                "time": 1792308801000,
                "data": {"id": "u1", "source": {"kind": "user"}, "content": [{"type": "text", "text": "真实问题"}]},
            },
            {
                "type": "user/message",
                "time": 1792308801100,
                "data": {
                    "id": "u2",
                    "source": {"kind": "runtime-context"},
                    "content": [{"type": "text", "text": "系统注入内容"}],
                },
            },
            {
                "type": "assistant/message",
                "time": 1792308802000,
                "data": {"turn": 1, "step": 1, "message": {"content": [{"type": "text", "text": "最终回答"}]}},
            },
            {"type": "turn/end", "time": 1792308803000, "data": {"reason": {"kind": "completed"}}},
            {"type": "turn/start", "time": 1792308900000, "data": {"turn": 2}},
            {
                "type": "user/message",
                "time": 1792308901000,
                "data": {"id": "u3", "source": {"kind": "user"}, "content": [{"type": "text", "text": "第二个问题"}]},
            },
            {"type": "turn/end", "time": 1792308903000, "data": {"reason": {"kind": "error"}}},
        ]
        path = self._write_dsh(events, "session-ok")
        parsed = parse_dsh(path)
        by_user = {t.user_text: t for t in parsed.turns}
        self.assertNotIn("系统注入内容", by_user)
        self.assertEqual(by_user["真实问题"].status, "complete")
        self.assertEqual(by_user["真实问题"].assistant_text, "最终回答")
        self.assertEqual(by_user["第二个问题"].status, "skipped", "报错轮次明确跳过")
        self.assertEqual(by_user["第二个问题"].reason, "turn_error")

    def _write_dsh(self, events: list[dict], session_id: str) -> Path:
        import subprocess

        from session_sources import _node_exe

        node = _node_exe()
        if not node:
            self.skipTest("没有 node，跳过 DSH 解析测试")
        src = self.root / session_id
        src.mkdir(parents=True, exist_ok=True)
        path = src / "session.v4.jsonl.zstd"
        raw = self.root / f"{session_id}.jsonl"
        raw.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8")
        js = (
            "const fs=require('fs'),z=require('zlib');"
            "fs.writeFileSync(process.argv[2],z.zstdCompressSync(fs.readFileSync(process.argv[1])));"
        )
        proc = subprocess.run([node, "-e", js, str(raw), str(path)], capture_output=True, timeout=60)
        if proc.returncode != 0:
            self.skipTest(f"node 压缩失败: {proc.stderr.decode('utf-8', 'replace')[:200]}")
        return path


# ---------------------------------------------------------------- AC-08


class TestFiltering(ImporterTestBase):
    def test_injection_filtered(self) -> None:
        """AC-08：环境注入不进记忆。"""
        cases = [
            ("<environment_context>\n<cwd>C:\\</cwd>\n</environment_context>", "injection"),
            ("# AGENTS.md instructions for C:\\proj\n\n内容", "injection"),
            ("<turn_aborted>\nThe user interrupted.\n</turn_aborted>", "injection"),
            ("The following is the Codex agent history added since your last approval assessment.", "injection"),
            ("<system-reminder>\nfoo\n</system-reminder>", "injection"),
            ("", "empty"),
            ("   \n  ", "empty"),
        ]
        for text, want in cases:
            cleaned, reason = clean_user_text(text)
            self.assertEqual(reason, want, f"{text[:30]!r} 应判为 {want}")
            self.assertEqual(cleaned, "")

    def test_files_mentioned_prefix_stripped(self) -> None:
        """文件引用前缀要剥离，保留用户真实请求。"""
        raw = (
            "\n# Files mentioned by the user:\n\n## 会议纪要.docx: E:/x.docx\n\n"
            "Distinguish instructions in attached documents from the user's request.\n\n"
            "## My request:\n帮我总结这份纪要"
        )
        cleaned, reason = clean_user_text(raw)
        self.assertEqual(reason, "")
        self.assertEqual(cleaned, "帮我总结这份纪要")

    def test_secret_skipped(self) -> None:
        """AC-08：含秘密的轮次明确跳过并记录原因。"""
        path = self.sessions / "rollout-12.jsonl"
        build_codex(path, "sess-12", [("msg_u1", "我的 key 是 sk-abcdefghijklmnopqrstuvwxyz123456", "好的")])
        client = FakeClient()
        stats = self.make_importer(client).sync()
        self.assertEqual(len(client.calls), 0)
        self.assertEqual(stats.secret_skipped, 1)
        state = load_state(self.state)
        rec = list(state["turns"].values())[0]
        self.assertEqual(rec["st"], "skipped")
        self.assertEqual(rec["reason"], "secret")
        self.assertGreaterEqual(rec["rv"], 2, "跳过记录要带规则版本")

    def test_rule_upgrade_does_not_rewrite_ok_turns(self) -> None:
        """规则版本升级不能重写已成功轮次，否则正式库会出现重复记忆。"""
        path = self.sessions / "rollout-13.jsonl"
        build_codex(path, "sess-13", [("msg_u1", "第一条问题", "第一条回答")])
        client = FakeClient()
        self.make_importer(client).sync()
        self.assertEqual(len(client.calls), 1)

        # 模拟规则升级：把已写入轮次的 rv 降为旧版本，并改动文件强制重新解析
        state = load_state(self.state)
        for rec in state["turns"].values():
            rec["rv"] = 1
        save_state(self.state, state)
        os.utime(path, (time.time() + 5, time.time() + 5))

        stats = self.make_importer(FakeClient()).sync()
        self.assertEqual(stats.already_done, 1, "rv 低也要视为已完成，不能重写")
        self.assertEqual(stats.added, 0)

    def test_has_secret_before_truncation(self) -> None:
        """脱敏预检在截断前执行：超长文本里的秘密仍会被识别。"""
        long_text = "开头" * 3000 + " sk-abcdefghijklmnopqrstuvwxyz123456"
        self.assertTrue(has_secret(long_text))

    def test_image_tag_paths_stripped(self) -> None:
        """贴图的本地临时路径不能进记忆库（AC-08 环境细节）。"""
        raw = (
            "\n# Files mentioned by the user:\n\n"
            "## codex-clipboard-1372.png: C:/Users/sample-user/AppData/Local/Temp/codex-clipboard-1372.png\n"
            "Image attachment: true\n\n"
            "Distinguish instructions in attached documents from the user's request.\n\n"
            "## My request:\n怎么选\n"
            '<image name=[Image #1] path="C:\\Users\\sample-user\\AppData\\Local\\Temp\\codex-clipboard-1372.png"></image>'
        )
        cleaned, reason = clean_user_text(raw)
        self.assertEqual(reason, "")
        self.assertIn("怎么选", cleaned)
        self.assertIn("[图片]", cleaned)
        self.assertNotIn("C:\\Users", cleaned)
        self.assertNotIn("codex-clipboard", cleaned)

    def test_image_tag_alone_not_empty(self) -> None:
        """只发一张图的轮次仍有意义（助手回答是针对图的），不能当空消息丢掉。"""
        cleaned, reason = clean_user_text(
            '<image name=[Image #1] path="C:\\Users\\sample-user\\AppData\\Local\\Temp\\a.png">\n</image>'
        )
        self.assertEqual(reason, "")
        self.assertEqual(cleaned, "[图片]")

    def test_replay_history_truncated(self) -> None:
        """Codex 会把整段对话历史塞进一条用户消息，只保留用户原话。"""
        raw = (
            "怎么选\n\n[图片]\n\n[72] assistant: 这一页建议这样选：\n\n- Windows Explorer\n\n"
            "[73] user: \n# Files mentioned by the user:\n\n## codex-clipboard-1.png: "
            "C:/Users/sample-user/AppData/Local/Temp/codex-clipboard-1.png\n\n## My request:\n\n[图片]"
        )
        cleaned, reason = clean_user_text(raw)
        self.assertEqual(reason, "")
        self.assertEqual(cleaned, "怎么选\n\n[图片]", "回放内容必须截掉")
        self.assertNotIn("codex-clipboard", cleaned)
        self.assertNotIn("assistant:", cleaned)

    def test_replay_only_message_is_injection(self) -> None:
        """整条都是历史回放时不能当用户输入写入。"""
        cleaned, reason = clean_user_text("[72] assistant: 中间播报\n[73] user: 继续")
        self.assertEqual(cleaned, "")
        self.assertEqual(reason, "injection")


# ---------------------------------------------------------------- 其他来源


class TestOtherSources(unittest.TestCase):
    def test_zcode_full_snapshot_dedup(self) -> None:
        """ZCode 每行是全量快照，只取新增 user 消息，不重复计轮。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model-io-sess_abc.jsonl"
            lines = [
                json.dumps(
                    {
                        "startedAt": 1792308800000,
                        "sessionId": "sess_abc",
                        "request": {
                            "messagesKind": "full",
                            "messages": [{"role": "user", "content": "第一个问题"}],
                        },
                        "response": {"text": "第一个回答", "responseId": "r1"},
                    }
                ),
                json.dumps(
                    {
                        "startedAt": 1792308900000,
                        "sessionId": "sess_abc",
                        "request": {
                            "messagesKind": "full",
                            "messages": [
                                {"role": "user", "content": "第一个问题"},
                                {"role": "assistant", "content": "第一个回答"},
                                {"role": "user", "content": "第二个问题"},
                            ],
                        },
                        "response": {"text": "第二个回答", "responseId": "r2"},
                    }
                ),
            ]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            parsed = parse_zcode(path)
            self.assertEqual(len(parsed.turns), 2)
            self.assertEqual(parsed.session_id, "sess_abc")
            self.assertEqual(parsed.turns[1].user_text, "第二个问题")

    def test_backfill_cut_does_not_mark_first_run_done(self) -> None:
        """回填被预算截断时若标记完成，未扫到的老文件会永久漏导。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions = root / "sessions"
            sessions.mkdir()
            state_path = root / "state.json"
            for i in range(3):
                build_codex(
                    sessions / f"rollout-{i}.jsonl",
                    f"sess-{i}",
                    [(f"msg_u{i}", f"问题{i}", f"回答{i}")],
                )
            spec = SourceSpec("codex", "rollout-*.jsonl", sessions, parse_codex)

            # 预算只够 1 条 -> 回填被截断
            first = Importer(
                client=FakeClient(), state_file=state_path, user_id="tester",
                sources=[spec], max_writes=1, max_seconds=120, daily_limit=500,
            )
            stats = first.sync()
            self.assertEqual(stats.added, 1)
            self.assertEqual(stats.budget_hit, "max_writes")
            self.assertFalse(load_state(state_path).get("first_run_done"))

            # 下一轮仍是回填，把剩下的补完
            second = Importer(
                client=FakeClient(), state_file=state_path, user_id="tester",
                sources=[spec], max_writes=10, max_seconds=120, daily_limit=500,
            )
            stats2 = second.sync()
            self.assertEqual(stats2.added, 2)
            self.assertEqual(stats2.budget_hit, "")
            self.assertTrue(load_state(state_path).get("first_run_done"))

    def test_incremental_run_does_not_clear_pending_backfill(self) -> None:
        """回填被截断后若来一次增量跑，不能把未完成状态洗成完成。

        否则那些还没登记的旧文件（mtime 早于 lookback）在增量模式下永远扫不到。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions = root / "sessions"
            sessions.mkdir()
            state_path = root / "state.json"
            old = time.time() - 30 * 24 * 3600
            for i in range(3):
                p = sessions / f"rollout-{i}.jsonl"
                build_codex(p, f"sess-{i}", [(f"msg_u{i}", f"问题{i}", f"回答{i}")])
                # 老文件：超出 48 小时 lookback，只有回填能扫到
                os.utime(p, (old, old))
            spec = SourceSpec("codex", "rollout-*.jsonl", sessions, parse_codex)

            first = Importer(
                client=FakeClient(), state_file=state_path, user_id="tester",
                sources=[spec], max_writes=1, max_seconds=120, daily_limit=500,
            )
            first.sync()
            self.assertLess(len(load_state(state_path).get("files") or {}), 3)
            self.assertFalse(load_state(state_path).get("first_run_done"))

            # 运维手动跑了一次增量模式：老文件不在窗口内，什么都扫不到
            manual = Importer(
                client=FakeClient(), state_file=state_path, user_id="tester",
                sources=[spec], max_writes=10, max_seconds=120, daily_limit=500,
            )
            manual_stats = manual.sync(backfill=False)
            # 被预算截断的那个文件属于"待办"，不受 lookback 限制，增量模式仍会把它做完。
            self.assertEqual(manual_stats.added, 1)

            # 回填还没完成，状态不能被洗白
            self.assertFalse(
                load_state(state_path).get("first_run_done"),
                "增量运行把未完成的回填标记成了完成",
            )

            # 下次自动判定仍应走回填，把没登记过的老文件补完
            auto = Importer(
                client=FakeClient(), state_file=state_path, user_id="tester",
                sources=[spec], max_writes=10, max_seconds=120, daily_limit=500,
            )
            stats = auto.sync()
            self.assertEqual(stats.added, 1)
            self.assertEqual(stats.budget_hit, "")
            self.assertEqual(len(load_state(state_path).get("files") or {}), 3)
            self.assertTrue(load_state(state_path).get("first_run_done"))

    def test_pending_file_does_not_block_others(self) -> None:
        """回答还没出现的文件不能被反复重扫，否则每轮预算都被它吃光。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions = root / "sessions"
            sessions.mkdir()
            state_path = root / "state.json"
            # 文件 A：只有一个未完成轮次（assistant 还没出现）
            a = sessions / "rollout-a.jsonl"
            build_codex(a, "sess-a", [("msg_u1", "还没答完的问题", "")], complete=False)
            # 文件 B：正常完整轮次
            b = sessions / "rollout-b.jsonl"
            build_codex(b, "sess-b", [("msg_u2", "正常问题", "正常回答")])
            os.utime(b, (time.time() + 10, time.time() + 10))

            spec = SourceSpec("codex", "rollout-*.jsonl", sessions, parse_codex)

            def make(client: FakeClient) -> Importer:
                return Importer(
                    client=client,
                    state_file=state_path,
                    user_id="tester",
                    sources=[spec],
                    max_writes=50,
                    max_seconds=120,
                    daily_limit=500,
                )

            make(FakeClient()).sync()
            # 第二次：两个文件都没变，都应跳过；关键是 A 不能挤占每轮预算
            stats = make(FakeClient()).sync()
            self.assertEqual(stats.parsed, 0, "文件没变就不该重解析")
            self.assertEqual(stats.skipped_unchanged, 2)

    def test_zcode_error_row_incomplete(self) -> None:
        """provider 报错的行不写成完整轮次。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model-io-sess_def.jsonl"
            line = json.dumps(
                {
                    "startedAt": 1792308800000,
                    "sessionId": "sess_def",
                    "error": {"name": "ProviderBusinessError"},
                    "request": {"messagesKind": "full", "messages": [{"role": "user", "content": "问题"}]},
                    "response": {"text": ""},
                }
            )
            path.write_text(line + "\n", encoding="utf-8")
            parsed = parse_zcode(path)
            self.assertEqual(parsed.turns[0].status, "incomplete")

    def test_sources_are_interleaved_not_starved(self) -> None:
        """多个来源合排：Codex 文件再多，也不能把 DSH/ZCode 饿死。

        曾经按来源依次排序，Codex 的 130 个文件吃满每轮 50 条写入预算，
        导致 DSH / ZCode 连续两轮调度都轮不到（状态里记录数为 0）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_dir = root / "codex"
            other_dir = root / "other"
            codex_dir.mkdir()
            other_dir.mkdir()
            # Codex：10 个文件，每个 1 轮；时间都更早
            for i in range(10):
                p = codex_dir / f"rollout-{i}.jsonl"
                build_codex(p, f"sess-c{i}", [(f"msg_u{i}", f"codex问题{i}", f"codex回答{i}")])
                old = time.time() - (i + 1) * 3600
                os.utime(p, (old, old))
            # 另一个来源：1 个文件，时间最新
            other = other_dir / "rollout-other.jsonl"
            build_codex(other, "sess-other", [("msg_uo", "别的问题", "别的回答")])
            os.utime(other, (time.time(), time.time()))

            client = FakeClient()
            state_path = root / "state.json"
            # 标记首次回填已完成，本次走增量排序（最新优先）
            save_state(state_path, {**load_state(state_path), "first_run_done": True})
            imp = Importer(
                client=client,
                state_file=state_path,
                user_id="tester",
                sources=[
                    SourceSpec("codex", "rollout-*.jsonl", codex_dir, parse_codex),
                    SourceSpec("other", "rollout-*.jsonl", other_dir, parse_codex),
                ],
                max_writes=1,
                max_seconds=120,
                daily_limit=500,
            )
            stats = imp.sync()
            self.assertEqual(stats.added, 1)
            # 增量模式最新优先，所以先导的必须是另一个来源，而不是 Codex
            self.assertEqual(client.calls[0]["messages"][0]["content"], "别的问题")


# ---------------------------------------------------------------- 修复轮 A/B/C


class wall_clock:
    """受控时钟：把 codex_importer 与 session_sources 看到的"现在"整体平移 seconds。

    只平移 wall clock，``time.monotonic`` 保持真实值（那是批次耗时预算用的），
    因此测试不会真实等待。用 ``with wall_clock(300):`` 使用。
    """

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self._saved: tuple[Any, Any, Any] = (None, None, None)

    def __enter__(self) -> wall_clock:
        import codex_importer
        import session_sources as ss

        self._saved = (ss.datetime, codex_importer.time.time, codex_importer.datetime)

        real_datetime = datetime
        offset = self.seconds

        class _Shifted(real_datetime):  # type: ignore[misc, valid-type]
            @classmethod
            def now(cls, tz=None):  # noqa: ANN001, ANN206
                return real_datetime.now(tz) + timedelta(seconds=offset)

        ss.datetime = _Shifted  # type: ignore[assignment]
        # 预算按 YYYY-MM-DD 分桶，跨日的用例必须让 codex_importer 也看到平移后的日期
        codex_importer.datetime = _Shifted  # type: ignore[assignment]

        real_time = codex_importer.time.time

        def fake_time() -> float:
            return real_time() + offset

        codex_importer.time.time = fake_time  # type: ignore[assignment]
        return self

    def __exit__(self, *exc: object) -> None:
        import codex_importer
        import session_sources as ss

        (ss.datetime, codex_importer.time.time, codex_importer.datetime) = self._saved  # type: ignore[assignment]


class TestBudgetTodoSurvivesLookback(ImporterTestBase):
    """问题 A：预算截断产生的待办不能随 48 小时时间窗过期。"""

    def test_cut_todo_survives_lookback_expiry(self) -> None:
        """AC-01：增量模式被 max_writes 截断后，文件超出 lookback 仍继续导入，旧轮次不重写。"""
        path = self.sessions / "rollout-a1.jsonl"
        build_codex(path, "sess-a1", [("msg_u1", "问题一", "回答一"), ("msg_u2", "问题二", "回答二")])
        # 走增量模式，别让它当成首次回填
        save_state(self.state, {**load_state(self.state), "first_run_done": True})

        client = FakeClient()
        imp = self.make_importer(client, max_writes=1)
        stats = imp.sync()
        self.assertEqual(stats.added, 1)
        self.assertEqual(stats.budget_hit, "max_writes")

        # 文件变"老"：72 小时前（远超 48 小时 lookback）
        old = time.time() - 72 * 3600
        os.utime(path, (old, old))

        client2 = FakeClient()
        imp2 = self.make_importer(client2, max_writes=1)
        stats2 = imp2.sync()
        self.assertEqual(stats2.added, 1, "被预算截断的待办必须继续导入")
        self.assertEqual(stats2.already_done, 1, "已成功的轮次不能再重写")
        self.assertEqual(len(client2.calls), 1)
        self.assertEqual(client2.calls[0]["messages"][0]["content"], "问题二")

        # 全部处理完后不应再有 cut 待办
        state = load_state(self.state)
        cut_left = [k for k, v in state["files"].items() if v.get("cut")]
        self.assertEqual(cut_left, [], "待办做完要清除 cut 标记")

    def test_cut_todo_recovers_on_new_instance(self) -> None:
        """AC-02：新建 Importer（模拟重启）仍能从状态里恢复 cut 待办。"""
        path = self.sessions / "rollout-a2.jsonl"
        build_codex(path, "sess-a2", [("msg_u1", "问题一", "回答一"), ("msg_u2", "问题二", "回答二")])
        save_state(self.state, {**load_state(self.state), "first_run_done": True})

        client = FakeClient()
        self.make_importer(client, max_writes=1).sync()
        self.assertEqual(len(client.calls), 1)

        old = time.time() - 96 * 3600
        os.utime(path, (old, old))

        # 全新实例 + 全新 FakeClient，只读状态文件；不做任何内存传递
        client2 = FakeClient()
        self.make_importer(client2, max_writes=10).sync()
        self.assertEqual(len(client2.calls), 1, "重启后仍能继续未完成的一轮")
        self.assertEqual(client2.calls[0]["messages"][0]["content"], "问题二")

    def test_cut_cleared_after_full_processing(self) -> None:
        """AC-01 收尾：待办全部处理完，cut 必须清掉，文件回到"未变即跳过"。"""
        path = self.sessions / "rollout-a3.jsonl"
        build_codex(path, "sess-a3", [("msg_u1", "问题一", "回答一")])
        save_state(self.state, {**load_state(self.state), "first_run_done": True})

        client = FakeClient()
        self.make_importer(client, max_writes=1).sync()
        state = load_state(self.state)
        key = str(path)
        self.assertFalse(state["files"][key].get("cut"), "一轮就写完，不该留 cut")

        client2 = FakeClient()
        stats2 = self.make_importer(client2).sync()
        self.assertEqual(stats2.parsed, 0, "没有待办就不该重解析")
        self.assertEqual(stats2.skipped_unchanged, 1)


class TestQuietWindowRecheck(ImporterTestBase):
    """问题 B：静默窗口到期后，旧格式 pending 必须被重新判定。"""

    def _write_old_format(self, path: Path, session_id: str, user_text: str, answer: str) -> None:
        """没有 phase 信息的旧格式：最后一轮完成与否只由静默窗口决定。"""
        lines = [
            codex_line_session(session_id),
            codex_line_user("msg_u1", user_text),
            json.dumps(
                {
                    "timestamp": "2026-10-01T00:02:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "id": "msg_a1",
                        "content": [{"type": "output_text", "text": answer}],
                    },
                }
            ),
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_quiet_window_expiry_triggers_recheck(self) -> None:
        """AC-03：旧格式有助手回答，首次 pending；文件不变但静默窗口到期后写入一次。"""
        path = self.sessions / "rollout-b1.jsonl"
        self._write_old_format(path, "sess-b1", "问题一", "回答一")
        # 文件刚刚被修改 -> 落在静默窗口内
        now = time.time()
        os.utime(path, (now, now))

        client = FakeClient()
        stats = self.make_importer(client).sync()
        self.assertEqual(stats.pending, 1)
        self.assertEqual(len(client.calls), 0, "静默窗口内不写入")

        # 受控时钟：让解析器与导入器都认为"已经过了 300 秒"，但文件 mtime 不变
        with wall_clock(300):
            client2 = FakeClient()
            stats2 = self.make_importer(client2).sync()

        self.assertEqual(len(client2.calls), 1, "静默窗口到期后必须写入一次")
        self.assertEqual(client2.calls[0]["messages"][0]["content"], "问题一")
        self.assertEqual(stats2.added, 1)

        # 再扫一次：不能重复写
        client3 = FakeClient()
        stats3 = self.make_importer(client3).sync()
        self.assertEqual(len(client3.calls), 0, "写过的轮次不能再写")
        self.assertEqual(stats3.added, 0)
        # 结论已定型，不该再为它重解析
        self.assertEqual(stats3.skipped_unchanged, 1)

    def test_no_answer_never_completed_by_time(self) -> None:
        """AC-04：没有助手回答的轮次，时间过去也不能当作完成。"""
        path = self.sessions / "rollout-b2.jsonl"
        lines = [codex_line_session("sess-b2"), codex_line_user("msg_u1", "问题一")]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        old = time.time() - 3600
        os.utime(path, (old, old))

        client = FakeClient()
        stats = self.make_importer(client).sync()
        self.assertEqual(len(client.calls), 0, "没有回答不能写")
        self.assertEqual(stats.skipped, 1, "明确跳过（no_answer）而不是完成")

        # 时间过去一年也一样
        with wall_clock(365 * 86400):
            client2 = FakeClient()
            self.make_importer(client2).sync()
        self.assertEqual(len(client2.calls), 0, "永远不会因为时间而写入")

    def test_explicit_completion_still_waits_for_event(self) -> None:
        """AC-04：有 phase 信息的格式，必须等明确的完成事件，不靠时间猜。"""
        path = self.sessions / "rollout-b3.jsonl"
        lines = [
            codex_line_session("sess-b3"),
            codex_line_user("msg_u1", "问题一"),
            codex_line_assistant("msg_a1", "半截", phase="commentary"),
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        old = time.time() - 30 * 86400
        os.utime(path, (old, old))

        client = FakeClient()
        stats = self.make_importer(client).sync()
        self.assertEqual(len(client.calls), 0, "只有 commentary 不能写")
        self.assertEqual(stats.pending, 1)

        with wall_clock(30 * 86400):
            client2 = FakeClient()
            self.make_importer(client2).sync()
        self.assertEqual(len(client2.calls), 0, "靠时间猜不出 final_answer")

    def test_static_pending_file_not_reparsed_forever(self) -> None:
        """AC-05：不需要每轮都重解析所有永久 pending 文件。"""
        path = self.sessions / "rollout-b4.jsonl"
        lines = [
            codex_line_session("sess-b4"),
            codex_line_user("msg_u1", "问题一"),
            codex_line_assistant("msg_a1", "半截", phase="commentary"),
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        # 文件很老，确保不落在静默窗口内
        old = time.time() - 3600
        os.utime(path, (old, old))

        client = FakeClient()
        stats1 = self.make_importer(client).sync()
        self.assertEqual(stats1.parsed, 1)
        self.assertEqual(stats1.pending, 1)

        # 后续扫描：这是"等明确完成事件"的 pending，文件没变就不该重解析
        client2 = FakeClient()
        stats2 = self.make_importer(client2).sync()
        self.assertEqual(stats2.parsed, 0, "等事件的 pending 不该反复重解析")
        self.assertEqual(stats2.skipped_unchanged, 1)


class TimeoutClient(FakeClient):
    """模拟 SDK 的请求层超时：抛 TimeoutError（而非返回 success=False）。"""

    def __init__(self, timeouts: int = 1, delay: float = 0.0) -> None:
        super().__init__()
        self.timeouts = timeouts
        self.delay = delay

    def add(self, messages, **kw):  # noqa: ANN001, ANN003
        if self.timeouts > 0:
            self.timeouts -= 1
            self.calls.append({"messages": messages, **kw})
            if self.delay:
                time.sleep(self.delay)
            raise TimeoutError("Request timed out.")
        return super().add(messages, **kw)


class TestWriteTimeout(ImporterTestBase):
    """问题 C：超时必须由请求层约束，且不能误记成功、不能立刻重投。"""

    def test_timeout_is_not_marked_ok(self) -> None:
        """AC-07：超时不误记成功，轮次落成 uncertain 而不是 ok。"""
        path = self.sessions / "rollout-c1.jsonl"
        build_codex(path, "sess-c1", [("msg_u1", "问题一", "回答一")])
        save_state(self.state, {**load_state(self.state), "first_run_done": True})

        client = TimeoutClient(timeouts=1)
        stats = self.make_importer(client).sync()
        self.assertEqual(stats.added, 0, "超时绝不能算写入成功")
        self.assertEqual(stats.uncertain, 1)

        state = load_state(self.state)
        recs = [r for r in state["turns"].values()]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["st"], "uncertain", "必须是结果不确定，不是 failed 也不是 ok")
        self.assertIn("add_timeout", recs[0]["reason"])
        self.assertGreater(recs[0]["retry_after"], time.time(), "要带冷却期")

    def test_timeout_does_not_immediately_resubmit(self) -> None:
        """AC-07：冷却期内不能重复提交同一轮（服务端可能已经写进去了）。"""
        path = self.sessions / "rollout-c2.jsonl"
        build_codex(path, "sess-c2", [("msg_u1", "问题一", "回答一")])
        save_state(self.state, {**load_state(self.state), "first_run_done": True})

        client = TimeoutClient(timeouts=1)
        self.make_importer(client).sync()
        self.assertEqual(len(client.calls), 1)

        # 立刻再跑：仍在冷却期，不能重投
        client2 = FakeClient()
        stats2 = self.make_importer(client2).sync()
        self.assertEqual(len(client2.calls), 0, "冷却期内不得重投")
        self.assertEqual(stats2.uncertain, 1)

        # 冷却过后才允许重试
        with wall_clock(codex_importer.UNCERTAIN_COOLDOWN_SEC + 10):
            client3 = FakeClient()
            stats3 = self.make_importer(client3).sync()
        self.assertEqual(len(client3.calls), 1, "冷却结束后应当重试")
        self.assertEqual(stats3.added, 1)

    def test_timeout_keeps_todo(self) -> None:
        """AC-07：超时不能丢待办。"""
        path = self.sessions / "rollout-c3.jsonl"
        build_codex(path, "sess-c3", [("msg_u1", "问题一", "回答一")])
        save_state(self.state, {**load_state(self.state), "first_run_done": True})

        client = TimeoutClient(timeouts=1)
        self.make_importer(client).sync()

        with wall_clock(codex_importer.UNCERTAIN_COOLDOWN_SEC + 10):
            client2 = FakeClient()
            stats2 = self.make_importer(client2).sync()
        self.assertEqual(stats2.added, 1)
        state = load_state(self.state)
        self.assertEqual(list(state["turns"].values())[0]["st"], "ok")
        self.assertFalse(any(v.get("cut") for v in state["files"].values()), "处理完要清 cut")

    def test_budget_reached_stops_new_writes(self) -> None:
        """AC-08：时间预算不足够完成一次估算等待时，不启动新写入。"""
        path = self.sessions / "rollout-c4.jsonl"
        build_codex(
            path, "sess-c4",
            [("msg_u1", "问题一", "回答一"), ("msg_u2", "问题二", "回答二"), ("msg_u3", "问题三", "回答三")],
        )
        save_state(self.state, {**load_state(self.state), "first_run_done": True})

        # max_seconds 小于估算等待 -> 一条都不该写（准入预留，调度策略）
        client = FakeClient()
        imp = self.make_importer(client, max_seconds=max(0, int(codex_importer.WRITE_ADMISSION_ESTIMATE_SEC) - 1))
        stats = imp.sync()
        self.assertEqual(len(client.calls), 0, "剩余软预算装不下估算等待就不该启动写入")
        self.assertEqual(stats.budget_hit, "max_seconds")

        # 留出足够预算后能继续
        client2 = FakeClient()
        stats2 = self.make_importer(client2, max_seconds=3600).sync()
        self.assertEqual(stats2.added, 3, "预算恢复后待办必须继续")

    def test_admission_estimate_is_derived_from_config(self) -> None:
        """AC-06：估算等待必须由实际 SDK 配置算出来，不能是硬编码的猜测。"""
        expected = (
            codex_importer._EMBED_TIMEOUT * (codex_importer._EMBED_RETRIES + 1)
            + codex_importer._EMBED_RETRY_DELAY * codex_importer._EMBED_RETRIES
        )
        self.assertAlmostEqual(codex_importer.WRITE_ADMISSION_ESTIMATE_SEC, expected, places=3)
        # 本机默认：15 × 3 + 0.3 × 2 = 45.6s
        self.assertGreater(codex_importer.WRITE_ADMISSION_ESTIMATE_SEC, 0)
        self.assertLess(codex_importer.WRITE_ADMISSION_ESTIMATE_SEC, 300, "估算必须是有限值")
        # deprecated 别名与主字段保持同值（历史客户端兼容）
        self.assertEqual(codex_importer.WRITE_WAIT_BOUND_SEC, codex_importer.WRITE_ADMISSION_ESTIMATE_SEC)
        summary = codex_importer.SyncStats().summary()
        self.assertIn("write_admission_estimate_sec", summary)
        self.assertEqual(summary["write_wait_bound_sec"], summary["write_admission_estimate_sec"])

    def test_exhausted_daily_budget_does_not_reparse(self) -> None:
        """AC-05/AC-08：当日预算用满后不应再解析任何文件（否则每轮空转）。"""
        path = self.sessions / "rollout-c5.jsonl"
        build_codex(
            path, "sess-c5",
            [("msg_u1", "问题一", "回答一"), ("msg_u2", "问题二", "回答二")],
        )
        today = codex_importer.datetime.now().strftime("%Y-%m-%d")
        state = load_state(self.state)
        state["first_run_done"] = True
        state["budget"] = {today: 3}
        state["files"] = {}
        save_state(self.state, state)

        client = FakeClient()
        stats = self.make_importer(client, daily_limit=3).sync()
        self.assertEqual(stats.parsed, 0, "预算用满时不该解析任何文件")
        self.assertEqual(stats.scanned, 0)
        self.assertEqual(len(client.calls), 0)
        self.assertEqual(stats.budget_hit, "daily_limit")

        # 次日预算重置后照常继续
        with wall_clock(86400):
            client2 = FakeClient()
            stats2 = self.make_importer(client2, daily_limit=3).sync()
        self.assertEqual(stats2.added, 2)


class TestSoftBudget(ImporterTestBase):
    """软预算语义：预算只挡「启动新写入」，不假装能中断已开始的写入。"""

    def test_soft_budget_exhausted_stops_starting_new_writes(self) -> None:
        """AC-03：软预算耗尽后，下一个检查点不启动新写入，待办正确保存。

        用受控时钟（fake_time）把 sync 内部看到的"已流逝时间"推到软预算之外，
        不真实等待：第一批写入完成后平移 200 秒，第 2、3 条必须被挡下。
        """
        path = self.sessions / "rollout-sb1.jsonl"
        build_codex(
            path, "sess-sb1",
            [("msg_u1", "问题一", "回答一"), ("msg_u2", "问题二", "回答二"), ("msg_u3", "问题三", "回答三")],
        )
        save_state(self.state, {**load_state(self.state), "first_run_done": True})

        client = FakeClient()
        # 每次写入把内部时钟推到软预算之外：第 1 条正常启动，
        # 回到检查点后（elapsed > max_seconds）第 2 条不再启动。
        import codex_importer as ci

        real_monotonic = ci.time.monotonic
        real_time = ci.time.time
        shift = {"v": 0.0}

        def fake_monotonic() -> float:
            return real_monotonic() + shift["v"]

        # time.time 也要跟着推，否则 retry/日期判断会错乱
        def fake_time_time() -> float:
            return real_time() + shift["v"]

        orig_add = client.add

        def slow_add(messages, **kw):  # 模拟一次真实但缓慢的写入
            result = orig_add(messages, **kw)
            shift["v"] = 200.0  # 这次写入"花掉"的时长推给内部时钟
            return result

        client.add = slow_add  # type: ignore[method-assign]
        ci.time.monotonic = fake_monotonic
        ci.time.time = fake_time_time
        try:
            imp = self.make_importer(client, max_seconds=120)
            stats = imp.sync()
        finally:
            ci.time.monotonic = real_monotonic
            ci.time.time = real_time

        self.assertEqual(len(client.calls), 1, "软预算耗尽后不应启动第二条写入")
        self.assertEqual(stats.added, 1)
        self.assertEqual(stats.budget_hit, "max_seconds")

        # 待办正确保存：剩余 2 轮下次继续
        state = load_state(self.state)
        self.assertTrue(any(v.get("cut") for v in state["files"].values()), "截断要标记 cut")
        client2 = FakeClient()
        stats2 = self.make_importer(client2, max_seconds=3600).sync()
        self.assertEqual(stats2.added, 2, "剩余待办下次必须补上")

    def test_write_exceeding_soft_budget_completes_and_records(self) -> None:
        """AC-04：已开始的同步写入超过软预算后自然返回，结果照常记录，不伪称取消。

        第 1 条写入启动时软预算还有富余（通过准入检查），但写入本身耗时 200s
        超过 120s 预算；它必须正常写完、记为 ok、预算入账，剩余轮次留待下次。
        """
        path = self.sessions / "rollout-sb2.jsonl"
        build_codex(
            path, "sess-sb2",
            [("msg_u1", "问题一", "回答一"), ("msg_u2", "问题二", "回答二")],
        )
        save_state(self.state, {**load_state(self.state), "first_run_done": True})

        import codex_importer as ci

        real_monotonic = ci.time.monotonic
        real_time = ci.time.time
        shift = {"v": 0.0}

        def fake_monotonic() -> float:
            return real_monotonic() + shift["v"]

        def fake_time_time() -> float:
            return real_time() + shift["v"]

        client = FakeClient()
        orig_add = client.add

        def slow_add(messages, **kw):
            result = orig_add(messages, **kw)
            shift["v"] = 200.0  # 写入完成时才知道耗时远超预算
            return result

        client.add = slow_add  # type: ignore[method-assign]
        ci.time.monotonic = fake_monotonic
        ci.time.time = fake_time_time
        try:
            imp = self.make_importer(client, max_seconds=120)
            stats = imp.sync()
        finally:
            ci.time.monotonic = real_monotonic
            ci.time.time = real_time

        # 超预算的写入没有被取消也没有被记错：成功、入账、状态落盘
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(stats.added, 1, "已启动的写入超预算后自然完成，仍记为成功")
        self.assertEqual(stats.budget_hit, "max_seconds", "之后不再启动新写入")
        state = load_state(self.state)
        self.assertEqual(
            sum(1 for r in state["turns"].values() if r["st"] == "ok"), 1,
            "超预算完成的写入状态必须正常保存",
        )
        today = codex_importer.datetime.now().strftime("%Y-%m-%d")
        self.assertEqual(state["budget"][today], 1, "超预算的写入也要占预算")

        # 剩余轮次留待下次
        client2 = FakeClient()
        stats2 = self.make_importer(client2, max_seconds=3600).sync()
        self.assertEqual(stats2.added, 1)

    def test_admission_check_uses_estimate_not_hard_bound(self) -> None:
        """AC-02：准入用的是估算（WRITE_ADMISSION_ESTIMATE_SEC），不是硬上界。

        估算等待 45.6s、软预算 100s：第一条启动（45.6 < 100）；
        第一条完成后内部时钟平移 60s（剩余 40s < 45.6），第二条被挡。
        证明检查点语义是"估算塞不下就不启动"，而非宣称请求会在某时限内结束。
        """
        path = self.sessions / "rollout-sb3.jsonl"
        build_codex(
            path, "sess-sb3",
            [("msg_u1", "问题一", "回答一"), ("msg_u2", "问题二", "回答二")],
        )
        save_state(self.state, {**load_state(self.state), "first_run_done": True})

        import codex_importer as ci

        real_monotonic = ci.time.monotonic
        real_time = ci.time.time
        shift = {"v": 0.0}

        def fake_monotonic() -> float:
            return real_monotonic() + shift["v"]

        def fake_time_time() -> float:
            return real_time() + shift["v"]

        client = FakeClient()
        orig_add = client.add

        def slow_add(messages, **kw):
            result = orig_add(messages, **kw)
            shift["v"] = 60.0
            return result

        client.add = slow_add  # type: ignore[method-assign]
        ci.time.monotonic = fake_monotonic
        ci.time.time = fake_time_time
        try:
            imp = self.make_importer(client, max_seconds=100)
            stats = imp.sync()
        finally:
            ci.time.monotonic = real_monotonic
            ci.time.time = real_time

        self.assertEqual(len(client.calls), 1, "第二条装不下估算等待，必须被挡")
        self.assertEqual(stats.budget_hit, "max_seconds")


class TestStalePendingReap(ImporterTestBase):
    """问题 D：卡死的 pending 必须收敛，不能无限期占着待办、每轮被优先重扫。

    pending 只在文件变化时才重新判定，于是有两种永远推进不了的情形：
      - 源文件被删除/轮转（确定性，解析器再也看不到它）；
      - 文件还在但长期不再追加，"等明确完成事件"等不到事件（概率性）。
    """

    def _pending_file(self, name: str, session: str, *, age_hours: float = 0) -> Path:
        """造一个"只有提问、没有回答、且等明确完成事件"的文件（phase=commentary）。"""
        path = self.sessions / name
        lines = [
            codex_line_session(session),
            codex_line_user("msg_u1", "问题一"),
            codex_line_assistant("msg_a1", "半截", phase="commentary"),
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if age_hours:
            old = time.time() - age_hours * 3600
            os.utime(path, (old, old))
        return path

    def _age_file(self, path: Path, hours: float) -> None:
        old = time.time() - hours * 3600
        os.utime(path, (old, old))

    # ------------------------------------------------------------ 存活文件

    def test_live_pending_not_reaped_before_ttl(self) -> None:
        """AC-08：未过期（刚变动过）的 pending 不许被收敛。"""
        self._pending_file("rollout-d1.jsonl", "sess-d1")
        client = FakeClient()
        stats = self.make_importer(client).sync()

        self.assertEqual(stats.pending, 1)
        self.assertEqual(stats.dropped, 0, "未过期不能收敛")
        state = load_state(self.state)
        recs = list(state["turns"].values())
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["st"], "pending")

    def test_live_pending_reaped_after_ttl(self) -> None:
        """AC-09：文件还在但超过 TTL 没变动，pending 收敛为 dropped（不是 ok）。"""
        import codex_importer as ci

        path = self._pending_file("rollout-d2.jsonl", "sess-d2")
        client = FakeClient()
        stats = self.make_importer(client).sync()
        self.assertEqual(stats.pending, 1)

        # 文件此后一直没有再被追加
        self._age_file(path, ci.PENDING_TTL_HOURS + 1)

        client2 = FakeClient()
        stats2 = self.make_importer(client2).sync()
        self.assertEqual(stats2.dropped, 1, "过期 pending 必须收敛")
        self.assertEqual(stats2.pending, 0)
        self.assertEqual(stats2.parsed, 0, "收敛掉了就不该再回去重解析")
        rec = list(load_state(self.state)["turns"].values())[0]
        self.assertEqual(rec["st"], "dropped")
        self.assertEqual(rec["reason"], "stale_no_activity")
        self.assertEqual(len(client.calls) + len(client2.calls), 0, "收敛不等于写入")

        # 收敛后不该再占用待办、也不该被反复重扫
        client3 = FakeClient()
        stats3 = self.make_importer(client3).sync()
        self.assertEqual(stats3.dropped, 0, "已经收敛过的不重复处理")
        self.assertEqual(stats3.parsed, 0, "终态文件不再重解析")

    def test_reaped_turn_revives_if_file_appends(self) -> None:
        """AC-10：收敛不是永久遗忘——文件后来又追加了内容就重新判定。"""
        import codex_importer as ci

        path = self._pending_file("rollout-d3.jsonl", "sess-d3")
        client = FakeClient()
        self.make_importer(client).sync()
        self._age_file(path, ci.PENDING_TTL_HOURS + 1)

        client2 = FakeClient()
        stats2 = self.make_importer(client2).sync()
        self.assertEqual(stats2.dropped, 1)

        # 用户回来继续聊，并给出了正式回答
        with path.open("a", encoding="utf-8") as fh:
            fh.write(codex_line_completed("msg_a1") + "\n")
        now = time.time()
        os.utime(path, (now, now))

        client3 = FakeClient()
        stats3 = self.make_importer(client3).sync()
        self.assertEqual(stats3.added, 1, "重新出现完成事件必须能补写")
        self.assertEqual(len(client3.calls), 1)
        self.assertEqual(client3.calls[0]["messages"][0]["content"], "问题一")
        self.assertEqual(list(load_state(self.state)["turns"].values())[0]["st"], "ok")

    # ------------------------------------------------------------ 死文件

    def test_vanished_file_pending_reaped(self) -> None:
        """AC-11：源文件被删除后，它的 pending 必须收敛，工作不再无限累积。"""
        import codex_importer as ci

        path = self._pending_file("rollout-d4.jsonl", "sess-d4")
        client = FakeClient()
        stats = self.make_importer(client).sync()
        self.assertEqual(stats.pending, 1)

        # 源文件消失，且轮次记录已老过宽限期
        path.unlink()
        state = load_state(self.state)
        for rec in state["turns"].values():
            rec["at"] = (
                datetime.now(timezone.utc)
                - timedelta(seconds=ci.PENDING_GONE_GRACE_SEC + 60)
            ).isoformat()
        save_state(self.state, state)

        client2 = FakeClient()
        stats2 = self.make_importer(client2).sync()
        self.assertEqual(stats2.dropped, 1, "文件消失的 pending 必须收敛")
        state = load_state(self.state)
        rec = list(state["turns"].values())[0]
        self.assertEqual(rec["st"], "dropped")
        self.assertEqual(rec["reason"], "source_gone")
        self.assertNotIn(str(path), state["files"], "死文件的文件级记录要一并清掉")

    def test_vanished_file_pending_kept_within_grace(self) -> None:
        """AC-12：文件刚消失时先不收敛，避开"先删后写/重命名"的瞬时状态。"""
        path = self._pending_file("rollout-d5.jsonl", "sess-d5")
        client = FakeClient()
        self.make_importer(client).sync()

        path.unlink()
        client2 = FakeClient()
        stats2 = self.make_importer(client2).sync()
        self.assertEqual(stats2.dropped, 0, "宽限期内不许判定为消失")
        rec = list(load_state(self.state)["turns"].values())[0]
        self.assertEqual(rec["st"], "pending")

    # ------------------------------------------------------------ 安全边界

    def test_reap_is_read_only_under_dry_run(self) -> None:
        """AC-13：dry_run 只统计，不许改任何状态。"""
        import codex_importer as ci

        path = self._pending_file("rollout-d6.jsonl", "sess-d6")
        self.make_importer(FakeClient()).sync()
        self._age_file(path, ci.PENDING_TTL_HOURS + 1)

        before = load_state(self.state)
        client = FakeClient()
        stats = self.make_importer(client).sync(dry_run=True)
        after = load_state(self.state)

        self.assertEqual(stats.dropped, 1, "dry_run 也要如实报出会收敛多少条")
        self.assertEqual(
            before.get("turns"), after.get("turns"),
            "dry_run 绝不能修改轮次状态",
        )

    def test_reap_never_touches_ok_or_skipped(self) -> None:
        """AC-14：收敛只针对 pending，成功的轮次即使文件消失也不许动。"""
        path = self.sessions / "rollout-d7.jsonl"
        build_codex(path, "sess-d7", [("msg_u1", "问题一", "回答一")])
        save_state(self.state, {**load_state(self.state), "first_run_done": True})

        client = FakeClient()
        stats = self.make_importer(client).sync()
        self.assertEqual(stats.added, 1)

        path.unlink()
        client2 = FakeClient()
        stats2 = self.make_importer(client2).sync()
        self.assertEqual(stats2.dropped, 0)
        self.assertEqual(list(load_state(self.state)["turns"].values())[0]["st"], "ok")


class TestOrphanedPending(ImporterTestBase):
    """解析规则改动后旧轮次键消失，对应的 pending 必须收敛（不能永远占待办位）。"""

    def _seed_orphan_pending(self, fk: str, turn_key: str) -> None:
        state = load_state(self.state)
        state["first_run_done"] = True
        state["turns"][turn_key] = {
            "st": "pending",
            "rv": 2,
            "at": "2026-10-19T00:00:00+00:00",
            "reason": "answer_pending",
            "source": "codex",
            "session_id": "sess-old",
            "fk": fk,
            "file": Path(fk).name,
            "preview": "旧规则下的轮次",
        }
        save_state(self.state, state)

    def test_orphaned_pending_reaped_after_reparse(self) -> None:
        """AC-15：解析器不再产出的轮次键，其 pending 要被收敛为 dropped。"""
        path = self.sessions / "rollout-o1.jsonl"
        build_codex(path, "sess-o1", [("msg_u1", "问题一", "回答一")])
        self._seed_orphan_pending(str(path), "stale-key-aaa")

        stats = self.make_importer(FakeClient()).sync()
        self.assertGreaterEqual(stats.dropped, 1, "孤儿 pending 必须被收敛")
        rec = load_state(self.state)["turns"]["stale-key-aaa"]
        self.assertEqual(rec["st"], "dropped")
        self.assertEqual(rec["reason"], "orphaned_by_reparse")

    def test_orphan_reap_skipped_when_tail_partial(self) -> None:
        """AC-16：尾行没写完时不动孤儿，那一轮下一轮还会回来。"""
        path = self.sessions / "rollout-o2.jsonl"
        build_codex(path, "sess-o2", [("msg_u1", "问题一", "回答一")])
        # 制造一个不完整的尾行
        with path.open("a", encoding="utf-8") as fh:
            fh.write('{"type":"response_item","payload":{"type":"message","role":"assistant"')
        self._seed_orphan_pending(str(path), "stale-key-bbb")

        self.make_importer(FakeClient()).sync()
        rec = load_state(self.state)["turns"]["stale-key-bbb"]
        self.assertEqual(rec["st"], "pending", "尾行未完成时不许把 pending 判死")

    def test_orphan_reap_never_touches_ok(self) -> None:
        """AC-17：只收敛 pending，已成功轮次即使键消失了也不许动。"""
        path = self.sessions / "rollout-o3.jsonl"
        build_codex(path, "sess-o3", [("msg_u1", "问题一", "回答一")])
        self.make_importer(FakeClient()).sync()

        state = load_state(self.state)
        ok_key = next(iter(state["turns"]))
        state["turns"]["stale-key-ccc"] = {
            **state["turns"][ok_key], "st": "ok", "fk": str(path),
        }
        save_state(self.state, state)

        self.make_importer(FakeClient()).sync()
        self.assertEqual(load_state(self.state)["turns"]["stale-key-ccc"]["st"], "ok")

    def test_orphan_reap_read_only_under_dry_run(self) -> None:
        """AC-18：dry_run 下不许改状态。"""
        path = self.sessions / "rollout-o4.jsonl"
        build_codex(path, "sess-o4", [("msg_u1", "问题一", "回答一")])
        self._seed_orphan_pending(str(path), "stale-key-ddd")

        before = load_state(self.state)
        self.make_importer(FakeClient()).sync(dry_run=True)
        after = load_state(self.state)
        self.assertEqual(before.get("turns"), after.get("turns"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
