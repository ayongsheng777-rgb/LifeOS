"""AI 统一大模型客户端（OpenAI 兼容，依赖仅 httpx）。

对齐《复刻指导 04》三条铁律：绝不阻断主流程 / 必须缓存 / 必须限流。
支持：chat / chat_json / probe；缓存、信号量限流(3)、推理模型兼容、容错 JSON 解析。
"""
import json
import time
import hashlib
import asyncio
import httpx

from app.config import settings

# 推理模型前缀（顺序敏感：deepseek-reasoner 须排在 deepseek 前）
_REASON_PREFIX = ("kimi-k3", "deepseek-v4-pro", "deepseek-reasoner", "o1", "o3")
# 强制 temperature（该模型只接受特定值，否则 400）
_FORCED_TEMP = {"kimi-k3": 1.0}

# 限流信号量
_SEM = asyncio.Semaphore(3)

# 缓存：md5(model+system+user) -> (expire_at, text)
_CACHE: dict = {}

stats = {
    "calls": 0, "ok": 0, "fail": 0, "cached": 0,
    "prompt_tokens": 0, "completion_tokens": 0, "last_error": "",
}


def _cache_key(model: str, system: str, user: str) -> str:
    h = hashlib.md5(f"{model}|{system}|{user}".encode("utf-8")).hexdigest()
    return h


def _is_reasoning(model: str) -> bool:
    m = (model or "").lower()
    return any(m.startswith(p) for p in _REASON_PREFIX)


def available() -> bool:
    """放宽判定：ai_enabled + active 或模型库任一项带有效 key。"""
    return settings.available()


def _extract_json(text: str):
    """容错解析：剥 ```json 围栏 / 去前后解释文字 / 中文引号替换。"""
    if not text:
        return None
    t = text.strip()
    # 去围栏
    if t.startswith("```"):
        t = t.split("```", 2)[1] if "```" in t[3:] else t
        if t.lower().startswith("json"):
            t = t[4:]
    # 找首个 { 或 [ 到最后一个 } 或 ]
    start = min([i for i in (t.find("{"), t.find("[")) if i >= 0], default=-1)
    end = max([i for i in (t.rfind("}"), t.rfind("]")) if i >= 0], default=-1)
    if start >= 0 and end > start:
        t = t[start:end + 1]
    # 中文引号替换
    t = t.replace("“", "\"").replace("”", "\"").replace("‘", "'").replace("’", "'")
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        return None


async def chat(system: str = None, user: str = "", *, model_profile=None,
               temperature: float = 0.7, max_tokens: int = 1024, json_mode: bool = False,
               cache_ttl: int = 900, timeout: int = None, require_enabled: bool = True) -> str | None:
    """统一对话入口。任意异常吞掉返回 None（不阻断主流程）。"""
    global stats
    # ① 总开关
    if require_enabled and not settings.ai_enabled:
        return None
    # ② 取 profile（参数优先 > active）
    mp = model_profile or settings.active_ai_profile()
    if mp is None or not getattr(mp, "api_key", ""):
        return None
    api_key = mp.api_key
    base_url = mp.base_url.rstrip("/")
    model = mp.model
    if not model:
        return None
    # 缓存命中
    ck = _cache_key(model, system or "", user)
    now = time.time()
    if ck in _CACHE:
        exp, val = _CACHE[ck]
        if exp > now:
            stats["cached"] += 1
            return val
    # ④ 推理模型兼容
    if model in _FORCED_TEMP:
        temperature = _FORCED_TEMP[model]
    effective_max = max_tokens
    effective_timeout = timeout or 45
    if _is_reasoning(model):
        effective_max = max(effective_max, 4096)
        effective_timeout = max(effective_timeout, 150)
    # ⑤ 组装请求
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if getattr(mp, "user_agent", ""):
        headers["User-Agent"] = mp.user_agent
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": effective_max,
        "stream": False,
    }
    if json_mode and _is_reasoning(model):
        payload["response_format"] = {"type": "json_object"}
    # 代理三级回落：profile.proxy → settings.ai_proxy → None
    proxy = getattr(mp, "proxy", "") or settings.ai_proxy or None
    stats["calls"] += 1
    try:
        async with _SEM:
            async with httpx.AsyncClient(proxy=proxy, timeout=httpx.Timeout(effective_timeout)) as client:
                resp = await client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
        if resp.status_code != 200:
            # ⑧ 非 200 → 精准人话提示
            err_body = ""
            try:
                err_body = resp.json().get("error", {}).get("message", "") or resp.text
            except Exception:
                err_body = resp.text
            if resp.status_code == 400 and json_mode:
                # ⑦ 400 且带 response_format → 去掉重试一次
                payload.pop("response_format", None)
                async with _SEM:
                    async with httpx.AsyncClient(proxy=proxy, timeout=httpx.Timeout(effective_timeout)) as client:
                        resp = await client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
                if resp.status_code != 200:
                    stats["fail"] += 1
                    stats["last_error"] = _human_error(resp.status_code, err_body)
                    return None
            else:
                stats["fail"] += 1
                stats["last_error"] = _human_error(resp.status_code, err_body)
                return None
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        # ⑨ content 为空 → 读 reasoning_content
        if not content:
            content = data.get("choices", [{}])[0].get("message", {}).get("reasoning_content", "")
        # 累计 usage
        usage = data.get("usage", {})
        stats["prompt_tokens"] += usage.get("prompt_tokens", 0)
        stats["completion_tokens"] += usage.get("completion_tokens", 0)
        stats["ok"] += 1
        # ⑩ 写缓存
        if cache_ttl and cache_ttl > 0:
            _CACHE[ck] = (now + cache_ttl, content)
        return content
    except httpx.ConnectError:
        stats["fail"] += 1
        stats["last_error"] = "连接失败：境外模型需配代理，国内模型检查网络"
        return None
    except httpx.ConnectTimeout:
        stats["fail"] += 1
        stats["last_error"] = "连接超时：境外需配代理/国内检查网络"
        return None
    except httpx.ReadTimeout:
        stats["fail"] += 1
        stats["last_error"] = "模型响应过慢（ReadTimeout）"
        return None
    except Exception as e:
        stats["fail"] += 1
        stats["last_error"] = f"{type(e).__name__}: {e}"
        # 不要静默：warning 方便排查
        import logging
        logging.getLogger("lifeos.ai").warning("chat 异常: %s", e)
        return None


async def chat_json(system: str, user: str, **kw) -> dict | None:
    kw.setdefault("json_mode", True)
    txt = await chat(system, user, **kw)
    if not txt:
        return None
    return _extract_json(txt)


async def embed(text: str, model: str = None, model_profile=None) -> list | None:
    """OpenAI 兼容 embeddings 端点。未配置 embedding 模型或 AI 不可用时返回 None（不阻断主流程）。

    用于长期记忆（Qdrant）向量化；向量维度须与 VectorMemory 的 vector_size 一致（默认 1536）。
    """
    mp = model_profile or settings.active_ai_profile()
    emb_model = model or settings.embedding_model
    if mp is None or not getattr(mp, "api_key", ""):
        return None
    if not emb_model:
        return None
    base_url = mp.base_url.rstrip("/")
    proxy = getattr(mp, "proxy", "") or settings.ai_proxy or None
    headers = {"Authorization": f"Bearer {mp.api_key}", "Content-Type": "application/json"}
    if getattr(mp, "user_agent", ""):
        headers["User-Agent"] = mp.user_agent
    try:
        async with _SEM:
            async with httpx.AsyncClient(proxy=proxy, timeout=httpx.Timeout(30)) as client:
                resp = await client.post(f"{base_url}/embeddings",
                                         headers=headers,
                                         json={"model": emb_model, "input": text})
        if resp.status_code != 200:
            logging.getLogger("lifeos.ai").warning("embed 失败 %s: %s", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        vec = data.get("data", [{}])[0].get("embedding")
        return vec if isinstance(vec, list) else None
    except Exception as e:
        logging.getLogger("lifeos.ai").warning("embed 异常: %s", e)
        return None


async def probe(model_profile=None, require_enabled: bool = False) -> dict:
    """连通性自检：发『只回复两个字：正常』。require_enabled=False 供保存前测试。"""
    mp = model_profile or settings.active_ai_profile()
    if mp is None or not getattr(mp, "api_key", ""):
        return {"ok": False, "reason": "未配置有效 api_key"}
    base_url = mp.base_url.rstrip("/")
    proxy = getattr(mp, "proxy", "") or settings.ai_proxy or None
    headers = {"Authorization": f"Bearer {mp.api_key}", "Content-Type": "application/json"}
    if getattr(mp, "user_agent", ""):
        headers["User-Agent"] = mp.user_agent
    payload = {"model": mp.model, "messages": [{"role": "user", "content": "只回复两个字：正常"}],
               "max_tokens": 10, "temperature": 0.1, "stream": False}
    t0 = time.time()
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=httpx.Timeout(20)) as client:
            resp = await client.post(f"{base_url}/chat/completions", headers=headers, json=payload)
        if resp.status_code != 200:
            return {"ok": False, "reason": _human_error(resp.status_code, resp.text[:200])}
        reply = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
        return {"ok": True, "model": mp.model, "base_url": base_url,
                "latency_ms": int((time.time() - t0) * 1000), "reply": reply.strip()}
    except Exception as e:
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}


def _human_error(code: int, body: str) -> str:
    if code == 401:
        return "Key 无效（401）"
    if code == 403:
        return "无权限（403）：检查模型是否对你开放"
    if code == 404:
        return "模型名不存在（404）"
    if code == 429:
        if "insufficient_quota" in body or "quota" in body.lower():
            return "频率/额度受限（429）：疑似欠费或额度不足"
        return "频率受限（429）：稍后重试"
    return f"HTTP {code}: {body[:160]}"
