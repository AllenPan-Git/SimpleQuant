"""
检查、下载、准备更新（后台线程；界面只读 UPDATER.status）

流程：读取最新发布 → 下载 manifest.json 与签名并验证 → 逐个对比本机程序文件
  → 有对应当前版本的补丁包、且补丁包含全部需要的文件：下载补丁包（只含变化的文件）；否则下载完整安装包
  → 校验 SHA256 → 补丁解压到 updates/<版本>/staging 并逐个校验 → 写 ready.json，状态 ready
  → 用户点「重启并更新」：apply() 启动 apply_update.ps1，程序退出；脚本等程序退出后替换文件，再重新打开程序
  → 下次启动：startup() 读取脚本写的 result.json，显示结果并清理

需要下载的内容超过 AUTO_DOWNLOAD_MB 时不自动下载（用户可能在用流量），等用户点「下载更新」。
源码运行（未打包）只提示有新版本，不下载。
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import requests

from simplequant import __version__
from simplequant.paths import DATA_ROOT, FROZEN
from simplequant import update as cfg
from simplequant.update import manifest as mf

UPDATE_DIR = DATA_ROOT / "updates"
APP_DIR = Path(sys.executable).resolve().parent      # 安装目录（只在打包版里有意义）
SCRIPT = Path(__file__).with_name("apply_update.ps1")
AUTO_DOWNLOAD_MB = 30
TIMEOUT = (10, 30)        # 连接、读取超时（秒）
RETRIES = 4
# Inno Setup 写的卸载信息（安装包 AppId + _is1）；补丁更新后改这里的版本号，「应用和功能」里显示才对
UNINSTALL_KEY = r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{E1A695AA-65CF-43E6-B7C1-E05D9C531879}_is1"


class UpdateError(Exception):
    """kind：network（连不上 / 下载失败）、verify（签名或校验值不对）、other"""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(detail or kind)
        self.kind, self.detail = kind, detail


@dataclass
class Status:
    state: str = "idle"       # idle checking latest available downloading ready error
    version: str = ""         # 新版本号
    notes: str = ""           # 发布说明（Markdown）
    page: str = cfg.RELEASES_PAGE
    size: int = 0             # 需要下载的字节数
    done: int = 0             # 已下载的字节数
    full: bool = False        # True：需要下载完整安装包
    error: str = ""           # UpdateError.kind
    detail: str = ""
    last: dict | None = None  # 上次更新的结果 {"ok", "version", "message"}（启动时读取，界面显示一次）


class Updater:
    def __init__(self, app_dir: Path = APP_DIR, work: Path = UPDATE_DIR, frozen: bool = FROZEN,
                 current: str = __version__, sources: list[str] | None = None, public_key: str | None = None):
        self.app_dir, self.work, self.frozen, self.current = Path(app_dir), Path(work), frozen, current
        self.sources = sources if sources is not None else cfg.SOURCES
        self.public_key = cfg.PUBLIC_KEY if public_key is None else public_key
        self.status = Status()
        self.session = requests.Session()
        self._plan: dict | None = None
        self._lock = threading.Lock()
        self._busy = False

    # ---------- 状态 ----------
    @property
    def installable(self) -> bool:
        """能否自动安装（打包版才能）"""
        return self.frozen

    @property
    def busy(self) -> bool:
        return self._busy

    def _set(self, **kw):
        for k, v in kw.items():
            setattr(self.status, k, v)

    def _run(self, fn, *args) -> bool:
        """后台运行 fn；已有任务在运行时返回 False"""
        with self._lock:
            if self._busy:
                return False
            self._busy = True

        def work():
            try:
                fn(*args)
            except UpdateError as e:
                self._set(state="error", error=e.kind, detail=e.detail)
            except Exception as e:  # noqa: BLE001 - 后台线程，出错只显示在设置页
                self._set(state="error", error="other", detail=f"{type(e).__name__}: {e}")
            finally:
                self._busy = False
        threading.Thread(target=work, daemon=True, name="update").start()
        return True

    # ---------- 启动时 ----------
    def startup(self):
        """读取上次更新的结果；已下载好的更新恢复为 ready；清理过期文件"""
        res = self.work / "result.json"
        if res.exists():
            try:
                self.status.last = json.loads(res.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                self.status.last = {"ok": False, "version": "", "message": "result"}
            self._clean(everything=True)
        ready = self.work / "ready.json"
        if ready.exists():
            try:
                plan = json.loads(ready.read_text(encoding="utf-8"))
                newer = mf.parse_version(plan["version"]) > mf.parse_version(self.current)
                present = Path(plan["installer"] if plan["mode"] == "installer" else plan["staging"]).exists()
            except (OSError, ValueError, KeyError):
                newer = present = False
            if newer and present and self.frozen:
                self._plan = plan
                self._set(state="ready", version=plan["version"], notes=plan.get("notes", ""),
                          full=plan["mode"] == "installer", page=plan.get("page", cfg.RELEASES_PAGE))
            else:
                self._clean(everything=True)

    def _clean(self, everything: bool = False, keep: str = ""):
        """删除下载、解压、备份的文件（keep：保留这个版本的目录）"""
        if not self.work.exists():
            return
        for p in self.work.iterdir():
            if p.name in ("update.log", "result.json") or (keep and p.name == keep):
                continue
            if everything or p.name != "ready.json":
                shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
        if everything:
            (self.work / "result.json").unlink(missing_ok=True)

    # ---------- 检查 ----------
    def check_async(self) -> bool:
        return self._run(self.check)

    def check(self):
        if self.status.state == "ready":
            return      # 已下载好的更新先装上；更新的版本等下次启动再检查（联网失败也不会把 ready 改成出错）
        self._set(state="checking", error="", detail="")
        rel = self._latest()
        if mf.parse_version(rel["version"]) <= mf.parse_version(self.current):
            self._set(state="latest", version=rel["version"])
            return
        self._set(version=rel["version"], notes=rel["notes"], page=rel["page"])
        if not self.installable:
            self._set(state="available", size=0, full=False)
            return
        m = self._manifest(rel)
        self._plan = self._make_plan(m, rel)
        self._set(state="available", size=self._plan["asset"]["size"], done=0, full=self._plan["full"])
        if self._plan["asset"]["size"] <= AUTO_DOWNLOAD_MB * 1e6:
            self.download()

    def _get(self, url: str, **kw) -> requests.Response:
        try:
            r = self.session.get(url, timeout=TIMEOUT, headers={"User-Agent": f"SimpleQuant/{self.current}"}, **kw)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            raise UpdateError("network", f"{type(e).__name__}: {e}") from e

    def _latest(self) -> dict:
        """{version, notes, page, assets: {文件名: 下载地址}}；按 SOURCES 顺序尝试"""
        err = UpdateError("network", "no source")
        for url in self.sources:
            try:
                d = self._get(url).json()
                return {"version": d["tag_name"].lstrip("vV"), "notes": d.get("body") or "",
                        "page": d.get("html_url") or cfg.RELEASES_PAGE,
                        "assets": {a["name"]: a["browser_download_url"] for a in d.get("assets", [])}}
            except UpdateError as e:
                err = e
            except (ValueError, KeyError, TypeError) as e:
                err = UpdateError("network", f"bad response: {e}")
        raise err

    def _manifest(self, rel: dict) -> dict:
        urls = rel["assets"]
        if mf.MANIFEST not in urls or mf.SIGNATURE not in urls:
            raise UpdateError("other", "release has no manifest")
        data = self._get(urls[mf.MANIFEST]).content
        sig = self._get(urls[mf.SIGNATURE]).text
        try:
            m = mf.load_verified(data, sig, self.public_key)
        except mf.ManifestError as e:
            raise UpdateError("verify", str(e)) from e
        if mf.parse_version(m["version"]) != mf.parse_version(rel["version"]):
            raise UpdateError("verify", f"manifest version {m['version']} != {rel['version']}")
        return m

    def _make_plan(self, m: dict, rel: dict) -> dict:
        """对比本机文件，决定下载补丁包还是完整安装包"""
        files = m["files"]
        need = []
        for r, (sha, size) in files.items():
            p = self.app_dir / r
            try:
                if p.stat().st_size == size and mf.sha256_file(p) == sha:
                    continue
            except OSError:
                pass
            need.append(r)
        patch = m.get("patches", {}).get(self.current)
        if patch and set(need) <= set(patch["files"]) and patch["name"] in rel["assets"]:
            asset, full = patch, False
            removed = [r for r in patch.get("removed", []) if (self.app_dir / r).exists()]
        else:
            asset, full, removed = m.get("installer"), True, []
            if not asset or asset["name"] not in rel["assets"]:
                raise UpdateError("other", "release has no installer")
        return {"version": m["version"], "notes": rel["notes"], "page": rel["page"], "asset": asset,
                "url": rel["assets"][asset["name"]], "full": full, "need": need, "removed": removed,
                "files": {r: files[r][0] for r in need}}

    # ---------- 下载 ----------
    def download_async(self) -> bool:
        return self._run(self.download)

    def download(self):
        plan = self._plan
        if not plan or not self.installable:
            return
        asset, version = plan["asset"], plan["version"]
        self._clean(everything=True, keep=version)
        vdir = self.work / version
        vdir.mkdir(parents=True, exist_ok=True)
        dest = vdir / asset["name"]
        self._set(state="downloading", size=asset["size"], done=0, error="", detail="")
        self._fetch(plan["url"], dest, asset["size"])
        if mf.sha256_file(dest) != asset["sha256"]:
            dest.unlink(missing_ok=True)
            raise UpdateError("verify", asset["name"])

        ready = {"version": version, "notes": plan["notes"], "page": plan["page"],
                 "app_dir": str(self.app_dir), "work": str(self.work),
                 "result": str(self.work / "result.json"), "log": str(self.work / "update.log"),
                 "uninstall_key": UNINSTALL_KEY}
        if plan["full"]:
            ready.update(mode="installer", installer=str(dest), sha256=asset["sha256"])
        else:
            staging = vdir / "staging"
            shutil.rmtree(staging, ignore_errors=True)
            with zipfile.ZipFile(dest) as z:
                for r, sha in plan["files"].items():
                    target = staging / r
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(r) as src, open(target, "wb") as out:
                        shutil.copyfileobj(src, out)
                    if mf.sha256_file(target) != sha:
                        raise UpdateError("verify", r)
            dest.unlink()
            ready.update(mode="patch", staging=str(staging), removed=plan["removed"],
                         files=[{"path": r, "sha256": sha} for r, sha in plan["files"].items()])
        (self.work / "ready.json").write_text(json.dumps(ready, ensure_ascii=False, indent=1), encoding="utf-8")
        self._plan = ready
        self._set(state="ready")

    def _fetch(self, url: str, dest: Path, size: int):
        """下载到 dest.part，断线后从断点继续（HTTP Range），最多重试 RETRIES 次"""
        part = dest.with_name(dest.name + ".part")
        err = None
        for attempt in range(RETRIES):
            have = part.stat().st_size if part.exists() else 0
            if have > size:
                part.unlink()
                have = 0
            if have == size:
                break
            headers = {"User-Agent": f"SimpleQuant/{self.current}"}
            if have:
                headers["Range"] = f"bytes={have}-"
            try:
                with self.session.get(url, headers=headers, stream=True, timeout=TIMEOUT) as r:
                    if r.status_code == 200:
                        have = 0                      # 服务器不支持断点续传：从头下载
                    elif r.status_code != 206:
                        r.raise_for_status()
                        raise requests.HTTPError(f"HTTP {r.status_code}")
                    self.status.done = have
                    with open(part, "ab" if have else "wb") as f:
                        for chunk in r.iter_content(256 * 1024):
                            f.write(chunk)
                            self.status.done += len(chunk)
            except requests.RequestException as e:
                err = e
                time.sleep(min(2 ** attempt, 8))
        if not part.exists() or part.stat().st_size != size:
            raise UpdateError("network", f"{type(err).__name__}: {err}" if err else "size mismatch")
        part.replace(dest)

    # ---------- 安装 ----------
    def apply(self) -> bool:
        """启动更新脚本（之后调用方应立即退出程序）；没有准备好的更新时返回 False"""
        if self.status.state != "ready" or not self._plan or not self.installable:
            return False
        plan = dict(self._plan, pid=os.getpid())
        script = self.work / SCRIPT.name
        shutil.copyfile(SCRIPT, script)                      # 安装目录里的脚本可能被替换，用副本
        plan_file = self.work / "plan.json"
        plan_file.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
        cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-WindowStyle", "Hidden", "-File", str(script), "-Plan", str(plan_file)]
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(self.work))
        try:   # 脱离可能存在的作业对象，程序退出时脚本不会被一起结束
            subprocess.Popen(cmd, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kw)
        except OSError:
            subprocess.Popen(cmd, creationflags=flags, **kw)
        return True


UPDATER = Updater()
