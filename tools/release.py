"""
发布新版本（只在发布者本机运行）

    .venv\\Scripts\\python tools\\release.py keygen        第一次：生成更新签名密钥，并把公钥写进 simplequant/update/__init__.py
    .venv\\Scripts\\python tools\\release.py               打包 → 清单与签名 → 补丁包 → 打标签 → 上传为 GitHub 草稿
    .venv\\Scripts\\python tools\\release.py --no-upload   只打包并生成发布文件（dist\\release\\<版本>\\），不打标签、不上传
    .venv\\Scripts\\python tools\\release.py --no-build    用已生成的发布文件直接上传（上次上传中断时）

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
import zipfile
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from simplequant import __version__  # noqa: E402
from simplequant import update as cfg  # noqa: E402
from simplequant.update import ed25519, manifest as mf  # noqa: E402

KEY_DIR = Path.home() / ".simplequant-release"
KEY_FILE = KEY_DIR / "signing_key"
TOKEN_FILE = KEY_DIR / "github_token.txt"
DIST = ROOT / "dist"
APP = DIST / "SimpleQuant"
OUT_ROOT = DIST / "release"
API = f"https://api.github.com/repos/{cfg.REPO}"
MAX_PATCHES = 5
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
    run([sys.executable, "-m", "PyInstaller", "SimpleQuant.spec", "--noconfirm", "--clean"], env=env)
    print("生成安装包…")
    run([str(find_iscc()), "/Q", f"/DAppVersion={version}", str(ROOT / "installer" / "SimpleQuant.iss")])
    installer = DIST / f"SimpleQuant-{version}-Setup.exe"
    if not installer.exists():
        die(f"没有找到安装包 {installer}")
    return installer


# ---------- 旧版本清单 ----------
def previous_manifests(version: str, token: str | None, out_root: Path = OUT_ROOT) -> dict[str, dict]:
    """{旧版本: files}，最多 MAX_PATCHES 个，从新到旧"""
    found = {}
    for p in out_root.glob("*/manifest.json"):
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
            if mf.MANIFEST in assets and mf.SIGNATURE in assets:
                data = requests.get(assets[mf.MANIFEST], timeout=60).content
                sig = requests.get(assets[mf.SIGNATURE], timeout=60).text
                found[v] = mf.load_verified(data, sig, cfg.PUBLIC_KEY)["files"]
    except (requests.RequestException, ValueError, KeyError, TypeError, mf.ManifestError) as e:
        print(f"  （没能从 GitHub 读取旧版本清单：{e}；只用本机已有的）")
    cur = mf.parse_version(version)
    older = sorted((v for v in found if mf.parse_version(v) < cur), key=mf.parse_version, reverse=True)
    return {v: found[v] for v in older[:MAX_PATCHES]}


def asset_info(path: Path, **extra) -> dict:
    return {"name": path.name, "sha256": mf.sha256_file(path), "size": path.stat().st_size, **extra}


def make_release(version: str, installer: Path, token: str | None, app: Path = APP, out_root: Path = OUT_ROOT) -> Path:
    """生成 out_root/<版本>/：安装包、补丁包、manifest.json 与签名、发布说明初稿"""
    secret = load_secret()
    out = out_root / version
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    shutil.copy2(installer, out / installer.name)
    print("计算程序文件校验值…")
    files = mf.hash_tree(app)
    patches = {}
    for old_v, old_files in previous_manifests(version, token, out_root).items():
        changed, removed = mf.diff(old_files, files)
        z = out / f"SimpleQuant-{version}-from-{old_v}.zip"
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for r in changed:
                zf.write(app / r, r)
        patches[old_v] = asset_info(z, files=changed, removed=removed)
        print(f"  补丁 {old_v} → {version}：{len(changed)} 个文件，{z.stat().st_size / 1e6:.2f} MB，删除 {len(removed)} 个")
    m = {"app": "SimpleQuant", "version": version, "files": files,
         "installer": asset_info(out / installer.name), "patches": patches}
    data = mf.dumps(m)
    (out / mf.MANIFEST).write_bytes(data)
    (out / mf.SIGNATURE).write_text(mf.sign(data, secret), encoding="ascii")
    mf.load_verified(data, (out / mf.SIGNATURE).read_text(encoding="ascii"), cfg.PUBLIC_KEY)   # 自检
    notes = out / "release_notes.md"
    sha = m["installer"]["sha256"].upper()
    notes.write_text(
        f"## 更新内容\n- \n\n## 下载\n已安装 0.1.1 及以后版本的程序会自动检测并下载更新；首次安装请下载下方的 "
        f"`{installer.name}`（Windows 10 / 11，64 位，无需管理员权限）。\n\nSHA256：`{sha}`\n\n"
        f"> 本软件仅供学习与研究使用，不构成任何投资建议。\n\n---\n\n"
        f"## Changes\n- \n\nInstalled copies (0.1.1 or later) update themselves. For a fresh install, download "
        f"`{installer.name}` below.\n\nThis software is for learning and research only and is not investment advice.\n",
        encoding="utf-8")
    print(f"\n发布文件：{out}")
    return out


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


def upload(version: str, out: Path, token: str):
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                      "X-GitHub-Api-Version": "2022-11-28"})
    tag = f"v{version}"
    rels = s.get(f"{API}/releases?per_page=30", timeout=30)
    if rels.status_code == 401:
        die("GitHub 令牌无效或已过期")
    rels.raise_for_status()
    rel = next((r for r in rels.json() if r["tag_name"] == tag), None)
    if rel and not rel["draft"]:
        die(f"{tag} 已经发布过；要发新版本请先改版本号")

    if not git("tag", "--list", tag):
        git("tag", "-a", tag, "-m", f"SimpleQuant {version}")
    git("push", "origin", tag)

    body = (out / "release_notes.md").read_text(encoding="utf-8")
    if rel:   # 上次中断留下的草稿：沿用，删掉已上传的文件
        for a in rel["assets"]:
            s.delete(f"{API}/releases/assets/{a['id']}", timeout=30).raise_for_status()
    else:
        r = s.post(f"{API}/releases", json={"tag_name": tag, "name": f"SimpleQuant {version}", "body": body,
                                            "draft": True}, timeout=30)
        r.raise_for_status()
        rel = r.json()
    upload_url = rel["upload_url"].split("{")[0]
    m = json.loads((out / mf.MANIFEST).read_text(encoding="utf-8"))
    names = [m["installer"]["name"], *(p["name"] for p in m["patches"].values()), mf.MANIFEST, mf.SIGNATURE]
    for name in names:   # 清单和签名最后上传
        path = out / name
        print(f"  上传 {name}（{path.stat().st_size / 1e6:.1f} MB）…")
        with open(path, "rb") as f:
            r = s.post(upload_url, params={"name": name}, data=f, timeout=(30, 600),
                       headers={"Content-Type": "application/octet-stream",
                                "Content-Length": str(path.stat().st_size)})
        r.raise_for_status()
        if r.json()["name"] != name:
            die(f"GitHub 把文件名改成了 {r.json()['name']}（文件名只能用英文字母、数字和 .-_）")
    print(f"\n已上传为草稿：{rel['html_url']}\n请在 GitHub 上检查发布说明，然后点 Publish release。")


def main():
    ap = argparse.ArgumentParser(description="SimpleQuant 发布脚本")
    ap.add_argument("command", nargs="?", default="release", choices=["release", "keygen"])
    ap.add_argument("--force", action="store_true", help="keygen：覆盖已有密钥")
    ap.add_argument("--no-upload", action="store_true", help="只打包并生成发布文件")
    ap.add_argument("--no-build", action="store_true", help="用 dist\\release\\<版本> 里已有的文件上传")
    ap.add_argument("--yes", action="store_true", help="上传前不再确认")
    args = ap.parse_args()
    if args.command == "keygen":
        return keygen(args.force)

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


if __name__ == "__main__":
    main()
