"""记账/收支技能 handler：自然语言记账 + 月度汇总。

- 含金额且带收支语义 → 记一笔（金额正则提取，收入/支出按关键词判定，类目启发式提取）
- 含「花了多少/这个月/账单」类词 → 月度汇总
- 金额缺失或无关 → 返回 None 交给默认 AI（不卡死）

数据走 app.skills.db_store.PgStore（PostgreSQL expenses 表，按 user_id 隔离）。
类目提取为启发式，可能不精准，可经 REST 端点显式指定。
"""
import re
import time

from app.skills.db_store import PgStore

store = PgStore("expense")

_AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)")
_INCOME_KW = ("收入", "入账", "工资", "赚", "进账", "收款", "报销", "回款", "分红")
# 类目提取时剔除的停用词（动词/量词/连接词），保留名词性词作类目
_STOP = set("记账 记一笔 记一笔账 收支 支出 收入 入账 工资 赚 进账 收款 报销 回款 "
            "花 花销 了 元 块 钱 我 你 他 今天 昨天 前天 本月 这个月 上个月 给 去 买 吃 "
            "喝 在 到 用 付 缴 交 还 借 存 取 充值 的 和 与 以及 还有 一笔 一单 共 总共".split())


class SkillHandler:
    def __init__(self, metadata: dict):
        self.metadata = metadata

    async def execute(self, message: str, context: list, user_id: str = None) -> str:
        uid = user_id or "me"
        text = message.strip()

        # 1. 月度汇总
        if any(k in text for k in ("花了多少", "这个月", "本月", "收支汇总", "账单", "查账", "总结", "结余")):
            m = re.search(r"(\d{4})[-/年]?(\d{1,2})", text)
            month = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}" if m else time.strftime("%Y-%m")
            items = await store.list_all(uid)
            cur = [i for i in items if (i.get("happened_at") or "").startswith(month)]
            income = sum(i["amount"] for i in cur if i.get("type") == "income")
            expense = sum(i["amount"] for i in cur if i.get("type") == "expense")
            return (f"【{month} 收支汇总】\n收入：{income:.2f}\n支出：{expense:.2f}\n"
                    f"结余：{income - expense:.2f}\n笔数：{len(cur)}")

        # 2. 记录一笔
        am = _AMOUNT_RE.search(text)
        if not am:
            return None  # 没有金额，交给 AI
        amount = float(am.group(1))
        is_income = any(k in text for k in _INCOME_KW)
        ttype = "income" if is_income else "expense"

        # 类目：剔除金额与停用词后取首个中文/英文词
        remainder = text
        for kw in _STOP:
            remainder = remainder.replace(kw, " ")
        remainder = re.sub(r"\d+(\.\d+)?", " ", remainder)
        remainder = re.sub(r"[^\u4e00-\u9fa5a-zA-Z]", " ", remainder).strip()
        tokens = remainder.split()
        category = tokens[0] if tokens else ("工资" if is_income else "其他")

        await store.add(uid, {
            "type": ttype,
            "amount": round(amount, 2),
            "category": category,
            "note": "",
            "happened_at": time.strftime("%Y-%m-%d"),
        })
        sign = "+" if ttype == "income" else "-"
        return f"已记录：{sign}{amount:.2f}（{category}）"
