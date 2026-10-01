"""
更新清单（发布脚本与客户端共用）

manifest.json：
    {"app": "SimpleQuant", "version": "0.1.1",
     "files": {"SimpleQuant.exe": [sha256, 字节数], "_internal/...": [...]},     # 安装目录里的全部程序文件
     "installer": {"name": "SimpleQuant-0.1.1-Setup.exe", "sha256": ..., "size": ...},
     "patches": {"0.1.0": {"name": "SimpleQuant-0.1.1-from-0.1.0.zip", "sha256": ..., "size": ...,
                           "files": [补丁里的文件], "removed": [新版本不再需要的文件]}}}
manifest.json.sig：对 manifest.json 原始字节的 Ed25519 签名（十六进制）
"""

import hashlib
import json
from pathlib import Path, PurePosixPath

from simplequant.update import ed25519

MANIFEST = "manifest.json"
SIGNATURE = "manifest.json.sig"


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


def hash_tree(root: Path) -> dict[str, list]:
    """{相对路径（/ 分隔）: [sha256, 字节数]}"""
    root = Path(root)
    return {p.relative_to(root).as_posix(): [sha256_file(p), p.stat().st_size]
            for p in sorted(root.rglob("*")) if p.is_file()}


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
    except (ValueError, KeyError, TypeError, AssertionError) as e:
        raise ManifestError(f"format: {e}") from e
    return m
