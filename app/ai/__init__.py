"""AI 模型功能模块（OpenAI 兼容多模型层）。"""
from app.ai.client import chat, chat_json, probe, available, stats
from app.ai import news_ai, analyzer, registry

__all__ = ["chat", "chat_json", "probe", "available", "stats", "news_ai", "analyzer", "registry"]
