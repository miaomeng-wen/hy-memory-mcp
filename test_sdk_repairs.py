"""T001 v2 回归测试：SDK 侧四类「静默失败」修复。

覆盖：
  1. EXTRACT 结构校验（extractor.py）——空响应/空对象/未知 schema/错误类型
     必须失败；显式合法空结果与 memory/facts/identity/basic_info 变体必须放行。
  2. 批量落地账本（writer.py）——ADD/UPDATE 全部成功、零确认、部分确认；
     shadow-only 无新增节点不误判；部分生效透传到 write_extracted。
  3. DEDUP 执行账本（dedup.py）——全成功/全失败/部分失败/planned_only/unknown；
     日志与返回值只报确认数。
  4. reconcile 候选一致性（reconciler.py）——candidate_map 只移除确认隐藏项。

全部使用假 LLM / 假存储 / 临时目录，不触碰正式服务与正式数据。
"""
import json
import os
import sys
import unittest
from datetime import datetime

os.environ["MEMORY_EMBEDDING_DIMS"] = "1024"
os.environ["RECONCILE_ENABLED"] = "true"

from hy_memory.agent.extractor import Extractor
from hy_memory.agent.llm_provider import LLMResponse
from hy_memory.agent.reconciler import (
    MemoryReconciler,
    ReconcileOp,
    ReconcileResult,
)
from hy_memory.config import MemoryConfig
from hy_memory.models.memory import MemoryLayer, MemoryNode, MemoryStatus, SourceType
from hy_memory.pipelines._retrieval import dedup as dedup_mod
from hy_memory.pipelines._retrieval.dedup import DedupItem, execute_dedup, plan_dedup
from hy_memory.pipelines.writer import MemoryWriter, WriteRequest


# ────────────────────────── 假依赖 ──────────────────────────

class FakeEmbed:
    """假 embedding 服务：固定向量，维度 8。"""

    def __init__(self, dims=8):
        self.dims = dims

    async def embed(self, text, *a, **kw):
        return [0.1] * self.dims

    async def embed_batch(self, texts, *a, **kw):
        return [[0.1] * self.dims for _ in texts]

    async def embed_queued(self, text, *a, **kw):
        return [0.1] * self.dims

    async def embed_queued_with_meta(self, text, *a, **kw):
        return [0.1] * self.dims, {"cached": False}


class FakeStore:
    """假向量库：内存 dict；可配置批量/单条回执，用来制造零确认与部分确认。"""

    def __init__(self, update_payload_result=True, batch_update_result=None,
                 upsert_batch_result=None):
        self.nodes = {}
        self._client = object()
        self._is_local = True
        self._n = 0
        self.update_payload_result = update_payload_result
        self.batch_update_result = batch_update_result
        self.upsert_batch_result = upsert_batch_result
        self.update_payload_calls = []
        self.batch_update_calls = []

    async def initialize(self):
        return None

    async def upsert(self, node):
        self._n += 1
        nid = getattr(node, "node_id", "") or f"n{self._n}"
        node.node_id = nid
        self.nodes[nid] = node
        return nid

    async def upsert_batch(self, nodes):
        ids = []
        for node in nodes:
            ids.append(await self.upsert(node))
        return ids if self.upsert_batch_result is None else self.upsert_batch_result

    async def get_by_id(self, nid):
        return self.nodes.get(nid)

    async def get_by_ids(self, ids):
        return [self.nodes[i] for i in ids if i in self.nodes]

    async def get_embeddings(self, ids):
        return {i: self.nodes[i].embedding for i in ids if i in self.nodes}

    async def update_payload(self, nid, updates):
        self.update_payload_calls.append((nid, updates))
        return self.update_payload_result

    async def update_payload_batch(self, rows):
        self.batch_update_calls.append(rows)
        if self.batch_update_result is not None:
            return self.batch_update_result
        return [r[0] for r in rows]

    async def delete(self, nid):
        return self.nodes.pop(nid, None) is not None

    async def delete_retrieval_clauses(self, nid):
        return None

    async def search(self, **kw):
        return []

    async def list_by_user(self, **kw):
        return []


class ScriptedLLM:
    """假 LLM：extract 用 complete_messages，reconcile 用 complete。"""

    def __init__(self, extract_payload="{}", ops_payload="[]", finish_reason="stop"):
        self.extract_payload = extract_payload
        self.ops_payload = ops_payload
        self.finish_reason = finish_reason
        self.prompts = []

    def _resp(self, content):
        return LLMResponse(
            content=content, tokens_used=10, prompt_tokens=5, completion_tokens=5,
            model="fake", finish_reason=self.finish_reason,
            response_id="fake-resp", choices_count=1,
        )

    async def complete_messages(self, messages, max_tokens=500, temperature=None,
                               tools=None, tool_choice=None):
        return self._resp(self.extract_payload)

    async def complete(self, prompt, max_tokens=500, **kw):
        self.prompts.append(prompt)
        return self._resp(self.ops_payload)


class FakeCache:
    """假 cache：只捕获 store_pipeline_log 的调用，用于核对 DEDUP 日志口径。"""

    def __init__(self):
        self.logs = []

    async def store_pipeline_log(self, **kwargs):
        self.logs.append(kwargs)


def make_config():
    cfg = MemoryConfig.from_env()
    cfg.llm.enable_intention = False
    cfg.recall.retrieval_clauses_enabled = False
    cfg.recall.session_episode_enabled = False
    cfg.recall.dynamic_episode_enabled = False
    cfg.recall.series_episode_enabled = False
    cfg.recall.entity_store_enabled = False
    return cfg


def build_writer(store, llm=None, ops=None):
    """手工装配 MemoryWriter，绕过 initialize()（不连真 store / 真 LLM）。"""
    from hy_memory.agent.mem_agent import MemAgent
    from hy_memory.agent.reconciler import MemoryReconciler
    from hy_memory.core.merger import Merger

    cfg = make_config()
    writer = MemoryWriter(cfg, embed_service=FakeEmbed(), vector_store=store,
                          cache=None, graph_store=None)
    writer._initialized = True
    writer._vector_store = store
    writer._vector_store_initialized = True
    writer._merger = Merger()
    if ops is not None:
        writer._reconciler = StubReconciler(ops)
    else:
        writer._reconciler = MemoryReconciler(cfg, llm_provider=llm)
    writer._mem_agent = MemAgent(cfg, llm_provider=llm)
    return writer, cfg


class StubReconciler:
    """直接返回给定 ops，用来精确控制「计划了几条」。"""

    def __init__(self, ops):
        self.ops = ops

    async def reconcile(self, **kwargs):
        return ReconcileResult(ops=self.ops, success=True)


def seed_node(store, nid, content="旧事实", layer=MemoryLayer.L2_FACT):
    node = MemoryNode(
        node_id=nid, user_id="tester", agent_id="codex", session_id="s0",
        layer=layer, content=content, source_type=SourceType.INFERRED,
        status=MemoryStatus.ACTIVE, is_latest=True, embedding=[0.1] * 8,
    )
    store.nodes[nid] = node
    return node


def full_req(request_id="req-x", content="x"):
    return WriteRequest(
        content=content, user_id="tester", agent_id="codex", session_id="s1",
        request_id=request_id, extra={"agent_mode": "full"},
    )


# ────────────────────── 1. EXTRACT 结构校验 ──────────────────────

class TestExtractSchemaValidation(unittest.IsolatedAsyncioTestCase):
    """extractor 必须区分「没有事实」和「没拿到结构」。"""

    async def _extract(self, payload):
        return await Extractor(ScriptedLLM(payload)).extract("测试内容", {})

    async def test_empty_response_is_not_success(self):
        res = await self._extract("")
        self.assertFalse(res.success)
        self.assertEqual(res.error_code, "EMPTY_RESPONSE")

    async def test_empty_object_is_not_success(self):
        res = await self._extract("{}")
        self.assertFalse(res.success)
        self.assertEqual(res.error_code, "INVALID_STRUCTURE")
        self.assertIn("empty object", res.error)

    async def test_unknown_schema_key_is_not_success(self):
        res = await self._extract(json.dumps({"unrecognized": "value"}))
        self.assertFalse(res.success)
        self.assertEqual(res.error_code, "INVALID_STRUCTURE")
        self.assertIn("no known schema key", res.error)

    async def test_explicit_empty_memory_is_legal(self):
        """显式 memory=[] 是「真的没有事实」，必须放行。"""
        res = await self._extract(json.dumps({"memory": []}))
        self.assertTrue(res.success, res.error)
        self.assertEqual(res.info, {"memory": []})

    async def test_memory_variant_is_accepted(self):
        res = await self._extract(json.dumps({"memory": [{"content": "用户在 Windows 部署服务"}]}))
        self.assertTrue(res.success, res.error)
        self.assertEqual(res.info["memory"][0]["content"], "用户在 Windows 部署服务")

    async def test_facts_variant_is_accepted(self):
        res = await self._extract(json.dumps({"facts": [{"content": "用户偏好中文回答"}]}))
        self.assertTrue(res.success, res.error)
        self.assertIn("facts", res.info)

    async def test_identity_variant_is_accepted(self):
        res = await self._extract(json.dumps({"identity": [{"content": "用户是后端工程师"}]}))
        self.assertTrue(res.success, res.error)
        self.assertIn("identity", res.info)

    async def test_basic_info_variant_is_accepted(self):
        res = await self._extract(json.dumps({"basic_info": {"name": "default"}}))
        self.assertTrue(res.success, res.error)
        self.assertEqual(res.info["basic_info"], {"name": "default"})

    async def test_wrong_type_is_rejected(self):
        res = await self._extract(json.dumps({"memory": "两条事实"}))
        self.assertFalse(res.success)
        self.assertEqual(res.error_code, "INVALID_STRUCTURE")
        self.assertIn("must be a list", res.error)

    async def test_item_without_content_is_rejected(self):
        res = await self._extract(json.dumps({"memory": [{"tag": "x"}]}))
        self.assertFalse(res.success)
        self.assertEqual(res.error_code, "INVALID_STRUCTURE")
        self.assertIn("has no content", res.error)

    async def test_non_object_item_is_rejected(self):
        res = await self._extract(json.dumps({"memory": ["裸字符串"]}))
        self.assertFalse(res.success)
        self.assertEqual(res.error_code, "INVALID_STRUCTURE")
        self.assertIn("must be an object", res.error)

    async def test_code_fence_is_stripped(self):
        payload = "```json\n" + json.dumps({"memory": [{"content": "围栏内的事实"}]}) + "\n```"
        res = await self._extract(payload)
        self.assertTrue(res.success, res.error)

    async def test_truncated_json_is_repaired_and_flagged(self):
        """finish_reason=length + json_repair 修复时必须打标记，不能当完整输出。"""
        llm = ScriptedLLM('{"memory": [{"content": "被截断的内', finish_reason="length")
        res = await Extractor(llm).extract("测试内容", {})
        self.assertTrue(res.success, res.error)
        self.assertTrue(res.truncated_repaired)


# ─────────────────── 2. writer 批量落地账本 ───────────────────

class TestWriterApplyLedger(unittest.IsolatedAsyncioTestCase):
    """ADD/UPDATE 的「计划 vs 确认」必须分开记账。"""

    async def _run(self, store, ops, request_id):
        writer, _ = build_writer(store, ops=ops)
        return await writer._reconcile_and_store(
            [op.content or "x" for op in ops],
            [{"content": op.content or "x"} for op in ops],
            full_req(request_id), store, request_id,
        )

    async def test_add_all_confirmed(self):
        store = FakeStore()
        ops = [
            ReconcileOp(op="ADD", content="事实一", layer="L2_FACT"),
            ReconcileOp(op="ADD", content="事实二", layer="L2_FACT"),
        ]
        ids, err, counts, _tok = await self._run(store, ops, "req-a")
        self.assertIsNone(err)
        self.assertEqual(len(ids), 2)
        self.assertEqual(counts["planned"], 2)
        self.assertEqual(counts["confirmed"], 2)
        self.assertEqual(counts["applied"], "all")

    async def test_add_zero_confirmed_is_reported(self):
        store = FakeStore(upsert_batch_result=[])
        ops = [
            ReconcileOp(op="ADD", content="事实一", layer="L2_FACT"),
            ReconcileOp(op="ADD", content="事实二", layer="L2_FACT"),
        ]
        ids, err, counts, _tok = await self._run(store, ops, "req-b")
        self.assertEqual(ids, [])
        self.assertIn("none was confirmed", err)
        self.assertEqual(counts["applied"], "none")
        self.assertEqual(counts["unconfirmed"], 2)
        self.assertEqual(set(counts["unconfirmed_reasons"]), {"batch_add_no_receipt"})

    async def test_update_zero_confirmed_is_reported(self):
        store = FakeStore(batch_update_result=[])
        seed_node(store, "old1", "旧一")
        seed_node(store, "old2", "旧二")
        ops = [
            ReconcileOp(op="UPDATE", content="新一", memory_id="old1", layer="L2_FACT"),
            ReconcileOp(op="UPDATE", content="新二", memory_id="old2", layer="L2_FACT"),
        ]
        ids, err, counts, _tok = await self._run(store, ops, "req-c")
        self.assertEqual(ids, [])
        self.assertIn("none was confirmed", err)
        self.assertEqual(counts["applied"], "none")
        self.assertEqual(set(counts["unconfirmed_reasons"]), {"batch_update_no_receipt"})

    async def test_update_partial_confirmed_exposes_unconfirmed(self):
        store = FakeStore(batch_update_result=["old1"])
        seed_node(store, "old1", "旧一")
        seed_node(store, "old2", "旧二")
        ops = [
            ReconcileOp(op="UPDATE", content="新一", memory_id="old1", layer="L2_FACT"),
            ReconcileOp(op="UPDATE", content="新二", memory_id="old2", layer="L2_FACT"),
        ]
        ids, err, counts, _tok = await self._run(store, ops, "req-d")
        self.assertEqual(ids, ["old1"])
        self.assertIsNone(err)
        self.assertEqual(counts["applied"], "partial")
        self.assertEqual(counts["confirmed"], 1)
        self.assertEqual(counts["unconfirmed_ids"], ["old2"])

    async def test_shadow_only_success_needs_no_new_node(self):
        """shadow-only UPDATE 不产生新节点，不能因此判成失败。"""
        store = FakeStore(update_payload_result=True)
        seed_node(store, "shadow1", "旧的重复项")
        ops = [ReconcileOp(op="UPDATE", content="", memory_id="shadow1", layer="L2_FACT")]
        ids, err, counts, _tok = await self._run(store, ops, "req-e")
        self.assertEqual(ids, [])
        self.assertIsNone(err)
        self.assertEqual(counts["applied"], "all")
        self.assertEqual(counts["confirmed_shadow_ids"], ["shadow1"])

    async def test_shadow_only_false_return_is_unconfirmed(self):
        """update_payload 返回 False = 确定未生效，必须记未确认。"""
        store = FakeStore(update_payload_result=False)
        seed_node(store, "shadow2", "旧的重复项")
        ops = [ReconcileOp(op="UPDATE", content="", memory_id="shadow2", layer="L2_FACT")]
        ids, err, counts, _tok = await self._run(store, ops, "req-f")
        self.assertEqual(ids, [])
        self.assertIn("none was confirmed", err)
        self.assertEqual(counts["applied"], "none")
        self.assertEqual(counts["unconfirmed_reasons"], ["shadow_update_not_confirmed"])

    async def test_write_extracted_partial_propagates_409(self):
        """部分生效必须透传到最终入口，且不可整批重放。"""
        store = FakeStore(batch_update_result=["old1"])
        seed_node(store, "old1", "旧一")
        seed_node(store, "old2", "旧二")
        writer, _ = build_writer(store, ops=[
            ReconcileOp(op="UPDATE", content="新一", memory_id="old1", layer="L2_FACT"),
            ReconcileOp(op="UPDATE", content="新二", memory_id="old2", layer="L2_FACT"),
        ])
        req = WriteRequest(
            content="新一", user_id="tester", agent_id="codex", session_id="s1",
            request_id="req-g",
            extra={"extracted_contents": ["新一", "新二"], "agent_mode": "full"},
        )
        resp = await writer.write_extracted(req)
        self.assertFalse(resp.success)
        self.assertEqual(resp.error_code, 409)
        self.assertIn("[PARTIAL_PERSIST]", resp.error_message)
        self.assertIn("retry only the unconfirmed ops", resp.error_message)
        self.assertEqual(resp.extra.get("agent_status"), "partial")
        self.assertTrue(resp.extra.get("partial_persist"))
        self.assertEqual((resp.extra.get("reconcile_ops") or {}).get("applied"), "partial")


class TestWriterInvalidExtract(unittest.IsolatedAsyncioTestCase):
    """无效抽取不能让原始 L1 被隐藏。"""

    async def test_invalid_extract_keeps_l1_visible(self):
        store = FakeStore()
        llm = ScriptedLLM(extract_payload="{}")
        writer, _ = build_writer(store, llm=llm)
        resp = await writer.write(WriteRequest(
            content="用户：把记忆服务部署到 Windows\n助手：好的",
            user_id="tester", agent_id="codex", session_id="s1",
            request_id="req-invalid", extra={"agent_mode": "full"},
        ))
        self.assertFalse(resp.success)
        self.assertEqual(resp.extra.get("agent_status"), "failed")
        self.assertEqual(resp.extra.get("agent_error_code"), "INVALID_STRUCTURE")
        # writer 在 agent 失败时会回滚 L1（_rollback_failed_l1）：
        # 要么节点被删除，要么仍在库里且保持 ACTIVE。绝不允许留在库里却是 SHADOW
        # —— 那等于「抽取失败」把原始对话变成不可检索。
        l1_nodes = [n for n in store.nodes.values() if n.layer == MemoryLayer.L1_RAW]
        for node in l1_nodes:
            self.assertEqual(node.status, MemoryStatus.ACTIVE,
                             "无效抽取不得把原始 L1 降级为 SHADOW")
        rollback = resp.extra.get("l1_rollback") or {}
        self.assertTrue(
            l1_nodes or rollback.get("deleted"),
            "L1 必须留存可检索，或被明确回滚掉（不能静默消失）",
        )

    async def test_explicit_empty_extract_keeps_existing_policy(self):
        """显式 memory=[] 是正常结论：success + L1 按既有策略降级。"""
        store = FakeStore()
        llm = ScriptedLLM(extract_payload=json.dumps({"memory": []}))
        writer, _ = build_writer(store, llm=llm)
        resp = await writer.write(WriteRequest(
            content="用户：纯技术问答", user_id="tester", agent_id="codex",
            session_id="s2", request_id="req-empty", extra={"agent_mode": "full"},
        ))
        self.assertTrue(resp.success, resp.error_message)
        self.assertEqual(resp.extra.get("agent_status"), "noop")
        self.assertEqual(resp.extra.get("agent_stored_ids"), [])
        l1_nodes = [n for n in store.nodes.values() if n.layer == MemoryLayer.L1_RAW]
        self.assertEqual(len(l1_nodes), 1)
        self.assertEqual(l1_nodes[0].status, MemoryStatus.SHADOW)


# ─────────────────── 3. DEDUP 执行账本 ───────────────────

def dedup_items(pairs):
    """pairs: [(node_id, embedding, gmt)] → DedupItem 列表。"""
    return [
        DedupItem(node_id=nid, embedding=emb, content=f"内容-{nid}",
                  is_latest=True, is_chain_head=True, gmt_created=gmt,
                  chain_node_ids=[nid])
        for nid, emb, gmt in pairs
    ]


class ScriptedPayloadStore:
    """按脚本依次返回 update_payload 结果，用来制造部分失败。"""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def update_payload(self, nid, updates):
        self.calls.append(nid)
        if not self.results:
            return True
        return self.results.pop(0)


class TestDedupApplyLedger(unittest.IsolatedAsyncioTestCase):
    TWO_PAIRS = [
        ("a1", [1.0, 0.0], 1.0), ("a2", [1.0, 0.0], 2.0),
        ("b1", [0.0, 1.0], 3.0), ("b2", [0.0, 1.0], 4.0),
    ]

    async def test_plan_is_not_treated_as_applied(self):
        store = ScriptedPayloadStore([True, True])
        plan = await execute_dedup(
            dedup_items(self.TWO_PAIRS), vector_store=store, trigger="test",
            delete_from_store=True,
        )
        self.assertIn("apply", plan)
        self.assertEqual(sorted(plan["delete_ids"]), ["a2", "b2"])
        self.assertEqual(sorted(plan["apply"]["confirmed_ids"]), ["a2", "b2"])
        self.assertEqual(plan["apply"]["confirmed"], 2)
        self.assertTrue(plan["apply"]["applied"])

    async def test_all_failed(self):
        store = ScriptedPayloadStore([False, False])
        plan = await execute_dedup(
            dedup_items(self.TWO_PAIRS), vector_store=store, trigger="test",
            delete_from_store=True,
        )
        self.assertEqual(plan["apply"]["confirmed"], 0)
        self.assertEqual(plan["apply"]["failed"], 2)
        self.assertEqual(plan["apply"]["applied"], "failed")
        self.assertEqual(sorted(plan["apply"]["failed_ids"]), ["a2", "b2"])

    async def test_partial_failure(self):
        store = ScriptedPayloadStore([True, False])
        plan = await execute_dedup(
            dedup_items(self.TWO_PAIRS), vector_store=store, trigger="test",
            delete_from_store=True,
        )
        self.assertEqual(plan["apply"]["confirmed"], 1)
        self.assertEqual(plan["apply"]["failed"], 1)
        self.assertEqual(plan["apply"]["applied"], "partial")

    async def test_exception_counts_as_failure(self):
        class BoomStore:
            async def update_payload(self, nid, updates):
                raise RuntimeError("store down")

        plan = await execute_dedup(
            dedup_items(self.TWO_PAIRS), vector_store=BoomStore(), trigger="test",
            delete_from_store=True,
        )
        self.assertEqual(plan["apply"]["confirmed"], 0)
        self.assertEqual(plan["apply"]["applied"], "failed")
        self.assertTrue(plan["apply"]["errors"])

    async def test_planned_only_never_claims_store_success(self):
        plan = await execute_dedup(
            dedup_items(self.TWO_PAIRS), vector_store=None, trigger="extractor",
            delete_from_store=False,
        )
        self.assertEqual(plan["apply"]["applied"], "planned_only")
        self.assertEqual(plan["apply"]["confirmed"], 0)
        self.assertEqual(plan["apply"]["confirmed_ids"], [])
        self.assertEqual(sorted(plan["delete_ids"]), ["a2", "b2"])

    async def test_missing_store_is_unknown(self):
        plan = await execute_dedup(
            dedup_items(self.TWO_PAIRS), vector_store=None, trigger="search",
            delete_from_store=True,
        )
        self.assertEqual(plan["apply"]["applied"], "unknown")
        self.assertEqual(plan["apply"]["confirmed"], 0)

    async def test_no_duplicates_is_noop(self):
        items = dedup_items([("a1", [1.0, 0.0], 1.0), ("b1", [0.0, 1.0], 2.0)])
        plan = await execute_dedup(
            items, vector_store=ScriptedPayloadStore([]), trigger="test",
            delete_from_store=True,
        )
        self.assertEqual(plan["delete_ids"], [])
        self.assertEqual(plan["apply"]["applied"], "noop")

    async def test_plan_failure_is_failed_not_noop(self):
        items = dedup_items([("a1", "不是向量", 1.0), ("a2", "也不是向量", 2.0)])
        with self.assertRaises(Exception):
            plan_dedup(items)
        plan = await execute_dedup(
            items, vector_store=ScriptedPayloadStore([]), trigger="test",
            delete_from_store=True,
        )
        self.assertEqual(plan["apply"]["applied"], "failed")
        self.assertTrue(plan["apply"]["errors"])

    async def test_log_reports_confirmed_not_planned(self):
        cache = FakeCache()
        store = ScriptedPayloadStore([True, False])
        await execute_dedup(
            dedup_items(self.TWO_PAIRS), vector_store=store, cache=cache,
            trigger="test", request_id="r1", user_id="u1", agent_id="a1",
            delete_from_store=True,
        )
        self.assertEqual(len(cache.logs), 1)
        log = cache.logs[0]
        parsed = json.loads(log["parsed"])
        self.assertEqual(parsed["planned_count"], 2)
        self.assertEqual(parsed["deleted_count"], 1, "deleted_count 必须是确认数")
        self.assertEqual(log["memory_ids"], [parsed["dedup_apply"]["confirmed_ids"][0]])


# ──────────── 4. reconcile 候选一致性（只移除确认隐藏项）────────────

class CandidateStore:
    """给 reconcile 用的假 store：search 返回固定候选，update_payload 可配。"""

    def __init__(self, nodes, update_result=True, search_score=0.99):
        self.nodes = {n.node_id: n for n in nodes}
        self.update_result = update_result
        self.search_score = search_score
        self._is_local = True
        self.update_calls = []

    async def search(self, **kw):
        return [{"node": n, "score": self.search_score} for n in self.nodes.values()]

    async def get_by_ids(self, ids):
        return [self.nodes[i] for i in ids if i in self.nodes]

    async def get_by_id(self, nid):
        return self.nodes.get(nid)

    async def get_embeddings(self, ids):
        return {i: self.nodes[i].embedding for i in ids if i in self.nodes}

    async def update_payload(self, nid, updates):
        self.update_calls.append(nid)
        return self.update_result


def cand_node(nid, content, embedding, gmt, layer=MemoryLayer.L2_FACT):
    return MemoryNode(
        node_id=nid, user_id="tester", agent_id="codex", session_id="s0",
        layer=layer, content=content, source_type=SourceType.INFERRED,
        status=MemoryStatus.ACTIVE, is_latest=True, embedding=embedding,
        gmt_created=datetime(2026, 10, 19, 6, 0, 0) if gmt == 1 else datetime(2026, 10, 20, 6, 0, 0),
    )


class TestReconcileCandidateConsistency(unittest.IsolatedAsyncioTestCase):
    """dedup 未确认隐藏时，候选必须留在 candidate_map 里（仍参与 reconcile）。"""

    def _nodes(self):
        return [
            cand_node("id-aaa", "用户住在杭州", [1.0, 0.0], 1),
            cand_node("id-bbb", "用户住在杭州", [1.0, 0.0], 2),
        ]

    async def _prompt(self, store):
        llm = ScriptedLLM(ops_payload="[]")
        cfg = make_config()
        reconciler = MemoryReconciler(cfg, llm_provider=llm)
        await reconciler.reconcile(
            new_memories=["用户住在杭州"],
            user_id="tester",
            agent_id="codex",
            vector_store=store,
            embed_service=FakeEmbed(),
            layers=[MemoryLayer.L2_FACT],
            cache=None,
            request_id="req-1",
            new_memories_with_meta=[{"content": "用户住在杭州", "layer": "L2_FACT"}],
        )
        self.assertTrue(llm.prompts, "reconcile 必须调用过 LLM")
        return llm.prompts[-1]

    async def test_baseline_candidates_are_in_prompt(self):
        """对照组：不触发去重（向量不同）时两个候选都进 prompt。"""
        nodes = [
            cand_node("id-aaa", "用户住在杭州", [1.0, 0.0], 1),
            cand_node("id-bbb", "用户偏好中文", [0.0, 1.0], 2),
        ]
        prompt = await self._prompt(CandidateStore(nodes))
        self.assertIn("id-aaa", prompt)
        self.assertIn("id-bbb", prompt)

    async def test_confirmed_hidden_candidate_is_removed(self):
        prompt = await self._prompt(CandidateStore(self._nodes(), update_result=True))
        self.assertIn("id-aaa", prompt)
        self.assertNotIn("id-bbb", prompt, "确认隐藏的候选不应再出现在 prompt 里")

    async def test_unconfirmed_candidate_is_kept(self):
        store = CandidateStore(self._nodes(), update_result=False)
        prompt = await self._prompt(store)
        self.assertTrue(store.update_calls, "dedup 必须尝试过隐藏")
        self.assertIn("id-aaa", prompt)
        self.assertIn("id-bbb", prompt,
                      "update_payload 返回 False 时候选必须保留，不能当已隐藏")


if __name__ == "__main__":
    unittest.main()
