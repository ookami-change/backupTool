#!/usr/bin/env python3
"""BackupTool - 本地目录定时备份小工具（零依赖，仅需 Python 3.8+）。

用法:
    python3 backup_tool.py                 启动服务并打开浏览器管理界面
    python3 backup_tool.py --no-browser    仅启动服务
    python3 backup_tool.py run-once        立即执行所有已启用任务后退出
    python3 backup_tool.py install-autostart / uninstall-autostart   (macOS) 登录时自动启动
"""
import argparse
import fnmatch
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(os.environ.get("BACKUP_TOOL_HOME", Path.home() / ".backup_tool"))
CONFIG_FILE = APP_DIR / "config.json"
LOG_FILE = APP_DIR / "backup.log"
DEFAULT_PORT = 8765
TS_FORMAT = "%Y%m%d-%H%M%S"
LAUNCHD_LABEL = "com.backuptool.agent"

log = logging.getLogger("backup_tool")


# ---------------------------------------------------------------- config

class Store:
    """线程安全的任务配置存储。"""

    def __init__(self, path=CONFIG_FILE):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.tasks = []
        self.load()

    def load(self):
        with self.lock:
            if self.path.exists():
                self.tasks = json.loads(self.path.read_text("utf-8")).get("tasks", [])

    def save(self):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"tasks": self.tasks}, ensure_ascii=False, indent=2), "utf-8")
            tmp.replace(self.path)

    def get(self, task_id):
        with self.lock:
            return next((t for t in self.tasks if t["id"] == task_id), None)

    def upsert(self, data, task_id=None):
        task = validate_task(data)
        with self.lock:
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

    def update_status(self, task_id, **fields):
        with self.lock:
            task = self.get(task_id)
            if task:
                task.update(fields)
                self.save()

    def snapshot(self):
        with self.lock:
            return [dict(t) for t in self.tasks]


def validate_task(data):
    source = Path(str(data.get("source", "")).strip()).expanduser()
    dest = Path(str(data.get("dest", "")).strip()).expanduser()
    if not str(data.get("source", "")).strip() or not source.is_dir():
        raise ValueError(f"源目录不存在: {source}")
    if not str(data.get("dest", "")).strip():
        raise ValueError("请填写备份目标目录")
    source, dest = source.resolve(), dest.resolve()
    if source == dest:
        raise ValueError("源目录和目标目录不能相同")
    interval = int(data.get("interval_minutes") or 60)
    keep = int(data.get("keep") or 10)
    mode = data.get("mode", "snapshot")
    if interval < 1:
        raise ValueError("备份间隔至少 1 分钟")
    if keep < 1:
        raise ValueError("保留份数至少 1")
    if mode not in ("snapshot", "zip"):
        raise ValueError("备份方式只能是 snapshot 或 zip")
    excludes = data.get("excludes", "")
    if isinstance(excludes, list):
        excludes = ",".join(excludes)
    return {
        "name": str(data.get("name") or source.name).strip(),
        "source": str(source),
        "dest": str(dest),
        "interval_minutes": interval,
        "keep": keep,
        "mode": mode,
        "excludes": ",".join(p.strip() for p in str(excludes).split(",") if p.strip()),
        "enabled": bool(data.get("enabled", True)),
    }


# ---------------------------------------------------------------- backup engine

def _excluded(name, patterns):
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def iter_files(source, patterns, skip_dir=None):
    """遍历源目录，返回 (绝对路径, 相对路径)；跳过排除项及位于源目录内的备份目标。"""
    for root, dirs, files in os.walk(source):
        root_path = Path(root)
        dirs[:] = [d for d in dirs
                   if not _excluded(d, patterns)
                   and (skip_dir is None or (root_path / d).resolve() != skip_dir)]
        for f in files:
            if _excluded(f, patterns):
                continue
            full = root_path / f
            if full.is_symlink() or not full.is_file():
                continue
            yield full, full.relative_to(source)


def _snapshot_name(existing):
    """生成新备份名，保证排序在已有备份之后（同一秒内追加 -2、-3 ...）。"""
    ts = datetime.now().strftime(TS_FORMAT)
    last = _backup_sort_key(existing[-1]) if existing else None
    n = 1
    while last is not None and (ts, n) <= last:
        n += 1
    return ts if n == 1 else f"{ts}-{n}"


def task_backup_root(task):
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in task["name"]) or "backup"
    return Path(task["dest"]) / f"{safe}-{task['id']}"


def list_backups(task):
    root = task_backup_root(task)
    if not root.exists():
        return []
    items = [p for p in root.iterdir()
             if not p.name.startswith(".") and (p.is_dir() or p.suffix == ".zip")]
    return sorted(items, key=_backup_sort_key)


def _backup_sort_key(p):
    # 名称形如 20261015-120000 或同一秒内的 20261015-120000-2
    stem = p.name[:-4] if p.name.endswith(".zip") else p.name
    ts, _, n = stem[:15], stem[15:16], stem[16:]
    return ts, int(n) if n.isdigit() else 1


def run_backup(task):
    """执行一次备份，返回 (文件数, 新拷贝数, 输出路径)。

    snapshot 模式: 每次生成一个完整目录快照，未变化的文件与上一份快照做硬链接，
    只占用变化文件的空间（类似 rsync --link-dest / Time Machine）。
    zip 模式: 每次生成一个 zip 压缩包。
    """
    source = Path(task["source"])
    if not source.is_dir():
        raise FileNotFoundError(f"源目录不存在: {source}")
    root = task_backup_root(task)
    root.mkdir(parents=True, exist_ok=True)
    patterns = [p for p in task.get("excludes", "").split(",") if p]
    skip = root.resolve()
    existing = list_backups(task)
    name = _snapshot_name(existing)
    previous = [p for p in existing if p.is_dir()]
    prev = previous[-1] if previous else None
    total = copied = 0

    if task["mode"] == "zip":
        out = root / f"{name}.zip"
        tmp = root / f".{name}.zip.part"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for full, rel in iter_files(source, patterns, skip):
                zf.write(full, rel.as_posix())
                total += 1
        tmp.replace(out)
        copied = total
    else:
        out = root / name
        tmp = root / f".{name}.part"
        if tmp.exists():
            shutil.rmtree(tmp)
        for full, rel in iter_files(source, patterns, skip):
            target = tmp / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            total += 1
            st = full.stat()
            if prev is not None:
                old = prev / rel
                try:
                    ost = old.stat()
                    if ost.st_size == st.st_size and int(ost.st_mtime) == int(st.st_mtime):
                        os.link(old, target)
                        continue
                except OSError:
                    pass
            shutil.copy2(full, target)
            copied += 1
        tmp.mkdir(parents=True, exist_ok=True)
        tmp.rename(out)

    prune(task)
    return total, copied, out


def prune(task):
    backups = list_backups(task)
    for old in backups[:-task["keep"]] if len(backups) > task["keep"] else []:
        log.info("[%s] 清理旧备份 %s", task["name"], old)
        if old.is_dir():
            shutil.rmtree(old, ignore_errors=True)
        else:
            old.unlink(missing_ok=True)


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
            task = dict(task)
            self.store.update_status(task_id, running=True)
            started = time.time()
            log.info("[%s] 开始备份 %s -> %s", task["name"], task["source"], task["dest"])
            total, copied, out = run_backup(task)
            msg = f"{total} 个文件，新复制 {copied} 个，耗时 {time.time() - started:.1f}s -> {out.name}"
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

def pick_folder():
    system = platform.system()
    try:
        if system == "Darwin":
            r = subprocess.run(["osascript", "-e",
                                'POSIX path of (choose folder with prompt "选择目录")'],
                               capture_output=True, text=True, timeout=300)
        elif system == "Windows":
            ps = ("Add-Type -AssemblyName System.Windows.Forms;"
                  "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
                  "if($d.ShowDialog() -eq 'OK'){$d.SelectedPath}")
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                               capture_output=True, text=True, timeout=300)
        else:
            r = subprocess.run(["zenity", "--file-selection", "--directory"],
                               capture_output=True, text=True, timeout=300)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    path = r.stdout.strip()
    if not path:
        return None
    return path.rstrip("/") or "/"


# ---------------------------------------------------------------- web ui

INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BackupTool</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1f2328;--muted:#656d76;--line:#d0d7de;--accent:#2563eb;--ok:#16a34a;--err:#dc2626}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--card:#161b22;--fg:#e6edf3;--muted:#8d96a0;--line:#30363d;--accent:#4f8cff}}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;background:var(--bg);color:var(--fg)}
.wrap{max-width:960px;margin:0 auto;padding:24px 16px}h1{font-size:22px;margin:0 0 4px}.sub{color:var(--muted);margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
label{display:block;font-size:12px;color:var(--muted);margin:10px 0 4px}
input,select{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}
.row{display:flex;gap:8px}.row>*{flex:1}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
@media (max-width:640px){.grid{grid-template-columns:1fr 1fr}}
button{padding:8px 14px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);cursor:pointer;font:inherit;white-space:nowrap}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}button.danger{color:var(--err)}
button:disabled{opacity:.5;cursor:default}.actions{display:flex;gap:8px;margin-top:14px;flex-wrap:wrap}
.task{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap}
.task h3{margin:0 0 4px;font-size:16px}.meta{color:var(--muted);font-size:12px;word-break:break-all}
.badge{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;border:1px solid var(--line);margin-left:6px}
.ok{color:var(--ok)}.error{color:var(--err)}.msg{margin-top:6px;font-size:12px}
#toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#333;color:#fff;padding:8px 16px;border-radius:6px;display:none}
.check{display:flex;align-items:center;gap:6px;margin-top:10px}.check input{width:auto}
</style></head><body><div class="wrap">
<h1>BackupTool</h1><div class="sub">选择本地目录，按间隔自动备份。服务运行期间会自动执行到期任务。</div>
<div class="card"><h2 id="formTitle" style="font-size:16px;margin:0">新建备份任务</h2>
<form id="form"><input type="hidden" id="tid">
<label>任务名称</label><input id="name" placeholder="默认使用源目录名">
<label>源目录（要备份的目录）</label><div class="row"><input id="source" required><button type="button" style="flex:0" onclick="pick('source')">选择…</button></div>
<label>目标目录（备份存放位置）</label><div class="row"><input id="dest" required><button type="button" style="flex:0" onclick="pick('dest')">选择…</button></div>
<div class="grid">
<div><label>备份间隔（分钟）</label><input id="interval_minutes" type="number" min="1" value="60"></div>
<div><label>保留份数</label><input id="keep" type="number" min="1" value="10"></div>
<div><label>备份方式</label><select id="mode"><option value="snapshot">目录快照（增量硬链接）</option><option value="zip">ZIP 压缩包</option></select></div>
<div><label>排除（逗号分隔）</label><input id="excludes" placeholder=".git,node_modules,*.tmp"></div>
</div>
<div class="check"><input type="checkbox" id="enabled" checked><span>启用定时备份</span></div>
<div class="actions"><button class="primary" type="submit">保存</button><button type="button" onclick="resetForm()">清空</button></div>
</form></div>
<div id="list"></div></div><div id="toast"></div>
<script>
const $=id=>document.getElementById(id);const F=['name','source','dest','interval_minutes','keep','mode','excludes'];
function toast(t){const e=$('toast');e.textContent=t;e.style.display='block';clearTimeout(e._t);e._t=setTimeout(()=>e.style.display='none',2500)}
async function api(path,opt={}){const r=await fetch(path,{headers:{'Content-Type':'application/json'},...opt});const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||r.statusText);return j}
async function pick(f){try{const j=await api('/api/pick-folder',{method:'POST'});if(j.path)$(f).value=j.path}catch(e){toast('无法打开选择框，请手动输入路径')}}
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
let tasks=[];
async function load(){tasks=(await api('/api/tasks')).tasks;render()}
function render(){$('list').innerHTML=tasks.length?tasks.map(t=>{
 const st=t.running?'<span class="badge">备份中…</span>':t.last_status==='ok'?'<span class="badge ok">成功</span>':t.last_status==='error'?'<span class="badge error">失败</span>':'';
 const next=t.enabled?(t.next_run?'下次：'+t.next_run.replace('T',' '):'即将执行'):'已暂停';
 return `<div class="card task"><div style="flex:1;min-width:260px"><h3>${esc(t.name)}${st}</h3>
 <div class="meta">${esc(t.source)} → ${esc(t.dest)}</div>
 <div class="meta">每 ${t.interval_minutes} 分钟 · 保留 ${t.keep} 份 · ${t.mode==='zip'?'ZIP':'快照'} · 已有 ${t.backup_count} 份 · 上次：${t.last_run?t.last_run.replace('T',' '):'从未'} · ${next}</div>
 ${t.last_message?`<div class="msg ${t.last_status}">${esc(t.last_message)}</div>`:''}</div>
 <div class="actions" style="margin:0"><button class="primary" ${t.running?'disabled':''} onclick="run('${t.id}')">立即备份</button>
 <button onclick="openDir('${t.id}')">打开</button><button onclick="edit('${t.id}')">编辑</button>
 <button class="danger" onclick="del('${t.id}')">删除</button></div></div>`}).join(''):'<div class="card meta">还没有备份任务</div>'}
function edit(id){const t=tasks.find(x=>x.id===id);$('tid').value=id;F.forEach(f=>$(f).value=t[f]);$('enabled').checked=t.enabled;$('formTitle').textContent='编辑任务：'+t.name;scrollTo(0,0)}
function resetForm(){$('form').reset();$('tid').value='';$('formTitle').textContent='新建备份任务'}
$('form').onsubmit=async e=>{e.preventDefault();const body={enabled:$('enabled').checked};F.forEach(f=>body[f]=$(f).value);const id=$('tid').value;
 try{await api(id?'/api/tasks/'+id:'/api/tasks',{method:id?'PUT':'POST',body:JSON.stringify(body)});toast('已保存');resetForm();load()}catch(err){toast(err.message)}}
async function run(id){try{await api(`/api/tasks/${id}/run`,{method:'POST'});toast('已开始备份');setTimeout(load,500)}catch(e){toast(e.message)}}
async function openDir(id){try{await api(`/api/tasks/${id}/open`,{method:'POST'})}catch(e){toast(e.message)}}
async function del(id){if(!confirm('删除任务？（已有备份文件不会被删除）'))return;await api('/api/tasks/'+id,{method:'DELETE'});load()}
load();setInterval(load,3000);
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
                tasks = store.snapshot()
                for t in tasks:
                    t["backup_count"] = len(list_backups(t))
                    if t.get("last_run"):
                        nxt = datetime.fromisoformat(t["last_run"]).timestamp() + t["interval_minutes"] * 60
                        t["next_run"] = datetime.fromtimestamp(nxt).isoformat(timespec="seconds")
                self._json(200, {"tasks": tasks})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._local_only():
                return
            parts = self._parts()
            try:
                if parts == ["api", "tasks"]:
                    self._json(200, store.upsert(self._body()))
                elif parts == ["api", "pick-folder"]:
                    self._json(200, {"path": pick_folder()})
                elif len(parts) == 4 and parts[:2] == ["api", "tasks"] and store.get(parts[2]):
                    if parts[3] == "run":
                        started = scheduler.trigger(parts[2])
                        self._json(200 if started else 409,
                                   {"ok": True} if started else {"error": "该任务正在备份中"})
                    elif parts[3] == "open":
                        root = task_backup_root(store.get(parts[2]))
                        root.mkdir(parents=True, exist_ok=True)
                        opener = {"Darwin": "open", "Windows": "explorer"}.get(platform.system(), "xdg-open")
                        subprocess.Popen([opener, str(root)])
                        self._json(200, {"ok": True})
                    else:
                        self._json(404, {"error": "not found"})
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
    parser = argparse.ArgumentParser(description="本地目录定时备份小工具")
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
