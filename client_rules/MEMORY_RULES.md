# 长期记忆使用规则（Hy-Memory MCP）

本机运行一个本地记忆服务：`http://127.0.0.1:19527/mcp`，提供 `memory_add` / `memory_search` /
`memory_profile` / `memory_health` / `memory_import` / `memory_import_status` 六个工具。
它跨工具共享同一份长期记忆（Codex / DeepSeek Harness / ZCode / Cursor 写到同一个库）。

## 何时检索

涉及用户偏好、历史决定、项目约定、上次结论的问题，先 `memory_search` 查一次再用，不要让用户重复交代。
检索时显式带上 `layers`（默认已含 `l1_raw`）：

```
memory_search(query="...", user_id="default", limit=5,
              layers=["l1_raw","l2_fact","l3_summary","l4_identity"])
```

- 返回值里 `ok:false` 才是链路失败；`ok:true, count:0` 是**真的没有**，不是失败。
- `error_kind` 含义：`sdk_read_failed` = SDK 显式失败但没透出原因；`wrapper_exception` = 包装层异常；
  `sdk_unexpected` = SDK 返回结构异常。三种都属于失败，不要当成"没有记忆"。

## 何时写入

只在满足下列**具体触发条件**时写一次，写之前先 `memory_search` 确认没有同义条目：

| 触发条件 | 例子 |
| --- | --- |
| 用户明确表达长期偏好 | 「以后回答都用中文」「我不喜欢 emoji」 |
| 用户确认了某个项目决策 | 「就按方案 B 做」「这个坑记住」 |
| 用户纠正了旧事实 | 「那个端口不是 8080，是 19527」 |
| 任务形成了已验证的可复用结论 | 实测通过的配置、命令、路径 |

**不要写**：一般问答、临时进度播报（「正在读取文件…」）、未执行的建议、你的猜测、从没验证过的结论。
同一任务的同一结论只写一次，不要反复写。

## 写 raw 还是 facts

- `memory_add(mode="raw")`（默认）：原始对话。**pro 模式下会调用一次 LLM 抽取**，慢（20–50 秒/条）
  且本机模型有失败率；`lite` 模式零 LLM 但只写入 `l1_raw`。
- `memory_add(mode="facts")`：你已经整理好的事实结论。走 `add_extracted`，跳过抽取直接 reconcile，
  仍会调用一次 LLM（pro 模式）。**适合**在任务结束时写「已实施且验证」的配置与决策。
- 批量事实可以一次传列表：`content=["事实1","事实2",...]`，一批只花一次 LLM 调用。

## 来源标注

写入时带上 `source`（标明来自哪个工具：`codex` / `dsh` / `zcode` / `cursor`）和 `session_id`，
便于日后追溯。不要写凭据、密钥、令牌、完整身份证号等敏感信息。

## 边界

- 记忆服务不可用（`memory_health` 失败）时**不要阻塞主任务**，正常交付并一句话说明即可。
- 安装并启用后台计划任务 `HyMemoryImport` 后，Codex / DeepSeek Harness / ZCode 历史会话每 5 分钟增量导入（零 LLM），不需要手工回填；当前不包含 Cursor 历史会话。
