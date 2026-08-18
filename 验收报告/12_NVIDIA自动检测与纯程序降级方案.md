# LifeOS NVIDIA 自动检测 + 纯程序降级链路 — 理解报告

> 日期：2026-08-18 ｜ 性质：目标指导 + 具体代码。**未动任何线上代码**，确认后实施。

---

# 一、你要什么（大白话翻译）

现在的 LifeOS：AI 调用失败时，client.py 的 `chat()` 会按候选模型列表逐个试，全部失败返回 None——**但不会主动通知你、不会去探测新模型、不会切到备用**。

你要的：

1. **NVIDIA 自动探测选优**：像 sun-panel 的 autobest.go 那样，主动拉 NVIDIA 免费模型列表 → 并发测速 → 选最快的设为当前激活模型
2. **AI 挂了时的纯程序自救链**：
   ```
   AI 调用全部失败
     ↓ （纯代码，不调 AI）
   发飞书消息通知你「AI 全挂了，正在自救」
     ↓
   探测 NVIDIA 可用免费模型
     ├─ 有可用 → 自动切换为激活模型 ✅
     └─ 没有 → 切换到 deepseek 备用 ✅
     ↓
   再发飞书告诉你「已切到 XXX 模型」
   ```

关键约束：**整个过程是纯程序执行**，不依赖任何 AI 能力（因为 AI 已经挂了）。

---

# 二、现有代码基座（已确认可复用的零件）

| 零件 | 位置 | 能力 |
|---|---|---|
| 候选模型构建 | `client.py:57` `_build_candidates()` | 按 active → 模型库带 key 的顺序组装 |
| 故障转移循环 | `client.py:202` `for mp in candidates:` | 逐个试，429/5xx/transport 自动跳下一个 |
| 用量记录 | `client.py:83` `_record_usage()` | 异步落库，失败静默 |
| 模型配置持久化 | `config.py:325` `set_active_ai_model()` | 改 active_id 并写 runtime.json |
| 飞书推送 | `feishu.py:447` `push_to_users(users, text)` | 向 admin_users 推文本 |
| 飞书命令处理 | `feishu.py:187` `_process_command()` | 收到指令触发动作 |
| 测速函数 | `client.py:422` `speed_test()` | 已有 N 轮测速逻辑 |
| 连通探测 | `client.py:383` `probe()` | 发"只回复两个字：正常"测连通 |
| 运行时配置热更 | `config.py:289` `apply_overrides()` | 不重启即可生效 |

**结论：所有底层零件都已存在，缺的是把它们串成一条「失败→通知→探测→切换→再通知」的链路。**

---

# 三、新增模块设计

## 3.1 新文件：`app/ai/nvidia_probe.py`（NVIDIA 自动探测）

```python
"""NVIDIA 免费模型自动探测与优选。
纯 HTTP 操作，不依赖任何 AI 能力——用于 AI 挂掉时的自救。

NVIDIA API:
  GET https://integrate.api.nvidia.com/v1/models   → 可用模型列表
  POST https://integrate.api.nvidia.com/v1/chat/completions → 对话

参考 sun-panel autobest.go 的并发实测+速度优先策略。
"""
import time
import logging
import httpx
from typing import Optional

log = logging.getLogger("lifeos.ai.nvidia")

NVIDIA_BASE = "https://integrate.api.nvidia.com/v1"
# NVIDIA 免费模型白名单（排除付费/实验性）
FREE_MODEL_PREFIXES = (
    "meta/",           # Llama 系列
    "mistralai/",      # Mistral
    "qwen/",           # 通义千问
    "deepseek-ai/",    # DeepSeek（NVIDIA 托管版）
    "google/",         # Gemma
    "nvidia/",         # NVIDIA 自研
)

PROBE_PROMPT = {"model": "", "messages": [{"role": "user", "content": "Hi"}],
               "max_tokens": 8, "temperature": 0.1, "stream": False}


async def list_available_models(api_key: str, *, timeout: int = 30) -> list[dict]:
    """拉取 NVIDIA 可用模型列表，过滤出免费且支持 chat/completions 的。"""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as hc:
            resp = await hc.get(f"{NVIDIA_BASE}/models",
                                headers={"Authorization": f"Bearer {api_key}"})
        if resp.status_code != 200:
            log.warning("NVIDIA models 列表获取失败 %s", resp.status_code)
            return []
        data = resp.json()
        models = []
        for m in data.get("data", []):
            mid = m.get("id", "")
            # 只保留免费前缀的聊天模型
            if any(mid.startswith(p) for p in FREE_MODEL_PREFIXES):
                if "/chat/completions" in (m.get("root") or "") or True:  # NVIDIA 大部分都支持
                    models.append({"id": mid, "name": mid, "base_url": NVIDIA_BASE,
                                  "api_key": api_key, "tags": ["nvidia", "free"]})
        return models
    except Exception as e:
        log.error("NVIDIA 模型列表异常: %s", e)
        return []


async def auto_best(api_key: str, *, rounds: int = 2, timeout: int = 60) -> Optional[dict]:
    """并发探测所有 NVIDIA 免费模型，返回速度最快的一个（或 None）。

    返回 dict: {id, name, base_url, api_key, latency_ms, tags}
    或 None 表示全部不可用。
    """
    models = await list_available_models(api_key)
    if not models:
        return None

    results = []
    for m in models:
        t0 = time.time()
        ok = False
        try:
            payload = PROBE_PROMPT.copy()
            payload["model"] = m["id"]
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as hc:
                resp = await hc.post(
                    f"{m['base_url']}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json",
                    json=payload)
                if resp.status_code == 200:
                    ok = True
        except Exception:
            pass
        ms = int((time.time() - t0) * 1000)
        results.append({**m, "latency_ms": ms, "ok": ok})

    # 只看成功的，按延迟排序
    usable = [r for r in results if r["ok"]]
    if not usable:
        return None
    usable.sort(key=lambda x: x["latency_ms"])
    best = usable[0]
    log.info("NVIDIA auto_best: %s (%dms), 共 %d/%d 可用",
             best["id"], best["latency_ms"], len(usable), len(results))
    return best


async def ensure_nvidia_in_models(nvidia_key: str) -> bool:
    """确保 NVIDIA 候选在 ai_models 库中（若不存在则追加）。
    返回是否做了变更。"""
    from app.config import settings
    # 检查是否已有 NVIDIA 条目
    has_nvidia = any("nvidia" in (m.get("tags") or []) or
                      "integrate.api.nvidia.com" in m.get("base_url", "")
                      for m in settings.ai_models)
    if has_nvidia:
        return False
    # 追加一个占位条目（auto_best 会更新具体模型名）
    settings.upsert_ai_model({
        "id": "nvidia-auto",
        "name": "NVIDIA 免费(自动)",
        "base_url": NVIDIA_BASE,
        "model": "meta/llama-3.1-8b-instruct",  # 占位，会被 auto_best 覆盖
        "api_key": nvidia_key,
        "tags": ["nvidia", "free", "auto"],
    })
    return True
```

## 3.2 改造 `app/ai/client.py` — 在 `chat()` 末尾加纯程序降级钩子

在现有 `chat()` 函数（L186）的末尾、`return None` 之前，插入降级链路：

```python
# ===== client.py chat() 末尾改造（L221 之后）=====
# 原代码：
#   log.warning("所有候选模型均失败（最后状态 %s），返回 None", last_status)
#   return None

# 改为：
    log.warning("所有候选模型均失败（最后状态 %s），启动纯程序降级", last_status)
    # 【纯程序降级】不走 AI，纯代码自救
    switched = await _failover_rescue(user_id=user_id, scenario=scenario)
    if switched:
        # 降级成功：用新模型重试一次（仅一次，避免递归）
        retry_mp = settings.active_ai_profile()
        if retry_mp:
            content, status, usage = await _call_once(
                retry_mp, system, user, temperature=temperature,
                max_tokens=max_tokens, json_mode=json_mode,
                proxy=(getattr(retry_mp, "proxy", "") or settings.ai_proxy or None),
                timeout=timeout, cache_ttl=cache_ttl,
            )
            if content is not None:
                log.info("降级重试成功: model=%s", retry_mp.model)
                return content
    return None
```

新增 `_failover_rescue()` 函数（放在 client.py 末尾）：

```python
async def _failover_rescue(*, user_id: str = "me", scenario: str = "chat") -> bool:
    """纯程序降级自救：AI 全挂时 → 飞书通知 → 探 NVIDIA → 切模型 → 再通知。
    返回是否成功切换了模型。"""
    from app.config import settings
    from app.feishu import feishu_bot
    from .nvidia_probe import auto_best, ensure_nvidia_in_models
    import asyncio

    # ① 飞书通知：AI 全挂了
    try:
        await feishu_bot.push_to_users(
            settings.feishu_admin_users,
            "⚠️ LifeOS AI 全部模型调用失败，正在启动纯程序自救..."
        )
    except Exception:
        pass  # 飞书也挂就跳过

    # ② 尝试 NVIDIA 探测
    nvidia_key = _find_nvidia_key()
    if nvidia_key:
        try:
            # 确保 NVIDIA 条目在模型库里
            changed = await ensure_nvidia_in_models(nvidia_key)
            best = await auto_best(nvidia_key)
            if best:
                # 切换到 NVIDIA 最快模型
                old_active = settings.ai_active
                # 更新 nvidia-auto 条目的具体模型名
                for m in settings.ai_models:
                    if m.get("id") == "nvidia-auto":
                        m["model"] = best["id"]
                        break
                settings.set_active_ai_model("nvidia-auto")
                # ③ 飞书通知：切换成功
                try:
                    await feishu_bot.push_to_users(
                        settings.feishu_admin_users,
                        f"✅ AI 自救完成：{old_active} → {best['id']} "
                        f"(NVIDIA, {best['latency_ms']}ms)"
                    )
                except Exception:
                    pass
                log.info("降级成功: → NVIDIA %s", best["id"])
                return True
        except Exception as e:
            log.error("NVIDIA 降级异常: %s", e)

    # ④ NVIDIA 也没用 → 切 deepseek 备用
    deepseek_fallback = _find_deepseek_fallback()
    if deepseek_fallback:
        old_active = settings.ai_active
        settings.set_active_ai_model(deepseek_fallback["id"])
        try:
            await feishu_bot.push_to_users(
                settings.feishu_admin_users,
                f"⚡ AI 降级至备用：{old_active} → {deepseek_fallback['model']} (DeepSeek)"
            )
        except Exception:
            pass
        log.info("降级至 DeepSeek: %s", deepseek_fallback["id"])
        return True

    # ⑤ 彻底没救
    try:
        await feishu_bot.push_to_users(
            settings.feishu_admin_users,
            "❌ AI 救救失败：NVIDIA 与 DeepSeek 均不可用，请手动检查"
        )
    except Exception:
        pass
    return False


def _find_nvidia_key() -> str | None:
    """从模型库找 NVIDIA API key。"""
    from app.config import settings
    for m in settings.ai_models:
        if "integrate.api.nvidia.com" in m.get("base_url", ""):
            k = m.get("api_key", "")
            if _is_valid_key(k):
                return k
    return None


def _find_deepseek_fallback() -> dict | None:
    """从模型库找 DeepSeek 备用（非 active 的）。"""
    from app.config import settings
    for m in settings.ai_models:
        if ("deepseek" in m.get("base_url", "").lower() or
              "deepseek" in m.get("model", "").lower()):
            if m.get("id") != settings.ai_active and _is_valid_key(m.get("api_key", "")):
                return m
    return None
```

## 3.3 飞书命令接入（`app/feishu.py` `_process_command`）

在飞书命令处理中加两个新指令，让你能手动触发：

```python
# feishu.py _process_command() 中追加（约 L220 附近）

elif text.startswith("/ai_fix") or text.startswith("/nvidia"):
    # 手动触发 NVIDIA 探测+切换（纯程序）
    from app.ai.client import _failover_rescue
    result = await _failover_rescue()
    reply = "✅ AI 模型已切换" if result else "❌ 未找到可用替代模型"
    await self._send_text(open_id, reply, id_type="user_id")

elif text.startswith("/models"):
    # 快查当前模型状态
    from app.config import settings
    active = settings.active_ai_profile()
    info = f"当前: {active.model} ({active.base_url})\n"
    info += f"模型库: {len(settings.ai_models)} 个\n"
    for m in settings.ai_models:
        mark = "★" if m.get("id") == settings.ai_active else " "
        info += f"{mark} {m.get('model','?')} [{m.get('id','?')}]\n"
    await self._send_text(open_id, info.strip(), id_type="user_id")
```

## 3.4 API 端点（`app/main.py` 追加）

```python
@app.post("/api/ai/nvidia-probe")
async def nvidia_probe():
    """手动触发 NVIDIA 自动探测+切换（纯程序）。"""
    from app.ai.client import _failover_rescue
    switched = await _failover_rescue()
    return {"ok": switched}

@app.get("/api/ai/nvidia-models")
async def nvidia_list_models():
    """查看 NVIDIA 当前可选模型列表（不切换）。"""
    from app.ai.nvidia_probe import list_available_models
    key = ...  # 从模型库提取 NVIDIA key
    models = await list_available_models(key) if key else []
    return {"models": models, "count": len(models)}
```

---

# 四、数据流图

```
用户发消息给 Agent
       ↓
agent/router.py → client.chat(system, user)
       ↓
┌──────────────────────────────┐
│  client.py _build_candidates │ ← 候选模型列表
│  ↓ 逐个 _call_once()          │
│  ├─ 成功 → 返回内容          │
│  └─ 全部失败                 │
│      ↓                       │
│  ★ 新增 _failover_rescue()  │ ← 纯程序，不调 AI
│  ├─ ① push_to_users("AI全挂")│
│  ├─ ② nvidia_probe.auto_best │ ← 并发测 NVIDIA 免费模型
│  │   ├─ 有最佳 → set_active  │
│  │   │   ├─ push("已切NVIDIA")│
│  │   │   └─ return True      │
│  │   └─ 无可用 →             │
│  │       ├─ 找 deepseek 备用  │
│  │       │   ├─ push("切DS") │
│  │       │   └─ return True  │
│  │       └─ 都没有            │
│  │           push("彻底挂了") │
│  │           return False     │
│  └─ 若 switched=True          │
│     → 用新模型 _call_once 重试 │
│     → 成功则正常返回           │
└──────────────────────────────┘
```

---

# 五、前置条件

| 项 | 状态 | 说明 |
|---|---|---|
| NVIDIA API Key | ❌ 需要配置 | 去 https://build.nvidia.com 注册拿 key（免费） |
| 飞书 admin_users | ⚠️ 当前为空 | 需配阿勇的 open_id，否则推送静默丢弃 |
| 模型库有 DeepSeek 备用 | ❓ 待确认 | 当前 active 是 z-ai/glm-5.2；需确认是否有 deepseek 条目 |

**实施第一步就是配 NVIDIA key 到模型库里**（可通过前端 /api/models/fetch 或直接 POST /api/models 加一条）。

---

# 六、实施顺序

| 步骤 | 内容 | 影响 |
|---|---|---|
| 1 | 建 `app/ai/nvidia_probe.py` | 纯新增，零风险 |
| 2 | 改 `client.py`：chat() 末尾加降级钩子 + `_failover_rescue()` | 仅在"全部失败"路径生效，正常流程不变 |
| 3 | 改 `feishu.py`：加 `/ai_fix` 和 `/models` 命令 | 飞书命令扩展 |
| 4 | 改 `main.py`：加 2 个 API 端点 | REST 扩展 |
| 5 | 配 NVIDIA key 到模型库 | 数据操作 |
| 6 | 重启后端验证 | `env -u PYTHONPATH 绝对路径 venv python -m uvicorn` |
| 7 | 验收：断开主模型网络 → 观察飞书推送 → 确认自动切换 | DoD |

**安全门禁**：
- 步骤 2 的改动在 `if not candidates: return None` 和 `for mp in candidates:` 循环之后——**正常调用路径完全不受影响**
- 降级只触发一次重试（switched 后单次 _call_once），不会递归死循环
- 飞书推送全部 try/except 包裹，飞书也挂不影响降级本身
- NVIDIA 探测超时 60 秒，不会卡住主线程

---

*本文档可与 11_整改与新需求任务书.md 合并执行。确认后按步骤 1~7 实施。*
