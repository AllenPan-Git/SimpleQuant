"""选股面板缓存：选股页与工具页（持仓诊断）共用

放在页面模块之外，两个页面拿到的是同一份缓存，切换页面不会重复载入同一个面板。
"""

from collections import OrderedDict

from simplequant.stocks import StockStore, UNIVERSES
from simplequant.stocks import panel_cache, universe as U

_PANELS: OrderedDict = OrderedDict()    # (股票池, 起, 止, 数据版本) → 面板；最多留 3 个
_TR_TRIED: set = set()                  # 本次启动已尝试补下载全收益指数的股票池


def store() -> StockStore:
    return StockStore()


def data_version(st: StockStore, universe: str) -> str:
    """数据有更新时让缓存的面板失效"""
    files = U.data_files(universe, st)
    return "|".join(str(f.stat().st_mtime) if f.exists() else "-" for f in files)


def get_panel(universe: str, start: str, end: str):
    st = store()
    if U.kind(universe) == "stock" and universe not in _TR_TRIED:     # 每次启动只试一次，官网连不上时不反复等
        _TR_TRIED.add(universe)
        st.ensure_tr_index(UNIVERSES[universe]["index"])
    key = (universe, start, end, data_version(st, universe))
    if key in _PANELS:
        _PANELS.move_to_end(key)
        return _PANELS[key]
    cache_dir = st.root.parent / "panel_cache"
    panel = panel_cache.load(cache_dir, *key)
    if panel is None:
        panel = U.build(universe, start, end, st)
        panel_cache.save(cache_dir, *key, panel)
    _PANELS[key] = panel
    while len(_PANELS) > 3:
        _PANELS.popitem(last=False)
    return panel


def universe_ready(universe: str) -> bool:
    return U.ready(universe, store())
