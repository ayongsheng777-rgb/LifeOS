"""场景限速（进程内滑动窗口，按用户）。

单实例部署足够；Redis 起后可升级为共享限流（与登录限流同理）。
阈值由环境变量 AI_RATE_LIMIT 控制，格式 "最大次数/窗口"：
  "30/m" → 每用户每分钟 30 次（默认）
  "20/60" → 每 60 秒 20 次
  "100/h" → 每小时 100 次
"""
import os
import time
from typing import Dict, List


def _parse_rate_limit(env_val: str, default_max: int = 30, default_win: int = 60):
    if not env_val:
        return default_max, default_win
    try:
        parts = env_val.strip().lower().split("/")
        max_r = int(parts[0])
        unit = parts[1] if len(parts) > 1 else "m"
        if unit in ("m", "min", "minute"):
            win = 60
        elif unit in ("s", "sec"):
            win = 1
        elif unit in ("h", "hour"):
            win = 3600
        elif unit.isdigit():
            win = int(unit)
        else:
            win = default_win
        return max(1, max_r), win
    except Exception:
        return default_max, default_win


class RateLimiter:
    def __init__(self, max_requests: int = 30, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: Dict[str, List[float]] = {}

    def allow(self, user_id: str) -> bool:
        now = time.time()
        hits = self._hits.setdefault(user_id, [])
        hits[:] = [t for t in hits if now - t < self.window_seconds]
        if len(hits) >= self.max_requests:
            return False
        hits.append(now)
        return True

    def remaining(self, user_id: str) -> int:
        now = time.time()
        hits = self._hits.get(user_id, [])
        hits = [t for t in hits if now - t < self.window_seconds]
        return max(0, self.max_requests - len(hits))


# 单例：从环境变量读取（默认 30/分钟）
_RATE_MAX, _RATE_WIN = _parse_rate_limit(os.environ.get("AI_RATE_LIMIT", ""))
ai_rate_limiter = RateLimiter(_RATE_MAX, _RATE_WIN)
