"""LifeOS 数据突变备份（本地磁盘 + NAS）。

设计要点：
- 备份对象：PostgreSQL(pg_dump) / Redis(RDB 快照) / Qdrant(tar，仅容器运行才备) / 配置目录 data/(OTP、运行时设置等)
- 多目标：本地磁盘 + NAS（任一已挂载可写目录），各目标独立 try/except，单点失败互不影响
- 调度：lifespan 每日定时（backup_schedule_hour）+ 手动 POST /api/backup/run
- 容器由 Docker Desktop 托管，后端虽为宿主进程，但 docker.exe 在 PATH 内，采用 `docker exec` 方式导出数据

安全：每个组件独立 try/except；二进制用字节流处理；不阻塞主流程。
"""
import os
import json
import shutil
import subprocess
import datetime
import threading
import logging

logger = logging.getLogger("lifeos.backup")

# ====================== 容器与凭据（与 docker-compose.yml 保持一致）======================
PG_CONTAINER = "lifeos-postgres"
PG_USER = "lifeos"
PG_DB = "lifeos"
PG_PASSWORD = os.environ.get("LIFEOS_DB_PW", "lifeos_pg_2026")

REDIS_CONTAINER = "lifeos-redis"
QDRANT_CONTAINER = "lifeos-qdrant"

# docker 可执行文件：优先 PATH，否则回退 Docker Desktop 默认安装路径（Windows）
DOCKER = shutil.which("docker") or r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"

# 状态文件名（隐藏）：放在每个目标根目录，供 /api/backup/status 读取
_STATUS_FILE = ".lifeos_backup_status.json"


# ====================== 底层执行 ======================
def _docker_exec(container, args, extra_env=None, text=False):
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return subprocess.run([DOCKER, "exec", container] + args,
                          capture_output=True, env=env, text=text)


def _is_running(container) -> bool:
    insp = subprocess.run([DOCKER, "inspect", "-f", "{{.State.Running}}", container],
                          capture_output=True, text=True)
    return insp.stdout.strip() == "true"


def _ts() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _backup_postgres(run_dir, ts):
    """pg_dump 自定义格式，经 stdout 落盘。"""
    out = os.path.join(run_dir, f"lifeos-pg-{ts}.dump")
    r = _docker_exec(PG_CONTAINER, ["pg_dump", "-U", PG_USER, "-d", PG_DB, "-F", "c"],
                     extra_env={"PGPASSWORD": PG_PASSWORD})
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "pg_dump 非零退出")
    if not r.stdout:
        raise RuntimeError("pg_dump 输出为空")
    with open(out, "wb") as f:
        f.write(r.stdout)
    return os.path.getsize(out)


def _backup_redis(run_dir, ts):
    """redis-cli --rdb - 把 RDB 快照经 stdout 输出（二进制）。"""
    out = os.path.join(run_dir, f"lifeos-redis-{ts}.rdb")
    r = _docker_exec(REDIS_CONTAINER, ["redis-cli", "--rdb", "-"])
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "redis-cli 非零退出")
    if not r.stdout:
        raise RuntimeError("RDB 输出为空")
    with open(out, "wb") as f:
        f.write(r.stdout)
    return os.path.getsize(out)


def _backup_qdrant(run_dir, ts):
    """仅当 lifeos-qdrant 容器实际运行才备份（tar 容器内 /qdrant/storage）。"""
    if not _is_running(QDRANT_CONTAINER):
        return None  # 明确跳过（未启用 Qdrant）
    out = os.path.join(run_dir, f"lifeos-qdrant-{ts}.tar.gz")
    r = subprocess.run([DOCKER, "exec", QDRANT_CONTAINER, "tar", "czf", "-", "-C", "/qdrant/storage", "."],
                       capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "tar 非零退出")
    if not r.stdout:
        raise RuntimeError("Qdrant 快照为空")
    with open(out, "wb") as f:
        f.write(r.stdout)
    return os.path.getsize(out)


def _backup_data_dir(run_dir, ts, data_dir):
    """打包 data/ 配置目录（OTP 密钥、运行时设置等）。用 shutil 保证跨平台。"""
    if not data_dir or not os.path.isdir(data_dir):
        return None
    base = os.path.join(run_dir, f"lifeos-data-{ts}")
    archive = shutil.make_archive(base, "gztar", root_dir=data_dir)
    return os.path.getsize(archive)


def _prune_old(root, now, retention):
    removed = 0
    if not os.path.isdir(root):
        return 0
    for name in os.listdir(root):
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            continue
        try:
            bts = datetime.datetime.strptime(os.path.basename(d.rstrip("\\/")), "%Y%m%d-%H%M%S")
        except ValueError:
            continue
        if (now - bts).days > retention:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed


# ====================== 单目标备份 ======================
def run_backup_target(root, ts, retention, data_dir):
    """对单个目标目录执行一次完整备份，返回结果 dict（不抛异常）。"""
    results = {}
    run_dir = os.path.join(root, ts)
    os.makedirs(run_dir, exist_ok=True)
    now = datetime.datetime.now()

    # 1) PostgreSQL
    try:
        sz = _backup_postgres(run_dir, ts)
        results["postgres"] = {"status": "ok", "size": sz}
    except Exception as e:
        results["postgres"] = {"status": "fail", "error": str(e)}

    # 2) Redis
    try:
        sz = _backup_redis(run_dir, ts)
        results["redis"] = {"status": "ok", "size": sz}
    except Exception as e:
        results["redis"] = {"status": "fail", "error": str(e)}

    # 3) Qdrant（可选）
    try:
        sz = _backup_qdrant(run_dir, ts)
        if sz is None:
            results["qdrant"] = {"status": "skip"}
        else:
            results["qdrant"] = {"status": "ok", "size": sz}
    except Exception as e:
        results["qdrant"] = {"status": "fail", "error": str(e)}

    # 4) 配置目录 data/
    try:
        sz = _backup_data_dir(run_dir, ts, data_dir)
        if sz is None:
            results["data"] = {"status": "skip"}
        else:
            results["data"] = {"status": "ok", "size": sz}
    except Exception as e:
        results["data"] = {"status": "fail", "error": str(e)}

    # 5) 清理旧备份
    try:
        removed = _prune_old(root, now, retention)
        results["pruned"] = removed
    except Exception as e:
        results["pruned"] = f"fail: {e}"

    # 清单
    manifest = {
        "timestamp": ts,
        "target": root,
        "results": results,
        "finished_at": now.isoformat(timespec="seconds"),
    }
    with open(os.path.join(run_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    # 隐藏状态文件（供 /api/backup/status 读取，无需遍历目录）
    status = {"last_backup": ts, "target": root, "results": results}
    with open(os.path.join(root, _STATUS_FILE), "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)

    return {"target": root, "timestamp": ts, "results": results}


# ====================== 多目标编排 ======================
def run_backup_all(local_root, nas_root="", retention=7, data_dir="./data"):
    """本地磁盘 + NAS 双目标备份；任一目标失败不互相影响。

    返回结构：
    {
      "started_at": ..., "finished_at": ...,
      "targets": [ {target, timestamp, results}, ... ]
    }
    """
    targets = []
    if local_root:
        targets.append(local_root)
    if nas_root:
        targets.append(nas_root)

    summary = {
        "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "targets": [],
    }
    ts = _ts()
    for t in targets:
        try:
            os.makedirs(t, exist_ok=True)
            summary["targets"].append(run_backup_target(t, ts, retention, data_dir))
        except Exception as e:
            summary["targets"].append({"target": t, "error": str(e)})
    summary["finished_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    return summary


# ====================== 定时调度（守护线程）======================
_backup_thread = None
_backup_stop = None


def _scheduler_loop(local_root, nas_root, retention, data_dir, hour):
    while not _backup_stop.is_set():
        now = datetime.datetime.now()
        # 计算到下一个目标小时点的等待秒数
        next_run = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run = next_run + datetime.timedelta(days=1)
        wait = (next_run - now).total_seconds()
        if _backup_stop.wait(wait):
            break
        try:
            run_backup_all(local_root, nas_root, retention, data_dir)
            logger.info("定时备份完成（目标小时=%02d:00）", hour)
        except Exception as e:
            logger.error("定时备份失败: %s", e)


def start_backup_scheduler(local_root, nas_root, retention, data_dir, hour):
    global _backup_thread, _backup_stop
    if _backup_thread and _backup_thread.is_alive():
        return
    _backup_stop = threading.Event()
    _backup_thread = threading.Thread(
        target=_scheduler_loop,
        args=(local_root, nas_root, retention, data_dir, hour),
        daemon=True,
    )
    _backup_thread.start()
    logger.info("备份定时调度已启动（每日 %02d:00，本地=%s，NAS=%s）", hour, local_root, nas_root or "无")


def stop_backup_scheduler():
    global _backup_stop
    if _backup_stop:
        _backup_stop.set()
    logger.info("备份定时调度已停止")


def is_scheduler_running() -> bool:
    """供 /api/backup/status 查询定时调度是否存活。"""
    return bool(_backup_thread and _backup_thread.is_alive())
