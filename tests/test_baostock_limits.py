"""BaoStock 的访问规则：每天请求次数上限、黑名单时整批停止（不联网）"""

import json

import pandas as pd
import pytest

from simplequant.stocks import StockStore
from simplequant.stocks import store as store_mod
from simplequant.stocks.store import BaoStockStopped, _Usage
from test_stocks import fake_fetcher


class FakeRS:
    def __init__(self, code="0", msg="", rows=()):
        self.error_code, self.error_msg, self.fields, self._rows = code, msg, ["a"], list(rows)

    def next(self):
        return bool(self._rows)

    def get_row_data(self):
        return self._rows.pop(0)


def test_usage_counts_per_day_and_stops_at_limit(tmp_path):
    path = tmp_path / "usage.json"
    u = _Usage(path, limit=3)
    for _ in range(3):
        u.take()
    with pytest.raises(BaoStockStopped, match="5 万次"):
        u.take()
    assert json.loads(path.read_text())["count"] == 3
    # 另一个进程（新对象）读到同一天的计数，也不能再请求
    with pytest.raises(BaoStockStopped):
        _Usage(path, limit=3).take()
    # 第二天重新计数
    path.write_text(json.dumps({"date": "2000-01-01", "count": 3}))
    v = _Usage(path, limit=3)
    v.take()
    assert v.count == 1


def test_blacklist_stops_query_without_retrying(monkeypatch, tmp_path):
    monkeypatch.setattr(store_mod, "USAGE", _Usage(tmp_path / "u.json"))
    calls = []

    def fn():
        calls.append(1)
        return FakeRS("10001011", "黑名单用户，请与管理员联系")

    with pytest.raises(BaoStockStopped, match="blacklist"):
        store_mod._query(fn)
    assert len(calls) == 1
    assert store_mod._query(lambda: FakeRS(rows=[["1"]])).equals(pd.DataFrame([["1"]], columns=["a"]))


def test_download_stops_whole_batch_and_keeps_progress(tmp_path):
    def fetch(code, start, end):
        if code == "sh.600002":
            raise BaoStockStopped("quota")
        return fake_fetcher(code, start, end)

    st = StockStore(tmp_path, fetcher=fetch, login=lambda: None, worker_init=lambda: None)
    codes = ["sh.600000", "sh.600001", "sh.600002", "sh.600003"]
    with pytest.raises(BaoStockStopped):
        st.update(codes, "2024-01-01", "2024-02-29", workers=4)
    assert st.has("sh.600000") and st.has("sh.600001") and not st.has("sh.600003")
    assert set(st._manifest()) == {"sh.600000", "sh.600001"}           # 进度已保存，下次从断点继续
    assert list(st.plan(codes, "2024-01-01", "2024-02-29")) == ["sh.600002", "sh.600003"]
