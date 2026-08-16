"""记忆分层管理（Memory V2）：统一编排 工作 / 短期 / 长期 三层。

- 工作记忆 WorkingMemory（进程内，TTL，轻量状态）
- 短期记忆 ShortMemory（Redis 多轮对话历史）
- 长期记忆 VectorMemory（Qdrant 经验向量；懒加载，失败降级）

对外暴露的方法都做了「失败不阻断主流程」的保护：
- 短期/工作记忆 Redis 异常 → 空结果（ShortMemory 内部已吞异常）；
- 长期记忆 Qdrant 不可用 / 未配 embedding → 返回 None 或空列表，由调用方优雅降级。

长期记忆保存前经过 _is_important 轻量护栏（长度 / 实质内容判断，不额外调用 LLM），
避免回声兜底、报错、纯问候等低价值内容污染 Qdrant。
"""
import time
import logging

from app.memory.short_memory import ShortMemory
from app.memory.working_memory import WorkingMemory
from app.memory.vector_memory import VectorMemory
from app.ai import client as ai_client
from app.config import settings

log = logging.getLogger("lifeos.memory")

# 纯问候 / 控制指令：不值得作为长期经验沉淀
_TRIVIAL_MSGS = {"你好", "您好", "hi", "hello", "在吗", "在么", "/reset", "清空上下文", "新对话"}
# 回声兜底 / 出错的标志串：说明本轮并非有效 AI 回复
_LOW_VALUE_MARKERS = (
    "(当前未配置 AI 模型",
    "仅作回声",
    "执行出错",
    "未返回结果",
    "未知技能",
    "未识别动作",
    "AI 暂时无回复",
)


class MemoryManager:
    def __init__(self):
        self.working = WorkingMemory()
        self.short = ShortMemory()
        self.long = None  # 长期记忆懒加载；不可用时置 False（哨兵），避免反复重试
        self.skill_hits = {}  # 技能命中计数（进程内，重启归零；个人实例足够）

    # ===================== 工作记忆 =====================
    def set_working(self, user_id: str, key: str, value) -> None:
        self.working.set(user_id, key, value)

    def get_working(self, user_id: str) -> dict:
        return self.working.get(user_id)

    def clear_working(self, user_id: str) -> None:
        self.working.clear(user_id)

    # ===================== 技能命中统计 =====================
    def bump_skill(self, name: str) -> None:
        """技能被实际分发执行时自增计数（供 Dashboard 展示命中热度）。"""
        self.skill_hits[name] = self.skill_hits.get(name, 0) + 1

    # ===================== 短期记忆 =====================
    def get_short(self, user_id: str) -> list:
        return self.short.get(user_id)

    def add_short(self, user_id: str, message: str, response: str) -> None:
        self.short.add(user_id, message, response)

    def clear_short(self, user_id: str) -> None:
        self.short.clear(user_id)

    # ===================== 长期记忆 =====================
    def _ensure_long(self):
        """懒加载长期记忆；失败置 False 哨兵。"""
        if self.long is False:
            return None
        if self.long is None:
            try:
                self.long = VectorMemory()
            except Exception as e:
                log.warning("长期记忆（Qdrant）不可用，已降级关闭: %s", e)
                self.long = False
        return self.long if self.long else None

    @staticmethod
    def _is_important(user_msg: str, assistant_msg: str) -> bool:
        """轻量重要性护栏：不调用 LLM，仅靠规则过滤低价值内容。"""
        if not assistant_msg:
            return False
        if any(m in assistant_msg for m in _LOW_VALUE_MARKERS):
            return False
        if (user_msg or "").strip() in _TRIVIAL_MSGS:
            return False
        combined = f"{user_msg}\n{assistant_msg}"
        # 过短（噪声/无意义）不沉淀
        if len(combined) < 15:
            return False
        return True

    async def save_long_if_important(self, user_id: str, user_msg: str, assistant_msg: str):
        """经护栏后向量化并存入长期记忆；不满足/不可用时返回 None（不阻断主流程）。"""
        if not self._is_important(user_msg, assistant_msg):
            return None
        lm = self._ensure_long()
        if lm is None:
            return None
        text = f"用户：{user_msg}\n助手：{assistant_msg}"
        try:
            vec = await ai_client.embed(text)
            if vec:
                meta = {
                    "importance": "high" if len(assistant_msg) > 120 else "normal",
                    "saved_at": int(time.time()),
                }
                return lm.save_experience(user_id, text, vec, metadata=meta)
        except Exception as e:
            log.warning("长期经验保存失败（忽略）: %s", e)
        return None

    def list_long(self, user_id: str = None, limit: int = 50):
        """列出长期经验；未配置/不可用返回 None（调用方据此返回 configured:false）。"""
        lm = self._ensure_long()
        if lm is None:
            return None
        try:
            return lm.list_experiences(user_id, limit=limit)
        except Exception as e:
            log.warning("长期经验列举失败: %s", e)
            return []

    def delete_long(self, point_id) -> bool:
        lm = self._ensure_long()
        if lm is None:
            return False
        try:
            return lm.delete_experience(point_id)
        except Exception as e:
            log.warning("长期经验删除失败: %s", e)
            return False
