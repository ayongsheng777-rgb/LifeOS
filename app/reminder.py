"""LifeOS 主动提醒引擎：定时扫描 reminders 表，到点走飞书推送。

与 backup.py 不同，本调度依赖异步 DB 引擎（引擎绑定于 uvicorn 主事件循环），
因此调度以【asyncio 任务】形式运行在主循环内（而非独立线程 + 新 loop），
避免「不同事件循环」冲突。

链路：due_reminders → push_to_users（飞书）→ 标记 sent / 顺延回访。
纯程序执行，不依赖任何 AI 能力。
"""
import asyncio
import logging
from typing import Optional

log = logging.getLogger("lifeos.reminder")

_stop: Optional[asyncio.Event] = None
_task: Optional[asyncio.Task] = None
SCAN_SECONDS = 60


async def _loop() -> None:
    from app.skills.db_store import (
        due_reminders, mark_reminder_sent, reschedule_reminder,
    )
    from app.feishu import bot
    from app.config import settings

    while not _stop.is_set():
        try:
            due = await due_reminders(advance_window=True)
            for r in due:
                text = f"⏰ 丽素 提醒：{r['title']}"
                if r.get("detail"):
                    text += f"\n{r['detail']}"
                try:
                    sent = await bot.push_to_users(settings.feishu_admin_users, text)
                except Exception:
                    sent = 0
                if sent > 0:
                    try:
                        if r.get("repeat_interval_days") and r.get("repeat_remaining", 0) > 0:
                            await reschedule_reminder(r["id"])
                        else:
                            await mark_reminder_sent(r["id"])
                    except Exception as e:
                        log.warning("提醒状态更新失败: %s", e)
                    log.info("提醒已推送: %s", r["title"])
        except Exception as e:
            log.error("提醒扫描异常: %s", e)
        # 等待 SCAN_SECONDS，期间可被 _stop 提前唤醒
        try:
            await asyncio.wait_for(_stop.wait(), timeout=SCAN_SECONDS)
        except asyncio.TimeoutError:
            pass


def start_reminder_scheduler() -> None:
    """在调用方所在事件循环内启动扫描任务（须在 async 上下文中调用）。"""
    global _task, _stop
    if _task is not None and not _task.done():
        return
    _stop = asyncio.Event()
    _task = asyncio.create_task(_loop())
    log.info("提醒调度已启动（每 %ds 扫描）", SCAN_SECONDS)


def stop_reminder_scheduler() -> None:
    global _stop, _task
    if _stop:
        _stop.set()
    if _task is not None:
        _task.cancel()
        _task = None
