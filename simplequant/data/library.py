"""
本地数据库：下载/导入的行情统一保存为 parquet + json 元数据，回测时直接从这里读取。
目录：<项目>/data_cache/
"""

import json
import re
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path

import pandas as pd

from .base import FREQ_LABELS
from ..paths import CACHE_DIR

LIB_DIR = CACHE_DIR
ALIASES = "_aliases.json"     # 被新数据替换掉的旧 id → 新 id（界面记住的旧 id 自动改用新数据）


@dataclass
class DatasetMeta:
    id: str
    name: str            # 展示名，如 "510300 沪深300ETF"
    symbol: str
    source: str
    freq: str
    adjust: str = ""
    start: str = ""
    end: str = ""
    rows: int = 0
    created: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))
    extra: dict = field(default_factory=dict)

    @property
    def freq_label(self) -> str:
        return FREQ_LABELS.get(self.freq, self.freq)


def make_id(source: str, symbol: str, freq: str, adjust: str, start: str, end: str) -> str:
    raw = f"{source}_{symbol}_{freq}_{adjust or 'none'}_{start}_{end}"
    return re.sub(r"[^\w\-]+", "_", raw)


def save(df: pd.DataFrame, meta: DatasetMeta, lib_dir: Path = LIB_DIR) -> DatasetMeta:
    lib_dir.mkdir(parents=True, exist_ok=True)
    meta.rows = len(df)
    meta.start = df.index[0].strftime("%Y-%m-%d")
    meta.end = df.index[-1].strftime("%Y-%m-%d")
    df.to_parquet(lib_dir / f"{meta.id}.parquet")
    (lib_dir / f"{meta.id}.json").write_text(json.dumps(asdict(meta), ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def load(dataset_id: str, lib_dir: Path = LIB_DIR) -> pd.DataFrame:
    return pd.read_parquet(lib_dir / f"{dataset_id}.parquet")


def exists(dataset_id: str, lib_dir: Path = LIB_DIR) -> bool:
    return (lib_dir / f"{dataset_id}.parquet").exists()


def list_datasets(lib_dir: Path = LIB_DIR) -> list[DatasetMeta]:
    if not lib_dir.exists():
        return []
    metas = []
    for p in sorted(lib_dir.glob("*.json")):
        if p.name == ALIASES:
            continue
        try:
            metas.append(DatasetMeta(**json.loads(p.read_text(encoding="utf-8"))))
        except (json.JSONDecodeError, TypeError):
            continue
    return sorted(metas, key=lambda m: m.created, reverse=True)


def delete(dataset_id: str, lib_dir: Path = LIB_DIR) -> None:
    for ext in (".parquet", ".json"):
        (lib_dir / f"{dataset_id}{ext}").unlink(missing_ok=True)


def _aliases(lib_dir: Path) -> dict:
    try:
        return json.loads((lib_dir / ALIASES).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def replace(old_id: str, new_id: str, lib_dir: Path | None = None) -> None:
    """删掉旧数据，并记下由哪份新数据代替"""
    lib_dir = lib_dir or LIB_DIR
    delete(old_id, lib_dir)
    aliases = _aliases(lib_dir)
    aliases[old_id] = new_id
    aliases.pop(new_id, None)           # 新 id 以前被替换过、现在又下载回来
    (lib_dir / ALIASES).write_text(json.dumps(aliases, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve(dataset_id: str, lib_dir: Path | None = None) -> str:
    """已被替换的旧 id → 现在代替它的 id（可能被替换了不止一次）；没被替换过的原样返回"""
    aliases, seen = _aliases(lib_dir or LIB_DIR), set()
    while dataset_id in aliases and dataset_id not in seen:
        seen.add(dataset_id)
        dataset_id = aliases[dataset_id]
    return dataset_id
