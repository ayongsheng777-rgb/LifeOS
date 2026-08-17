"""AI 统一大模型客户端（OpenAI 兼容，依赖仅 httpx）。

对齐《复刻指导 04》三条铁律：绝不阻断主流程 / 必须缓存 / 必须限流。
支持：chat / chat_json / chat_stream / probe；缓存、信号量限流(3)、推理模型兼容、容错 JSON 解析。

Phase 4 增强：
- 故障转移（Failover）：主模型 429/5xx/超时 → 自动尝试下一个带有效 key 的模型。
- 用量埋点：每次调用（成败）异步记录到 ai_usage（模型/场景/token/费用/耗时）。
- 流式：chat_stream 异步生成器，逐块吐字（SSE 由端点封装）。
"""
import json
import time
import asyncio
import logging
import httpx

from app.config import settings, AIProfile

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

log = logging.getLogger("lifeos.ai")


def _cache_key(model: str, system: str, user: str) -> str:
    import hashlib
    h = hashlib.md5(f"{model}|{system}|{user}".encode("utf-8")).hexdigest()
    return h


def _is_reasoning(model: str) -> bool:
    m = (model or "").lower()
    return any(m.startswith(p) for p in _REASON_PREFIX)


def available() -> bool:
    """放宽判定：ai_enabled + active 或模型库任一项带有效 key。"""
    return settings.available()


# ===================== 候选模型（Failover 来源）=====================
def _build_candidates(model_profile=None):
    """按优先级组装候选模型：显式指定 → active → 模型库其余带有效 key 的。"""
    cands = []
    seen = set()
    if model_profile and getattr(model_profile, "api_key", ""):
        cands.append(model_profile)
        seen.add(model_profile.id)
    active = settings.active_ai_profile()
    if active and active.id not in seen:
        cands.append(active)
        seen.add(active.id)
    for m in settings.ai_models:
        if _is_valid_key(m.get("api_key", "")):
            p = AIProfile.from_dict(m)
            if p.id not in seen:
                cands.append(p)
                seen.add(p.id)
    return cands


def _is_valid_key(key: str) -> bool:
    return bool(key) and key.strip().lower() not in (
        "your", "xxx", "sk-xxx", "changeme", "placeholder", "todo", "")


# ===================== 用量埋点（异步，失败静默）=====================
async def _record_usage(user_id: str, *, model: str, scenario: str = "chat",
                        input_tokens: int = 0, output_tokens: int = 0,
                        latency_ms: int = 0, ok: bool = True, error: str = None) -> None:
    try:
        from app.ai.usage_store import usage_store as us
        await us.record(user_id, model=model, scenario=scenario,
                        input_tokens=input_tokens, output_tokens=output_tokens,
                        latency_ms=latency_ms, ok=ok, error=error)
    except Exception:
        pass  # 用量记录绝不影响主流程


# ===================== 单次调用（含缓存 + 400 重试）=====================
async def _call_once(mp, system, user, *, temperature, max_tokens, json_mode, proxy, timeout, cache_ttl):
    """对单个模型发一次请求。返回 (content_or_None, status_str, usage_or_None)。

    status_str: "200" 成功 / HTTP 状态码 / "transport" 网络异常。
    """
    global stats
    model = mp.model
    ck = _cache_key(model, system or "", user)
    now = time.time()
    if cache_ttl and cache_ttl > 0 and ck in _CACHE:
        exp, val = _CACHE[ck]
        if exp > now:
            stats["cached"] += 1
            return (val, "200", None)

    if model in _FORCED_TEMP:
        temperature = _FORCED_TEMP[model]
    effective_max = max_tokens
    effective_timeout = timeout or 45
    if _is_reasoning(model):
        effective_max = max(effective_max, 4096)
        effective_timeout = max(effective_timeout, 150)

    headers = {"Authorization": f"Bearer {mp.api_key}", "Content-Type": "application/json"}
    if getattr(mp, "user_agent", ""):
        headers["User-Agent"] = mp.user_agent
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})
    payload = {
        "model": model, "messages": messages,
        "temperature": temperature, "max_tokens": effective_max, "stream": False,
    }
    if json_mode and _is_reasoning(model):
        payload["response_format"] = {"type": "json_object"}

    stats["calls"] += 1
    try:
        async with _SEM:
            async with httpx.AsyncClient(proxy=proxy, timeout=httpx.Timeout(effective_timeout)) as hc:
                resp = await hc.post(f"{mp.base_url.rstrip('/')}/chat/completions", headers=headers, json=payload)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
        stats["fail"] += 1
        stats["last_error"] = "连接失败/超时"
        return (None, "transport", None)
    except Exception as e:
        stats["fail"] += 1
        stats["last_error"] = f"{type(e).__name__}: {e}"
        return (None, "transport", None)

    if resp.status_code != 200:
        err_body = ""
        try:
            err_body = resp.json().get("error", {}).get("message", "") or resp.text
        except Exception:
            err_body = resp.text
        if resp.status_code == 400 and json_mode:
            payload.pop("response_format", None)
            try:
                async with _SEM:
                    async with httpx.AsyncClient(proxy=proxy, timeout=httpx.Timeout(effective_timeout)) as hc:
                        resp = await hc.post(f"{mp.base_url.rstrip('/')}/chat/completions", headers=headers, json=payload)
            except Exception:
                stats["fail"] += 1
                stats["last_error"] = _human_error(resp.status_code, err_body)
                return (None, str(resp.status_code), None)
            if resp.status_code != 200:
                stats["fail"] += 1
                stats["last_error"] = _human_error(resp.status_code, err_body)
                return (None, str(resp.status_code), None)
        else:
            stats["fail"] += 1
            stats["last_error"] = _human_error(resp.status_code, err_body)
            return (None, str(resp.status_code), None)

    data = resp.json()
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not content:
        content = data.get("choices", [{}])[0].get("message", {}).get("reasoning_content", "")
    usage = data.get("usage", {})
    stats["ok"] += 1
    stats["prompt_tokens"] += usage.get("prompt_tokens", 0)
    stats["completion_tokens"] += usage.get("completion_tokens", 0)
    if cache_ttl and cache_ttl > 0:
        _CACHE[ck] = (now + cache_ttl, content)
    return (content, "200", usage)


# ===================== 统一对话入口（含 Failover）=====================
async def chat(system: str = None, user: str = "", *, model_profile=None,
               temperature: float = 0.7, max_tokens: int = 1024, json_mode: bool = False,
               cache_ttl: int = 900, timeout: int = None, require_enabled: bool = True,
               user_id: str = "me", scenario: str = "chat") -> str | None:
    """统一对话入口。任意异常吞掉返回 None（不阻断主流程）。

    Failover：主模型失败（transport / 非 200）自动尝试下一个候选模型。
    每次调用（成败）异步记录用量，不影响返回值。
    """
    if require_enabled and not settings.ai_enabled:
        return None
    candidates = _build_candidates(model_profile)
    if not candidates:
        return None

    last_status = "no_candidate"
    for mp in candidates:
        t0 = time.time()
        content, status, usage = await _call_once(
            mp, system, user, temperature=temperature, max_tokens=max_tokens,
            json_mode=json_mode, proxy=(getattr(mp, "proxy", "") or settings.ai_proxy or None),
            timeout=timeout, cache_ttl=cache_ttl,
        )
        latency_ms = int((time.time() - t0) * 1000)
        if content is not None:
            in_tok = (usage or {}).get("prompt_tokens", 0) if usage else 0
            out_tok = (usage or {}).get("completion_tokens", 0) if usage else 0
            await _record_usage(user_id, model=mp.model, scenario=scenario,
                                input_tokens=in_tok, output_tokens=out_tok,
                                latency_ms=latency_ms, ok=True)
            return content
        # 失败 → 记录该候选的失败，尝试下一个
        last_status = status
        await _record_usage(user_id, model=mp.model, scenario=scenario,
                            latency_ms=latency_ms, ok=False, error=str(status))
    log.warning("所有候选模型均失败（最后状态 %s），返回 None", last_status)
    return None


def _extract_json(text: str):
    """容错解析：剥 ```json 围栏 / 去前后解释文字 / 中文引号替换。"""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1] if "```" in t[3:] else t
        if t.lower().startswith("json"):
            t = t[4:]
    start = min([i for i in (t.find("{"), t.find("[")) if i >= 0], default=-1)
    end = max([i for i in (t.rfind("}"), t.rfind("]")) if i >= 0], default=-1)
    if start >= 0 and end > start:
        t = t[start:end + 1]
    t = t.replace("“", "\"").replace("”", "\"").replace("‘", "'").replace("’", "'")
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        return None


async def chat_json(system: str, user: str, **kw) -> dict | None:
    kw.setdefault("json_mode", True)
    txt = await chat(system, user, **kw)
    if not txt:
        return None
    return _extract_json(txt)


async def chat_stream(system: str = None, user: str = "", *, model_profile=None,
                      temperature: float = 0.7, max_tokens: int = 1024,
                      user_id: str = "me", scenario: str = "chat"):
    """流式对话：异步生成器，逐块 yield 文本片段。

    AI 不可用 / 失败 → 不 yield（调用方应负责兜底）。
    用法：async for piece in chat_stream(...): ...
    """
    if not settings.ai_enabled:
        return
    mp = model_profile or settings.active_ai_profile()
    if mp is None or not getattr(mp, "api_key", ""):
        return
    api_key = mp.api_key
    base_url = mp.base_url.rstrip("/")
    model = mp.model
    if not model:
        return

    effective_max = max_tokens
    effective_timeout = 60
    if _is_reasoning(model):
        effective_max = max(effective_max, 4096)
        effective_timeout = max(effective_timeout, 150)
    proxy = getattr(mp, "proxy", "") or settings.ai_proxy or None
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if getattr(mp, "user_agent", ""):
        headers["User-Agent"] = mp.user_agent
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user})
    payload = {"model": model, "messages": messages, "temperature": temperature,
               "max_tokens": effective_max, "stream": True}

    full: list = []
    final_usage = None
    try:
        async with _SEM:
            async with httpx.AsyncClient(proxy=proxy, timeout=httpx.Timeout(effective_timeout)) as hc:
                async with hc.stream("POST", f"{base_url}/chat/completions", headers=headers, json=payload) as resp:
                    if resp.status_code != 200:
                        yield f"[模型错误 {resp.status_code}]"
                        return
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                        except Exception:
                            continue
                        if obj.get("usage"):
                            final_usage = obj["usage"]
                        choices = obj.get("choices") or [{}]
                        delta = choices[0].get("delta", {}) if choices else {}
                        piece = delta.get("content") or delta.get("reasoning_content") or ""
                        if piece:
                            full.append(piece)
                            yield piece
    except Exception as e:
        log.warning("chat_stream 异常: %s", e)
        if not full:
            yield f"[流式异常: {e}]"
        return
    finally:
        reply = "".join(full)
        in_tok = (final_usage or {}).get("prompt_tokens") or max(1, len(user) // 4)
        out_tok = (final_usage or {}).get("completion_tokens") or max(0, len(reply) // 4)
        await _record_usage(user_id, model=model, scenario=scenario,
                            input_tokens=in_tok, output_tokens=out_tok,
                            ok=bool(reply), error=None if reply else "empty_stream")


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
            async with httpx.AsyncClient(proxy=proxy, timeout=httpx.Timeout(30)) as hc:
                resp = await hc.post(f"{base_url}/embeddings", headers=headers,
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
        async with httpx.AsyncClient(proxy=proxy, timeout=httpx.Timeout(20)) as hc:
            resp = await hc.post(f"{base_url}/chat/completions", headers=headers, json=payload)
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


async def speed_test(model_profile=None, rounds: int = 3, require_enabled: bool = False) -> dict:
    """测速：对指定模型跑 N 轮流式请求，返回每轮 TTFT(首字延迟)/总延迟/输出 token/吞吐(tps) + 均值。

    失败静默返回结构化结果（ok=False + reason），不抛异常。
    """
    mp = model_profile or settings.active_ai_profile()
    if mp is None or not getattr(mp, "api_key", ""):
        return {"ok": False, "reason": "未配置有效 api_key"}
    base_url = mp.base_url.rstrip("/")
    proxy = getattr(mp, "proxy", "") or settings.ai_proxy or None
    model = mp.model
    if not model:
        return {"ok": False, "reason": "模型名为空"}
    headers = {"Authorization": f"Bearer {mp.api_key}", "Content-Type": "application/json"}
    if getattr(mp, "user_agent", ""):
        headers["User-Agent"] = mp.user_agent
    payload = {"model": model, "messages": [{"role": "user", "content": "请用一句话介绍你自己"}],
               "max_tokens": 64, "temperature": 0.7, "stream": True}
    rounds = max(1, min(int(rounds or 3), 10))
    results = []
    for _ in range(rounds):
        t0 = time.time()
        ttft = None
        out_tok = 0
        full: list = []
        try:
            async with _SEM:
                async with httpx.AsyncClient(proxy=proxy, timeout=httpx.Timeout(60)) as hc:
                    async with hc.stream("POST", f"{base_url}/chat/completions",
                                         headers=headers, json=payload) as resp:
                        if resp.status_code != 200:
                            body = await resp.aread()
                            results.append({"ok": False,
                                            "reason": _human_error(resp.status_code,
                                                                   body[:200].decode("utf-8", "ignore"))})
                            continue
                        async for line in resp.aiter_lines():
                            line = line.strip()
                            if not line or not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                break
                            try:
                                obj = json.loads(data)
                            except Exception:
                                continue
                            if obj.get("usage"):
                                out_tok = obj["usage"].get("completion_tokens") or out_tok
                            choices = obj.get("choices") or [{}]
                            delta = choices[0].get("delta", {}) if choices else {}
                            piece = delta.get("content") or delta.get("reasoning_content") or ""
                            if piece and ttft is None:
                                ttft = (time.time() - t0) * 1000
                            if piece:
                                full.append(piece)
        except Exception as e:
            results.append({"ok": False, "reason": f"{type(e).__name__}: {e}"})
            continue
        total_ms = (time.time() - t0) * 1000
        text = "".join(full)
        if not out_tok:
            out_tok = max(0, len(text) // 4)
        tps = (out_tok / (total_ms / 1000.0)) if total_ms > 0 else 0
        results.append({
            "ok": True,
            "ttft_ms": round(ttft if ttft is not None else total_ms, 1),
            "latency_ms": round(total_ms, 1),
            "output_tokens": out_tok,
            "tps": round(tps, 2),
        })
    ok_rounds = [r for r in results if r.get("ok")]
    avg = None
    if ok_rounds:
        avg = {
            "ttft_ms": round(sum(r["ttft_ms"] for r in ok_rounds) / len(ok_rounds), 1),
            "latency_ms": round(sum(r["latency_ms"] for r in ok_rounds) / len(ok_rounds), 1),
            "tps": round(sum(r["tps"] for r in ok_rounds) / len(ok_rounds), 2),
        }
    return {"ok": bool(ok_rounds), "model": model, "rounds": results, "avg": avg,
            "success": len(ok_rounds), "total": rounds}
