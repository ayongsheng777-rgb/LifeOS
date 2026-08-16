"""工作记忆（Working Memory）：进程内极短期状态。

与「短期记忆(Redis 多轮历史)」「长期记忆(Qdrant 经验)」区分：
- 仅存进程内，不依赖外部服务；
- 默认 TTL 30 分钟，过期自动失效（避免陈旧状态误导）；
- 典型用途：最近一次意图/命中的技能、待确认项、当前任务进度；
- 支持 Clean-Slate 清空（与短期记忆一致）。
"""
import time
from typing import Any, Dict, Optional


class WorkingMemory:
    def __init__(self, ttl: int = 1800):
        self.ttl = ttl
        self._store: Dict[str, Dict[str, Dict[str, Any]]] = {}  # user_id -> {key: {"v","ts"}}

    def set(self, user_id: str, key: str, value: Any) -> None:
        bucket = self._store.setdefault(user_id, {})
        bucket[key] = {"v": value, "ts": time.time()}

    def get(self, user_id: str) -> Dict[str, Any]:
        self._purge(user_id)
        return {k: item["v"] for k, item in self._store.get(user_id, {}).items()}

    def clear(self, user_id: str) -> None:
        self._store.pop(user_id, None)

    def _purge(self, user_id: str) -> None:
        now = time.time()
        bucket = self._store.get(user_id)
        if not bucket:
            return
        expired = [k for k, it in bucket.items() if now - it["ts"] > self.ttl]
        for k in expired:
            del bucket[k]
