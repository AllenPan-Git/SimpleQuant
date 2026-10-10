"""
自动更新：签名、清单、客户端（本机模拟的 GitHub 服务器）、替换文件的 PowerShell 脚本；
macOS / Linux 的整体替换方式（客户端部分在任何系统上都能测，apply_update.sh 只在 macOS / Linux 上测）
"""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from simplequant.update import client, ed25519, manifest as mf
from simplequant.update.client import Updater

ROOT = Path(__file__).resolve().parents[1]


# ---------------- Ed25519 ----------------
@pytest.mark.parametrize("secret, public, msg, sig", [
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
])
def test_ed25519_rfc8032_vectors(secret, public, msg, sig):
    sk, pk, m, s = (bytes.fromhex(x) for x in (secret, public, msg, sig))
    assert ed25519.public_key(sk) == pk
    assert ed25519.sign(sk, m) == s
    assert ed25519.verify(pk, m, s)
    assert not ed25519.verify(pk, m + b"x", s)
    assert not ed25519.verify(pk, m, s[:-1] + bytes([s[-1] ^ 1]))
    assert not ed25519.verify(pk, m, s[:10])


def test_public_key_is_set():
    from simplequant import update
    assert len(bytes.fromhex(update.PUBLIC_KEY)) == 32


# ---------------- 清单 ----------------
def test_parse_version_and_safe_path():
    assert mf.parse_version("v0.1.10") > mf.parse_version("0.1.9") > mf.parse_version("0.1")
    assert mf.parse_version("0.2.0-beta") == (0, 2, 0)
    assert mf.safe_path("_internal/a/b.py") and mf.safe_path("SimpleQuant.exe")
    for bad in ("", "../x", "a/../../x", "/abs", "C:/x", "a\\b"):
        assert not mf.safe_path(bad)


def test_diff():
    old = {"a": ["1", 1], "b": ["2", 1], "gone": ["3", 1]}
    new = {"a": ["1", 1], "b": ["9", 1], "added": ["4", 1]}
    assert mf.diff(old, new) == (["b", "added"], ["gone"])


SECRET = bytes(range(32))
PUB = ed25519.public_key(SECRET).hex()


def _signed(m: dict) -> tuple[bytes, str]:
    data = mf.dumps(m)
    return data, mf.sign(data, SECRET)


def test_load_verified_rejects_tampering_and_bad_paths():
    m = {"app": "SimpleQuant", "version": "1.0.0", "files": {"a.py": ["00", 1]}, "patches": {}}
    data, sig = _signed(m)
    assert mf.load_verified(data, sig, PUB)["version"] == "1.0.0"
    with pytest.raises(mf.ManifestError):
        mf.load_verified(data.replace(b"1.0.0", b"9.0.0"), sig, PUB)
    with pytest.raises(mf.ManifestError):
        mf.load_verified(data, sig, ed25519.public_key(bytes(32)).hex())
    with pytest.raises(mf.ManifestError):
        mf.load_verified(data, "zz", PUB)
    bad = dict(m, files={"../../evil.exe": ["00", 1]})
    with pytest.raises(mf.ManifestError, match="format"):
        mf.load_verified(*_signed(bad), PUB)


# ---------------- 模拟的 GitHub ----------------
class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        srv = self.server
        path = self.path.split("?")[0]
        if path == "/latest":
            return self._send(200, json.dumps(srv.latest).encode())
        name = path.removeprefix("/dl/")
        if name not in srv.files:
            return self._send(404, b"")
        data = srv.files[name]
        rng = self.headers.get("Range")
        start = int(rng.split("=")[1].split("-")[0]) if rng else 0
        srv.requests.append((name, start))
        self.send_response(206 if rng else 200)
        self.send_header("Content-Length", str(len(data) - start))
        self.end_headers()
        if name in srv.cut:                      # 模拟断线：只发一半就断开
            srv.cut.discard(name)
            self.wfile.write(data[start:start + (len(data) - start) // 2])
            self.wfile.flush()
            self.close_connection = True
            return
        self.wfile.write(data[start:])

    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.files, srv.requests, srv.cut, srv.latest = {}, [], set(), {}
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield srv
    srv.shutdown()


OLD = {"SimpleQuant.exe": b"exe" * 1000, "_internal/a.py": b"a = 1\n", "_internal/b.dll": b"same" * 100,
       "_internal/old.py": b"old\n"}
NEW = {"SimpleQuant.exe": b"exe" * 1000, "_internal/a.py": b"a = 2\n", "_internal/b.dll": b"same" * 100,
       "_internal/pkg/new.py": "新文件 = True\n".encode("utf-8")}


def _write_tree(root: Path, files: dict):
    for r, data in files.items():
        (root / r).parent.mkdir(parents=True, exist_ok=True)
        (root / r).write_bytes(data)


def _publish(srv, tmp_path, version="1.1.0", with_patch=True, tamper=False):
    """在模拟服务器上发布 NEW：清单、签名、从 1.0.0 的补丁、安装包"""
    build = tmp_path / f"build-{version}"
    _write_tree(build, NEW)
    files = mf.hash_tree(build)
    old_files = {r: [hashlib.sha256(d).hexdigest(), len(d)] for r, d in OLD.items()}
    installer = b"MZ fake installer" * 120000   # 约 2 MB：断线前能写下几块（每块 256 KB）
    srv.files[f"SimpleQuant-{version}-Setup.exe"] = installer
    m = {"app": "SimpleQuant", "version": version, "files": files, "patches": {},
         "installer": {"name": f"SimpleQuant-{version}-Setup.exe", "size": len(installer),
                       "sha256": hashlib.sha256(installer).hexdigest()}}
    if with_patch:
        changed, removed = mf.diff(old_files, files)
        z = tmp_path / "patch.zip"
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
            for r in changed:
                zf.write(build / r, r)
        name = f"SimpleQuant-{version}-from-1.0.0.zip"
        srv.files[name] = z.read_bytes()
        m["patches"]["1.0.0"] = {"name": name, "size": z.stat().st_size, "sha256": mf.sha256_file(z),
                                 "files": changed, "removed": removed}
    data, sig = _signed(m)
    if tamper:
        data = data.replace(b'"1.1.0"', b'"1.1.1"')
    srv.files[mf.MANIFEST], srv.files[mf.SIGNATURE] = data, sig.encode()
    srv.latest = {"tag_name": f"v{version}", "body": "## 更新内容\n- 修复", "html_url": f"{srv.url}/page",
                  "assets": [{"name": n, "browser_download_url": f"{srv.url}/dl/{n}"} for n in srv.files]}
    return m


@pytest.fixture
def installed(tmp_path):
    app = tmp_path / "app"
    _write_tree(app, OLD)
    return app


def _updater(srv, app, tmp_path, **kw) -> Updater:
    kw = {"frozen": True, "current": "1.0.0", "sources": [f"{srv.url}/latest"], "system": "windows", **kw}
    kw.setdefault("target", kw["system"])              # 测试发布的清单按系统名命名（manifest-linux.json）
    return Updater(app_dir=app, work=tmp_path / "work", public_key=PUB, **kw)


def test_check_latest(server, installed, tmp_path):
    _publish(server, tmp_path, version="1.0.0")
    u = _updater(server, installed, tmp_path)
    u.check()
    assert u.status.state == "latest"


def test_patch_download_and_staging(server, installed, tmp_path):
    _publish(server, tmp_path)
    u = _updater(server, installed, tmp_path)
    u.check()
    s = u.status
    assert s.state == "ready" and s.version == "1.1.0" and not s.full and "修复" in s.notes
    plan = json.loads((tmp_path / "work" / "ready.json").read_text(encoding="utf-8"))
    assert plan["mode"] == "patch"
    assert sorted(f["path"] for f in plan["files"]) == ["_internal/a.py", "_internal/pkg/new.py"]
    assert plan["removed"] == ["_internal/old.py"]
    staging = Path(plan["staging"])
    assert (staging / "_internal/a.py").read_bytes() == NEW["_internal/a.py"]
    assert not (staging / "SimpleQuant.exe").exists()         # 没变的文件不下载
    assert not any(n.endswith(".exe") for n, _ in server.requests)


def test_local_file_changed_falls_back_to_installer(server, installed, tmp_path):
    """本机的 exe 被改过（补丁里没有它）：改为下载完整安装包"""
    _publish(server, tmp_path)
    (installed / "SimpleQuant.exe").write_bytes(b"corrupted")
    u = _updater(server, installed, tmp_path)
    u.check()
    assert u.status.state == "ready" and u.status.full
    plan = json.loads((tmp_path / "work" / "ready.json").read_text(encoding="utf-8"))
    assert plan["mode"] == "installer" and Path(plan["installer"]).exists()


def test_large_download_waits_for_user_and_resumes(server, installed, tmp_path, monkeypatch):
    _publish(server, tmp_path, with_patch=False)
    monkeypatch.setattr(client, "AUTO_DOWNLOAD_MB", 0.001)
    u = _updater(server, installed, tmp_path)
    u.check()
    assert u.status.state == "available" and u.status.full and u.status.size > 0
    assert not any(n.endswith(".exe") for n, _ in server.requests)
    server.cut.add("SimpleQuant-1.1.0-Setup.exe")             # 第一次下载中途断线
    u.download()
    assert u.status.state == "ready"
    starts = [st for n, st in server.requests if n.endswith(".exe")]
    assert starts[0] == 0 and starts[1] > 0                     # 第二次从断点继续
    plan = json.loads((tmp_path / "work" / "ready.json").read_text(encoding="utf-8"))
    assert mf.sha256_file(Path(plan["installer"])) == plan["sha256"]


def test_bad_signature_is_rejected(server, installed, tmp_path):
    _publish(server, tmp_path, tamper=True)
    u = _updater(server, installed, tmp_path)
    with pytest.raises(client.UpdateError) as e:
        u.check()
    assert e.value.kind == "verify"
    assert not (tmp_path / "work" / "ready.json").exists()


def test_release_without_this_platform(server, installed, tmp_path):
    """新版本不再打包本平台（如 0.2.5 之后的 Intel Mac）：报 platform，不当成出错"""
    _publish(server, tmp_path)
    u = _updater(server, installed, tmp_path, system="macos", target="macos-x86_64")
    with pytest.raises(client.UpdateError) as e:
        u.check()
    assert e.value.kind == "platform" and u.status.version == "1.1.0"


def test_network_error_and_background_status(installed, tmp_path):
    u = Updater(app_dir=installed, work=tmp_path / "work", frozen=True, current="1.0.0",
                sources=["http://127.0.0.1:9/latest"], public_key=PUB, system="windows")
    assert u.check_async()
    for _ in range(100):
        if not u.busy:
            break
        threading.Event().wait(0.05)
    assert u.status.state == "error" and u.status.error == "network"


def test_source_mode_only_reports(server, installed, tmp_path):
    _publish(server, tmp_path)
    u = _updater(server, installed, tmp_path, frozen=False)
    u.check()
    assert u.status.state == "available" and u.status.version == "1.1.0"
    assert server.requests == []                                 # 不下载清单和更新文件
    assert not u.apply()


def test_startup_restores_ready_and_reads_result(server, installed, tmp_path):
    _publish(server, tmp_path)
    _updater(server, installed, tmp_path).check()
    u = _updater(server, installed, tmp_path, sources=["http://127.0.0.1:9/latest"])
    u.startup()
    assert u.status.state == "ready" and u.status.version == "1.1.0"
    u.check()                                                    # 联网失败也保持 ready
    assert u.status.state == "ready"
    # 更新完成后的下一次启动：读取结果并清理
    work = tmp_path / "work"
    (work / "result.json").write_text(json.dumps({"ok": True, "version": "1.1.0", "message": ""}),
                                      encoding="utf-8-sig")
    u = _updater(server, installed, tmp_path, current="1.1.0")
    u.startup()
    assert u.status.last["ok"] and u.status.state == "idle"
    assert not (work / "ready.json").exists() and not (work / "1.1.0").exists()


def test_apply_starts_script(server, installed, tmp_path, monkeypatch):
    _publish(server, tmp_path)
    u = _updater(server, installed, tmp_path)
    assert not u.apply()                                         # 还没准备好
    u.check()
    calls = []
    monkeypatch.setattr(client.subprocess, "Popen", lambda cmd, **kw: calls.append(cmd))
    assert u.apply()
    cmd = calls[0]
    assert cmd[0] == "powershell.exe" and cmd[cmd.index("-File") + 1].endswith("apply_update.ps1")
    plan = json.loads(Path(cmd[cmd.index("-Plan") + 1]).read_text(encoding="utf-8"))
    assert plan["pid"] > 0 and plan["mode"] == "patch"


# ---------------- 替换文件的 PowerShell 脚本 ----------------
needs_ps = pytest.mark.skipif(sys.platform != "win32" or not shutil.which("powershell.exe"),
                              reason="Windows PowerShell")


def _run_script(plan: dict, tmp_path: Path) -> dict:
    plan = dict(plan, no_restart=True)
    f = tmp_path / "plan-test.json"
    f.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                    str(client.SCRIPT), "-Plan", str(f)], timeout=120, capture_output=True)
    return json.loads(Path(plan["result"]).read_text(encoding="utf-8-sig"))


def test_script_is_ascii():
    client.SCRIPT.read_text(encoding="ascii")


@needs_ps
def test_script_applies_patch(server, installed, tmp_path):
    """含中文和空格的路径；替换、新增、删除；成功后清掉备份"""
    app = tmp_path / "程序 目录"
    shutil.copytree(installed, app)
    (app / "_internal" / "__pycache__").mkdir()
    _publish(server, tmp_path)
    u = _updater(server, app, tmp_path)
    u.check()
    plan = json.loads((tmp_path / "work" / "ready.json").read_text(encoding="utf-8"))
    res = _run_script(plan, tmp_path)
    assert res["ok"] and res["version"] == "1.1.0"
    assert mf.hash_tree(app) == mf.hash_tree(tmp_path / "build-1.1.0")
    assert not (tmp_path / "work" / "backup").exists()
    assert not (app / "_internal" / "__pycache__").exists()


@needs_ps
def test_script_rolls_back_when_a_file_is_locked(server, installed, tmp_path):
    _publish(server, tmp_path)
    u = _updater(server, installed, tmp_path)
    u.check()
    plan = json.loads((tmp_path / "work" / "ready.json").read_text(encoding="utf-8"))
    plan["files"].sort(key=lambda f: f["path"])                  # a.py 先替换，再处理被占用的 new.py 位置
    (installed / "_internal" / "pkg").mkdir()
    locked = installed / "_internal" / "pkg" / "new.py"
    locked.write_bytes(b"in use")
    before = mf.hash_tree(installed)
    with open(locked, "rb"):                                     # 打开着的文件不能移动
        res = _run_script(plan, tmp_path)
    assert not res["ok"]
    assert mf.hash_tree(installed) == before                     # a.py 已恢复成旧版本


@needs_ps
def test_script_refuses_bad_hash(server, installed, tmp_path):
    _publish(server, tmp_path)
    u = _updater(server, installed, tmp_path)
    u.check()
    plan = json.loads((tmp_path / "work" / "ready.json").read_text(encoding="utf-8"))
    (Path(plan["staging"]) / "_internal" / "a.py").write_bytes(b"tampered")
    before = mf.hash_tree(installed)
    res = _run_script(plan, tmp_path)
    assert not res["ok"] and "verify" in res["message"]
    assert mf.hash_tree(installed) == before


# ---------------- 打包配置 ----------------
def test_packaging_config_for_updates():
    spec = (ROOT / "SimpleQuant.spec").read_text(encoding="utf-8")
    assert "apply_update.ps1" in spec and "apply_update.sh" in spec
    assert '"simplequant": "py"' in spec                       # 自己的代码不进 exe，补丁才小
    iss = (ROOT / "installer" / "SimpleQuant.iss").read_text(encoding="utf-8-sig")
    assert "[UninstallDelete]" in iss and "-Setup" in iss
    bat = (ROOT / "build.bat").read_text(encoding="utf-8")
    sys.path.insert(0, str(ROOT / "tools"))
    import release
    for k, v in release.BUILD_ENV.items():
        assert f"set {k}={v}" in bat


# ---------------- macOS / Linux：拼出新程序目录、整体替换 ----------------
def _can_symlink(tmp_path: Path) -> bool:
    try:
        os.symlink("x", tmp_path / "symlink-probe")
        return True
    except OSError:                 # Windows 没开开发者模式时不能建符号链接
        return False


def _publish_unix(srv, tmp_path, system="linux", links=None, version="1.1.0"):
    """发布 NEW 的 macOS / Linux 版：manifest-<系统>.json、从 1.0.0 的补丁、整个目录的 .tar.gz"""
    build = tmp_path / f"build-{version}" / "app"
    _write_tree(build, NEW)
    for r, target in (links or {}).items():
        os.symlink(target, build / r)
    files = mf.hash_tree(build)
    old_files = {r: [hashlib.sha256(d).hexdigest(), len(d)] for r, d in OLD.items()}
    changed, removed = mf.diff(old_files, files)
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for r in changed:
            zf.write(build / r, r)
    t = io.BytesIO()
    with tarfile.open(fileobj=t, mode="w:gz") as tf:
        tf.add(build, arcname="app")
    patch, archive = f"SimpleQuant-{version}-{system}-from-1.0.0.zip", f"SimpleQuant-{version}-{system}.tar.gz"
    srv.files[patch], srv.files[archive] = z.getvalue(), t.getvalue()
    m = {"app": "SimpleQuant", "version": version, "files": files, "links": mf.links_tree(build),
         "archive": {"name": archive, "size": len(t.getvalue()), "sha256": hashlib.sha256(t.getvalue()).hexdigest()},
         "patches": {"1.0.0": {"name": patch, "size": len(z.getvalue()),
                               "sha256": hashlib.sha256(z.getvalue()).hexdigest(),
                               "files": changed, "removed": removed}}}
    data, sig = _signed(m)
    srv.files[mf.manifest_name(system)], srv.files[mf.signature_name(system)] = data, sig.encode()
    srv.latest = {"tag_name": f"v{version}", "body": "", "html_url": f"{srv.url}/page",
                  "assets": [{"name": n, "browser_download_url": f"{srv.url}/dl/{n}"} for n in srv.files]}
    return build


def test_manifest_names_and_links():
    assert mf.manifest_name("windows") == "manifest.json" and mf.signature_name("windows") == "manifest.json.sig"
    assert mf.manifest_name("macos") == "manifest-macos.json"
    assert mf.safe_link("Contents/Frameworks/lib.dylib", "../Resources/lib.dylib")
    for rel, target in [("a/b", "/etc/passwd"), ("a/b", "../../x"), ("x", ".."), ("../a", "b"), ("a", "C:/x")]:
        assert not mf.safe_link(rel, target)


def test_unix_patch_builds_new_tree(server, installed, tmp_path):
    links = {"_internal/link.py": "a.py"} if _can_symlink(tmp_path) else {}
    build = _publish_unix(server, tmp_path, links=links)
    u = _updater(server, installed, tmp_path, system="linux")
    u.check()
    assert u.status.state == "ready" and not u.status.full
    plan = json.loads((tmp_path / "work" / "ready.json").read_text(encoding="utf-8"))
    new = Path(plan["new_dir"])
    assert plan["mode"] == "swap" and new.name == installed.name
    assert mf.hash_tree(new) == mf.hash_tree(build) and mf.links_tree(new) == mf.links_tree(build)
    assert mf.hash_tree(installed) != mf.hash_tree(build)           # 当前程序目录不动
    assert not any(n.endswith(".tar.gz") for n, _ in server.requests)
    u2 = _updater(server, installed, tmp_path, system="linux")
    u2.startup()
    assert u2.status.state == "ready" and u2.status.version == "1.1.0"


def test_unix_falls_back_to_archive(server, installed, tmp_path):
    build = _publish_unix(server, tmp_path, system="macos")
    (installed / "SimpleQuant.exe").write_bytes(b"corrupted")
    u = _updater(server, installed, tmp_path, system="macos")
    u.check()
    assert u.status.state == "ready" and u.status.full
    plan = json.loads((tmp_path / "work" / "ready.json").read_text(encoding="utf-8"))
    assert mf.hash_tree(Path(plan["new_dir"])) == mf.hash_tree(build)
    assert not (tmp_path / "work" / "1.1.0" / "extract").exists()


def test_unix_apply_starts_shell_script(server, installed, tmp_path, monkeypatch):
    _publish_unix(server, tmp_path)
    u = _updater(server, installed, tmp_path, system="linux")
    u.check()
    calls = []
    monkeypatch.setattr(client.subprocess, "Popen", lambda cmd, **kw: calls.append((cmd, kw)))
    assert u.apply()
    cmd, kw = calls[0]
    assert cmd[0] == "/bin/sh" and cmd[1].endswith("apply_update.sh") and cmd[3:5] == ["linux", str(installed)]
    assert kw["start_new_session"] and (tmp_path / "work" / "apply_update.sh").exists()


def test_shell_script_is_ascii_and_lf():
    data = client.SH_SCRIPT.read_bytes()
    data.decode("ascii")
    assert b"\r\n" not in data


needs_sh = pytest.mark.skipif(sys.platform == "win32" or not shutil.which("sh"), reason="macOS / Linux")


def _fake_app(root: Path, marker: Path, content: bytes):
    root.mkdir(parents=True)
    (root / "data.txt").write_bytes(content)
    exe = root / "SimpleQuant"
    exe.write_text(f"#!/bin/sh\necho started > '{marker}'\n")
    exe.chmod(0o755)


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


@needs_sh
def test_shell_script_swaps_and_restarts(tmp_path):
    app, work = tmp_path / "程序 目录" / "app", tmp_path / "work"
    new = work / "1.1.0" / "app" / "app"
    marker = tmp_path / "restarted"
    _fake_app(app, marker, b"old")
    _fake_app(new, marker, b"new")
    r = subprocess.run(["/bin/sh", str(client.SH_SCRIPT), str(_dead_pid()), "linux", str(app), str(new), str(work),
                        "1.1.0"], timeout=60)
    assert r.returncode == 0
    res = json.loads((work / "result.json").read_text(encoding="utf-8"))
    assert res == {"ok": True, "version": "1.1.0", "message": ""}
    assert (app / "data.txt").read_bytes() == b"new" and not new.exists()
    assert not Path(str(app) + ".update-backup").exists()
    for _ in range(50):
        if marker.exists():
            break
        threading.Event().wait(0.1)
    assert marker.exists()


@needs_sh
def test_shell_script_keeps_old_version_when_new_is_missing(tmp_path):
    app, work = tmp_path / "app", tmp_path / "work"
    work.mkdir()
    _fake_app(app, tmp_path / "restarted", b"old")
    r = subprocess.run(["/bin/sh", str(client.SH_SCRIPT), str(_dead_pid()), "linux", str(app),
                        str(tmp_path / "missing"), str(work), "1.1.0"], timeout=60)
    assert r.returncode != 0
    assert not json.loads((work / "result.json").read_text(encoding="utf-8"))["ok"]
    assert (app / "data.txt").read_bytes() == b"old"


# ---------------- 发布脚本：macOS / Linux 清单 ----------------
def _release():
    sys.path.insert(0, str(ROOT / "tools"))
    import release
    return release


def test_release_patches_and_unix_manifest_checks(tmp_path):
    release = _release()
    app = tmp_path / "app"
    _write_tree(app, NEW)
    files = mf.hash_tree(app)
    old = {r: [hashlib.sha256(d).hexdigest(), len(d)] for r, d in OLD.items()}
    out = tmp_path / "out"
    out.mkdir()
    patches = release.make_patches("1.1.0", app, files, {"1.0.0": old}, out, "linux-x86_64")
    p = patches["1.0.0"]
    assert p["name"] == "SimpleQuant-1.1.0-linux-x86_64-from-1.0.0.zip" and p["removed"] == ["_internal/old.py"]
    with zipfile.ZipFile(out / p["name"]) as z:
        assert sorted(z.namelist()) == ["_internal/a.py", "_internal/pkg/new.py"]
    assert release.make_patches("1.1.0", app, files, {"1.0.0": old}, out)["1.0.0"]["name"] == \
        "SimpleQuant-1.1.0-from-1.0.0.zip"                       # Windows 的文件名与以前相同

    archive = {"name": "SimpleQuant-1.1.0-linux-x86_64.tar.gz", "size": 10, "sha256": "00"}
    m = {"app": "SimpleQuant", "version": "1.1.0", "files": files, "links": {}, "archive": archive,
         "patches": patches}
    assets = {archive["name"]: {"size": 10}, p["name"]: {"size": p["size"]}}
    release.check_unix_manifest(m, "1.1.0", "linux-x86_64", assets)
    for bad_m, bad_assets in [(dict(m, version="1.0.9"), assets),
                              (m, {archive["name"]: {"size": 10}}),                       # 补丁包没上传
                              (m, {**assets, archive["name"]: {"size": 11}}),             # 大小不一致
                              (dict(m, links={"a": "/etc/passwd"}), assets)]:
        with pytest.raises(SystemExit):
            release.check_unix_manifest(bad_m, "1.1.0", "linux-x86_64", bad_assets)


def test_release_targets_match_workflow(monkeypatch):
    release = _release()
    wf = (ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
    for t in release.UNIX_TARGETS:
        assert f"target: {t}" in wf
    monkeypatch.setattr(release, "changelog", lambda v: ("- 中文条目", "- English item"))
    notes = release.release_notes("1.2.3", "SimpleQuant-1.2.3-Setup.exe")
    assert all(f"SimpleQuant-1.2.3-{t}" in notes for t in release.UNIX_TARGETS)
    assert "macos-x86_64" not in wf and "macos-x86_64" not in notes     # Intel Mac 最后支持 0.2.5
    assert f"/releases/tag/v{release.LAST_INTEL})" in notes
    assert "## 更新内容\n- 中文条目\n" in notes and "## Changes\n- English item\n" in notes


def test_release_changelog(tmp_path):
    release = _release()
    p = tmp_path / "CHANGELOG.md"
    p.write_text("# 更新记录\n\n## 未发布\n\n### 更新内容\n- 新的\n\n### Changes\n- new\n\n"
                 "## 1.2.10（2026-10-05）\n\n### 更新内容\n- 甲\n  - 乙\n\n### Changes\n- A\n  - B\n\n"
                 "## 1.2.1（2026-10-04）\n\n### 更新内容\n- 旧\n\n### Changes\n- old\n", encoding="utf-8")
    assert release.changelog("1.2.10", p) == ("- 甲\n  - 乙", "- A\n  - B")
    assert release.changelog("1.2.1", p) == ("- 旧", "- old")              # 不会匹配到 1.2.10
    for v in ("1.2.2", "1.2"):
        with pytest.raises(SystemExit):
            release.changelog(v, p)
    p.write_text("## 1.3.0\n\n### 更新内容\n- 甲\n", encoding="utf-8")    # 缺英文
    with pytest.raises(SystemExit):
        release.changelog("1.3.0", p)
    zh, en = release.changelog("0.2.0")                                    # 仓库里的 CHANGELOG.md
    assert zh.startswith("- ") and en.startswith("- ")


# ---------------- 依赖版本 ----------------
def test_constraints_cover_requirements():
    sys.path.insert(0, str(ROOT / "tools"))
    import pin_versions
    pinned = {}
    for line in (ROOT / "constraints.txt").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            spec, marker = line.split(";")
            name, ver = spec.split("==")
            pinned[name.strip()] = ver.strip()
            assert marker.strip() == f'python_version >= "{pin_versions.MIN_PYTHON}"'
    assert set(pin_versions.names()) == set(pinned)          # 改了 requirements.txt 要重新运行 tools/pin_versions.py
    for script in ("start.bat", "start.sh", ".github/workflows/build.yml", ".github/workflows/sources.yml"):
        assert "-r requirements.txt -c constraints.txt" in (ROOT / script).read_text(encoding="utf-8"), script


def test_strip_download_section():
    from simplequant.update.client import strip_download
    body = "## 下载\n| 系统 | 安装包 |\n|---|---|\n\n## 更新内容\n- 修复 A\n\n---\n\n## Download\nlinks\n\n## Changes\n- Fixed A\n"
    out = strip_download(body)
    assert "安装包" not in out and "links" not in out
    assert out.startswith("## 更新内容") and "- 修复 A" in out and "- Fixed A" in out
