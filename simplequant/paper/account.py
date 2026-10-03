"""
模拟账户的存储

data_cache/paper/<账户id>/
    account.json    设置：策略、资金与费率、开始日期、标的/股票池、状态、数据截至日期
    fills.parquet   成交账本（只追加，已记录的成交不会被改写）
    nav.parquet     每日资产（只追加）
    state.json      最新一次运行的结果：今日信号、当前持仓、指标、一致性检查
    error.json      上一次运行失败的时间和原因（成功运行后删除）
    prices/         单标的账户自己的行情数据（前复权，每次运行时刷新）
"""

import datetime as dt
import json
import re
import shutil
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

import pandas as pd

from ..engine.base_strategy import ORDER_COLUMNS
from ..paths import CACHE_DIR

PAPER_ROOT = CACHE_DIR / "paper"
NAV_COLUMNS = ["value", "cash", "benchmark"]


@dataclass
class PaperAccount:
    id: str
    name: str
    kind: str                   # "single"（单标的策略）/ "selection"（多因子选股）
    spec: dict                  # 策略描述
    broker: dict                # BrokerConfig 的字段
    start: str                  # 从这天收盘起产生信号（下一个交易日开盘第一次成交）
    assets: list = field(default_factory=list)   # 单标的：[{"source", "symbol", "asset", "name"}]
    universe: str = ""          # 多因子选股的股票池
    status: str = "active"      # active / paused
    created: str = field(default_factory=lambda: dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
    last_run: str = ""
    data_through: str = ""      # 已处理到哪天的数据
    note: str = ""

    def __post_init__(self):
        # 同一标的选了两份数据（例如同一只 ETF 的两次下载）时只保留一份：模拟盘按代码重新取数据，重复会被当成两个标的
        seen, uniq = set(), []
        for a in self.assets:
            key = (a.get("source"), a.get("symbol"))
            if key not in seen:
                seen.add(key)
                uniq.append(a)
        self.assets = uniq

    # ---------- 路径 ----------
    @property
    def dir(self) -> Path:
        return PAPER_ROOT / self.id

    # ---------- 读写 ----------
    def save(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "account.json").write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")

    def fills(self) -> pd.DataFrame:
        p = self.dir / "fills.parquet"
        return pd.read_parquet(p) if p.exists() else pd.DataFrame(columns=ORDER_COLUMNS)

    def save_fills(self, df: pd.DataFrame):
        df.to_parquet(self.dir / "fills.parquet")

    def nav(self) -> pd.DataFrame:
        p = self.dir / "nav.parquet"
        return pd.read_parquet(p) if p.exists() else pd.DataFrame(columns=NAV_COLUMNS)

    def save_nav(self, df: pd.DataFrame):
        df.to_parquet(self.dir / "nav.parquet")

    def state(self) -> dict:
        p = self.dir / "state.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def save_state(self, state: dict):
        (self.dir / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    def error(self) -> dict:
        """上一次运行失败时的 {"time", "message"}；上一次成功或从未失败时为空"""
        p = self.dir / "error.json"
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def save_error(self, message: str):
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "error.json").write_text(json.dumps(
            {"time": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "message": message}, ensure_ascii=False),
            encoding="utf-8")

    def clear_error(self):
        (self.dir / "error.json").unlink(missing_ok=True)


def new_id(name: str) -> str:
    slug = re.sub(r"[^\w]+", "", name)[:12] or "acct"
    return f"{dt.date.today():%Y%m%d}_{slug}_{uuid.uuid4().hex[:6]}"


def list_accounts(root: Path | None = None) -> list[PaperAccount]:
    root = root or PAPER_ROOT
    if not root.exists():
        return []
    out = []
    for p in sorted(root.glob("*/account.json")):
        try:
            out.append(PaperAccount(**json.loads(p.read_text(encoding="utf-8"))))
        except (json.JSONDecodeError, TypeError):
            continue
    return sorted(out, key=lambda a: a.created, reverse=True)


def load_account(account_id: str, root: Path | None = None) -> PaperAccount:
    root = root or PAPER_ROOT
    return PaperAccount(**json.loads((root / account_id / "account.json").read_text(encoding="utf-8")))


def delete_account(account: PaperAccount):
    if account.dir.exists() and account.dir.parent == PAPER_ROOT:
        shutil.rmtree(account.dir)
