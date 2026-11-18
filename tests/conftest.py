import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def make_prices(n=400, freq="B", seed=0, start="2021-01-04", drift=0.0003, amp=0.15, period=60):
    """带正弦波动的合成行情，保证均线会多次交叉"""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n, freq=freq)
    t = np.arange(n)
    close = 10 * np.exp(drift * t + amp * np.sin(2 * np.pi * t / period) + rng.normal(0, 0.005, n).cumsum())
    open_ = close * (1 + rng.normal(0, 0.002, n))
    df = pd.DataFrame({
        "open": open_, "high": np.maximum(open_, close) * 1.005, "low": np.minimum(open_, close) * 0.995,
        "close": close, "volume": rng.integers(1e5, 1e6, n).astype(float),
    }, index=idx)
    df.index.name = "datetime"
    return df


@pytest.fixture
def prices():
    return make_prices()


@pytest.fixture(autouse=True)
def _no_library_sync(monkeypatch):
    """模拟盘更新后的「顺便更新本地数据库」会联网、读真实数据目录：测试里默认不做（要测它的测试自己换回来）"""
    from simplequant.paper import library_sync
    real = library_sync.sync
    monkeypatch.setattr(library_sync, "sync", lambda *a, **k: None)
    return real


@pytest.fixture(autouse=True)
def _reset_data_sites():
    """数据接口的「暂停访问」状态与新浪缓存是进程级的，每个测试前后清空"""
    from simplequant.data import net, sina
    net.reset()
    sina.clear_cache()
    yield
    net.reset()
    sina.clear_cache()
