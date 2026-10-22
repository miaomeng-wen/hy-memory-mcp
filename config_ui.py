"""本地配置表单（Windows 版）：填 API Key 并写进本目录的 .env，然后自动测试连通性。

启动：
    .venv\\Scripts\\python.exe config_ui.py
浏览器打开 http://127.0.0.1:19528

只监听回环地址；key 写入磁盘后页面不再回显。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse

# Windows 控制台默认 cp1252/gbk，中文会抛 UnicodeEncodeError
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


PORT = int(os.getenv("CONFIG_UI_PORT", "19528"))
BASE_DIR = Path(__file__).resolve().parent
WORKDIR = Path(os.getenv("HYMEMORY_WORKDIR", str(BASE_DIR)))
ENV_FILE = WORKDIR / ".env"
ENV_BACKUP = WORKDIR / ".env.bak"

EMBEDDING_PRESETS = [
    {
        "id": "qwen-v4",
        "label": "千问 text-embedding-v4（推荐）",
        "model": "text-embedding-v4",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "dims": 1024,
    },
    {
        "id": "qwen-v3",
        "label": "千问 text-embedding-v3",
        "model": "text-embedding-v3",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "dims": 1024,
    },
    {
        "id": "openai-small",
        "label": "OpenAI text-embedding-3-small",
        "model": "text-embedding-3-small",
        "base_url": "https://api.openai.com/v1",
        "dims": 1536,
    },
    {
        "id": "ollama",
        "label": "本地 Ollama nomic-embed-text",
        "model": "nomic-embed-text",
        "base_url": "http://127.0.0.1:11434/v1",
        "dims": 768,
    },
]

LLM_PRESETS = [
    {
        "id": "buddy-wb-step",
        "label": "Buddy Switch · 国际版 Step-5-Preview（本地网关示例）",
        "model": "step-5-preview",
        "base_url": "http://127.0.0.1:57891/v1",
    },
    {
        "id": "buddy-wb-ds-sg",
        "label": "Buddy Switch · 国际版 DeepSeek V4.1 Flash （本地网关示例）",
        "model": "deepseek-v4.1-flash-sg",
        "base_url": "http://127.0.0.1:57891/v1",
    },
    {
        "id": "buddy-wb-ds-free",
        "label": "Buddy Switch · DeepSeek V4.1 Flash （本地网关示例）",
        "model": "deepseek-v4.1-flash",
        "base_url": "http://127.0.0.1:57891/v1",
    },
    {
        "id": "buddy-qoder",
        "label": "Buddy Switch · Qoder 网关（本机 57895）",
        "model": "deepseek-v4.1-flash",
        "base_url": "http://127.0.0.1:57895/v1",
    },
    {
        "id": "qwen-plus",
        "label": "千问 qwen-plus（同一把 key）",
        "model": "qwen-plus",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    },
    {
        "id": "samenone",
        "label": "暂不配置（先用 lite 模式）",
        "model": "",
        "base_url": "",
    },
]

app = FastAPI()


def mask(value: str) -> str:
    if not value:
        return "(未设置)"
    if len(value) <= 10:
        return "***"
    return f"{value[:6]}…{value[-4:]}"


def read_env() -> dict[str, str]:
    data: dict[str, str] = {}
    if not ENV_FILE.exists():
        return data
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        data[key.strip()] = val.strip().strip('"').strip("'")
    return data


def write_env(values: dict[str, str]) -> None:
    """原子写入 .env；覆盖前备份。"""
    WORKDIR.mkdir(parents=True, exist_ok=True)
    merged = read_env()
    merged.update({k: v for k, v in values.items() if v is not None})

    order = [
        "MEMORY_MODE",
        "MEMORY_DATA_DIR",
        "MEMORY_DEFAULT_USER_ID",
        "MEMORY_DEFAULT_AGENT_ID",
        "MEMORY_MCP_HOST",
        "MEMORY_MCP_PORT",
        "MEMORY_EMBEDDER_PROVIDER",
        "MEMORY_EMBEDDER_BASE_URL",
        "MEMORY_EMBEDDER_MODEL",
        "MEMORY_EMBEDDING_DIMS",
        "MEMORY_EMBEDDER_API_KEY",
        "MEMORY_LLM_PROVIDER",
        "MEMORY_LLM_BASE_URL",
        "MEMORY_LLM_MODEL",
        "MEMORY_LLM_API_KEY",
    ]
    keys = [k for k in order if k in merged] + [k for k in merged if k not in order]

    lines = ["# 由 config_ui.py 生成，请勿提交到版本库", ""]
    for k in keys:
        v = merged[k]
        if v == "":
            continue
        lines.append(f'{k}="{v}"')
    lines.append("")

    tmp = ENV_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    if ENV_FILE.exists():
        shutil.copy2(ENV_FILE, ENV_BACKUP)
    os.replace(tmp, ENV_FILE)


def _post(url: str, key: str, payload: dict, timeout: int = 60) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"ok": True, "status": resp.status, "json": json.loads(resp.read().decode("utf-8"))}
    except urllib.error.HTTPError as exc:
        return {
            "ok": False,
            "status": exc.code,
            "detail": exc.read().decode("utf-8", errors="replace")[:400],
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": None, "detail": f"{type(exc).__name__}: {exc}"}


def check_embedding(env: dict[str, str]) -> dict:
    base = env.get("MEMORY_EMBEDDER_BASE_URL", "").rstrip("/")
    model = env.get("MEMORY_EMBEDDER_MODEL", "")
    key = env.get("MEMORY_EMBEDDER_API_KEY", "")
    dims = int(env.get("MEMORY_EMBEDDING_DIMS") or 0)

    if not (base and model and key):
        return {"ok": False, "error": "配置不完整：base_url / model / api_key 三者都必须有"}

    r = _post(f"{base}/embeddings", key, {"model": model, "input": "连通性测试"})
    if not r["ok"]:
        return {"ok": False, "error": f"HTTP {r.get('status')}", "detail": r.get("detail", "")}

    try:
        vec = r["json"]["data"][0]["embedding"]
    except (KeyError, IndexError, TypeError):
        return {"ok": False, "error": "响应里没有向量", "detail": str(r["json"])[:300]}

    actual = len(vec)
    if dims and actual != dims:
        return {
            "ok": False,
            "error": f"维度不一致：配置 {dims}，实际 {actual}",
            "hint": f"请把 EMBEDDING_DIMS 改成 {actual}",
            "actual_dims": actual,
        }
    return {"ok": True, "dims": actual, "model": r["json"].get("model")}


def check_llm(env: dict[str, str]) -> dict:
    base = env.get("MEMORY_LLM_BASE_URL", "").rstrip("/")
    model = env.get("MEMORY_LLM_MODEL", "")
    key = env.get("MEMORY_LLM_API_KEY", "")
    if not (base and model and key):
        return {"ok": True, "skipped": True, "note": "未配置 LLM"}

    r = _post(
        f"{base}/chat/completions",
        key,
        {"model": model, "messages": [{"role": "user", "content": "回复OK"}], "max_tokens": 8},
        timeout=60,
    )
    if not r["ok"]:
        hint = ""
        st = r.get("status")
        if st in (401, 403):
            hint = "key 无效或没有该模型的权限"
        elif st is None:
            hint = "连不上，检查 base_url 端口与网关是否已启动"
        elif st == 404:
            hint = "模型名可能写错"
        return {"ok": False, "error": f"HTTP {st}", "hint": hint, "detail": r.get("detail", "")}

    try:
        content = r["json"]["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        content = str(r["json"])[:200]
    return {"ok": True, "model": r["json"].get("model"), "reply": content}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    cur = read_env()
    state = {
        "embedder_base": cur.get("MEMORY_EMBEDDER_BASE_URL", EMBEDDING_PRESETS[0]["base_url"]),
        "embedder_model": cur.get("MEMORY_EMBEDDER_MODEL", EMBEDDING_PRESETS[0]["model"]),
        "dims": cur.get("MEMORY_EMBEDDING_DIMS", "1024"),
        "embedder_key_masked": mask(cur.get("MEMORY_EMBEDDER_API_KEY", "")),
        "mode": cur.get("MEMORY_MODE", "lite"),
        "user_id": cur.get("MEMORY_DEFAULT_USER_ID", "default"),
        "llm_base": cur.get("MEMORY_LLM_BASE_URL", ""),
        "llm_model": cur.get("MEMORY_LLM_MODEL", ""),
        "llm_key_masked": mask(cur.get("MEMORY_LLM_API_KEY", "")),
        "env_path": str(ENV_FILE),
        "env_exists": ENV_FILE.exists(),
    }
    return (
        HTML_TEMPLATE.replace("__PRESETS__", json.dumps(EMBEDDING_PRESETS, ensure_ascii=False))
        .replace("__LLM_PRESETS__", json.dumps(LLM_PRESETS, ensure_ascii=False))
        .replace("__STATE__", json.dumps(state, ensure_ascii=False))
    )


@app.post("/save")
def save(
    embedder_preset: str = Form(...),
    embedder_key: str = Form(""),
    embedder_base: str = Form(""),
    embedder_model: str = Form(""),
    dims: str = Form("1024"),
    mode: str = Form("lite"),
    user_id: str = Form("default"),
    llm_preset: str = Form("samenone"),
    llm_key: str = Form(""),
    llm_base: str = Form(""),
    llm_model: str = Form(""),
) -> JSONResponse:
    cur = read_env()

    preset = next((p for p in EMBEDDING_PRESETS if p["id"] == embedder_preset), None)
    final_key = embedder_key.strip() or cur.get("MEMORY_EMBEDDER_API_KEY", "")
    final_base = embedder_base.strip() or (preset["base_url"] if preset else "")
    final_model = embedder_model.strip() or (preset["model"] if preset else "")

    if not final_key:
        return JSONResponse({"ok": False, "error": "Embedding API Key 不能为空"}, status_code=200)
    if not final_base or not final_model:
        return JSONResponse({"ok": False, "error": "Embedding base_url / model 不能为空"}, status_code=200)

    try:
        dims_int = int(dims)
        if dims_int <= 0:
            raise ValueError
    except ValueError:
        return JSONResponse({"ok": False, "error": "维度必须是正整数"}, status_code=200)

    lp = next((p for p in LLM_PRESETS if p["id"] == llm_preset), None)
    final_llm_key = llm_key.strip() or cur.get("MEMORY_LLM_API_KEY", "")
    final_llm_base = llm_base.strip() or (lp["base_url"] if lp else "")
    final_llm_model = llm_model.strip() or (lp["model"] if lp else "")

    values = {
        "MEMORY_MODE": mode,
        "MEMORY_DATA_DIR": str(WORKDIR / "data"),
        "MEMORY_DEFAULT_USER_ID": user_id.strip() or "default",
        "MEMORY_DEFAULT_AGENT_ID": "default_agent",
        "MEMORY_MCP_HOST": "127.0.0.1",
        "MEMORY_MCP_PORT": "19527",
        "MEMORY_EMBEDDER_PROVIDER": "openai",
        "MEMORY_EMBEDDER_BASE_URL": final_base,
        "MEMORY_EMBEDDER_MODEL": final_model,
        "MEMORY_EMBEDDING_DIMS": str(dims_int),
        "MEMORY_EMBEDDER_API_KEY": final_key,
        "MEMORY_LLM_PROVIDER": "openai" if final_llm_model else "",
        "MEMORY_LLM_BASE_URL": final_llm_base,
        "MEMORY_LLM_MODEL": final_llm_model,
        "MEMORY_LLM_API_KEY": final_llm_key,
    }
    write_env(values)

    emb = check_embedding(values)
    llm = check_llm(values)
    all_ok = emb.get("ok") and llm.get("ok")
    return JSONResponse(
        {
            "ok": bool(all_ok),
            "saved": True,
            "env_path": str(ENV_FILE),
            "embedding": emb,
            "llm": llm,
        }
    )


@app.get("/current")
def current() -> JSONResponse:
    cur = read_env()
    safe = {k: (mask(v) if "KEY" in k else v) for k, v in cur.items()}
    return JSONResponse({"env_path": str(ENV_FILE), "values": safe})


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hy-Memory 配置</title>
<style>
  :root{--bg:#0f1115;--card:#181b21;--line:#2a2f38;--fg:#e6e8ec;--muted:#8b93a1;
        --acc:#4f8cff;--ok:#35c07a;--err:#f0566c}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
       padding:32px 16px}
  .wrap{max-width:720px;margin:0 auto}
  h1{font-size:20px;margin:0 0 4px}
  .sub{color:var(--muted);font-size:13px;margin-bottom:24px;word-break:break-all}
  .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px;margin-bottom:16px}
  .card h2{font-size:14px;margin:0 0 14px;color:#aab2c0;font-weight:600;letter-spacing:.3px}
  label{display:block;font-size:12px;color:var(--muted);margin:12px 0 5px}
  input,select{width:100%;padding:9px 11px;background:#101317;color:var(--fg);
    border:1px solid var(--line);border-radius:7px;font-size:13px;font-family:ui-monospace,monospace}
  input:focus,select:focus{outline:none;border-color:var(--acc)}
  input::placeholder{color:#4d5563}
  .hint{font-size:11.5px;color:var(--muted);margin-top:5px}
  .row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
  button{width:100%;margin-top:20px;padding:11px;border:0;border-radius:8px;
    background:var(--acc);color:#fff;font-size:14px;font-weight:600;cursor:pointer}
  button:hover{filter:brightness(1.08)}
  button:disabled{opacity:.55;cursor:default}
  .result{margin-top:16px;padding:13px 15px;border-radius:8px;font-size:13px;display:none;white-space:pre-wrap}
  .result.show{display:block}
  .result.ok{background:rgba(53,192,122,.1);border:1px solid rgba(53,192,122,.35);color:var(--ok)}
  .result.err{background:rgba(240,86,108,.1);border:1px solid rgba(240,86,108,.35);color:var(--err)}
  .badge{display:inline-block;padding:1px 7px;border-radius:20px;font-size:11px;margin-left:6px}
  .badge.set{background:rgba(53,192,122,.16);color:var(--ok)}
  .badge.unset{background:rgba(139,147,161,.16);color:var(--muted)}
  .foot{color:#5d6572;font-size:11.5px;text-align:center;margin-top:20px}
</style>
</head>
<body>
<div class="wrap">
  <h1>Hy-Memory 配置</h1>
  <div class="sub" id="envpath"></div>

  <form id="f">
    <div class="card">
      <h2>EMBEDDING（必填）</h2>
      <label>预设</label>
      <select id="embedder_preset" name="embedder_preset"></select>

      <label>API Key <span id="ekey_badge"></span></label>
      <input id="embedder_key" name="embedder_key" type="password"
             placeholder="sk-... （留空则沿用已保存的 key）" autocomplete="off" spellcheck="false">

      <label>Base URL</label>
      <input id="embedder_base" name="embedder_base" spellcheck="false">

      <div class="row">
        <div>
          <label>模型</label>
          <input id="embedder_model" name="embedder_model" spellcheck="false">
        </div>
        <div>
          <label>向量维度</label>
          <input id="dims" name="dims" type="number" min="1" spellcheck="false">
        </div>
      </div>
      <div class="hint">维度必须和模型实际输出一致。text-embedding-v4 是 1024。</div>
    </div>

    <div class="card">
      <h2>LLM（选填，pro/ultra 模式需要）</h2>
      <label>预设</label>
      <select id="llm_preset" name="llm_preset"></select>
      <div class="row">
        <div>
          <label>API Key <span id="lkey_badge"></span></label>
          <input id="llm_key" name="llm_key" type="password" placeholder="留空沿用" autocomplete="off" spellcheck="false">
        </div>
        <div>
          <label>模型</label>
          <input id="llm_model" name="llm_model" spellcheck="false">
        </div>
      </div>
      <label>Base URL</label>
      <input id="llm_base" name="llm_base" spellcheck="false">
      <div class="hint">Buddy Switch 网关须先在 App 里打开；本机地址用 127.0.0.1。</div>
    </div>

    <div class="card">
      <h2>其它</h2>
      <div class="row">
        <div>
          <label>模式</label>
          <select id="mode" name="mode">
            <option value="lite">lite — 零 LLM，先跑通</option>
            <option value="pro">pro — 完整抽取</option>
            <option value="ultra">ultra — 含图记忆</option>
          </select>
        </div>
        <div>
          <label>默认用户 ID</label>
          <input id="user_id" name="user_id" spellcheck="false">
        </div>
      </div>
      <div class="hint">所有智能体默认读写这个 user 的记忆。</div>
    </div>

    <button id="btn" type="submit">保存并验证</button>
    <div id="result" class="result"></div>
  </form>

  <div class="foot">仅监听 127.0.0.1 · key 写入磁盘后不再页面回显</div>
</div>

<script>
const EMB = __PRESETS__;
const LLM = __LLM_PRESETS__;
const S = __STATE__;
const $ = id => document.getElementById(id);

$("envpath").textContent = "写入位置：" + S.env_path;

function fillSelect(sel, list, currentModel){
  sel.innerHTML = list.map(p=>`<option value="${p.id}">${p.label}</option>`).join("");
  const hit = list.findIndex(p => p.model && p.model === currentModel);
  if (hit >= 0) sel.selectedIndex = hit;
}
fillSelect($("embedder_preset"), EMB, S.embedder_model);
fillSelect($("llm_preset"), LLM, S.llm_model);

$("embedder_base").value = S.embedder_base;
$("embedder_model").value = S.embedder_model;
$("dims").value = S.dims;
$("mode").value = S.mode;
$("user_id").value = S.user_id;
$("llm_base").value = S.llm_base;
$("llm_model").value = S.llm_model;

// 已保存过 LLM：按 base_url + model 反选；未保存过则默认选 Buddy Switch 国际版（第一项）
if (!S.llm_model) {
  $("llm_preset").selectedIndex = 0;
} else {
  const exact = LLM.findIndex(p => p.base_url === S.llm_base && p.model === S.llm_model);
  const byBase = LLM.findIndex(p => p.base_url === S.llm_base);
  const idx = exact >= 0 ? exact : byBase;
  if (idx >= 0) $("llm_preset").selectedIndex = idx;
}

function badge(el, masked){
  if (!masked || masked.startsWith("(")) { el.className="badge unset"; el.textContent="未设置"; }
  else { el.className="badge set"; el.textContent="已设置 " + masked; }
}
badge($("ekey_badge"), S.embedder_key_masked);
badge($("lkey_badge"), S.llm_key_masked);

// 把某个预设的值填进对应输入框
function applyEmbedderPreset(id){
  const p = EMB.find(x=>x.id===id);
  if (!p) return;
  $("embedder_base").value = p.base_url;
  $("embedder_model").value = p.model;
  $("dims").value = p.dims;
}
function applyLlmPreset(id){
  const p = LLM.find(x=>x.id===id);
  if (!p) return;
  $("llm_base").value = p.base_url;
  // 模型为空时清空输入框，避免残留上一个预设的模型名
  $("llm_model").value = p.model || "";
}

$("embedder_preset").onchange = e => applyEmbedderPreset(e.target.value);
$("llm_preset").onchange = e => applyLlmPreset(e.target.value);

// 首次加载：若字段为空，用当前选中的预设补齐（onchange 不会自动触发）
if (!$("embedder_base").value || !$("embedder_model").value) {
  applyEmbedderPreset($("embedder_preset").value);
}
if (!$("llm_base").value && !$("llm_model").value) {
  applyLlmPreset($("llm_preset").value);
}

$("f").onsubmit = async ev => {
  ev.preventDefault();
  const btn = $("btn"), box = $("result");
  btn.disabled = true; btn.textContent = "保存并验证中…";
  box.className = "result";
  try {
    const res = await fetch("/save", {method:"POST", body:new FormData($("f"))});
    const d = await res.json();
    const e = d.embedding || {}, l = d.llm || {};
    const lines = [];
    if (d.error) lines.push("✗ " + d.error);
    if (e.ok) lines.push(`✓ Embedding  ${e.model} · ${e.dims} 维`);
    else lines.push(`✗ Embedding  ${e.error || ""}${e.hint ? "\n   " + e.hint : ""}${e.detail ? "\n   " + e.detail : ""}`);
    if (l.skipped) lines.push("· LLM 未配置（lite 模式可忽略）");
    else if (l.ok) lines.push(`✓ LLM  ${l.model} · 回复「${(l.reply||"").slice(0,20)}」`);
    else lines.push(`✗ LLM  ${l.error || ""}${l.hint ? "\n   " + l.hint : ""}${l.detail ? "\n   " + l.detail : ""}`);
    if (d.saved) lines.push(`\n已写入 ${d.env_path}`);
    box.className = "result show " + (d.ok ? "ok" : "err");
    box.textContent = lines.join("\n");
    if (d.saved) {
      const r = await fetch("/current").then(x=>x.json());
      badge($("ekey_badge"), r.values.MEMORY_EMBEDDER_API_KEY);
      badge($("lkey_badge"), r.values.MEMORY_LLM_API_KEY);
      $("embedder_key").value = ""; $("llm_key").value = "";
    }
  } catch (err) {
    box.className = "result show err";
    box.textContent = "请求失败: " + err;
  } finally {
    btn.disabled = false; btn.textContent = "保存并验证";
  }
};
</script>
</body>
</html>
"""


if __name__ == "__main__":
    WORKDIR.mkdir(parents=True, exist_ok=True)
    print(f"配置界面: http://127.0.0.1:{PORT}")
    print(f"写入位置: {ENV_FILE}")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
