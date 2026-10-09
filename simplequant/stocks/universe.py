"""
股票池的统一入口：股票指数成分股（BaoStock，StockStore）与可转债全市场（simplequant/bonds，CBStore）
选股页、模拟盘、导出脚本都通过这里判断有没有数据、取基准（交易日历）、更新数据、生成面板
"""

import pandas as pd

from .store import StockStore, UNIVERSES


def kind(universe: str) -> str:
    """stock / cb"""
    return UNIVERSES.get(universe, {}).get("kind", "stock")


def _cb_store():
    from ..bonds import CBStore
    return CBStore()


def ready(universe: str, store: StockStore | None = None) -> bool:
    if kind(universe) == "cb":
        st = _cb_store()
        return st.ready() and any(st.root.joinpath("daily").glob("*.parquet"))
    st = store or StockStore()
    return st.has_universe(universe) and (st.root / "index" / f"{UNIVERSES[universe]['index']}.parquet").exists()


def load_benchmark(universe: str, store: StockStore | None = None) -> pd.DataFrame:
    """基准指数日线（索引即交易日历）"""
    if kind(universe) == "cb":
        st = _cb_store()
        idx = st.load_index()
        start = st.data_start()                   # 指数从 2010 年起；起始日之前退市的转债没有下载
        return idx.loc[start:] if start else idx
    return (store or StockStore()).load_index(UNIVERSES[universe]["index"])


def build(universe: str, start: str, end: str, store: StockStore | None = None):
    if kind(universe) == "cb":
        from ..bonds.panel import build_cb_panel
        return build_cb_panel(_cb_store(), start, end)
    from .panel import build_panel
    return build_panel(store or StockStore(), universe, start, end)


def data_files(universe: str, store: StockStore | None = None) -> list:
    """数据有更新时这些文件会变（界面用来让缓存的面板失效）"""
    if kind(universe) == "cb":
        root = _cb_store().root
        return [root / "manifest.json", root / "info.parquet", root / "index.parquet"]
    st = store or StockStore()
    return [st.root / "manifest.json", st.root / "universe" / f"{universe}.parquet",
            st.root / "index" / f"{UNIVERSES[universe]['index']}.parquet",
            st.root / "fin_manifest.json", st.root / "industry.parquet"]


def update(universe: str, start: str, end: str, store: StockStore | None = None, workers: int = 4,
           fin: bool = False, div: bool = False, industry: bool = False) -> dict[str, str]:
    """增量更新（模拟盘、导出脚本用）；返回失败的 {代码: 错误}"""
    if kind(universe) == "cb":
        st = _cb_store()
        return st.update_all(start, end, workers=min(workers, 3))
    st = store or StockStore()
    uni = st.update_universe(universe, start, end)
    st.update_index(UNIVERSES[universe]["index"], start, end)
    codes = sorted(uni["code"].unique())
    errors = st.update(codes, start, end, workers=workers)
    if fin:
        errors.update(st.update_fundamentals(codes, int(start[:4]) - 1, workers=workers))
    if industry:
        st.update_industry()
    if div:
        errors.update(st.update_dividends(codes, int(start[:4]) - 1, workers=workers))
    return errors
