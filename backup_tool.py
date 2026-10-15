#!/usr/bin/env python3
"""BackupTool - 本地目录定时备份到腾讯云 COS 的小工具（零依赖，仅需 Python 3.8+）。

用法:
    python3 backup_tool.py                 启动服务并打开浏览器管理界面
    python3 backup_tool.py --no-browser    仅启动服务
    python3 backup_tool.py run-once        立即执行所有已启用任务后退出
    python3 backup_tool.py install-autostart / uninstall-autostart   (macOS) 登录时自动启动
"""
import argparse
import fnmatch
import hashlib
import hmac
import json
import logging
import os
import platform
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(os.environ.get("BACKUP_TOOL_HOME", Path.home() / ".backup_tool"))
CONFIG_FILE = APP_DIR / "config.json"
MANIFEST_DIR = APP_DIR / "manifests"
LOG_FILE = APP_DIR / "backup.log"
DEFAULT_PORT = 8765
DEFAULT_INTERVAL = 120
UPLOAD_WORKERS = 4
LAUNCHD_LABEL = "com.backuptool.agent"
COS_FIELDS = ("secret_id", "secret_key", "bucket", "region", "prefix")

log = logging.getLogger("backup_tool")


# ---------------------------------------------------------------- config

class Store:
    """线程安全的任务与 COS 配置存储。"""

    def __init__(self, path=CONFIG_FILE):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.tasks = []
        self.cos = {}
        self.load()

    def load(self):
        with self.lock:
            if self.path.exists():
                data = json.loads(self.path.read_text("utf-8"))
                self.tasks = data.get("tasks", [])
                self.cos = data.get("cos", {})
            for t in self.tasks:  # 兼容旧版单目录任务
                if "sources" not in t:
                    t["sources"] = [t["source"]] if t.get("source") else []
                for k in ("source", "dest", "mode", "keep", "upload_cos"):
                    t.pop(k, None)
                t.setdefault("delete_remote", False)

    def save(self):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"tasks": self.tasks, "cos": self.cos},
                                      ensure_ascii=False, indent=2), "utf-8")
            os.chmod(tmp, 0o600)  # 含 COS 密钥，仅本人可读
            tmp.replace(self.path)

    def get(self, task_id):
        with self.lock:
            return next((t for t in self.tasks if t["id"] == task_id), None)

    def upsert(self, data, task_id=None):
        task = validate_task(data)
        with self.lock:
            if any(t["name"] == task["name"] and t["id"] != task_id for t in self.tasks):
                raise ValueError(f"任务名称「{task['name']}」已存在（任务名称用作 COS 上的目录名）")
            existing = self.get(task_id) if task_id else None
            if existing:
                existing.update(task)
                task = existing
            else:
                task.update(id=uuid.uuid4().hex[:8], last_run=None, last_status=None,
                            last_message="", running=False)
                self.tasks.append(task)
            self.save()
            return dict(task)

    def delete(self, task_id):
        with self.lock:
            self.tasks = [t for t in self.tasks if t["id"] != task_id]
            self.save()
        manifest_path(task_id).unlink(missing_ok=True)

    def update_status(self, task_id, **fields):
        with self.lock:
            task = self.get(task_id)
            if task:
                task.update(fields)
                self.save()

    def snapshot(self):
        with self.lock:
            return [dict(t, sources=list(t["sources"])) for t in self.tasks]

    def cos_config(self):
        """实际生效的 COS 配置，密钥可由环境变量 COS_SECRET_ID / COS_SECRET_KEY 提供。"""
        with self.lock:
            cfg = dict(self.cos)
        cfg["secret_id"] = cfg.get("secret_id") or os.environ.get("COS_SECRET_ID", "")
        cfg["secret_key"] = cfg.get("secret_key") or os.environ.get("COS_SECRET_KEY", "")
        return cfg

    def public_cos(self):
        """给界面展示的配置，不回传密钥。"""
        cfg = self.cos_config()
        return {"secret_id": cfg["secret_id"], "has_secret_key": bool(cfg["secret_key"]),
                "bucket": cfg.get("bucket", ""), "region": cfg.get("region", ""),
                "prefix": cfg.get("prefix", ""), "configured": cos_configured(cfg)}

    def set_cos(self, data):
        cfg = {k: str(data.get(k) or "").strip() for k in COS_FIELDS}
        cfg["prefix"] = cfg["prefix"].strip("/")
        with self.lock:
            if not cfg["secret_key"]:  # 留空表示不修改已保存的密钥
                cfg["secret_key"] = self.cos.get("secret_key", "")
            self.cos = cfg
            self.save()


def source_dir_name(source):
    return Path(source).name or "root"


def validate_task(data):
    raw = data.get("sources") or []
    if isinstance(raw, str):
        raw = raw.splitlines()
    sources = []
    for s in raw:
        s = str(s).strip()
        if not s:
            continue
        p = Path(s).expanduser()
        if not p.is_dir():
            raise ValueError(f"目录不存在: {p}")
        p = str(p.resolve())
        if p not in sources:
            sources.append(p)
    if not sources:
        raise ValueError("请至少选择一个要备份的目录")
    names = [source_dir_name(s) for s in sources]
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        raise ValueError(f"目录名重复：{'、'.join(dup)}（同名目录会在 COS 上互相覆盖，请拆成不同任务）")
    name = str(data.get("name") or "").strip().strip("/") or names[0]
    if "/" in name:
        raise ValueError("任务名称不能包含 /")
    interval = int(data.get("interval_minutes") or DEFAULT_INTERVAL)
    if interval < 1:
        raise ValueError("备份间隔至少 1 分钟")
    excludes = data.get("excludes", "")
    if isinstance(excludes, list):
        excludes = ",".join(excludes)
    return {
        "name": name,
        "sources": sources,
        "interval_minutes": interval,
        "excludes": ",".join(p.strip() for p in str(excludes).split(",") if p.strip()),
        "delete_remote": bool(data.get("delete_remote", False)),
        "enabled": bool(data.get("enabled", True)),
    }


# ---------------------------------------------------------------- tencent cos

def cos_configured(cfg):
    return all(cfg.get(k) for k in ("secret_id", "secret_key", "bucket", "region"))


def _cos_quote(s):
    return urllib.parse.quote(str(s), safe="-_.~")


def _xml_find(el, name):
    return [c for c in el.iter() if c.tag.split("}")[-1] == name]


class CosClient:
    """腾讯云 COS XML API 的最小实现。

    签名算法见 https://cloud.tencent.com/document/product/436/7778
    """

    def __init__(self, cfg, timeout=120):
        self.secret_id = cfg["secret_id"]
        self.secret_key = cfg["secret_key"]
        self.host = f"{cfg['bucket']}.cos.{cfg['region']}.myqcloud.com"
        self.endpoint = cfg.get("endpoint") or f"https://{self.host}"
        self.timeout = timeout

    def _authorization(self, method, path, params, headers):
        now = int(time.time())
        key_time = f"{now - 60};{now + 3600}"
        sign_key = hmac.new(self.secret_key.encode(), key_time.encode(), hashlib.sha1).hexdigest()

        def fmt(d):
            items = sorted((_cos_quote(k).lower(), _cos_quote(v)) for k, v in d.items())
            return ";".join(k for k, _ in items), "&".join(f"{k}={v}" for k, v in items)

        param_list, param_str = fmt(params)
        header_list, header_str = fmt(headers)
        http_string = f"{method.lower()}\n{path}\n{param_str}\n{header_str}\n"
        string_to_sign = f"sha1\n{key_time}\n{hashlib.sha1(http_string.encode()).hexdigest()}\n"
        signature = hmac.new(sign_key.encode(), string_to_sign.encode(), hashlib.sha1).hexdigest()
        return (f"q-sign-algorithm=sha1&q-ak={self.secret_id}&q-sign-time={key_time}"
                f"&q-key-time={key_time}&q-header-list={header_list}"
                f"&q-url-param-list={param_list}&q-signature={signature}")

    def request(self, method, key="", params=None, data=None, length=None):
        params = {k: str(v) for k, v in (params or {}).items()}
        path = "/" + key
        url = self.endpoint + urllib.parse.quote(path, safe="/-_.~")
        if params:
            url += "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", self._authorization(method, path, params, {"host": self.host}))
        if length is not None:
            req.add_header("Content-Length", str(length))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            code = re.search(r"<Code>(.*?)</Code>", body)
            raise RuntimeError(f"COS {method} /{key} 失败: HTTP {e.code} "
                               f"{code.group(1) if code else body[:200]}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"无法连接 COS ({self.host}): {e.reason}") from None

    def put_file(self, key, path):
        size = path.stat().st_size
        with open(path, "rb") as f:
            self.request("PUT", key, data=f if size else b"", length=size)

    def delete(self, key):
        self.request("DELETE", key)

    def list_keys(self, prefix, limit=None):
        keys, marker = [], ""
        while True:
            params = {"prefix": prefix, "max-keys": limit or 1000}
            if marker:
                params["marker"] = marker
            root = ET.fromstring(self.request("GET", "", params))
            page = [k.text or "" for c in _xml_find(root, "Contents") for k in _xml_find(c, "Key")]
            keys.extend(page)
            truncated = [t.text for t in _xml_find(root, "IsTruncated")]
            if limit or not page or truncated[:1] != ["true"]:
                return keys
            nxt = [t.text for t in _xml_find(root, "NextMarker")]
            marker = nxt[0] if nxt and nxt[0] else page[-1]


# ---------------------------------------------------------------- sync engine

def _excluded(name, patterns):
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def iter_files(source, patterns):
    """遍历目录，返回 (绝对路径, 相对路径)；跳过排除项和符号链接。"""
    for root, dirs, files in os.walk(source):
        dirs[:] = [d for d in dirs if not _excluded(d, patterns)]
        root_path = Path(root)
        for f in files:
            if _excluded(f, patterns):
                continue
            full = root_path / f
            if full.is_symlink() or not full.is_file():
                continue
            yield full, full.relative_to(source)


def remote_root(cfg, task):
    parts = [cfg.get("prefix", "").strip("/"), task["name"]]
    return "/".join(p for p in parts if p) + "/"


def manifest_path(task_id):
    return MANIFEST_DIR / f"{task_id}.json"


def sync_task(cfg, task, progress=None):
    """把任务的所有目录增量同步到 COS：<前缀>/<任务名>/<目录名>/<相对路径>。

    本地记录每个已上传文件的 (大小, 修改时间)，只上传新增或有变化的文件。
    返回 (文件总数, 上传数, 删除数, 错误列表)。
    """
    client = CosClient(cfg)
    root = remote_root(cfg, task)
    target = f"{cfg['bucket']}/{root}"
    mpath = manifest_path(task["id"])
    manifest = {}
    if mpath.exists():
        saved = json.loads(mpath.read_text("utf-8"))
        if saved.get("target") == target:
            manifest = saved.get("files", {})

    patterns = [p for p in task.get("excludes", "").split(",") if p]
    errors = []
    current = {}      # 远端相对 key -> 本地路径
    scanned = set()   # 成功扫描的目录；扫描失败（如移动硬盘未接入）的目录不做删除判断
    configured = {source_dir_name(s) for s in task["sources"]}
    for src in task["sources"]:
        if not Path(src).is_dir():
            errors.append(f"目录不存在，已跳过: {src}")
            continue
        base = source_dir_name(src)
        for full, rel in iter_files(Path(src), patterns):
            current[f"{base}/{rel.as_posix()}"] = full
        scanned.add(base)

    todo = []
    for key, full in current.items():
        try:
            st = full.stat()
        except OSError as e:
            errors.append(f"{full}: {e}")
            continue
        sig = [st.st_size, int(st.st_mtime)]
        if manifest.get(key) != sig:
            todo.append((key, full, sig))

    lock = threading.Lock()
    state = {"done": 0, "saved_at": time.time()}

    def save_manifest():
        MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
        mpath.write_text(json.dumps({"target": target, "files": manifest}, ensure_ascii=False), "utf-8")

    def upload(item):
        key, full, sig = item
        client.put_file(root + key, full)
        with lock:
            manifest[key] = sig
            state["done"] += 1
            if time.time() - state["saved_at"] > 5:  # 定期落盘，中断后可续传
                save_manifest()
                state["saved_at"] = time.time()
                if progress:
                    progress(state["done"], len(todo))

    deleted = 0
    try:
        with ThreadPoolExecutor(UPLOAD_WORKERS) as pool:
            futures = [(pool.submit(upload, item), item) for item in todo]
            for fut, item in futures:
                try:
                    fut.result()
                except Exception as e:  # noqa: BLE001 - 单个文件失败不影响其他文件
                    errors.append(f"{item[1]}: {e}")
        # 本地已删除的文件，以及已从任务中移除的目录下的文件
        gone = [k for k in manifest
                if (k not in current and k.split("/", 1)[0] in scanned)
                or k.split("/", 1)[0] not in configured]
        for key in gone:
            if task.get("delete_remote"):
                try:
                    client.delete(root + key)
                    deleted += 1
                except RuntimeError as e:
                    errors.append(str(e))
                    continue
            manifest.pop(key)
    finally:
        save_manifest()
    return len(current), state["done"], deleted, errors


# ---------------------------------------------------------------- scheduler

class Scheduler:
    def __init__(self, store):
        self.store = store
        self.stop_event = threading.Event()
        self.running = set()
        self.lock = threading.Lock()

    def is_due(self, task):
        if not task["enabled"]:
            return False
        if not task.get("last_run"):
            return True
        last = datetime.fromisoformat(task["last_run"])
        return (datetime.now() - last).total_seconds() >= task["interval_minutes"] * 60

    def trigger(self, task_id, wait=False):
        with self.lock:
            if task_id in self.running:
                return False
            self.running.add(task_id)
        t = threading.Thread(target=self._run, args=(task_id,), daemon=True)
        t.start()
        if wait:
            t.join()
        return True

    def _run(self, task_id):
        task = self.store.get(task_id)
        try:
            if not task:
                return
            task = dict(task, sources=list(task["sources"]))
            self.store.update_status(task_id, running=True, last_message="正在扫描目录…")
            cfg = self.store.cos_config()
            if not cos_configured(cfg):
                raise RuntimeError("尚未配置腾讯云 COS，请先在「腾讯云 COS 设置」中填写")
            started = time.time()
            log.info("[%s] 开始备份 %s -> cos://%s/%s", task["name"], ", ".join(task["sources"]),
                     cfg["bucket"], remote_root(cfg, task))
            total, uploaded, deleted, errors = sync_task(
                cfg, task, lambda n, m: self.store.update_status(task_id, last_message=f"正在上传 {n}/{m}…"))
            msg = f"{total} 个文件，上传 {uploaded} 个"
            if deleted:
                msg += f"，删除远端 {deleted} 个"
            msg += f"，耗时 {time.time() - started:.1f}s"
            if errors:
                for e in errors:
                    log.error("[%s] %s", task["name"], e)
                raise RuntimeError(f"{msg}；{len(errors)} 个错误，例如：{errors[0]}")
            log.info("[%s] 备份完成：%s", task["name"], msg)
            self.store.update_status(task_id, last_status="ok", last_message=msg)
        except Exception as e:  # noqa: BLE001 - 记录任何失败
            log.exception("[%s] 备份失败", task_id)
            self.store.update_status(task_id, last_status="error", last_message=str(e))
        finally:
            self.store.update_status(task_id, running=False,
                                     last_run=datetime.now().isoformat(timespec="seconds"))
            with self.lock:
                self.running.discard(task_id)

    def loop(self):
        while not self.stop_event.is_set():
            for task in self.store.snapshot():
                if self.is_due(task):
                    self.trigger(task["id"])
            self.stop_event.wait(20)

    def start(self):
        threading.Thread(target=self.loop, daemon=True).start()


# ---------------------------------------------------------------- folder picker

def pick_folders():
    """弹出系统目录选择框（支持多选），返回路径列表；无法弹出时返回 None。"""
    system = platform.system()
    try:
        if system == "Darwin":
            script = ('set fs to choose folder with prompt "选择要备份的目录（可按住 ⌘ 多选）" '
                      'with multiple selections allowed\n'
                      'set out to ""\n'
                      'repeat with f in fs\n set out to out & POSIX path of f & linefeed\nend repeat\n'
                      'return out')
            r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
        elif system == "Windows":
            ps = ("Add-Type -AssemblyName System.Windows.Forms;"
                  "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
                  "if($d.ShowDialog() -eq 'OK'){$d.SelectedPath}")
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                               capture_output=True, text=True, timeout=600)
        else:
            r = subprocess.run(["zenity", "--file-selection", "--directory", "--multiple",
                                "--separator=\n"], capture_output=True, text=True, timeout=600)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return [p.rstrip("/") or "/" for p in r.stdout.splitlines() if p.strip()]


# ---------------------------------------------------------------- web ui

INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BackupTool</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1f2328;--muted:#656d76;--line:#d0d7de;--accent:#2563eb;--ok:#16a34a;--err:#dc2626}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--card:#161b22;--fg:#e6edf3;--muted:#8d96a0;--line:#30363d;--accent:#4f8cff}}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;background:var(--bg);color:var(--fg)}
a{color:var(--accent)}
.wrap{max-width:960px;margin:0 auto;padding:24px 16px}h1{font-size:22px;margin:0 0 4px}.sub{color:var(--muted);margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
label{display:block;font-size:12px;color:var(--muted);margin:10px 0 4px}
input{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}
.row{display:flex;gap:8px}.row>input{flex:1;min-width:0}.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}
@media (max-width:640px){.grid{grid-template-columns:1fr}}
button{padding:8px 14px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);cursor:pointer;font:inherit;white-space:nowrap}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}button.danger{color:var(--err)}
button.link{border:0;background:none;padding:0 4px;color:var(--err)}
button:disabled{opacity:.5;cursor:default}.actions{display:flex;gap:8px;margin-top:14px;flex-wrap:wrap}
.task{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap}
.task h3{margin:0 0 4px;font-size:16px}.meta{color:var(--muted);font-size:12px;word-break:break-all}
.badge{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;border:1px solid var(--line);margin-left:6px;font-weight:normal}
.ok{color:var(--ok)}.error{color:var(--err)}.msg{margin-top:6px;font-size:12px}
.dirs{border:1px solid var(--line);border-radius:6px;background:var(--bg);margin-bottom:8px}
.dirs div{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:6px 10px;border-top:1px solid var(--line);word-break:break-all}
.dirs div:first-child{border-top:0}.dirs .empty{color:var(--muted)}
#toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#333;color:#fff;padding:8px 16px;border-radius:6px;display:none;max-width:90vw}
.check{display:flex;align-items:center;gap:6px;margin-top:10px}.check input{width:auto}
summary{cursor:pointer;font-size:16px;font-weight:600}
</style></head><body><div class="wrap">
<h1>BackupTool</h1><div class="sub">选择本地目录，按间隔自动增量上传到腾讯云 COS。服务运行期间会自动执行到期任务。</div>
<details class="card" id="cosCard"><summary>腾讯云 COS 设置 <span class="badge" id="cosState">未配置</span></summary>
<form id="cosForm">
<div class="meta" style="margin-top:8px">密钥在 <a href="https://console.cloud.tencent.com/cam/capi" target="_blank">访问管理 → API 密钥</a> 获取，建议使用只授权该存储桶的子账号密钥。密钥只保存在本机 ~/.backup_tool/config.json。</div>
<div class="grid">
<div><label>SecretId</label><input id="cos_secret_id" autocomplete="off"></div>
<div><label>SecretKey</label><input id="cos_secret_key" type="password" autocomplete="new-password"></div>
<div><label>存储桶名称（含 APPID）</label><input id="cos_bucket" placeholder="examplebucket-1250000000"></div>
<div><label>所属地域</label><input id="cos_region" placeholder="ap-shanghai"></div>
</div>
<label>路径前缀（可选）</label><input id="cos_prefix" placeholder="backups">
<div class="actions"><button class="primary" type="submit">保存</button><button type="button" onclick="testCos()">测试连接</button></div>
</form></details>
<div class="card"><h2 id="formTitle" style="font-size:16px;margin:0">新建备份任务</h2>
<form id="form"><input type="hidden" id="tid">
<label>要备份的目录</label><div class="dirs" id="dirs"></div>
<div class="row"><input id="manualDir" placeholder="也可手动输入路径后点「添加」"><button type="button" onclick="addManual()">添加</button><button type="button" class="primary" onclick="pick()">选择目录…</button></div>
<div class="grid">
<div><label>任务名称（COS 上的目录名）</label><input id="name" placeholder="默认使用第一个目录名"></div>
<div><label>备份间隔（分钟）</label><input id="interval_minutes" type="number" min="1" value="120"></div>
</div>
<label>排除（逗号分隔）</label><input id="excludes" placeholder=".git,node_modules,*.tmp,.DS_Store">
<div class="check"><input type="checkbox" id="delete_remote"><span>本地删除的文件也从 COS 删除（不勾选则 COS 上保留）</span></div>
<div class="check"><input type="checkbox" id="enabled" checked><span>启用定时备份</span></div>
<div class="actions"><button class="primary" type="submit">保存</button><button type="button" onclick="resetForm()">清空</button></div>
</form></div>
<div id="list"></div></div><div id="toast"></div>
<script>
const $=id=>document.getElementById(id);const F=['name','interval_minutes','excludes'];let dirs=[];
function toast(t,ms){const e=$('toast');e.textContent=t;e.style.display='block';clearTimeout(e._t);e._t=setTimeout(()=>e.style.display='none',ms||3000)}
async function api(path,opt={}){const r=await fetch(path,{headers:{'Content-Type':'application/json'},...opt});const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||r.statusText);return j}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function renderDirs(){$('dirs').innerHTML=dirs.length?dirs.map((d,i)=>`<div><span>${esc(d)}</span><button type="button" class="link" onclick="dirs.splice(${i},1);renderDirs()">移除</button></div>`).join(''):'<div class="empty">还没有选择目录（选择框里按住 ⌘ 可多选）</div>'}
function addDirs(list){list.forEach(d=>{d=d.trim();if(d&&!dirs.includes(d))dirs.push(d)});renderDirs()}
function addManual(){addDirs([$('manualDir').value]);$('manualDir').value=''}
async function pick(){try{const j=await api('/api/pick-folders',{method:'POST'});if(j.paths)addDirs(j.paths);else toast('无法打开选择框，请手动输入路径')}catch(e){toast(e.message)}}
let tasks=[];
async function load(){tasks=(await api('/api/tasks')).tasks;render()}
function render(){$('list').innerHTML=tasks.length?tasks.map(t=>{
 const st=t.running?'<span class="badge">备份中…</span>':t.last_status==='ok'?'<span class="badge ok">成功</span>':t.last_status==='error'?'<span class="badge error">失败</span>':'';
 const next=t.enabled?(t.next_run?'下次：'+t.next_run.replace('T',' '):'即将执行'):'已暂停';
 return `<div class="card task"><div style="flex:1;min-width:260px"><h3>${esc(t.name)}${st}</h3>
 ${t.sources.map(s=>`<div class="meta">${esc(s)}</div>`).join('')}
 <div class="meta" style="margin-top:4px">→ cos://${esc(t.remote)} · 每 ${t.interval_minutes} 分钟${t.delete_remote?' · 同步删除':''} · 上次：${t.last_run?t.last_run.replace('T',' '):'从未'} · ${next}</div>
 ${t.last_message?`<div class="msg ${t.running?'':t.last_status}">${esc(t.last_message)}</div>`:''}</div>
 <div class="actions" style="margin:0"><button class="primary" ${t.running?'disabled':''} onclick="run('${t.id}')">立即备份</button>
 <button onclick="edit('${t.id}')">编辑</button><button class="danger" onclick="del('${t.id}')">删除</button></div></div>`}).join(''):'<div class="card meta">还没有备份任务</div>'}
function edit(id){const t=tasks.find(x=>x.id===id);$('tid').value=id;F.forEach(f=>$(f).value=t[f]);dirs=[...t.sources];renderDirs();
 $('enabled').checked=t.enabled;$('delete_remote').checked=!!t.delete_remote;$('formTitle').textContent='编辑任务：'+t.name;scrollTo(0,0)}
function resetForm(){$('form').reset();$('tid').value='';dirs=[];renderDirs();$('formTitle').textContent='新建备份任务'}
$('form').onsubmit=async e=>{e.preventDefault();if($('manualDir').value.trim())addManual();
 const body={sources:dirs,enabled:$('enabled').checked,delete_remote:$('delete_remote').checked};F.forEach(f=>body[f]=$(f).value);const id=$('tid').value;
 try{await api(id?'/api/tasks/'+id:'/api/tasks',{method:id?'PUT':'POST',body:JSON.stringify(body)});toast('已保存');resetForm();load()}catch(err){toast(err.message,5000)}}
async function run(id){try{await api(`/api/tasks/${id}/run`,{method:'POST'});toast('已开始备份');setTimeout(load,500)}catch(e){toast(e.message)}}
async function del(id){if(!confirm('删除任务？（COS 上已上传的文件不会被删除）'))return;await api('/api/tasks/'+id,{method:'DELETE'});load()}
const C=['secret_id','bucket','region','prefix'];
async function loadCos(){const c=await api('/api/cos');C.forEach(f=>$('cos_'+f).value=c[f]);$('cos_secret_key').value='';
 $('cos_secret_key').placeholder=c.has_secret_key?'已保存，留空则不修改':'';$('cosState').textContent=c.configured?c.bucket:'未配置';
 $('cosState').className='badge'+(c.configured?' ok':' error');if(!c.configured)$('cosCard').open=true}
$('cosForm').onsubmit=async e=>{e.preventDefault();const body={secret_key:$('cos_secret_key').value};C.forEach(f=>body[f]=$('cos_'+f).value);
 try{await api('/api/cos',{method:'POST',body:JSON.stringify(body)});toast('COS 设置已保存');loadCos();load()}catch(err){toast(err.message)}}
async function testCos(){toast('正在连接…');try{await api('/api/cos/test',{method:'POST'});toast('连接成功')}catch(e){toast(e.message,6000)}}
renderDirs();load();loadCos();setInterval(load,3000);
</script></body></html>"""


def make_handler(store, scheduler):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _json(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def _parts(self):
            return [p for p in self.path.split("?")[0].split("/") if p]

        def _local_only(self):
            # 防止其他网页跨站调用本地接口
            origin = self.headers.get("Origin")
            host = self.headers.get("Host", "")
            if origin and origin.split("//")[-1] != host:
                self._json(403, {"error": "forbidden"})
                return False
            return True

        def do_GET(self):
            parts = self._parts()
            if not parts:
                body = INDEX_HTML.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif parts == ["api", "tasks"]:
                cfg = store.cos_config()
                tasks = store.snapshot()
                for t in tasks:
                    t["remote"] = f"{cfg.get('bucket') or '<未配置>'}/{remote_root(cfg, t)}"
                    if t.get("last_run"):
                        nxt = datetime.fromisoformat(t["last_run"]).timestamp() + t["interval_minutes"] * 60
                        t["next_run"] = datetime.fromtimestamp(nxt).isoformat(timespec="seconds")
                self._json(200, {"tasks": tasks})
            elif parts == ["api", "cos"]:
                self._json(200, store.public_cos())
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._local_only():
                return
            parts = self._parts()
            try:
                if parts == ["api", "tasks"]:
                    self._json(200, store.upsert(self._body()))
                elif parts == ["api", "cos"]:
                    store.set_cos(self._body())
                    self._json(200, store.public_cos())
                elif parts == ["api", "cos", "test"]:
                    cfg = store.cos_config()
                    if not cos_configured(cfg):
                        raise ValueError("请先填写并保存 SecretId、SecretKey、存储桶和地域")
                    try:
                        CosClient(cfg, timeout=15).list_keys(cfg.get("prefix", ""), limit=1)
                    except RuntimeError as e:
                        raise ValueError(str(e)) from None
                    self._json(200, {"ok": True})
                elif parts == ["api", "pick-folders"]:
                    self._json(200, {"paths": pick_folders()})
                elif len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] == "run" \
                        and store.get(parts[2]):
                    started = scheduler.trigger(parts[2])
                    self._json(200 if started else 409,
                               {"ok": True} if started else {"error": "该任务正在备份中"})
                else:
                    self._json(404, {"error": "not found"})
            except (ValueError, TypeError) as e:
                self._json(400, {"error": str(e)})

        def do_PUT(self):
            if not self._local_only():
                return
            parts = self._parts()
            if len(parts) == 3 and parts[:2] == ["api", "tasks"] and store.get(parts[2]):
                try:
                    self._json(200, store.upsert(self._body(), parts[2]))
                except (ValueError, TypeError) as e:
                    self._json(400, {"error": str(e)})
            else:
                self._json(404, {"error": "not found"})

        def do_DELETE(self):
            if not self._local_only():
                return
            parts = self._parts()
            if len(parts) == 3 and parts[:2] == ["api", "tasks"]:
                store.delete(parts[2])
                self._json(200, {"ok": True})
            else:
                self._json(404, {"error": "not found"})

    return Handler


# ---------------------------------------------------------------- autostart (macOS)

def plist_path():
    return Path.home() / "Library/LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def install_autostart(port):
    if platform.system() != "Darwin":
        sys.exit("自动启动目前仅支持 macOS（launchd）")
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key><array>
    <string>{sys.executable}</string><string>{Path(__file__).resolve()}</string>
    <string>--no-browser</string><string>--port</string><string>{port}</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{APP_DIR / 'launchd.out'}</string>
  <key>StandardErrorPath</key><string>{APP_DIR / 'launchd.err'}</string>
</dict></plist>
""")
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
    subprocess.run(["launchctl", "load", str(path)], check=True)
    print(f"已安装开机自启：{path}\n管理界面：http://127.0.0.1:{port}")


def uninstall_autostart():
    path = plist_path()
    if path.exists():
        subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
        path.unlink()
    print("已移除开机自启")


# ---------------------------------------------------------------- main

def setup_logging():
    APP_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
                                  logging.StreamHandler()])


def main(argv=None):
    parser = argparse.ArgumentParser(description="本地目录定时备份到腾讯云 COS")
    parser.add_argument("command", nargs="?", default="serve",
                        choices=["serve", "run-once", "install-autostart", "uninstall-autostart"])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    setup_logging()

    if args.command == "install-autostart":
        return install_autostart(args.port)
    if args.command == "uninstall-autostart":
        return uninstall_autostart()

    store = Store()
    for t in store.tasks:  # 上次异常退出可能残留 running 标记
        t["running"] = False
    scheduler = Scheduler(store)

    if args.command == "run-once":
        for t in store.snapshot():
            if t["enabled"]:
                scheduler.trigger(t["id"], wait=True)
        return

    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(store, scheduler))
    scheduler.start()
    url = f"http://127.0.0.1:{args.port}"
    log.info("BackupTool 已启动：%s  (配置: %s)", url, CONFIG_FILE)
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("已退出")


if __name__ == "__main__":
    main()
