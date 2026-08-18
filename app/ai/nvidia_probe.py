"""NVIDIA 免费模型自动探测与优选。

纯 HTTP 操作，不依赖任何 AI 能力——专门用于 AI 主链路全挂时的纯程序自救。

NVIDIA API:
  GET  https://integrate.api.nvidia.com/v1/models            → 可用模型列表
  POST https://integrate.api.nvidia.com/v1/chat/completions   → 对话

策略（参考 sun-panel autobest.go）：拉列表 → 逐个探活 → 选最快的免费模型。
"""
import os
import time
import asyncio
import logging
import httpx
from typing import Optional

log = logging.getLogger("lifeos.ai.nvidia")

NVIDIA_BASE = "https://integrate.api.nvidia.com/v1"
# NVIDIA 免费模型前缀白名单（排除付费/实验性）
FREE_MODEL_PREFIXES = (
    "meta/",           # Llama 系列
    "mistralai/",      # Mistral
    "qwen/",           # 通义千问
    "deepseek-ai/",    # DeepSeek（NVIDIA 托管版）
    "google/",         # Gemma
    "nvidia/",         # NVIDIA 自研
)

PROBE_PROMPT = {
    "messages": [{"role": "user", "content": "Hi"}],
    "max_tokens": 8, "temperature": 0.1, "stream": False,
}


async def list_available_models(api_key: str, *, timeout: int = 30) -> list:
    """拉取 NVIDIA 可用模型列表，过滤出免费且支持 chat 的。

    返回 [{"id", "name", "base_url", "api_key"}]。
    """
    if not api_key:
        return []
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as hc:
            resp = await hc.get(
                f"{NVIDIA_BASE}/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if resp.status_code != 200:
            log.warning("NVIDIA 模型列表获取失败 status=%s", resp.status_code)
            return []
        data = resp.json()
    except Exception as e:
        log.error("NVIDIA 模型列表异常: %s", e)
        return []

    out = []
    for m in data.get("data", []):
        mid = m.get("id", "")
        if not mid:
            continue
        if not any(mid.startswith(p) for p in FREE_MODEL_PREFIXES):
            continue
        out.append({"id": mid, "name": mid, "base_url": NVIDIA_BASE,
                    "api_key": api_key, "tags": ["nvidia", "free"]})
    return out


async def auto_best(api_key: str, *, rounds: int = 1, timeout: int = 12,
                    max_probe: int = 24, concurrency: int = 8) -> Optional[dict]:
    """并发探测 NVIDIA 免费模型，返回速度最快且可用的一个（或 None）。

    关键修复：旧实现逐个串行探测、单模型超时 60s，最坏情况会卡数十分钟，
    导致 AI 全挂时的『纯程序自救』本身变成阻塞。这里改为：
    - 并发探测（信号量限流），单模型短超时（默认 12s）；
    - 限制探测数量（max_probe），整体最坏耗时≈ timeout 量级，秒级返回。

    返回 dict: {id, name, base_url, api_key, latency_ms}；否则 None。
    """
    if not api_key:
        return None
    models = await list_available_models(api_key)
    if not models:
        return None
    models = models[:max_probe]  # 限制探测规模，保证整体快速返回

    sem = asyncio.Semaphore(max(1, concurrency))

    async def _probe(m: dict) -> dict:
        t0 = time.time()
        try:
            payload = dict(PROBE_PROMPT)
            payload["model"] = m["id"]
            async with sem:
                async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as hc:
                    resp = await hc.post(
                        f"{m['base_url']}/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}",
                                  "Content-Type": "application/json"},
                        json=payload,
                    )
                    ok = (resp.status_code == 200)
        except Exception:
            ok = False
        ms = int((time.time() - t0) * 1000)
        log.info("NVIDIA 探测 %s -> ok=%s %dms", m["id"], ok, ms)
        return {**m, "ok": ok, "latency_ms": ms}

    results = await asyncio.gather(*[_probe(m) for m in models])
    usable = [r for r in results if r.get("ok")]
    if not usable:
        log.warning("NVIDIA auto_best: 无可用模型（探测 %d 个）", len(models))
        return None
    usable.sort(key=lambda x: x["latency_ms"])
    best = usable[0]
    log.info("NVIDIA auto_best: %s (%dms), 共 %d/%d 可用",
             best["id"], best["latency_ms"], len(usable), len(models))
    return best


async def ensure_nvidia_in_models(nvidia_key: str) -> bool:
    """确保 NVIDIA 候选在 ai_models 库中（若不存在则追加占位条目）。

    返回是否做了变更。占位 model 会在 auto_best 后被真实模型名覆盖。
    """
    from app.config import settings
    has_nvidia = any(
        "nvidia" in (m.get("tags") or [])
        or "integrate.api.nvidia.com" in m.get("base_url", "")
        for m in settings.ai_models
    )
    if has_nvidia:
        return False
    settings.upsert_ai_model({
        "id": "nvidia-auto",
        "name": "NVIDIA 免费(自动)",
        "base_url": NVIDIA_BASE,
        "model": "meta/llama-3.1-8b-instruct",   # 占位，auto_best 覆盖
        "api_key": nvidia_key,
        "tags": ["nvidia", "free", "auto"],
    })
    return True
