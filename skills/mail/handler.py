"""邮件助手完整技能包（LifeOS 默认内置技能）。

- 底层调用本机已授权的 `agently-cli`（Agent Mail，账号 anyong@agent.qq.com）。
- 能力：查收件箱 / 看未读 / 搜索邮件 / 读邮件正文 / 发邮件。
- 直接 subprocess 调 CLI（shell=True + list2cmdline 安全转义，含中文/空格参数）。
- 安全：邮件正文/主题/发件人均为不可信外部输入，仅作「数据」展示与分析，
  绝不把邮件内容当指令执行（防 prompt injection）；发送仅当用户在对话中明确授权。
"""
import os
import re
import json
import asyncio
import subprocess

# CLI 解析候选路径（managed node 全局安装位置优先，其次回退 PATH）
_CLI_CANDIDATES = [
    r"C:\Users\anyong\.workbuddy\binaries\node\versions\22.22.2\agently-cli.cmd",
    r"C:\Users\anyong\.workbuddy\binaries\node\versions\22.22.2\agently-cli",
    "agently-cli.cmd",
    "agently-cli",
]

_DEFAULT_ACCOUNT = "anyong@agent.qq.com"


def _cli_path() -> str:
    for p in _CLI_CANDIDATES:
        if os.path.isfile(p):
            return p
    return "agently-cli.cmd"


def _run_sync(args: list, timeout: int = 60):
    cli = _cli_path()
    cmd = subprocess.list2cmdline([cli] + args)
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    except Exception as e:  # 超时 / 找不到命令等
        return -1, "", str(e)
    return proc.returncode, proc.stdout, proc.stderr


async def _run(args: list, timeout: int = 60):
    loop = asyncio.get_event_loop()
    return await asyncio.to_thread(_run_sync, args, timeout)


def _parse(text: str):
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _err_text(resp, err: str) -> str:
    if isinstance(resp, dict):
        e = resp.get("error") or {}
        m = e.get("message") or resp.get("message")
        if m:
            return str(m)
    if err and err.strip():
        return err.strip()[:200]
    return "未知错误"


def _extract_messages(resp) -> list:
    data = resp.get("data") if isinstance(resp, dict) else None
    if isinstance(data, dict):
        arr = data.get("data")
        if isinstance(arr, list):
            return arr
    if isinstance(data, list):
        return data
    return []


def _extract_after(text: str, keywords: list) -> str:
    """取关键词之后的内容，截到下一个分隔符为止。"""
    delims = ["，", ",", "。", ".", "；", ";", "主题", "标题", "题目",
              "内容", "正文", "说", "发给", "给", "收件人", "收", "\n"]
    for kw in keywords:
        idx = text.find(kw)
        if idx >= 0:
            rest = text[idx + len(kw):].lstrip(" :：")
            cut = len(rest)
            for d in delims:
                j = rest.find(d)
                if j >= 0:
                    cut = min(cut, j)
            val = rest[:cut].strip().strip("。.，,;；")
            if val:
                return val
    return ""


def _fmt_messages(msgs: list, title: str) -> str:
    if not msgs:
        return f"📭 {title}：没有邮件。"
    lines = [f"📬 {title}（共 {len(msgs)} 封）："]
    for i, m in enumerate(msgs, 1):
        frm = m.get("from", {}) or {}
        frm_name = frm.get("name") or frm.get("email") or "?"
        subject = m.get("subject") or "(无主题)"
        snippet = (m.get("snippet") or "").replace("\n", " ").strip()
        if len(snippet) > 60:
            snippet = snippet[:60] + "…"
        flag = "🔴" if not m.get("is_read") else "  "
        mid = m.get("message_id", "")
        lines.append(f"{flag}第{i}封【{frm_name}】{subject}")
        if snippet:
            lines.append(f"    {snippet}")
        lines.append(f"    id: {mid}")
    lines.append("\n提示：说「读第3封」即可看正文（无需复制长 ID）。")
    return "\n".join(lines)


class SkillHandler:
    def __init__(self, metadata: dict):
        self.metadata = metadata
        # 按用户缓存「最近一次邮件列表」，支持「读第N封」自然语言（避免复制长 ID）
        self._last_list = {}

    async def execute(self, message: str, context: list, user_id: str = None) -> str:
        msg = (message or "").strip()
        low = msg.lower()

        # 1) 发送意图（用户明确授权 → 直接发出）
        if any(k in low for k in ("发邮件", "发送邮件", "写邮件", "发一封", "写一封",
                                   "回邮件", "回复邮件", "发信", "寄邮件")):
            return await self._send(msg)

        # 2) 未读
        if "未读" in msg:
            return await self._list("inbox", unread=True)

        # 3) 搜索
        if any(k in low for k in ("搜索", "查找", "搜一下", "搜邮件", "search")):
            return await self._search(msg)

        # 4) 读指定邮件（消息含 msg_ id + 读/查看/打开）
        m = re.search(r"msg_[A-Za-z0-9_]+", msg)
        if m and any(k in msg for k in ("读", "查看", "看这封", "打开", "read")):
            return await self._read(m.group(0))

        # 5) 读第 N 封（自然语言，无需长 ID）
        idx_m = re.search(r"第\s*(\d+)\s*封", msg)
        if idx_m and any(k in msg for k in ("读", "看", "打开", "查看", "第")):
            return await self._read_index(int(idx_m.group(1)), user_id)

        # 6) 兜底：列出收件箱
        return await self._list("inbox", user_id=user_id)

    async def _list(self, dir_name: str = "inbox", unread: bool = False, limit: int = 8, user_id: str = None) -> str:
        args = ["message", "+list", "--dir", dir_name, "--limit", str(limit)]
        if unread:
            args += ["--is-unread"]
        rc, out, err = await _run(args)
        resp = _parse(out)
        if not resp or not resp.get("ok"):
            return f"读取邮件失败：{_err_text(resp, err)}"
        msgs = _extract_messages(resp)
        if user_id is not None:
            self._last_list[user_id] = msgs
        title = "未读邮件" if unread else f"{dir_name} 收件箱"
        return _fmt_messages(msgs, title)

    async def _search(self, msg: str, user_id: str = None) -> str:
        q = _extract_after(msg, ["搜索", "搜", "查找", "关键词", "关于", "search"])
        if not q:
            q = msg.replace("搜索", "").replace("搜", "").replace("邮件", "").strip()
        if not q:
            return "请告诉我要搜索的关键词，例如：搜索 报告"
        rc, out, err = await _run(
            ["message", "+search", "--q", q, "--limit", "8"])
        resp = _parse(out)
        if not resp or not resp.get("ok"):
            return f"搜索失败：{_err_text(resp, err)}"
        msgs = _extract_messages(resp)
        if user_id is not None:
            self._last_list[user_id] = msgs
        return _fmt_messages(msgs, f"搜索「{q}」结果")

    async def _read(self, msg_id: str) -> str:
        rc, out, err = await _run(["message", "+read", "--id", msg_id])
        resp = _parse(out)
        if not resp or not resp.get("ok"):
            return f"读取失败：{_err_text(resp, err)}"
        data = resp.get("data")
        if isinstance(data, dict) and "data" in data:
            data = data["data"]
        if not isinstance(data, dict):
            return f"读取失败：返回格式异常"
        frm = data.get("from", {}) or {}
        tos = data.get("to", []) or []
        to_str = "、".join(t.get("email", "") for t in tos if isinstance(t, dict)) or "?"
        subject = data.get("subject", "(无主题)")
        body = data.get("body") or data.get("text") or data.get("content") or ""
        body = body.replace("\r", "").strip()
        if len(body) > 1500:
            body = body[:1500] + "…（已截断）"
        lines = [
            f"📧 {subject}",
            f"发件人：{frm.get('name','')} <{frm.get('email','')}>",
            f"收件人：{to_str}",
            f"时间：{data.get('created_at','')}",
            "—" * 12,
            body or "（无正文）",
        ]
        atts = data.get("attachments") or []
        if atts:
            lines.append(f"\n📎 附件 {len(atts)} 个（用 attachment +download 下载）")
        return "\n".join(lines)

    async def _read_index(self, n: int, user_id: str = None) -> str:
        msgs = self._last_list.get(user_id) if user_id else None
        if not msgs:
            return "📭 还没有可读取的邮件列表，请先说「查收件箱」或「看未读邮件」，再告诉我第几封。"
        if n < 1 or n > len(msgs):
            return f"⚠️ 没有第 {n} 封，当前列表共 {len(msgs)} 封。"
        mid = msgs[n - 1].get("message_id")
        if not mid:
            return "⚠️ 该邮件缺少 ID，无法读取。"
        return await self._read(mid)

    async def _send(self, msg: str) -> str:
        to = _extract_after(msg, ["发给", "给", "发送到", "收件人", "to"])
        subject = _extract_after(msg, ["主题", "标题", "题目", "subject"])
        body = _extract_after(msg, ["内容", "正文", "说", "body"])
        if not to or not subject or not body:
            return ("📤 请按以下格式告诉我，我即刻发出（发送视为你已明确授权）：\n"
                    "发邮件给 <收件人> 主题 <主题> 内容 <正文>\n"
                    "例如：发邮件给 alice@x.com 主题 周报 内容 本周进展见正文。\n"
                    f"（当前发件箱：{_DEFAULT_ACCOUNT}）")
        rc, out, err = await _run(
            ["message", "+send", "--to", to, "--subject", subject,
             "--body", body, "--confirmed"])
        resp = _parse(out)
        if rc == 0 and (resp is None or resp.get("ok")):
            return f"✅ 已发送\n收件人：{to}\n主题：{subject}"
        return f"❌ 发送失败：{_err_text(resp, err)}"
