"""
更新清单（发布脚本与客户端共用）

manifest.json：
    {"app": "SimpleQuant", "version": "0.1.1",
     "files": {"SimpleQuant.exe": [sha256, 字节数], "_internal/...": [...]},     # 安装目录里的全部程序文件
     "installer": {"name": "SimpleQuant-0.1.1-Setup.exe", "sha256": ..., "size": ...},
     "patches": {"0.1.0": {"name": "SimpleQuant-0.1.1-from-0.1.0.zip", "sha256": ..., "size": ...,
                           "files": [补丁里的文件], "removed": [新版本不再需要的文件]}}}
manifest.json.sig：对 manifest.json 原始字节的 Ed25519 签名（十六进制）

macOS / Linux 每个平台一份：manifest-macos-arm64.json、manifest-linux-x86_64.json 等（Windows 沿用 manifest.json，旧版本客户端读的就是它）。
与 Windows 的区别：没有 installer，改为 "archive"（整个程序目录的 .tar.gz，补丁用不上时整体替换）；
"links" 记录程序目录里的符号链接 {相对路径: 链接目标}（macOS 的 .app 里有），补丁更新时据此重建。
"""

import hashlib
import json
import os
import posixpath
from pathlib import Path, PurePosixPath

from simplequant.update import ed25519

MANIFEST = "manifest.json"
SIGNATURE = "manifest.json.sig"


def manifest_name(target: str = "windows") -> str:
    """target：windows、macos-arm64、linux-x86_64……（system.target）"""
    return MANIFEST if target == "windows" else f"manifest-{target}.json"


def signature_name(target: str = "windows") -> str:
    return manifest_name(target) + ".sig"


class ManifestError(Exception):
    pass


def parse_version(v: str) -> tuple[int, ...]:
    """'v0.1.10' → (0, 1, 10)；无法识别的部分按 0 处理"""
    parts = []
    for x in str(v).strip().lstrip("vV").split("."):
        digits = "".join(c for c in x if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def scan_tree(root: Path) -> tuple[list[Path], dict[str, str]]:
    """(普通文件, {符号链接的相对路径: 链接目标})；不进入链接指向的目录"""
    root = Path(root)
    files, links = [], {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = Path(dirpath, name)
            if p.is_symlink():
                links[p.relative_to(root).as_posix()] = os.readlink(p).replace("\\", "/")
            elif name in filenames:
                files.append(p)
    return sorted(files), dict(sorted(links.items()))


def hash_tree(root: Path) -> dict[str, list]:
    """{相对路径（/ 分隔）: [sha256, 字节数]}（符号链接不算，见 links_tree）"""
    root = Path(root)
    return {p.relative_to(root).as_posix(): [sha256_file(p), p.stat().st_size] for p in scan_tree(root)[0]}


def links_tree(root: Path) -> dict[str, str]:
    return scan_tree(root)[1]


def safe_link(rel: str, target: str) -> bool:
    """链接本身在程序目录内，目标是相对路径且不指出程序目录"""
    if not safe_path(rel) or not target or target.startswith("/") or ":" in target or "\\" in target:
        return False
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(rel), target))
    return resolved != ".." and not resolved.startswith("../")


def safe_path(rel: str) -> bool:
    """清单里的路径只能指向安装目录内部"""
    pp = PurePosixPath(rel)
    return bool(rel) and not pp.is_absolute() and ":" not in rel and "\\" not in rel and ".." not in pp.parts


def diff(old: dict, new: dict) -> tuple[list[str], list[str]]:
    """(新增或内容变化的文件, 新版本不再有的文件)"""
    changed = [r for r, (sha, _) in new.items() if old.get(r, [None])[0] != sha]
    removed = [r for r in old if r not in new]
    return changed, removed


def dumps(manifest: dict) -> bytes:
    return json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8")


def sign(data: bytes, secret: bytes) -> str:
    return ed25519.sign(secret, data).hex()


def load_verified(data: bytes, signature: str, public_hex: str) -> dict:
    """验证签名并解析；签名不对、格式不对都抛 ManifestError"""
    try:
        ok = bool(public_hex) and ed25519.verify(bytes.fromhex(public_hex), data, bytes.fromhex(signature.strip()))
    except ValueError:
        ok = False
    if not ok:
        raise ManifestError("signature")
    try:
        m = json.loads(data.decode("utf-8"))
        files = m["files"]
        assert m["app"] == "SimpleQuant" and isinstance(files, dict) and m["version"]
        assert all(safe_path(r) for r in files)
        for p in m.get("patches", {}).values():
            assert all(safe_path(r) for r in p["files"] + p.get("removed", []))
        assert all(safe_link(r, t) for r, t in m.get("links", {}).items())
    except (ValueError, KeyError, TypeError, AssertionError) as e:
        raise ManifestError(f"format: {e}") from e
    return m
