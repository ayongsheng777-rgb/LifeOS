"""短期记忆（Redis 支持，懒连接，失败不阻断主流程）。

存储结构：list，每条为 {"role": "user"|"assistant", "content": str, "time": float}。
支持 Clean-Slate 隔离：clear(user_id) 彻底清空该用户上下文。
"""
import json
import time
import redis

from app.config import settings


class ShortMemory:
    def __init__(self):
        self._r = None

    def _max_items(self) -> int:
        """最多保留的轮数（每轮=用户+AI 各1条）。可经环境变量覆盖。"""
        try:
            return max(1, int(getattr(settings, "short_memory_max_rounds", 50)))
        except Exception:
            return 50

    def _ttl(self) -> int:
        """Redis 过期秒数（天数×86400）。可经环境变量覆盖。"""
        try:
            days = max(1, int(getattr(settings, "short_memory_ttl_days", 30)))
        except Exception:
            days = 30
        return days * 60 * 60 * 24

    def _conn(self):
        if self._r is None:
            self._r = redis.Redis.from_url(settings.redis_url, decode_responses=True,
                                           socket_connect_timeout=3, socket_timeout=3)
        return self._r

    def _key(self, user_id: str) -> str:
        return f"lifeos:mem:{user_id}"

    def get(self, user_id: str) -> list:
        try:
            raw = self._conn().lrange(self._key(user_id), 0, -1)
            return [json.loads(x) for x in raw]
        except Exception:
            return []

    def add(self, user_id: str, message: str, response: str) -> None:
        try:
            r = self._conn()
            key = self._key(user_id)
            r.rpush(key, json.dumps({"role": "user", "content": message, "time": time.time()},
                                    ensure_ascii=False))
            r.rpush(key, json.dumps({"role": "assistant", "content": response, "time": time.time()},
                                    ensure_ascii=False))
            r.ltrim(key, -self._max_items() * 2, -1)  # 只保留最近 N 轮
            r.expire(key, self._ttl())               # 按配置天数保留
        except Exception:
            pass  # 记忆失败不阻断主流程

    def clear(self, user_id: str) -> None:
        try:
            self._conn().delete(self._key(user_id))
        except Exception:
            pass
