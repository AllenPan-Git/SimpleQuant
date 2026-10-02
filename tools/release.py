"""
发布新版本（只在发布者本机运行）

    .venv\\Scripts\\python tools\\release.py keygen        第一次：生成更新签名密钥，并把公钥写进 simplequant/update/__init__.py
    .venv\\Scripts\\python tools\\release.py               打包 → 清单与签名 → 补丁包 → 打标签 → 上传为 GitHub 草稿
    .venv\\Scripts\\python tools\\release.py --no-upload   只打包并生成发布文件（dist\\release\\<版本>\\），不打标签、不上传
    .venv\\Scripts\\python tools\\release.py --no-build    用已生成的发布文件直接上传（上次上传中断时）
    .venv\\Scripts\\python tools\\release.py --sign-only   只给 macOS / Linux 的清单签名（上次没等到 GitHub Actions 打包完时）
    python tools/release.py ci                         GitHub Actions 里调用：生成本平台的清单（未签名）与补丁包并上传

三个系统（PyInstaller 不能交叉编译）：
    Windows：本机打包（安装包 + manifest.json），与以前相同
    macOS / Linux：推送标签后 GitHub Actions（.github/workflows/build.yml）在各系统上打包，`release.py ci` 把
        dmg / tar.gz、补丁包、未签名的 manifest-<平台>.json 上传到同一个草稿；
        本机的 release.py 等它们上传完，下载这几个清单（很小）签名，再上传 .sig。私钥始终只在本机

发布前：改好 simplequant/__init__.py 的版本号，测试通过，提交并推送到 GitHub。
上传后：在 GitHub 打开草稿，检查发布说明（dist\\release\\<版本>\\release_notes.md 是初稿），点 Publish。
        发布之后，用户的程序才会检测到新版本；草稿对外不可见。

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


def release_notes(version: str, installer: str) -> str:
    n = f"SimpleQuant-{version}"
    return (
        f"## 更新内容\n- \n\n## 下载\n已安装 0.1.1 及以后版本的程序会自动检测并下载更新。首次安装请下载：\n\n"
        f"| 系统 | 文件 |\n|---|---|\n"
        f"| Windows 10 / 11（64 位，无需管理员权限） | `{installer}` |\n"
        f"| macOS 11 及以上（Apple 芯片：M1 / M2 / M3 / M4…） | `{n}-macos-arm64.dmg` |\n"
        f"| macOS 11 及以上（Intel 芯片） | `{n}-macos-x86_64.dmg` |\n"
        f"| Linux（x86_64） | `{n}-linux-x86_64.tar.gz` |\n\n"
        f"macOS 版未经 Apple 公证，第一次打开会被拦下：请在「系统设置 → 隐私与安全性」中点「仍要打开」，"
        f"或在终端运行 `xattr -dr com.apple.quarantine /Applications/SimpleQuant.app`。\n\n"
        f"> 本软件仅供学习与研究使用，不构成任何投资建议。\n\n---\n\n"
        f"## Changes\n- \n\nInstalled copies (0.1.1 or later) update themselves. For a fresh install, download "
        f"`{installer}` (Windows), `{n}-macos-arm64.dmg` / `{n}-macos-x86_64.dmg` (macOS, Apple silicon / Intel) or "
        f"`{n}-linux-x86_64.tar.gz` (Linux). The macOS app is not notarized: on first launch, allow it under "
        f"System Settings → Privacy & Security → Open Anyway.\n\n"
        f"This software is for learning and research only and is not investment advice.\n")


# ---------- GitHub ----------
def github(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                      "X-GitHub-Api-Version": "2022-11-28"})
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
    print(f"\n全部平台已就绪：{rel['html_url']}\n请在 GitHub 上检查发布说明，然后点 Publish release。")


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
    ap.add_argument("command", nargs="?", default="release", choices=["release", "keygen", "ci"])
    ap.add_argument("--force", action="store_true", help="keygen：覆盖已有密钥")
    ap.add_argument("--no-upload", action="store_true", help="只打包并生成发布文件")
    ap.add_argument("--no-build", action="store_true", help="用 dist\\release\\<版本> 里已有的文件上传")
    ap.add_argument("--yes", action="store_true", help="上传前不再确认")
    ap.add_argument("--sign-only", action="store_true", help="只给 GitHub Actions 上传的 macOS / Linux 清单签名")
    ap.add_argument("--upload", action="store_true", help="ci：上传到当前标签的发布草稿")
    args = ap.parse_args()
    if args.command == "keygen":
        return keygen(args.force)
    if args.command == "ci":
        return ci(args.upload)
    if args.sign_only:
        return sign_unix(__version__, load_token())

    version = __version__
    print(f"SimpleQuant {version}")
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


if __name__ == "__main__":
    main()
