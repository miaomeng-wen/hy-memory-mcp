# hy-memory-mcp

基于**腾讯混元开源项目 [Hy-Memory](https://github.com/Tencent-Hunyuan/Hy-Memory)** 开发的 Windows 本地共享记忆服务。
通过 MCP HTTP 接口，让多个 AI 客户端访问同一份长期记忆，并支持 Codex、DeepSeek Harness、ZCode
历史会话的增量导入。个人维护，非腾讯官方项目。

## 功能与边界

- 六个 MCP 工具：`memory_add`、`memory_search`、`memory_profile`、`memory_health`、
  `memory_import`、`memory_import_status`。
- 支持 MCP HTTP 的客户端可配置接入；历史文件解析适配器目前只有 Codex、DeepSeek Harness、ZCode。
- 历史导入包含去重、进度保存、重试、每日预算与单实例锁，由服务进程写入记忆库。
- 包含本地配置界面、Windows 管理脚本和针对 `hy-memory==2.0.3` 的可靠性补丁。
- 当前以 Windows + Python 3.12 为目标；客户端历史格式变化可能需要更新适配器。

## 快速开始

在项目目录打开 PowerShell，准备 Python 3.12：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe apply_patches.py
.\.venv\Scripts\python.exe apply_patches.py --check
Copy-Item -LiteralPath .env.example -Destination .env
```

编辑 `.env`，填写自己的 Embedding API Key、地址、模型和匹配的向量维度。
示例默认 `lite`，无需配置 LLM；`pro` / `ultra` 还需要填写 LLM 地址、模型和 API Key。
`lite` 与历史导入不调用 LLM，但仍需计算向量，使用远程 Embedding 时可能收费。

也可运行 `open_config.bat`，打开 `http://127.0.0.1:19528` 配置表单。
表单内预设只是填写示例，供应商、网关、模型及费用以你自己的服务为准。
“保存并验证”会向配置的服务发送测试请求，配置变更后需重启记忆服务。

```powershell
.\.venv\Scripts\python.exe memory_server.py
```

保持当前工作目录为项目目录，避免 `.env` 示例中的相对数据路径落到其他位置。
默认 MCP 地址：`http://127.0.0.1:19527/mcp`。
运行数据写在项目的 `data/` 下；SDK 可能在其中继续创建 `data/` 子目录。
更换 Embedding 模型或维度前，应备份旧库并为新配置使用独立数据目录。

## 客户端接入

在客户端的 MCP 设置中添加 HTTP 服务地址。不同客户端配置结构不同；通用示例：

```json
{
  "mcpServers": {
    "memory": {
      "url": "http://127.0.0.1:19527/mcp"
    }
  }
}
```

这是通用示例，不保证直接适配每个客户端。按照客户端自己的 HTTP MCP 设置填写。
可参考 `client_rules/MEMORY_RULES.md` 添加记忆使用规则；其中的 `user_id`
应与 `.env` 中的 `MEMORY_DEFAULT_USER_ID` 一致。

检索会默认包含 `l1_raw`，也可以显式传入：

```text
memory_search(query="项目约定", user_id="default", limit=5,
              layers=["l1_raw","l2_fact","l3_summary","l4_identity"])
```

`ok=true, count=0` 表示无匹配；`ok=false` 表示失败，需要检查 `error_kind`。
`memory_add(mode="facts")` 跳过事实抽取，但在 pro 模式下协调写入仍可能调用 LLM。

### Cursor

先启动记忆服务，再编辑全局配置 `~/.cursor/mcp.json`
（Windows：`%USERPROFILE%\.cursor\mcp.json`），将上面 JSON 示例中的 `memory`
条目合并到现有 `mcpServers`，保留其他服务器配置。
该配置供所有项目使用；修改后打开或重新加载 Cursor，确认 `memory` 已连接并发现六个工具。
服务需要保持运行，Cursor 直接通过 Streamable HTTP 连接，无需再启动一份记忆服务。

要让当前项目中的 Agent 按规则调用记忆，在该项目创建 `.cursor/rules/hy-memory.mdc`，
先写入以下 frontmatter，再在其后粘贴 [MEMORY_RULES.md](client_rules/MEMORY_RULES.md) 的完整内容：

```mdc
---
description: Hy-Memory shared long-term memory
alwaysApply: true
---
```

将规则中的 `user_id="default"` 改为与自己的 `MEMORY_DEFAULT_USER_ID` 一致；
从 Cursor 写入时使用 `source="cursor"` 并带上 `session_id`。
项目规则仅在对应项目生效；其他项目需要各自添加，也可将规则正文加入 Cursor 的 User Rules。
MCP 配置和记忆调用规则分别决定工具是否可用、Agent 何时调用工具。

可在 Agent 中输入：「调用 memory_search 查询项目约定，user_id 使用我配置的用户，
layers 使用 l1_raw、l2_fact、l3_summary、l4_identity。」
按需批准 Cursor 提示的工具调用，并确认返回 `ok=true`；`count=0` 也表示请求成功。

2026-10-22 在 Windows 的 Cursor 3.26.16 上已验证连接成功，工具快照显示
`connected`、`toolCount=6`，且六个工具描述均已生成；独立 HTTP 客户端运行
`test_mcp.py --no-write` 全部通过，包含深度检索检查。本次未通过 Cursor 对话验证写入。
**Cursor 可以调用共享记忆，但 Cursor 历史聊天尚未纳入自动导入**；
当前历史导入仍只覆盖 Codex、DeepSeek Harness、ZCode。

参考：[Cursor MCP 文档](https://cursor.com/docs/mcp)、[Cursor Rules 文档](https://cursor.com/docs/rules)。

## 历史导入与自动启动

历史导入会读取当前用户的客户端会话。先确认需要导入哪些来源，再手动触发或安装计划任务。
来源路径覆盖变量见 `.env.example`。DeepSeek Harness 的压缩会话还需要支持
`zlib.zstdDecompressSync` 的 Node.js，可通过 `HYIMPORT_NODE` 指定可执行文件。

```powershell
.\.venv\Scripts\python.exe run_import.py --status
.\.venv\Scripts\python.exe run_import.py --max-writes 20
```

默认单次最多写 50 条、软预算 120 秒、每日最多 500 条。软预算限制新写入的启动，
已开始的请求可能在预算之后返回。客户端等待超时也不代表服务内导入已停止。

确认手动运行正常后，再按需安装计划任务：

```powershell
# 服务登录自启：需管理员 PowerShell
powershell -ExecutionPolicy Bypass -File .\install_autostart.ps1
# 历史导入：当前用户登录期间每 5 分钟运行
powershell -ExecutionPolicy Bypass -File .\install_import.ps1
```

卸载分别运行 `uninstall_autostart.ps1` 和 `uninstall_import.ps1`。
默认任务名为 `HyMemoryMCP`、`HyMemoryImport`；安装脚本可能替换同名任务。
若本机已有一套服务，请勿同时启动本副本或重复注册任务。
`service.bat` 提供管理菜单；`stop_server.bat` 按默认端口结束监听进程，使用前确认端口归属。

## 测试和诊断

安装依赖并应用补丁后，运行现有离线测试（假客户端/存储与临时样本）：

```powershell
.\.venv\Scripts\python.exe -m unittest test_importer test_server test_contract test_sdk_repairs
```

启动服务后，可另外运行只读 MCP 冒烟检查：

```powershell
.\.venv\Scripts\python.exe test_mcp.py --no-write
powershell -ExecutionPolicy Bypass -File .\doctor.ps1
```

`test_mcp.py` 不带 `--no-write` 时会写入测试用户，导入命令会写入实际记忆库。
完整的全新环境安装、真实供应商请求和所有客户端组合仍需分别验证。

## 数据与隐私

服务默认只监听本机回环地址，面向本机可信客户端，未提供多用户身份认证和隔离。
不要直接作为公网或共享网络服务部署。数据保存在本地，但配置远程 Embedding/LLM 时，
相应文本会发送给该供应商；这不是完全离线运行方案。

导入器会过滤部分凭据格式，但不能保证识别所有敏感内容。记忆库、日志、导入状态、
`.env` 和备份都可能包含私密信息，已列入 `.gitignore`，不应上传。
发布前仍需检查实际待提交文件及 Git 历史；忽略规则不会移除已经提交的文件。

## 上游与许可证

上游 Hy-Memory 由腾讯混元开源，使用 Apache-2.0 许可证。本项目同样采用 Apache-2.0，见 `LICENSE`。
Hy-Memory SDK 通过依赖安装，不随仓库打包；本仓库中的上游源码片段和修改补丁保留腾讯版权与来源说明。
上游许可证原文副本见 `third_party/HY-MEMORY-LICENSE`，来源与修改说明见 `NOTICE`。

本项目的独立扩展包括 MCP 服务、会话增量导入、Windows 部署管理和配套测试；SDK 修改内容在
`apply_patches.py` 与各补丁文件中列明。本项目由个人独立维护，不代表腾讯或腾讯混元官方，
也不表示获得其认可或背书。
补丁仅适配 2.0.3，升级 SDK 后应重新验证，不能假定补丁可直接沿用。
