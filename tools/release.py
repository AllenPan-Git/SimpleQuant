"""
发布新版本（只在发布者本机运行）

    .venv\\Scripts\\python tools\\release.py keygen        第一次：生成更新签名密钥，并把公钥写进 simplequant/update/__init__.py
    .venv\\Scripts\\python tools\\release.py               打包 → 清单与签名 → 补丁包 → 打标签 → 上传为 GitHub 草稿
    .venv\\Scripts\\python tools\\release.py --no-upload   只打包并生成发布文件（dist\\release\\<版本>\\），不打标签、不上传
    .venv\\Scripts\\python tools\\release.py --no-build    用已生成的发布文件直接上传（上次上传中断时）
    .venv\\Scripts\\python tools\\release.py --sign-only   只给 macOS / Linux 的清单签名（上次没等到 GitHub Actions 打包完时）
    .venv\\Scripts\\python tools\\release.py publish     发布草稿：按 CHANGELOG.md 更新说明，等 Actions 全部通过后公开
    .venv\\Scripts\\python tools\\release.py --publish   上传、签名之后接着发布（不再到网页上点 Publish）
    python tools/release.py ci                         GitHub Actions 里调用：生成本平台的清单（未签名）与补丁包并上传

三个系统（PyInstaller 不能交叉编译）：
    Windows：本机打包（安装包 + manifest.json），与以前相同
    macOS / Linux：推送标签后 GitHub Actions（.github/workflows/build.yml）在各系统上打包，`release.py ci` 把
        dmg / tar.gz、补丁包、未签名的 manifest-<平台>.json 上传到同一个草稿；
        本机的 release.py 等它们上传完，下载这几个清单（很小）签名，再上传 .sig。私钥始终只在本机

发布前：改好 simplequant/__init__.py 的版本号，在 CHANGELOG.md 把「未发布」改成「<版本>（日期）」，
        测试通过，提交并推送到 GitHub。发布说明的「更新内容」「Changes」两节取自 CHANGELOG.md。
上传后：草稿对外不可见；运行 release.py publish（或一开始就加 --publish）才公开，用户的程序这时才会检测到新版本。
        发布前想改说明：改 CHANGELOG.md 后运行 publish 即可（会重新生成正文），也可以在网页上改好后点 Publish。

补丁包：与之前最多 MAX_PATCHES 个版本各比一次，只打包变化的文件。旧版本的清单先找 dist\\release\\，
        没有就从 GitHub 下载（并验证签名）。打包时固定 PYTHONHASHSEED、SOURCE_DATE_EPOCH，
        依赖没变时 exe 和 base_library.zip 逐字节相同，只改代码的版本补丁只有几十 KB。

密钥与令牌放在 %USERPROFILE%\\.simplequant-release\\（不在项目里，不会被提交）：
    signing_key        Ed25519 私钥（十六进制）。务必备份：丢失后已安装的程序无法再验证新版本，只能手动重装
    github_token.txt   GitHub fine-grained 令牌（只授权 SimpleQuant 仓库的 Contents 读写）；
                       也可以用环境变量 SIMPLEQUANT_GITHUB_TOKEN
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from simplequant import __version__  # noqa: E402
from simplequant import update as cfg  # noqa: E402
from simplequant.update import ed25519, manifest as mf  # noqa: E402
from simplequant import system  # noqa: E402

KEY_DIR = Path.home() / ".simplequant-release"
KEY_FILE = KEY_DIR / "signing_key"
TOKEN_FILE = KEY_DIR / "github_token.txt"
DIST = ROOT / "dist"
APP = DIST / "windows" / "SimpleQuant"
OUT_ROOT = DIST / "release"
API = f"https://api.github.com/repos/{os.environ.get('GITHUB_REPOSITORY') or cfg.REPO}"
MAX_PATCHES = 5
UNIX_TARGETS = ["macos-arm64", "macos-x86_64", "linux-x86_64"]     # 与 .github/workflows/build.yml 的矩阵一致
CI_WAIT_MIN = 45
MIRROR_URL = "https://1829763294.share.123pan.cn/123pan/mv2ejv-wbhtA?pwd=ZfDO"     # 123 云盘（提取码 ZfDO），发新版时手动把安装包传进去
# 固定的打包环境：两次打包同样的代码得到相同的文件（见文件开头）
BUILD_ENV = {"PYTHONHASHSEED": "0", "SOURCE_DATE_EPOCH": "1735689600"}


def die(msg: str):
    print(f"\n[停止] {msg}")
    sys.exit(1)


def run(cmd: list, **kw) -> str:
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)
    if r.returncode:
        die(f"命令失败：{' '.join(map(str, cmd))}\n{r.stdout[-3000:]}\n{r.stderr[-3000:]}")
    return r.stdout.strip()


def git(*args) -> str:
    return run(["git", *args])


# ---------- 密钥 ----------
def keygen(force: bool):
    if KEY_FILE.exists() and not force:
        die(f"已有签名密钥 {KEY_FILE}。重新生成会让已安装的程序无法验证以后的版本；确实需要时加 --force")
    secret = os.urandom(32)
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(secret.hex(), encoding="ascii")
    pub = ed25519.public_key(secret).hex()
    init = ROOT / "simplequant" / "update" / "__init__.py"
    text = init.read_text(encoding="utf-8")
    text = re.sub(r'^PUBLIC_KEY = "[0-9a-f]*"', f'PUBLIC_KEY = "{pub}"', text, count=1, flags=re.M)
    init.write_text(text, encoding="utf-8")
    print(f"私钥：{KEY_FILE}（请备份到安全的地方，不要发给别人、不要提交到仓库）")
    print(f"公钥：{pub}（已写入 {init.relative_to(ROOT)}）")


def load_secret() -> bytes:
    if not KEY_FILE.exists():
        die(f"没有签名密钥 {KEY_FILE}，先运行 release.py keygen")
    secret = bytes.fromhex(KEY_FILE.read_text(encoding="ascii").strip())
    if ed25519.public_key(secret).hex() != cfg.PUBLIC_KEY:
        die("签名私钥与 simplequant/update/__init__.py 里的 PUBLIC_KEY 不对应")
    return secret


def load_token() -> str:
    token = os.environ.get("SIMPLEQUANT_GITHUB_TOKEN") or (
        TOKEN_FILE.read_text(encoding="utf-8").strip() if TOKEN_FILE.exists() else "")
    if not token:
        die(f"没有 GitHub 令牌：把令牌保存到 {TOKEN_FILE}，或设置环境变量 SIMPLEQUANT_GITHUB_TOKEN")
    return token


# ---------- 打包 ----------
def find_iscc() -> Path:
    env = os.environ.get
    for base in (Path(env("LOCALAPPDATA", "-")) / "Programs", Path(env("ProgramFiles(x86)", "-")),
                 Path(env("ProgramFiles", "-"))):
        if (p := base / "Inno Setup 6" / "ISCC.exe").exists():
            return p
    die("没有找到 Inno Setup 6（winget install JRSoftware.InnoSetup）")


def build(version: str) -> Path:
    env = {**os.environ, **BUILD_ENV}
    print("打包 exe（约 2 分钟）…")
    run([sys.executable, "-m", "PyInstaller", "SimpleQuant.spec", "--noconfirm", "--clean",
         "--distpath", str(DIST / "windows"), "--workpath", str(ROOT / "build" / "windows")], env=env)
    print("生成安装包…")
    run([str(find_iscc()), "/Q", f"/DAppVersion={version}", str(ROOT / "installer" / "SimpleQuant.iss")])
    installer = DIST / "windows" / f"SimpleQuant-{version}-Setup.exe"
    if not installer.exists():
        die(f"没有找到安装包 {installer}")
    return installer


# ---------- 旧版本清单 ----------
def previous_manifests(version: str, token: str | None, out_root: Path | None = OUT_ROOT,
                       target: str = "windows") -> dict[str, dict]:
    """{旧版本: files}，最多 MAX_PATCHES 个，从新到旧（out_root=None：只从 GitHub 读，GitHub Actions 里用）"""
    found = {}
    name, sig_name = mf.manifest_name(target), mf.signature_name(target)
    for p in (out_root.glob(f"*/{name}") if out_root else []):
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
            found[m["version"]] = m["files"]
        except (OSError, ValueError, KeyError):
            pass
    try:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        rels = requests.get(f"{API}/releases?per_page=30", headers=headers, timeout=30).json()
        for rel in rels:
            v = rel["tag_name"].lstrip("vV")
            if rel.get("draft") or v in found:
                continue
            assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
            if name in assets and sig_name in assets:
                data = requests.get(assets[name], timeout=60).content
                sig = requests.get(assets[sig_name], timeout=60).text
                found[v] = mf.load_verified(data, sig, cfg.PUBLIC_KEY)["files"]
    except (requests.RequestException, ValueError, KeyError, TypeError, mf.ManifestError) as e:
        print(f"  （没能从 GitHub 读取旧版本清单：{e}；只用本机已有的）")
    cur = mf.parse_version(version)
    older = sorted((v for v in found if mf.parse_version(v) < cur), key=mf.parse_version, reverse=True)
    return {v: found[v] for v in older[:MAX_PATCHES]}


def asset_info(path: Path, **extra) -> dict:
    return {"name": path.name, "sha256": mf.sha256_file(path), "size": path.stat().st_size, **extra}


def make_patches(version: str, app: Path, files: dict, previous: dict[str, dict], out: Path,
                 target: str = "windows") -> dict:
    """与每个旧版本比较，只打包变化的文件；在 macOS / Linux 上打的 zip 带权限位（可执行文件解压后仍可执行）"""
    patches = {}
    for old_v, old_files in previous.items():
        changed, removed = mf.diff(old_files, files)
        tag = "" if target == "windows" else f"-{target}"
        z = out / f"SimpleQuant-{version}{tag}-from-{old_v}.zip"
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for r in changed:
                zf.write(app / r, r)
        patches[old_v] = asset_info(z, files=changed, removed=removed)
        print(f"  补丁 {old_v} → {version}：{len(changed)} 个文件，{z.stat().st_size / 1e6:.2f} MB，删除 {len(removed)} 个")
    return patches


def make_release(version: str, installer: Path, token: str | None, app: Path = APP, out_root: Path = OUT_ROOT) -> Path:
    """生成 out_root/<版本>/：安装包、补丁包、manifest.json 与签名、发布说明初稿"""
    secret = load_secret()
    out = out_root / version
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    shutil.copy2(installer, out / installer.name)
    print("计算程序文件校验值…")
    files = mf.hash_tree(app)
    patches = make_patches(version, app, files, previous_manifests(version, token, out_root), out)
    m = {"app": "SimpleQuant", "version": version, "files": files,
         "installer": asset_info(out / installer.name), "patches": patches}
    data = mf.dumps(m)
    (out / mf.MANIFEST).write_bytes(data)
    (out / mf.SIGNATURE).write_text(mf.sign(data, secret), encoding="ascii")
    mf.load_verified(data, (out / mf.SIGNATURE).read_text(encoding="ascii"), cfg.PUBLIC_KEY)   # 自检
    (out / "release_notes.md").write_text(release_notes(version, installer.name), encoding="utf-8")
    print(f"\n发布文件：{out}")
    return out


def changelog(version: str, path: Path = ROOT / "CHANGELOG.md") -> tuple[str, str]:
    """CHANGELOG.md 里该版本的「更新内容」「Changes」两节（版本标题形如「## 0.2.1（2026-10-05）」）"""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    m = re.search(rf"^## {re.escape(version)}(?![.\d]).*?$(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not m:
        die(f"CHANGELOG.md 里没有 {version} 的条目：把「## 未发布」改成「## {version}（日期）」，提交后再运行")
    parts = {}
    for title in ("更新内容", "Changes"):
        sec = re.search(rf"^### {title}\s*$(.*?)(?=^### |\Z)", m.group(1), re.M | re.S)
        parts[title] = sec.group(1).strip() if sec else ""
        if not parts[title]:
            die(f"CHANGELOG.md 的 {version} 缺少「### {title}」一节")
    return parts["更新内容"], parts["Changes"]


def release_notes(version: str, installer: str) -> str:
    n = f"SimpleQuant-{version}"
    zh, en = changelog(version)
    # 附件列表按文件名排序、无法调整，安装包会排在 manifest 后面；所以下载表放在说明最前面，文件名直接做成链接
    base = f"https://github.com/{os.environ.get('GITHUB_REPOSITORY') or cfg.REPO}/releases/download/v{version}"
    win, arm, x86, lin = installer, f"{n}-macos-arm64.dmg", f"{n}-macos-x86_64.dmg", f"{n}-linux-x86_64.tar.gz"
    a = lambda f: f"[{f}]({base}/{f})"  # noqa: E731
    return (
        f"## 下载\n首次安装请点击下载（已安装 0.1.1 及以后版本的程序会自动检测并下载更新）：\n\n"
        f"| 系统 | 安装包 |\n|---|---|\n"
        f"| Windows 10 / 11（64 位，无需管理员权限） | {a(win)} |\n"
        f"| macOS 11 及以上（Apple 芯片：M1 / M2 / M3 / M4…） | {a(arm)} |\n"
        f"| macOS 11 及以上（Intel 芯片） | {a(x86)} |\n"
        f"| Linux（x86_64） | {a(lin)} |\n\n"
        f"国内下载慢可以用 123 云盘：{MIRROR_URL}\n\n"
        f"macOS 版未经 Apple 公证，第一次打开会被拦下：请在「系统设置 → 隐私与安全性」中点「仍要打开」，"
        f"或在终端运行 `xattr -dr com.apple.quarantine /Applications/SimpleQuant.app`。\n\n"
        f"下方「Assets」中的其余文件（manifest、`-from-` 增量包）供程序自动更新使用，无需下载。\n\n"
        f"## 更新内容\n{zh}\n\n"
        f"> 本软件仅供学习与研究使用，不构成任何投资建议。\n\n---\n\n"
        f"## Download\nFor a fresh install: {a(win)} (Windows), {a(arm)} / {a(x86)} (macOS, Apple silicon / Intel) "
        f"or {a(lin)} (Linux). Installed copies (0.1.1 or later) update themselves. The macOS app is not notarized: "
        f"on first launch, allow it under System Settings → Privacy & Security → Open Anyway. "
        f"The other assets are used by the auto-updater.\n\n"
        f"## Changes\n{en}\n\n"
        f"This software is for learning and research only and is not investment advice.\n")


# ---------- GitHub ----------
def github(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                      "X-GitHub-Api-Version": "2022-11-28"})
    # 等 Actions 时要轮询几十分钟，偶尔连接被断开不应让发布中断；只重试 GET 等幂等请求，上传（POST）不重试
    retry = Retry(total=5, connect=5, read=5, backoff_factor=2, status_forcelist=(502, 503, 504),
                  allowed_methods=frozenset({"GET", "HEAD"}))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def find_release(s: requests.Session, tag: str) -> dict | None:
    """按标签找发布（包括草稿；草稿只有有写权限的令牌才能看到）"""
    r = s.get(f"{API}/releases?per_page=30", timeout=30)
    if r.status_code == 401:
        die("GitHub 令牌无效或已过期")
    r.raise_for_status()
    return next((x for x in r.json() if x["tag_name"] == tag), None)


def upload_asset(s: requests.Session, rel: dict, path: Path, name: str | None = None):
    """上传一个文件；同名的旧文件先删掉（重新运行时）"""
    name = name or path.name
    for a in rel.get("assets", []):
        if a["name"] == name:
            s.delete(f"{API}/releases/assets/{a['id']}", timeout=30).raise_for_status()
    print(f"  上传 {name}（{path.stat().st_size / 1e6:.1f} MB）…")
    with open(path, "rb") as f:
        r = s.post(rel["upload_url"].split("{")[0], params={"name": name}, data=f, timeout=(30, 900),
                   headers={"Content-Type": "application/octet-stream", "Content-Length": str(path.stat().st_size)})
    r.raise_for_status()
    if r.json()["name"] != name:
        die(f"GitHub 把文件名改成了 {r.json()['name']}（文件名只能用英文字母、数字和 .-_）")


def download_asset(s: requests.Session, asset: dict) -> bytes:
    """草稿里的文件没有公开下载地址，要用 API 加令牌下载"""
    r = s.get(f"{API}/releases/assets/{asset['id']}", headers={"Accept": "application/octet-stream"}, timeout=120)
    r.raise_for_status()
    return r.content


# ---------- 上传 ----------
def check_git(version: str):
    if git("status", "--porcelain", "--untracked-files=no"):
        die("有未提交的改动，先提交并推送")
    git("fetch", "origin", "--tags")
    if subprocess.run(["git", "merge-base", "--is-ancestor", "HEAD", "origin/main"], cwd=ROOT).returncode:
        die("当前提交还没有推送到 origin/main，先 git push")
    tag = f"v{version}"
    if git("tag", "--list", tag):
        if git("rev-list", "-n", "1", tag) != git("rev-parse", "HEAD"):
            die(f"标签 {tag} 已存在且不是当前提交；版本号是否忘了改？")


def upload(version: str, out: Path, token: str) -> dict:
    s = github(token)
    tag = f"v{version}"
    rel = find_release(s, tag)
    if rel and not rel["draft"]:
        die(f"{tag} 已经发布过；要发新版本请先改版本号")

    # 先建草稿再推送标签：标签触发 GitHub Actions 打包 macOS / Linux 版，要上传到这个草稿里
    body = (out / "release_notes.md").read_text(encoding="utf-8")
    if not rel:
        r = s.post(f"{API}/releases", json={"tag_name": tag, "name": f"SimpleQuant {version}", "body": body,
                                            "draft": True}, timeout=30)
        r.raise_for_status()
        rel = r.json()
    if not git("tag", "--list", tag):
        git("tag", "-a", tag, "-m", f"SimpleQuant {version}")
    git("push", "origin", tag)

    m = json.loads((out / mf.MANIFEST).read_text(encoding="utf-8"))
    names = [m["installer"]["name"], *(p["name"] for p in m["patches"].values()), mf.MANIFEST, mf.SIGNATURE]
    for name in names:   # 清单和签名最后上传
        upload_asset(s, rel, out / name)
    print(f"\n已上传 Windows 版：{rel['html_url']}")
    return rel


# ---------- macOS / Linux：等 GitHub Actions 上传后签名 ----------
def check_unix_manifest(m: dict, version: str, target: str, assets: dict):
    """签名前检查 GitHub Actions 生成的清单：版本、格式、引用的文件都在草稿里且大小一致"""
    if m.get("app") != "SimpleQuant" or mf.parse_version(m.get("version", "")) != mf.parse_version(version):
        die(f"{mf.manifest_name(target)} 的版本是 {m.get('version')}，不是 {version}")
    if not all(mf.safe_path(r) for r in m["files"]) or \
            not all(mf.safe_link(r, t) for r, t in m.get("links", {}).items()):
        die(f"{mf.manifest_name(target)} 里有不安全的路径")
    for a in [m["archive"], *m.get("patches", {}).values()]:
        if a["name"] not in assets or assets[a["name"]]["size"] != a["size"]:
            die(f"草稿里没有 {a['name']}，或大小与清单不一致")


def sign_unix(version: str, token: str, out_root: Path = OUT_ROOT, wait_min: float = CI_WAIT_MIN):
    secret = load_secret()
    s = github(token)
    tag = f"v{version}"
    out = out_root / version
    out.mkdir(parents=True, exist_ok=True)
    pending = list(UNIX_TARGETS)
    deadline = time.time() + wait_min * 60
    print(f"\n等待 GitHub Actions 打包 macOS / Linux 版（通常 15~25 分钟，最多等 {wait_min} 分钟）…")
    while pending:
        rel = find_release(s, tag)
        if not rel:
            die(f"没有找到 {tag} 的发布草稿")
        assets = {a["name"]: a for a in rel["assets"]}
        for target in list(pending):
            name, sig_name = mf.manifest_name(target), mf.signature_name(target)
            if name not in assets:
                continue
            data = download_asset(s, assets[name])
            check_unix_manifest(json.loads(data.decode("utf-8")), version, target, assets)
            (out / name).write_bytes(data)
            (out / sig_name).write_text(mf.sign(data, secret), encoding="ascii")
            mf.load_verified(data, (out / sig_name).read_text(encoding="ascii"), cfg.PUBLIC_KEY)   # 自检
            upload_asset(s, rel, out / sig_name)
            pending.remove(target)
            print(f"  {target}：已签名")
        if pending:
            if time.time() > deadline:
                die(f"还没等到 {', '.join(pending)}；在 GitHub 的 Actions 页面查看打包进度，完成后运行 "
                    f"release.py --sign-only。未签名的平台不会收到自动更新")
            time.sleep(30)
    print(f"\n全部平台已就绪：{rel['html_url']}")


# ---------- 发布草稿 ----------
def publish(version: str, token: str, wait_min: float = CI_WAIT_MIN):
    """按 CHANGELOG.md 重新生成说明；等该标签的 GitHub Actions 全部结束且通过、各平台清单都已签名，再公开"""
    s = github(token)
    tag = f"v{version}"
    rel = find_release(s, tag)
    if not rel:
        die(f"没有找到 {tag} 的发布草稿")
    if not rel["draft"]:
        die(f"{tag} 已经发布过")
    names = {a["name"] for a in rel["assets"]}
    missing = [n for n in [mf.MANIFEST, mf.SIGNATURE, *(mf.signature_name(t) for t in UNIX_TARGETS)] if n not in names]
    if missing:
        die(f"草稿里还缺 {', '.join(missing)}；先运行 release.py --sign-only")
    body = release_notes(version, f"SimpleQuant-{version}-Setup.exe")
    sha = git("rev-list", "-n", "1", tag)
    deadline = time.time() + wait_min * 60
    print(f"等待 {tag} 的 GitHub Actions 全部结束（Intel Mac 最慢，整个运行约 25 分钟）…")
    while True:
        # 公开仓库查询运行状态不需要令牌（fine-grained 令牌未必有 Actions 权限）
        runs = requests.get(f"{API}/actions/runs", params={"head_sha": sha, "event": "push"},
                            timeout=30).json().get("workflow_runs", [])
        runs = [r for r in runs if r["head_branch"] == tag]     # 同一提交先前推到 ci/** 分支的运行不算
        if runs and all(r["status"] == "completed" for r in runs):
            bad = [f"{r['name']}：{r['conclusion']}" for r in runs if r["conclusion"] != "success"]
            if bad:
                die(f"GitHub Actions 没有全部通过（{'；'.join(bad)}），不发布。确认无碍后可在网页上发布")
            break
        if time.time() > deadline:
            die(f"等了 {wait_min} 分钟 GitHub Actions 还没结束；结束后再运行 release.py publish")
        time.sleep(30)
    # 修改发布草稿时必须同时传 tag_name，否则 GitHub 会把草稿的标签重置成 untagged-…
    r = s.patch(f"{API}/releases/{rel['id']}", json={"tag_name": tag, "body": body, "draft": False}, timeout=30)
    r.raise_for_status()
    print(f"\n已发布：{r.json()['html_url']}")


# ---------- GitHub Actions 里：本平台的清单与补丁 ----------
def ci(upload_to_release: bool):
    """
    在 macOS / Linux 打包机上运行（先运行 tools/build_unix.sh）。生成未签名的 manifest-<平台>.json 与补丁包；
    由标签触发时上传到该标签的发布草稿（GITHUB_TOKEN），草稿由本机的 release.py 先建好
    """
    version, target = __version__, system.TARGET
    dist = DIST / system.SYSTEM
    app = dist / ("SimpleQuant.app" if system.MACOS else "SimpleQuant")
    stem = f"SimpleQuant-{version}-{target}"
    archive = dist / f"{stem}.tar.gz"
    if not app.is_dir() or not archive.exists():
        die(f"没有找到 {app} 或 {archive}，先运行 tools/build_unix.sh")
    out = OUT_ROOT / version / target
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    files, links = mf.scan_tree(app)
    files = {p.relative_to(app).as_posix(): [mf.sha256_file(p), p.stat().st_size] for p in files}
    token = os.environ.get("GITHUB_TOKEN")
    patches = make_patches(version, app, files, previous_manifests(version, token, None, target), out, target)
    m = {"app": "SimpleQuant", "version": version, "target": target, "files": files, "links": links,
         "archive": asset_info(archive), "patches": patches}
    (out / mf.manifest_name(target)).write_bytes(mf.dumps(m))
    print(f"{target}：{len(files)} 个文件，{len(links)} 个符号链接，补丁 {len(patches)} 个 → {out}")
    if not upload_to_release:
        return
    if not token:
        die("没有 GITHUB_TOKEN")
    s = github(token)
    tag = f"v{version}"
    for _ in range(20):          # 草稿由 release.py 在推送标签之前建好；以防万一多等一会儿
        rel = find_release(s, tag)
        if rel:
            break
        time.sleep(30)
    else:
        die(f"没有找到 {tag} 的发布草稿（应先在本机运行 release.py）")
    if not rel["draft"]:
        die(f"{tag} 已经发布，不再改动")
    uploads = [archive, *(out / p["name"] for p in patches.values())]
    if (dmg := dist / f"{stem}.dmg").exists():
        uploads.insert(0, dmg)
    for path in uploads + [out / mf.manifest_name(target)]:      # 清单最后上传：本机看到它就说明文件齐了
        upload_asset(s, find_release(s, tag), path)


def main():
    ap = argparse.ArgumentParser(description="SimpleQuant 发布脚本")
    ap.add_argument("command", nargs="?", default="release", choices=["release", "keygen", "ci", "publish"])
    ap.add_argument("--force", action="store_true", help="keygen：覆盖已有密钥")
    ap.add_argument("--no-upload", action="store_true", help="只打包并生成发布文件")
    ap.add_argument("--no-build", action="store_true", help="用 dist\\release\\<版本> 里已有的文件上传")
    ap.add_argument("--yes", action="store_true", help="上传前不再确认")
    ap.add_argument("--sign-only", action="store_true", help="只给 GitHub Actions 上传的 macOS / Linux 清单签名")
    ap.add_argument("--upload", action="store_true", help="ci：上传到当前标签的发布草稿")
    ap.add_argument("--publish", action="store_true", help="上传、签名之后接着发布（等 GitHub Actions 全部通过）")
    args = ap.parse_args()
    if args.command == "keygen":
        return keygen(args.force)
    if args.command == "ci":
        return ci(args.upload)
    if args.command == "publish":
        return publish(__version__, load_token())
    if args.sign_only:
        sign_unix(__version__, load_token())
        return publish(__version__, load_token()) if args.publish else None

    version = __version__
    print(f"SimpleQuant {version}")
    changelog(version)          # 没写更新内容就不必打包
    token = None if args.no_upload else load_token()
    load_secret()
    if not args.no_upload:
        check_git(version)
    out = OUT_ROOT / version
    if args.no_build:
        if not (out / mf.MANIFEST).exists():
            die(f"{out} 里没有发布文件，去掉 --no-build 重新生成")
    else:
        out = make_release(version, build(version), token)
    if args.no_upload:
        return
    if not args.yes and input(f"\n将打标签 v{version} 并上传到 GitHub（草稿，发布前对外不可见）。继续？[y/N] ").strip().lower() != "y":
        die("已取消")
    upload(version, out, token)
    sign_unix(version, token)
    if args.publish:
        publish(version, token)
    else:
        print("检查草稿无误后运行 release.py publish（也可在网页上点 Publish release）。")


if __name__ == "__main__":
    main()
