# Upstream source: Tencent-Hunyuan/Hy-Memory, hy-memory 2.0.3
# Upstream copyright: Copyright (C) 2026 Tencent. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Modified by the hy-memory-mcp project; these are local modifications.
# Modification details are documented below and in NOTICE.
# Upstream license copy: third_party/HY-MEMORY-LICENSE

"""把本地对 hy_memory 包的修改重新应用到 site-packages。

pip 升级 hy-memory 后 site-packages 会被覆盖，跑一遍这个脚本即可恢复。

用法：
    .venv\\Scripts\\python.exe apply_patches.py           # 应用
    .venv\\Scripts\\python.exe apply_patches.py --check   # 只检查，不改文件
    .venv\\Scripts\\python.exe apply_patches.py --revert  # 撤销本脚本的全部补丁

补丁分两类：
* ``PATCHES``       —— 片段替换补丁（幂等判据：新片段在、旧片段不在）；
* ``DIFF_PATCHES``  —— unified diff 补丁（内容在 ``patches/*.diff``），适合大块改动；
  判据是 diff 头里的 ``sha256-after`` / ``sha256-before``：文件等于 after 即已应用，
  等于 before 即待应用，两者都不是则说明上游已改动或只应用了一半，报 WARN 不硬改。

顺序：先应用全部片段补丁，再应用 diff 补丁 —— diff 的基线是「pristine + 片段补丁」，
所以 reconicler 这类同时有两种补丁的文件必须按这个顺序。

改动清单（2026-10-19）：
片段补丁
1. agent/reconciler.py  MAX_TOKENS 2000 -> 8000
                        MAX_TOKENS_UPPER_BOUND 8000 -> 16000
                        BASE_OVERHEAD_TOKENS 500 -> 1500
2. config.py            agent_max_tokens 默认 2000 -> 8000
3. agent/reconciler.py  空响应不再被判为合法空数组

diff 补丁（T001 真伪结果修复）
4. agent/reconciler.py            DEDUP 只移除确认隐藏的候选
5. agent/extractor.py             EXTRACT 结构校验
6. pipelines/writer.py            ADD/UPDATE/SUPERSEDE 落地对账、partial 暴露
7. pipelines/_retrieval/dedup.py  计划/确认/失败分账

原因（1-3）：step-5-preview 是推理模型，思维链和正式输出共用 max_tokens。
实测 22 次 RECONCILE 有 10 次 completion_tokens 精确等于 2000 且 content
为空（parsed 只有 "[]"），一条 op 都没产出。三个关思考参数
（reasoning_effort=none / thinking:{type:disabled} / enable_thinking=false）
在该网关全部静默失效，只能抬高上限。

第 3 项是独立缺陷：即使抬高上限，只要模型返回空串，原代码就走
`_is_empty_array` 分支当作"模型说无需操作"，跳过重试与 ADD-all 兜底，
导致整批事实静默丢失。抬高上限只是降低触发概率，不能根治。

原因（4-7）：SDK 把"计划"当"已落地"上报 —— 计划隐藏数当删除数、
批量 UPDATE/ADD 无回执仍记成功、结构错误的抽取结果当"无事实"成功、
MCP 与导入器只看 ``success is False``。详见 FIX-REPORT.md。
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent
PATCH_DIR = ROOT / "patches"

# (相对路径, 旧片段, 新片段, 说明)
PATCHES: list[tuple[str, str, str, str]] = [
    (
        "agent/reconciler.py",
        """    MAX_TOKENS = 2000
    PER_OP_TOKENS = 300
    BASE_OVERHEAD_TOKENS = 500
    MAX_TOKENS_UPPER_BOUND = 8000""",
        """    MAX_TOKENS = 8000
    PER_OP_TOKENS = 300
    BASE_OVERHEAD_TOKENS = 1500
    MAX_TOKENS_UPPER_BOUND = 16000""",
        "reconciler: 抬高 reconcile 的 max_tokens 基础值与上限",
    ),
    (
        "config.py",
        '_get_env_int("MEMORY_AGENT_MAX_TOKENS", 2000)',
        '_get_env_int("MEMORY_AGENT_MAX_TOKENS", 8000)',
        "config: agent_max_tokens 默认值 2000 -> 8000",
    ),
    (
        "agent/reconciler.py",
        """                _is_empty_array = self._strip_code_fence(raw_response) in ("[]", "")
                if ops or _is_empty_array:
                    _parse_ok = True
                    break""",
        """                # [本地修改 2026-10-19] 原实现把空响应（"" / 纯空白）也当成合法的
                # 空数组 "[]"，于是 _parse_ok=True 直接跳出循环 —— 既不再重试，也
                # 走不到下面的 _fallback_add_ops 兜底，整批抽取结果被静默丢弃，
                # 只留下一条 LENGTH(response)=0 的 RECONCILE 日志。
                # 实测 10 次此类失败全部伴随 WRITE_TIMELINE 但零写入。
                # 语义上 "[]" 是模型明确回答"无需任何 op"，是合法结果；
                # 空响应则是被 max_tokens 截断或上游返回空，必须按失败处理。
                _stripped = self._strip_code_fence(raw_response)
                _is_empty_array = _stripped == "[]"
                _is_blank = not _stripped.strip()
                if ops or _is_empty_array:
                    _parse_ok = True
                    break
                if _is_blank:
                    logger.warning(
                        "[reconciler] RECONCILE returned blank response "
                        f"(attempt {_attempt + 1}/{_MAX_PARSE_ATTEMPTS}), "
                        f"completion_tokens={getattr(response, 'completion_tokens', 0)}"
                    )""",
        "reconciler: 空响应不再被当成合法空数组（避免整批记忆静默丢弃）",
    ),
]

# (相对路径, patches/ 下的 diff 文件, 说明)
DIFF_PATCHES: list[tuple[str, str, str]] = [
    (
        "agent/reconciler.py",
        "reconciler-dedup-consistency.diff",
        "reconciler: DEDUP 只移除确认隐藏的候选（计划数 != 已隐藏）",
    ),
    (
        "agent/extractor.py",
        "extractor-schema-validation.diff",
        "extractor: EXTRACT 结构校验（{} / 未知字段 / 错误类型不得当成功空结果）",
    ),
    (
        "pipelines/writer.py",
        "writer-confirm-ops.diff",
        "writer: ADD/UPDATE/SUPERSEDE 落地对账、partial 暴露、无效抽取不隐藏 L1",
    ),
    (
        "pipelines/_retrieval/dedup.py",
        "dedup-apply-ledger.diff",
        "dedup: 计划/确认/失败分账，返回 apply 账本，按 update_payload 回执计数",
    ),
]

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")
_META_RE = re.compile(r"^#\s*sha256-(before|after):\s*([0-9a-f]{64})\s*$")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_diff(diff: str) -> tuple[list[tuple[str, str]], str, str]:
    """把 unified diff 解析成 [(旧块, 新块), ...]，并取出 before/after 摘要。"""
    before = after = ""
    hunks: list[tuple[str, str]] = []
    old_lines: list[str] | None = None
    new_lines: list[str] | None = None
    for raw in diff.splitlines(keepends=True):
        meta = _META_RE.match(raw.strip())
        if meta:
            if meta.group(1) == "before":
                before = meta.group(2)
            else:
                after = meta.group(2)
            continue
        if _HUNK_RE.match(raw):
            if old_lines is not None:
                hunks.append(("".join(old_lines), "".join(new_lines or [])))
            old_lines, new_lines = [], []
            continue
        if old_lines is None:
            # 文件头（--- / +++ / diff --git / index）
            continue
        tag, body = raw[:1], raw[1:]
        if tag == " ":
            old_lines.append(body)
            new_lines.append(body)
        elif tag == "-":
            old_lines.append(body)
        elif tag == "+":
            new_lines.append(body)
        elif tag == "\\":
            # "\ No newline at end of file"
            if old_lines and old_lines[-1].endswith("\n"):
                old_lines[-1] = old_lines[-1].rstrip("\n")
            if new_lines and new_lines[-1].endswith("\n"):
                new_lines[-1] = new_lines[-1].rstrip("\n")
        else:
            raise ValueError(f"无法解析的 diff 行: {raw!r}")
    if old_lines is not None:
        hunks.append(("".join(old_lines), "".join(new_lines or [])))
    return hunks, before, after


def apply_unified_diff(text: str, diff: str, *, reverse: bool = False) -> str:
    """把 unified diff 套到 text 上（reverse=True 时反向撤销）。"""
    hunks, _before, _after = parse_diff(diff)
    for old_block, new_block in hunks:
        src, dst = (new_block, old_block) if reverse else (old_block, new_block)
        if not src:
            raise ValueError("diff 片段为空，无法定位")
        count = text.count(src)
        if count != 1:
            raise ValueError(
                f"diff 片段无法唯一匹配（出现 {count} 次）：{src.splitlines()[:1]}"
            )
        text = text.replace(src, dst, 1)
    return text


def locate_package() -> Path:
    spec = importlib.util.find_spec("hy_memory")
    if spec is None or not spec.submodule_search_locations:
        raise SystemExit("找不到 hy_memory 包，请确认已激活正确的 venv")
    return Path(list(spec.submodule_search_locations)[0])


def _read_diff(name: str) -> str:
    path = PATCH_DIR / name
    if not path.exists():
        raise SystemExit(f"缺少补丁文件: {path}")
    return path.read_text(encoding="utf-8")


def _apply_fragments(pkg: Path, *, check_only: bool, revert: bool) -> tuple[int, int]:
    """片段替换补丁。返回 (failed, changed)。"""
    failed = 0
    changed = 0
    for rel, old, new, desc in PATCHES:
        path = pkg / rel
        if not path.exists():
            print(f"[MISS] {rel} 不存在，跳过（{desc}）")
            failed += 1
            continue

        text = path.read_text(encoding="utf-8")
        applied = new in text and old not in text
        if applied and not revert:
            print(f"[OK]   {rel} 已是最新（{desc}）")
            continue
        if revert:
            if not applied:
                print(f"[SKIP] {rel} 未应用，无需撤销（{desc}）")
                continue
            if check_only is False:
                path.write_text(text.replace(new, old, 1), encoding="utf-8")
                changed += 1
                print(f"[UNDO] {rel} 已撤销（{desc}）")
            continue
        if old not in text:
            print(f"[WARN] {rel} 找不到目标片段，可能上游已改动（{desc}）")
            print(f"       期望片段:\n{old}")
            failed += 1
            continue
        if check_only:
            print(f"[TODO] {rel} 需要打补丁（{desc}）")
            failed += 1
            continue

        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        changed += 1
        print(f"[PATCH] {rel} 已修改（{desc}）")
    return failed, changed


def _projected_text(rel: str, text: str) -> str:
    """把尚未应用的片段补丁在内存里套上，用于 --check 时判定 diff 补丁状态。

    diff 补丁的 before 摘要是「pristine + 片段补丁」，所以只读检查时不能直接拿
    磁盘内容比对，否则会误报「上游已改动」。
    """
    for prel, old, new, _desc in PATCHES:
        if prel != rel:
            continue
        if new in text:
            continue
        if old in text:
            text = text.replace(old, new, 1)
    return text


def _apply_diffs(pkg: Path, *, check_only: bool, revert: bool) -> tuple[int, int]:
    """unified diff 补丁。基线 = pristine + 片段补丁，所以必须在片段补丁之后跑。

    返回 (failed, changed)。
    """
    failed = 0
    changed = 0
    for rel, diff_name, desc in DIFF_PATCHES:
        path = pkg / rel
        if not path.exists():
            print(f"[MISS] {rel} 不存在，跳过（{desc}）")
            failed += 1
            continue

        diff = _read_diff(diff_name)
        _hunks, before_sha, after_sha = parse_diff(diff)
        if not before_sha or not after_sha:
            print(f"[MISS] {diff_name} 缺少 sha256-before/after 头，无法判定状态")
            failed += 1
            continue

        text = path.read_text(encoding="utf-8")
        cur_sha = _sha256(text)
        is_after = cur_sha == after_sha
        # diff 的 before 摘要是「pristine + 片段补丁」，所以判定 before 状态时必须先把
        # 尚未应用的片段补丁在内存里投影上去。三种模式都要投影：
        #   --check     片段补丁还没写盘；
        #   --revert    可能刚被撤掉（同一文件既有片段补丁又有 diff 补丁）；
        #   应用        片段补丁已写盘，投影是恒等操作。
        is_before = _sha256(_projected_text(rel, text)) == before_sha

        if revert:
            if is_before:
                print(f"[SKIP] {rel} 未应用，无需撤销（{desc}）")
                continue
            if not is_after:
                print(f"[WARN] {rel} 当前内容既不是 before 也不是 after，"
                      f"无法安全撤销（{desc}）")
                print(f"       实际 sha256={cur_sha}")
                failed += 1
                continue
            if check_only:
                print(f"[TODO] {rel} 可以撤销（{desc}）")
                failed += 1
                continue
            try:
                path.write_text(apply_unified_diff(text, diff, reverse=True), encoding="utf-8")
            except ValueError as exc:
                print(f"[WARN] {rel} 撤销失败：{exc}（{desc}）")
                failed += 1
                continue
            changed += 1
            print(f"[UNDO] {rel} 已撤销（{desc}）")
            continue

        if is_after:
            print(f"[OK]   {rel} 已是最新（{desc}）")
            continue
        if not is_before:
            # 既不是 before 也不是 after：上游已改 或 只应用了一半，绝不硬改
            print(f"[WARN] {rel} 当前内容与补丁的 before/after 都不一致，"
                  f"可能上游已改动或只应用了一部分（{desc}）")
            print(f"       期望 before sha256={before_sha}")
            print(f"       期望 after  sha256={after_sha}")
            print(f"       实际       sha256={cur_sha}")
            failed += 1
            continue
        if check_only:
            print(f"[TODO] {rel} 需要打补丁（{desc}）")
            failed += 1
            continue
        try:
            patched = apply_unified_diff(text, diff)
        except ValueError as exc:
            print(f"[WARN] {rel} 补丁应用失败：{exc}（{desc}）")
            failed += 1
            continue
        if _sha256(patched) != after_sha:
            print(f"[WARN] {rel} 应用后摘要不符，已放弃写入（{desc}）")
            failed += 1
            continue
        path.write_text(patched, encoding="utf-8")
        changed += 1
        print(f"[PATCH] {rel} 已修改（{desc}）")
    return failed, changed


def main(argv: list[str]) -> int:
    check_only = "--check" in argv
    revert = "--revert" in argv
    if check_only and revert:
        raise SystemExit("--check 与 --revert 不能同时使用")

    pkg = locate_package()
    print(f"hy_memory: {pkg}")
    print(f"模式: {'撤销' if revert else '仅检查' if check_only else '应用'}")
    print()

    failed = 0
    changed = 0

    # 顺序是有语义的：
    #   应用 —— 先片段补丁再 diff 补丁（diff 的 before 摘要 = pristine + 片段补丁）；
    #   撤销 —— 反过来先撤 diff 再撤片段，否则 diff 的 after 摘要对不上，
    #           会误报「无法安全撤销」。
    if revert:
        f1, c1 = _apply_diffs(pkg, check_only=check_only, revert=True)
        f2, c2 = _apply_fragments(pkg, check_only=check_only, revert=True)
    else:
        f2, c2 = _apply_fragments(pkg, check_only=check_only, revert=False)
        f1, c1 = _apply_diffs(pkg, check_only=check_only, revert=False)
    failed = f1 + f2
    changed = c1 + c2

    print()
    if revert:
        print(f"撤销完成，{changed} 个文件已还原"
              + (f"，{failed} 项需要人工处理" if failed else ""))
    elif check_only:
        print("检查完成" + ("，有未应用的补丁" if failed else "，全部已应用"))
    else:
        print(f"完成，{changed} 个文件已修改"
              + (f"，{failed} 项需要人工处理" if failed else ""))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
