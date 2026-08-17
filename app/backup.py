"""LifeOS 数据突变备份（本地磁盘 + NAS）。

设计要点：
- 备份对象：PostgreSQL(pg_dump) / Redis(RDB 快照) / Qdrant(tar，仅容器运行才备) / 配置目录 data/(OTP、运行时设置等)
- 多目标：本地磁盘 + NAS（任一已挂载可写目录），各目标独立 try/except，单点失败互不影响
- 调度：lifespan 每日定时（backup_schedule_hour）+ 手动 POST /api/backup/run
- 容器由 Docker Desktop 托管，后端虽为宿主进程，但 docker.exe 在 PATH 内，采用 `docker exec` 方式导出数据

安全：每个组件独立 try/except；二进制用字节流处理；不阻塞主流程。
"""
import os
import re
import io
import json
import stat
import shutil
import tempfile
import subprocess
import tarfile
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


# ====================== 目标归一化 + 传输层 ======================
def normalize_target(t):
    """把前端/配置中的目标（可能只有旧版 {path,enabled}）归一化为含 method 的完整结构，
    并派生稳定 path 作为状态/还原主键。"""
    if isinstance(t, str):
        t = {"path": t}
    t = dict(t or {})
    raw_path = (t.get("path") or "").strip()
    typ = t.get("type") or ("nas" if raw_path.startswith("\\\\") else "local")
    method = (t.get("method") or ("smb" if typ == "nas" else "local")).lower()
    host = (t.get("host") or "").strip()
    try:
        port = int(t.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    share = (t.get("share") or "").strip().strip("\\/")
    directory = (t.get("directory") or "").strip().strip("\\/")
    username = (t.get("username") or "").strip()
    password = t.get("password") or ""
    enabled = bool(t.get("enabled", True))

    if method == "smb" and not host and raw_path.startswith("\\\\"):
        # 旧 UNC 路径拆出 host/share/directory，便于 net use 认证
        parts = raw_path.replace("/", "\\").lstrip("\\").split("\\")
        if len(parts) >= 2:
            host, share = parts[0], parts[1]
            directory = "/".join(parts[2:])

    if method == "local":
        path = raw_path or directory
    elif method == "smb":
        base = raw_path
        if not base and host and share:
            base = f"\\\\{host}\\{share}"
            if directory:
                base = base.rstrip("\\") + "\\" + directory
        path = base
    elif method == "sftp":
        port = port or 22
        path = f"sftp://{username}@{host}:{port}/{directory}".rstrip("/")
    elif method == "ftp":
        port = port or 21
        path = f"ftp://{host}:{port}/{directory}".rstrip("/")
    elif method == "webdav":
        port = port or (443 if t.get("https") else 80)
        scheme = "https" if t.get("https") else "http"
        path = f"{scheme}://{host}:{port}/{directory}".rstrip("/")
    else:
        path = raw_path
    return {
        "type": typ, "method": method, "host": host, "port": port,
        "share": share, "directory": directory, "username": username,
        "password": password, "enabled": enabled, "path": path,
    }


class BackupDest:
    """抽象备份目标：确保就绪、上传文件、列点、下载、清理、写本地状态缓存。"""
    def __init__(self, norm, data_dir):
        self.norm = norm
        self.data_dir = data_dir
        self.root = norm["path"]

    def ensure(self, ts):
        raise NotImplementedError

    def put_local(self, ts, rel_name, local_path):
        raise NotImplementedError

    def put_text(self, ts, rel_name, text):
        raise NotImplementedError

    def list_points(self):
        raise NotImplementedError

    def fetch_to_local(self, ts, rel_name, local_dir):
        raise NotImplementedError

    def prune(self, retention):
        raise NotImplementedError

    def close(self):
        pass

    def _status_path(self):
        safe = re.sub(r'[^A-Za-z0-9._-]', '_', self.root)
        d = os.path.join(self.data_dir, "backup_status")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, safe + ".json")

    def write_status(self, status):
        try:
            with open(self._status_path(), "w", encoding="utf-8") as f:
                json.dump(status, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("写本地状态缓存失败: %s", e)


class _FileDest(BackupDest):
    """本地磁盘 / SMB 共享（Windows 原生 UNC，可选 net use 认证）。"""
    def _auth(self):
        if self.norm["method"] == "smb" and self.norm.get("username") and self.norm.get("host") and self.norm.get("share"):
            unc = f"\\\\{self.norm['host']}\\{self.norm['share']}"
            pw = self.norm.get("password") or ""
            cmd = ["net", "use", unc, f"/user:{self.norm['username']}", pw]
            try:
                subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            except Exception as e:
                logger.warning("net use %s 失败(可能已连接): %s", unc, e)

    def ensure(self, ts):
        self._auth()
        os.makedirs(os.path.join(self.root, ts), exist_ok=True)

    def _remote(self, ts, rel_name):
        return os.path.join(self.root, ts, rel_name)

    def put_local(self, ts, rel_name, local_path):
        dst = self._remote(ts, rel_name)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(local_path, dst)

    def put_text(self, ts, rel_name, text):
        with open(self._remote(ts, rel_name), "w", encoding="utf-8") as f:
            f.write(text)

    def list_points(self):
        self._auth()
        root = self.root
        if not os.path.isdir(root):
            return []
        points = []
        for name in sorted(os.listdir(root), reverse=True):
            d = os.path.join(root, name)
            if not os.path.isdir(d):
                continue
            try:
                datetime.datetime.strptime(name, "%Y%m%d-%H%M%S")
            except ValueError:
                continue
            info = {"timestamp": name, "path": d, "components": {}}
            mfp = os.path.join(d, "manifest.json")
            if os.path.exists(mfp):
                try:
                    with open(mfp, "r", encoding="utf-8") as f:
                        info["components"] = json.load(f).get("results", {})
                except Exception:
                    pass
            points.append(info)
        return points

    def fetch_to_local(self, ts, rel_name, local_dir):
        self._auth()
        src = self._remote(ts, rel_name)
        dst = os.path.join(local_dir, rel_name)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        return dst

    def prune(self, retention):
        self._auth()
        _prune_old(self.root, datetime.datetime.now(), retention)


class _SftpDest(BackupDest):
    """SFTP（paramiko）。"""
    def __init__(self, norm, data_dir):
        super().__init__(norm, data_dir)
        self._ssh = None
        self._sftp = None
        self._base = norm["directory"].strip("/") or "."

    def _connect(self):
        import paramiko
        self._ssh = paramiko.SSHClient()
        self._ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kw = {"hostname": self.norm["host"], "port": self.norm["port"] or 22, "timeout": 20}
        if self.norm.get("username"):
            kw["username"] = self.norm["username"]
        if self.norm.get("password"):
            kw["password"] = self.norm["password"]
        self._ssh.connect(**kw)
        self._sftp = self._ssh.open_sftp()

    def _remote(self, ts, rel_name):
        return f"{self._base}/{ts}/{rel_name}"

    def _makedirs(self, path):
        parts = [p for p in path.split("/") if p]
        cur = ""
        for p in parts:
            cur = f"{cur}/{p}" if cur else p
            try:
                self._sftp.mkdir(cur)
            except IOError:
                pass

    def ensure(self, ts):
        if self._sftp is None:
            self._connect()
        self._makedirs(f"{self._base}/{ts}")

    def put_local(self, ts, rel_name, local_path):
        self._sftp.put(local_path, self._remote(ts, rel_name))

    def put_text(self, ts, rel_name, text):
        self._sftp.putfo(io.StringIO(text), self._remote(ts, rel_name))

    def list_points(self):
        if self._sftp is None:
            self._connect()
        try:
            entries = self._sftp.listdir_attr(self._base)
        except IOError:
            return []
        points = []
        for e in sorted(entries, key=lambda x: x.filename, reverse=True):
            if not stat.S_ISDIR(e.st_mode):
                continue
            name = e.filename
            try:
                datetime.datetime.strptime(name, "%Y%m%d-%H%M%S")
            except ValueError:
                continue
            info = {"timestamp": name, "path": f"{self._base}/{name}", "components": {}}
            try:
                with self._sftp.open(f"{self._base}/{name}/manifest.json") as f:
                    info["components"] = json.loads(f.read().decode("utf-8")).get("results", {})
            except Exception:
                pass
            points.append(info)
        return points

    def fetch_to_local(self, ts, rel_name, local_dir):
        dst = os.path.join(local_dir, rel_name)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        self._sftp.get(self._remote(ts, rel_name), dst)
        return dst

    def prune(self, retention):
        if self._sftp is None:
            self._connect()
        try:
            entries = self._sftp.listdir_attr(self._base)
        except IOError:
            return
        now = datetime.datetime.now()
        for e in entries:
            if not stat.S_ISDIR(e.st_mode):
                continue
            try:
                bts = datetime.datetime.strptime(e.filename, "%Y%m%d-%H%M%S")
            except ValueError:
                continue
            if (now - bts).days > retention:
                self._rmtree_remote(f"{self._base}/{e.filename}")

    def _rmtree_remote(self, path):
        for e in self._sftp.listdir_attr(path):
            fp = f"{path}/{e.filename}"
            if stat.S_ISDIR(e.st_mode):
                self._rmtree_remote(fp)
            else:
                self._sftp.remove(fp)
        self._sftp.rmdir(path)

    def close(self):
        try:
            if self._sftp:
                self._sftp.close()
            if self._ssh:
                self._ssh.close()
        except Exception:
            pass


class _FtpDest(BackupDest):
    """FTP（ftplib，标准库）。"""
    def __init__(self, norm, data_dir):
        super().__init__(norm, data_dir)
        self._ftp = None
        self._base = norm["directory"].strip("/") or "/"

    def _connect(self):
        from ftplib import FTP
        self._ftp = FTP(timeout=20)
        self._ftp.connect(self.norm["host"], self.norm["port"] or 21)
        if self.norm.get("username"):
            self._ftp.login(self.norm["username"], self.norm.get("password") or "")
        else:
            self._ftp.login()

    def _remote(self, ts, rel_name):
        return f"{self._base}/{ts}/{rel_name}"

    def _makedirs(self, path):
        cur = ""
        for p in path.split("/"):
            if not p:
                continue
            cur = f"{cur}/{p}" if cur else p
            try:
                self._ftp.mkd(cur)
            except Exception:
                pass

    def ensure(self, ts):
        if self._ftp is None:
            self._connect()
        self._makedirs(f"{self._base}/{ts}")

    def put_local(self, ts, rel_name, local_path):
        with open(local_path, "rb") as f:
            self._ftp.storbinary(f"STOR {self._remote(ts, rel_name)}", f)

    def put_text(self, ts, rel_name, text):
        self._ftp.storlines(f"STOR {self._remote(ts, rel_name)}", io.StringIO(text))

    def list_points(self):
        if self._ftp is None:
            self._connect()
        try:
            names = self._ftp.nlst(self._base)
        except Exception:
            return []
        points = []
        for full in names:
            name = full.rstrip("/").split("/")[-1]
            try:
                datetime.datetime.strptime(name, "%Y%m%d-%H%M%S")
            except ValueError:
                continue
            info = {"timestamp": name, "path": f"{self._base}/{name}", "components": {}}
            try:
                buf = io.BytesIO()
                self._ftp.retrbinary(f"RETR {self._base}/{name}/manifest.json", buf.write)
                info["components"] = json.loads(buf.getvalue().decode("utf-8")).get("results", {})
            except Exception:
                pass
            points.append(info)
        return points

    def fetch_to_local(self, ts, rel_name, local_dir):
        dst = os.path.join(local_dir, rel_name)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            self._ftp.retrbinary(f"RETR {self._remote(ts, rel_name)}", f.write)
        return dst

    def prune(self, retention):
        if self._ftp is None:
            self._connect()
        try:
            names = self._ftp.nlst(self._base)
        except Exception:
            return
        now = datetime.datetime.now()
        for full in names:
            name = full.rstrip("/").split("/")[-1]
            try:
                bts = datetime.datetime.strptime(name, "%Y%m%d-%H%M%S")
            except ValueError:
                continue
            if (now - bts).days > retention:
                self._rmtree_remote(f"{self._base}/{name}")

    def _rmtree_remote(self, path):
        try:
            names = self._ftp.nlst(path)
        except Exception:
            return
        for full in names:
            name = full.rstrip("/").split("/")[-1]
            if name in (".", ".."):
                continue
            fp = f"{path}/{name}"
            try:
                self._ftp.delete(fp)
            except Exception:
                self._rmtree_remote(fp)
        try:
            self._ftp.rmd(path)
        except Exception:
            pass

    def close(self):
        try:
            if self._ftp:
                self._ftp.quit()
        except Exception:
            pass


class _WebdavDest(BackupDest):
    """WebDAV（requests，支持 http/https，需服务器端开启 WebDAV）。"""
    def __init__(self, norm, data_dir):
        super().__init__(norm, data_dir)
        self._sess = None
        scheme = "https" if norm.get("https") else "http"
        port = norm["port"] or (443 if norm.get("https") else 80)
        base = (norm["directory"] or "").strip("/")
        self._base_url = f"{scheme}://{norm['host']}:{port}"
        if base:
            self._base_url += "/" + base

    def _connect(self):
        import requests
        self._sess = requests.Session()
        if self.norm.get("username"):
            self._sess.auth = (self.norm["username"], self.norm.get("password") or "")

    def _url(self, ts, rel_name):
        return f"{self._base_url}/{ts}/{rel_name}"

    def _makedirs(self, path):
        cur = self._base_url
        for p in path.split("/"):
            if not p:
                continue
            cur = f"{cur}/{p}"
            try:
                r = self._sess.request("MKCOL", cur, timeout=20)
                if r.status_code not in (201, 200, 405):
                    logger.debug("WebDAV MKCOL %s -> %s", cur, r.status_code)
            except Exception as e:
                logger.warning("WebDAV MKCOL %s 失败: %s", cur, e)

    def ensure(self, ts):
        if self._sess is None:
            self._connect()
        self._makedirs(f"{self._base_url}/{ts}")

    def put_local(self, ts, rel_name, local_path):
        with open(local_path, "rb") as f:
            self._sess.put(self._url(ts, rel_name), data=f, timeout=60)

    def put_text(self, ts, rel_name, text):
        self._sess.put(self._url(ts, rel_name), data=text.encode("utf-8"), timeout=60)

    def list_points(self):
        if self._sess is None:
            self._connect()
        try:
            r = self._sess.request("PROPFIND", self._base_url, headers={"Depth": "1"}, timeout=20)
            r.raise_for_status()
        except Exception as e:
            logger.warning("WebDAV PROPFIND 失败: %s", e)
            return []
        import xml.etree.ElementTree as ET
        ns = {"d": "DAV:"}
        now = datetime.datetime.now()
        points = []
        try:
            root = ET.fromstring(r.content)
        except Exception:
            return []
        for resp in root.findall("d:response", ns):
            href = resp.findtext("d:href", namespaces=ns) or ""
            name = href.rstrip("/").split("/")[-1]
            try:
                datetime.datetime.strptime(name, "%Y%m%d-%H%M%S")
            except ValueError:
                continue
            info = {"timestamp": name, "path": href, "components": {}}
            try:
                mr = self._sess.request("PROPFIND", f"{self._base_url}/{name}/manifest.json",
                                        headers={"Depth": "0"}, timeout=20)
                if mr.status_code == 200:
                    info["components"] = json.loads(mr.text).get("results", {})
            except Exception:
                pass
            points.append(info)
        points.sort(key=lambda x: x["timestamp"], reverse=True)
        return points

    def fetch_to_local(self, ts, rel_name, local_dir):
        dst = os.path.join(local_dir, rel_name)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        r = self._sess.get(self._url(ts, rel_name), timeout=60)
        r.raise_for_status()
        with open(dst, "wb") as f:
            f.write(r.content)
        return dst

    def prune(self, retention):
        if self._sess is None:
            self._connect()
        pts = self.list_points()
        cutoff = datetime.datetime.now() - datetime.timedelta(days=retention)
        for p in pts:
            try:
                bts = datetime.datetime.strptime(p["timestamp"], "%Y%m%d-%H%M%S")
            except ValueError:
                continue
            if bts < cutoff:
                try:
                    self._sess.request("DELETE", f"{self._base_url}/{p['timestamp']}", timeout=20)
                except Exception:
                    pass

    def close(self):
        try:
            if self._sess:
                self._sess.close()
        except Exception:
            pass


def get_dest(norm, data_dir):
    m = norm["method"]
    if m in ("local", "smb"):
        return _FileDest(norm, data_dir)
    if m == "sftp":
        return _SftpDest(norm, data_dir)
    if m == "ftp":
        return _FtpDest(norm, data_dir)
    if m == "webdav":
        return _WebdavDest(norm, data_dir)
    return _FileDest(norm, data_dir)


# ====================== 单目标备份 ======================
def _safe_name(root):
    """把任意目标路径转成可用于目录/文件名的稳定短串（防特殊字符）。"""
    return re.sub(r'[^A-Za-z0-9._-]', '_', root)


def run_backup_target(norm, ts, retention, data_dir):
    """对单个归一化目标执行一次完整备份（含 4 组件），返回结果 dict（不抛异常）。

    norm: normalize_target() 产出的完整结构（含 method/host 等，供传输层建连）。
    """
    root = norm["path"]
    results = {}
    # 本地暂存：先把各组件导出到本地，再交给传输层 put（远程目标必须，本地/smb 也兼容）
    stage = os.path.join(data_dir, "backup_stage", _safe_name(root), ts)
    os.makedirs(stage, exist_ok=True)
    dest = get_dest(norm, data_dir)
    now = datetime.datetime.now()
    logger.info("开始备份目标 %s（method=%s，时间戳 %s）", root, norm["method"], ts)
    try:
        dest.ensure(ts)

        # 1) PostgreSQL
        try:
            logger.info("导出 PostgreSQL（pg_dump）...")
            sz = _backup_postgres(stage, ts)
            rel = f"lifeos-pg-{ts}.dump"
            dest.put_local(ts, rel, os.path.join(stage, rel))
            results["postgres"] = {"status": "ok", "size": sz}
            logger.info("PostgreSQL 备份完成：%.2f MB", sz / 1e6)
        except Exception as e:
            results["postgres"] = {"status": "fail", "error": str(e)}
            logger.error("PostgreSQL 备份失败：%s", e)

        # 2) Redis
        try:
            logger.info("导出 Redis（RDB 快照）...")
            sz = _backup_redis(stage, ts)
            rel = f"lifeos-redis-{ts}.rdb"
            dest.put_local(ts, rel, os.path.join(stage, rel))
            results["redis"] = {"status": "ok", "size": sz}
            logger.info("Redis 备份完成：%.2f MB", sz / 1e6)
        except Exception as e:
            results["redis"] = {"status": "fail", "error": str(e)}
            logger.error("Redis 备份失败：%s", e)

        # 3) Qdrant（可选）
        try:
            logger.info("导出 Qdrant（向量快照）...")
            sz = _backup_qdrant(stage, ts)
            if sz is None:
                results["qdrant"] = {"status": "skip"}
                logger.info("Qdrant 未运行，跳过")
            else:
                rel = f"lifeos-qdrant-{ts}.tar.gz"
                dest.put_local(ts, rel, os.path.join(stage, rel))
                results["qdrant"] = {"status": "ok", "size": sz}
                logger.info("Qdrant 备份完成：%.2f MB", sz / 1e6)
        except Exception as e:
            results["qdrant"] = {"status": "fail", "error": str(e)}
            logger.error("Qdrant 备份失败：%s", e)

        # 4) 配置目录 data/
        try:
            logger.info("打包配置目录 data/ ...")
            sz = _backup_data_dir(stage, ts, data_dir)
            if sz is None:
                results["data"] = {"status": "skip"}
                logger.info("data/ 不存在，跳过")
            else:
                rel = f"lifeos-data-{ts}.tar.gz"
                dest.put_local(ts, rel, os.path.join(stage, rel))
                results["data"] = {"status": "ok", "size": sz}
                logger.info("配置目录备份完成：%.2f MB", sz / 1e6)
        except Exception as e:
            results["data"] = {"status": "fail", "error": str(e)}
            logger.error("配置目录备份失败：%s", e)

        # 5) 清单（本地 + 远程各一份）
        manifest = {
            "timestamp": ts,
            "target": root,
            "method": norm["method"],
            "results": results,
            "finished_at": now.isoformat(timespec="seconds"),
        }
        mtext = json.dumps(manifest, ensure_ascii=False, indent=2)
        with open(os.path.join(stage, "manifest.json"), "w", encoding="utf-8") as f:
            f.write(mtext)
        dest.put_text(ts, "manifest.json", mtext)

        # 6) 清理旧备份（由传输层按目标实现）
        try:
            dest.prune(retention)
            results["pruned"] = "ok"
            logger.info("已清理过期备份（保留 %d 天）", retention)
        except Exception as e:
            results["pruned"] = f"fail: {e}"
            logger.error("清理过期备份失败：%s", e)

        # 7) 本地状态缓存（供 /api/backup/status，无需遍历远程目录）
        status = {
            "last_backup": ts,
            "target": root,
            "method": norm["method"],
            "enabled": norm["enabled"],
            "results": results,
        }
        dest.write_status(status)
        logger.info("结束目标 %s 备份流程", root)
    except Exception as e:
        logger.error("目标 %s 备份流程异常：%s", root, e)
    finally:
        dest.close()
        shutil.rmtree(stage, ignore_errors=True)

    return {"target": root, "method": norm["method"], "timestamp": ts, "results": results}


# ====================== 多目标编排 ======================
def run_backup_all(targets, retention, data_dir):
    """多目标备份（本地磁盘 + NAS，协议含 SMB/SFTP/FTP/WebDAV），任一目标失败不互相影响。

    targets: [原始目标 dict（可能含 {path,enabled} 或完整 {method,host,...}）, ...]
    返回结构：
    {
      "started_at": ..., "finished_at": ...,
      "targets": [ {target, method, timestamp, results} | {target, error}, ... ]
    }
    """
    enabled = []
    for t in (targets or []):
        n = normalize_target(t)
        if n["enabled"] and n["path"]:
            enabled.append(n)
    if not enabled:
        logger.warning("没有启用的备份目标，跳过备份")
        return {"started_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "targets": [], "warning": "无启用目标"}

    logger.info("开始全量备份（%d 个启用目标，保留=%d天）", len(enabled), retention)
    summary = {
        "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "targets": [],
    }
    ts = _ts()
    for n in enabled:
        try:
            summary["targets"].append(run_backup_target(n, ts, retention, data_dir))
        except Exception as e:
            summary["targets"].append({"target": n["path"], "error": str(e)})
    summary["finished_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    logger.info("全量备份结束（%d 个目标）", len(summary["targets"]))
    return summary


# ====================== 还原支持 ======================
VALID_COMPONENTS = ("postgres", "redis", "qdrant", "data")

_REL_BY_COMPONENT = {
    "postgres": "lifeos-pg-{ts}.dump",
    "redis": "lifeos-redis-{ts}.rdb",
    "qdrant": "lifeos-qdrant-{ts}.tar.gz",
    "data": "lifeos-data-{ts}.tar.gz",
}


def list_backup_points(norm, data_dir):
    """列出某归一化目标下所有备份时间点（目录名 = 时间戳），返回每个点的组件概要。

    norm: normalize_target() 产出；远程目标经传输层 list_points() 读取。
    """
    dest = get_dest(norm, data_dir)
    try:
        return dest.list_points()
    finally:
        dest.close()


def _restore_postgres(point_dir, ts):
    """pg_restore 自定义格式，经容器 stdin 还原（--clean --if-exists 先清后建）。"""
    dump = os.path.join(point_dir, f"lifeos-pg-{ts}.dump")
    if not os.path.exists(dump):
        raise RuntimeError("找不到 PostgreSQL 备份文件")
    with open(dump, "rb") as f:
        data = f.read()
    r = subprocess.run(
        [DOCKER, "exec", "-i", PG_CONTAINER, "pg_restore", "-U", PG_USER,
         "-d", PG_DB, "--clean", "--if-exists", "-F", "c"],
        input=data, capture_output=True,
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).decode("utf-8", "ignore").strip() or "pg_restore 非零退出")
    return True


def _restore_redis(point_dir, ts):
    """把 RDB 快照拷入容器数据目录并重启 Redis，使其加载该快照。"""
    rdb = os.path.join(point_dir, f"lifeos-redis-{ts}.rdb")
    if not os.path.exists(rdb):
        raise RuntimeError("找不到 Redis 备份文件")
    cp = subprocess.run([DOCKER, "cp", rdb, f"{REDIS_CONTAINER}:/data/dump.rdb"], capture_output=True)
    if cp.returncode != 0:
        raise RuntimeError((cp.stderr or cp.stdout).decode("utf-8", "ignore").strip() or "docker cp 失败")
    rs = subprocess.run([DOCKER, "restart", REDIS_CONTAINER], capture_output=True)
    if rs.returncode != 0:
        raise RuntimeError((rs.stderr or rs.stdout).decode("utf-8", "ignore").strip() or "redis 重启失败")
    return True


def _restore_qdrant(point_dir, ts):
    """解包 Qdrant 向量快照到临时目录，停容器→拷入→启动，确保重新加载存储。"""
    tar = os.path.join(point_dir, f"lifeos-qdrant-{ts}.tar.gz")
    if not os.path.exists(tar):
        raise RuntimeError("找不到 Qdrant 备份文件（该时间点未备份 Qdrant）")
    tmp = os.path.join(point_dir, "_qdrant_restore_tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    with tarfile.open(tar, "r:gz") as tf:
        tf.extractall(tmp)
    sp = subprocess.run([DOCKER, "stop", QDRANT_CONTAINER], capture_output=True)
    if sp.returncode != 0:
        raise RuntimeError((sp.stderr or sp.stdout).decode("utf-8", "ignore").strip() or "qdrant 停止失败")
    cp = subprocess.run([DOCKER, "cp", tmp + "/.", f"{QDRANT_CONTAINER}:/qdrant/storage/"], capture_output=True)
    if cp.returncode != 0:
        subprocess.run([DOCKER, "start", QDRANT_CONTAINER], capture_output=True)
        raise RuntimeError((cp.stderr or cp.stdout).decode("utf-8", "ignore").strip() or "docker cp 失败")
    st = subprocess.run([DOCKER, "start", QDRANT_CONTAINER], capture_output=True)
    if st.returncode != 0:
        raise RuntimeError((st.stderr or st.stdout).decode("utf-8", "ignore").strip() or "qdrant 启动失败")
    shutil.rmtree(tmp, ignore_errors=True)
    return True


def _restore_data(point_dir, ts, data_dir):
    """解包配置目录（data/）并覆盖回当前 data 目录。

    注意：保护当前 settings_runtime.json（含面板/调度配置）不被旧备份覆盖，
    避免还原后备份目标与定时调度配置回退。
    """
    tar = os.path.join(point_dir, f"lifeos-data-{ts}.tar.gz")
    if not os.path.exists(tar):
        raise RuntimeError("找不到配置目录备份（该时间点未备份 data）")
    if not data_dir or not os.path.isdir(data_dir):
        raise RuntimeError("当前 data 目录不可用")
    tmp = os.path.join(point_dir, "_data_restore_tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    with tarfile.open(tar, "r:gz") as tf:
        tf.extractall(tmp)
    # 保留当前运行时配置
    runtime_file = os.path.join(data_dir, "settings_runtime.json")
    backup_runtime = None
    if os.path.exists(runtime_file):
        with open(runtime_file, "r", encoding="utf-8") as f:
            backup_runtime = f.read()
    for name in os.listdir(tmp):
        if name == "settings_runtime.json":
            continue
        src = os.path.join(tmp, name)
        dst = os.path.join(data_dir, name)
        if os.path.isdir(src):
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    if backup_runtime is not None:
        with open(runtime_file, "w", encoding="utf-8") as f:
            f.write(backup_runtime)
    shutil.rmtree(tmp, ignore_errors=True)
    return True


def run_restore(norm, timestamp, components, data_dir):
    """还原指定归一化目标/时间点的部分或全部组件；返回各组件结果（不抛异常）。

    远程目标（SFTP/FTP/WebDAV/SMB）会先经传输层把组件拉到本地暂存，再走现有还原逻辑。
    """
    root = norm["path"]
    dest = get_dest(norm, data_dir)
    local_point = os.path.join(data_dir, "restore_stage", timestamp)
    os.makedirs(local_point, exist_ok=True)
    try:
        comps = [c for c in (components or []) if c in VALID_COMPONENTS]
        if not comps:
            raise RuntimeError("未选择任何有效还原组件")
        logger.info("开始还原（目标=%s，时间点=%s，组件=%s）", root, timestamp, ",".join(comps))
        results = {}
        for c in comps:
            rel = _REL_BY_COMPONENT[c].format(ts=timestamp)
            try:
                # 远程目标先把文件拉到本地暂存；本地目标也会复制一份（幂等）
                dest.fetch_to_local(timestamp, rel, local_point)
                if c == "postgres":
                    ok = _restore_postgres(local_point, timestamp)
                elif c == "redis":
                    ok = _restore_redis(local_point, timestamp)
                elif c == "qdrant":
                    ok = _restore_qdrant(local_point, timestamp)
                elif c == "data":
                    ok = _restore_data(local_point, timestamp, data_dir)
                else:
                    ok = False
                results[c] = {"status": "ok"} if ok else {"status": "fail", "error": "未知失败"}
                logger.info("还原组件 %s 完成", c)
            except Exception as e:
                results[c] = {"status": "fail", "error": str(e)}
                logger.error("还原组件 %s 失败：%s", c, e)
        logger.info("还原流程结束")
        return {
            "target": root,
            "timestamp": timestamp,
            "components": comps,
            "results": results,
            "finished_at": datetime.datetime.now().isoformat(timespec="seconds"),
        }
    finally:
        dest.close()
        shutil.rmtree(local_point, ignore_errors=True)


# ====================== 定时调度（守护线程）======================
_backup_thread = None
_backup_stop = None


def _scheduler_loop(targets, retention, data_dir, hour):
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
            enabled = [t for t in (targets or []) if t.get("enabled") and t.get("path")]
            run_backup_all(enabled, retention, data_dir)
            logger.info("定时备份完成（目标小时=%02d:00）", hour)
        except Exception as e:
            logger.error("定时备份失败: %s", e)


def start_backup_scheduler(targets, retention, data_dir, hour):
    global _backup_thread, _backup_stop
    if _backup_thread and _backup_thread.is_alive():
        return
    _backup_stop = threading.Event()
    _backup_thread = threading.Thread(
        target=_scheduler_loop,
        args=(targets, retention, data_dir, hour),
        daemon=True,
    )
    _backup_thread.start()
    enabled = [t for t in (targets or []) if t.get("enabled") and t.get("path")]
    logger.info("备份定时调度已启动（每日 %02d:00，%d 个启用目标）", hour, len(enabled))


def stop_backup_scheduler():
    global _backup_stop
    if _backup_stop:
        _backup_stop.set()
    logger.info("备份定时调度已停止")


def is_scheduler_running() -> bool:
    """供 /api/backup/status 查询定时调度是否存活。"""
    return bool(_backup_thread and _backup_thread.is_alive())
