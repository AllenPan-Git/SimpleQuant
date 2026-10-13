"""
选股面板的磁盘缓存：建一次面板要十几秒（沪深300），存下来后重开软件只需读一个文件

- 键 = 股票池、起止日、数据版本（数据文件的修改时间）、软件版本：数据更新或换了版本（建面板的规则可能变了）就重建
- 最多留 MAX_FILES 个文件，按最近使用保留；读不出来的文件删掉重建
- 可转债面板的选股条件是 lambda，不能 pickle：存之前去掉、读出来按 CB_FILTERS 补上
- pickle 后用 zstd 压缩（pyarrow 自带）：沪深300 约 120 MB → 40 MB，可转债约 490 MB → 75 MB，读写各不到半秒
"""

import hashlib
import os
import pickle
import struct
from dataclasses import replace
from pathlib import Path

from .. import __version__

MAX_FILES = 3
MAGIC = b"SQP1"          # 文件头：标记 + 8 字节原始长度，之后是 zstd 压缩的 pickle


def _codec():
    import pyarrow as pa
    return pa.Codec("zstd", compression_level=3)


def _path(root: Path, universe: str, start: str, end: str, data_version: str) -> Path:
    h = hashlib.sha1(f"{universe}|{start}|{end}|{data_version}|{__version__}".encode()).hexdigest()[:16]
    return root / f"{universe}_{start}_{end}_{h}.pkl"


def _restore(panel):
    if panel.kind == "cb":
        from ..bonds.panel import CB_FILTERS
        panel.extra_filters = {k: m["fn"] for k, m in CB_FILTERS.items()}
    return panel


def load(root: Path, universe: str, start: str, end: str, data_version: str):
    """有缓存就返回面板，否则 None"""
    f = _path(root, universe, start, end, data_version)
    if not f.exists():
        return None
    try:
        data = f.read_bytes()
        if data[:4] != MAGIC:
            raise ValueError("not a panel cache")
        (size,) = struct.unpack("<Q", data[4:12])
        panel = pickle.loads(_codec().decompress(data[12:], decompressed_size=size, asbytes=True))
    except Exception:  # noqa: BLE001 — 文件不完整、旧格式等：当作没有缓存
        f.unlink(missing_ok=True)
        return None
    os.utime(f)                     # 记录最近使用
    return _restore(panel)


def save(root: Path, universe: str, start: str, end: str, data_version: str, panel) -> None:
    """存一份；写失败（磁盘满、没有权限）不影响使用"""
    try:
        root.mkdir(parents=True, exist_ok=True)
        f = _path(root, universe, start, end, data_version)
        prefix = f"{universe}_{start}_{end}_"
        for old in root.glob(f"{prefix}*.pkl"):         # 同一区间的旧版本数据已经用不上了
            if old != f:
                old.unlink(missing_ok=True)
        tmp = f.with_suffix(".tmp")
        raw = pickle.dumps(replace(panel, extra_filters={}), protocol=pickle.HIGHEST_PROTOCOL)
        tmp.write_bytes(MAGIC + struct.pack("<Q", len(raw)) + _codec().compress(raw, asbytes=True))
        os.replace(tmp, f)
        files = sorted(root.glob("*.pkl"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[MAX_FILES:]:
            old.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 — 缓存只是加速，存不下也照常使用
        pass
