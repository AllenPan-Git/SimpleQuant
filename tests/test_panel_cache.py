"""选股面板的磁盘缓存"""

from dataclasses import replace

from test_stocks import make_panel
from simplequant.stocks import panel_cache as C


def _same(a, b):
    return all(a.fields[k].equals(b.fields[k]) for k in a.fields) and a.member.equals(b.member) \
        and a.can_buy.equals(b.can_buy) and a.benchmark.equals(b.benchmark)


def test_round_trip_and_key(tmp_path):
    p = make_panel()
    C.save(tmp_path, "hs300", "2021-01-04", "2022-06-01", "v1", p)
    q = C.load(tmp_path, "hs300", "2021-01-04", "2022-06-01", "v1")
    assert q is not None and _same(p, q)
    assert C.load(tmp_path, "hs300", "2021-01-04", "2022-06-01", "v2") is None     # 数据更新了
    assert C.load(tmp_path, "hs300", "2021-01-05", "2022-06-01", "v1") is None     # 换了区间
    C.save(tmp_path, "hs300", "2021-01-04", "2022-06-01", "v2", p)                  # 同区间的旧数据版本删掉
    assert len(list(tmp_path.glob("*.pkl"))) == 1


def test_software_version_in_key(tmp_path, monkeypatch):
    p = make_panel()
    C.save(tmp_path, "hs300", "a", "b", "v1", p)
    monkeypatch.setattr(C, "__version__", "99.0.0")
    assert C.load(tmp_path, "hs300", "a", "b", "v1") is None


def test_broken_file_is_rebuilt(tmp_path):
    p = make_panel()
    C.save(tmp_path, "hs300", "a", "b", "v1", p)
    f = next(tmp_path.glob("*.pkl"))
    f.write_bytes(f.read_bytes()[:100])
    assert C.load(tmp_path, "hs300", "a", "b", "v1") is None
    assert not f.exists()


def test_keeps_most_recent_files(tmp_path):
    p = make_panel(n=5, days=50)
    for i in range(5):
        C.save(tmp_path, "hs300", f"s{i}", "e", "v1", p)
    assert len(list(tmp_path.glob("*.pkl"))) == C.MAX_FILES
    assert C.load(tmp_path, "hs300", "s4", "e", "v1") is not None
    assert C.load(tmp_path, "hs300", "s0", "e", "v1") is None


def test_cb_filters_restored(tmp_path):
    from simplequant.bonds.panel import CB_FILTERS
    p = replace(make_panel(n=5, days=50), kind="cb", extra_filters={k: m["fn"] for k, m in CB_FILTERS.items()})
    C.save(tmp_path, "cb", "a", "b", "v1", p)          # lambda 不能 pickle：存之前去掉
    q = C.load(tmp_path, "cb", "a", "b", "v1")
    assert set(q.extra_filters) == set(CB_FILTERS) and callable(next(iter(q.extra_filters.values())))


def test_unwritable_dir_does_not_raise(tmp_path):
    f = tmp_path / "file"
    f.write_text("x")
    C.save(f / "sub", "hs300", "a", "b", "v1", make_panel(n=5, days=50))     # 父路径是文件，建不了目录
