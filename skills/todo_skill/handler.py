"""待办/任务技能 handler：自然语言增删查改。

仅做个人待办管理；无法识别的语句返回 None，交给 router 的默认 AI 对话（不卡死）。
数据走 app.skills.db_store.PgStore（PostgreSQL todos 表，按 user_id 隔离）。
"""
import re
import time

from app.skills.db_store import PgStore

store = PgStore("todo")

_ADD_RE = re.compile(r"^(?:添加|新增|新建|记一下|记一个|提醒我|帮我记|加个?|待办[：: ]?|任务[：: ]?)\s*(.*)$")
_DONE_RE = re.compile(r"(?:完成|做完|搞定|已做|已经做|标记完成)\s*(.*)$")
_DEL_RE = re.compile(r"^(?:删除|去掉|移除|删掉|取消)\s*(.*)$")
_LIST_KW = ("列出", "列表", "我的待办", "还有什么", "待办列表", "任务列表", "待办事项", "我的任务")


class SkillHandler:
    def __init__(self, metadata: dict):
        self.metadata = metadata

    async def execute(self, message: str, context: list, user_id: str = None) -> str:
        uid = user_id or "me"
        text = message.strip()

        # 1. 列表查询
        if any(k in text for k in _LIST_KW):
            items = await store.list_all(uid)
            if not items:
                return "你当前没有待办事项。"
            open_items = [i for i in items if not i.get("done")]
            lines = []
            for i in open_items:
                due = f" (截止 {i.get('due')})" if i.get("due") else ""
                pri = i.get("priority")
                pri_s = f"[{pri}]" if pri else ""
                lines.append(f"· {i['title']}{pri_s}{due}")
            done_n = len(items) - len(open_items)
            head = f"你有 {len(open_items)} 条待办（已完成 {done_n} 条）："
            return head + "\n" + ("\n".join(lines) if lines else "（全部完成啦）")

        # 2. 删除
        m = _DEL_RE.match(text)
        if m and m.group(1).strip():
            kw = m.group(1).strip()
            removed = await store.delete_where(uid, lambda x: kw in (x.get("title") or ""))
            return (f"已删除匹配「{kw}」的 {removed} 条待办。") if removed \
                else f"没找到含「{kw}」的待办。"

        # 3. 完成
        m = _DONE_RE.search(text)
        if m and m.group(1).strip():
            kw = m.group(1).strip()
            items = await store.list_where(
                uid, lambda x: not x.get("done", False) and kw in (x.get("title") or ""))
            if items:
                await store.update(uid, items[0]["id"],
                                   {"done": True, "done_at": int(time.time())})
                return f"已标记完成：{items[0]['title']}"
            return f"没找到未完成的、含「{kw}」的待办。"

        # 4. 添加
        m = _ADD_RE.match(text)
        title = m.group(1).strip() if m else None
        if not title:
            m2 = re.match(r"^(.*?)\s*(?:待办|任务)\s*$", text)
            if m2:
                title = m2.group(1).strip()
        if title:
            await store.add(uid, {"title": title, "done": False, "priority": None, "due": None})
            return f"已添加待办：{title}"

        # 命中关键词但解析不出动作 → 交给 AI 默认对话
        return None
