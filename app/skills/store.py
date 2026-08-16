"""LifeOS 轻量持久化存储：基于 data/ 目录的 JSON 文件（原子写 + 异步锁）。

设计取舍（对齐《复刻指导》基建规范）：
- 个人实例规模，无需 Postgres；复用 settings.data_dir（容器内 /app/data，已挂卷）
- 原子写：先写 .tmp 再 os.replace，避免半截文件
- 异步锁串行化同一 store 的读写，防并发撕裂（个人实例单进程，足够）
- 每个业务一个文件：skill_todo.json / skill_expense.json

仅供 Skill / REST 端点内部使用；对外不暴露路径。
"""
import os
import json
import time
import asyncio
import uuid

from app.config import settings


class JsonStore:
    """一个业务一张 JSON 表，结构：{"items": [...]}。"""

    def __init__(self, name: str):
        self.name = name
        self._path = os.path.join(settings.data_dir, f"skill_{name}.json")
        self._lock = asyncio.Lock()

    def _load(self) -> dict:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or "items" not in data:
                return {"items": []}
            return data
        except (FileNotFoundError, json.JSONDecodeError):
            return {"items": []}

    def _save(self, data: dict) -> None:
        os.makedirs(settings.data_dir, exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)

    async def list_all(self, user_id: str) -> list:
        async with self._lock:
            return [x for x in self._load()["items"] if x.get("user_id") == user_id]

    async def list_where(self, user_id: str, predicate) -> list:
        async with self._lock:
            return [x for x in self._load()["items"]
                    if x.get("user_id") == user_id and predicate(x)]

    async def add(self, user_id: str, payload: dict) -> dict:
        async with self._lock:
            data = self._load()
            item = {
                "id": uuid.uuid4().hex[:12],
                "user_id": user_id,
                "created_at": int(time.time()),
            }
            item.update(payload)
            data["items"].append(item)
            self._save(data)
            return item

    async def find(self, user_id: str, item_id: str) -> dict | None:
        async with self._lock:
            for it in self._load()["items"]:
                if it.get("id") == item_id and it.get("user_id") == user_id:
                    return it
            return None

    async def update(self, user_id: str, item_id: str, patch: dict) -> dict | None:
        async with self._lock:
            data = self._load()
            for it in data["items"]:
                if it.get("id") == item_id and it.get("user_id") == user_id:
                    it.update(patch)
                    self._save(data)
                    return it
            return None

    async def delete(self, user_id: str, item_id: str) -> bool:
        async with self._lock:
            data = self._load()
            before = len(data["items"])
            data["items"] = [
                x for x in data["items"]
                if not (x.get("id") == item_id and x.get("user_id") == user_id)
            ]
            if len(data["items"]) < before:
                self._save(data)
                return True
            return False

    async def delete_where(self, user_id: str, predicate) -> int:
        async with self._lock:
            data = self._load()
            before = len(data["items"])
            data["items"] = [
                x for x in data["items"]
                if not (x.get("user_id") == user_id and predicate(x))
            ]
            removed = before - len(data["items"])
            if removed:
                self._save(data)
            return removed
