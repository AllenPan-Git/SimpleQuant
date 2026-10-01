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
        try:
            metas.append(DatasetMeta(**json.loads(p.read_text(encoding="utf-8"))))
        except (json.JSONDecodeError, TypeError):
            continue
    return sorted(metas, key=lambda m: m.created, reverse=True)


def delete(dataset_id: str, lib_dir: Path = LIB_DIR) -> None:
    for ext in (".parquet", ".json"):
        (lib_dir / f"{dataset_id}{ext}").unlink(missing_ok=True)
