"""
AI 因子挖掘（stocks/mining.py）
核心：发给模型的、决定入选的只用挖掘期；检验期只在最后算一次；用到未来数据的代码静态检查查不出来时也要拦住
"""

import json

import numpy as np
import pandas as pd
import pytest

from test_stocks import make_panel
from simplequant.engine.credibility import check_mining
from simplequant.llm import MINE_SCHEMA, propose_factors, ask
from simplequant.llm.nl import factor_system_prompt, mine_system_prompt
from simplequant.stocks import mining as MN

START = "2021-06-01"
EP = 'def factor(p):\n    """盈利收益率"""\n    return 1 / p["pe"]\n'
MOM = 'def factor(p):\n    """200 日动量"""\n    return p["close"] / p["close"].shift(200) - 1\n'
NOISE = 'def factor(p):\n    """成交额（假数据里与收益无关）"""\n    return p["volume"] * 0 + p["turnover"]\n'
LEAK = 'def factor(p):\n    """整段历史里的排名：静态检查查不出来"""\n    return p["close"].rank(axis=0, pct=True)\n'
IMPORT = 'def factor(p):\n    import os\n    return p["close"]\n'
CRASH = 'def factor(p):\n    raise ValueError("boom")\n'


@pytest.fixture(scope="module")
def panel():
    return make_panel(n=60, days=700)


def fac(name, code, idea="x"):
    return {"name": name, "idea": idea, "code": code, "desc": ""}


class FakeProvider:
    def __init__(self, rounds):
        self.rounds, self.calls = list(rounds), []

    def chat(self, system, messages, schema=None):
        self.calls.append({"system": system, "message": messages[-1]["content"], "schema": schema})
        r = self.rounds.pop(0)
        if isinstance(r, Exception):
            raise r
        return json.dumps({"factors": r, "notes": f"第 {len(self.calls)} 轮"}, ensure_ascii=False)


def test_split_keeps_mining_labels_inside_mining_period(panel):
    m = MN.Miner(panel, START, MN.MineConfig(horizon=20, test_frac=0.3))
    cal = panel.calendar
    last = cal.get_loc(m.mine_dates[-1])
    assert cal[last + 1 + 20] <= m.mine_end < m.test_start          # 标签用到的开盘价也在挖掘期内
    assert m.test_dates[0] >= m.test_start and all(c <= m.mine_end for c in m.cuts)
    with pytest.raises(ValueError, match="过短"):
        MN.Miner(panel, str(cal[-80].date()), MN.MineConfig(horizon=20))


def test_round_statuses(panel):
    m = MN.Miner(panel, START, MN.MineConfig(per_round=6, max_corr=0.9))
    m.baseline()
    assert any(e["key"] == "ep" for e in m.existing)
    got = m.add_round({"factors": [fac("长动量", MOM), fac("盈利", EP), fac("泄露", LEAK), fac("导入", IMPORT),
                                   fac("崩溃", CRASH), fac("长动量又一次", MOM)]})
    st = {c.name: c.status for c in got}
    assert st["盈利"] == "duplicate" and m.candidates[1].corr_with.startswith("盈利收益率")
    assert st["泄露"] == "lookahead" and st["导入"] == "review" and st["崩溃"] == "error"
    assert "boom" in m.candidates[4].reason
    assert st["长动量又一次"] == "repeat"
    assert st["长动量"] == "accepted" and m.candidates[0].direction == 1 and not m.candidates[0].test
    assert m.tried == 2                                       # 只有算出 IC 的才计入多重检验


def test_low_cover_reason(panel):
    """覆盖不足要写出覆盖率，反馈给模型时它才知道怎么改（10-12 实测：上涨日才有值 + min_periods=15）"""
    sparse = ('def factor(p):\n    """上涨日成交额"""\n    up = p["amount"].where(p["close"].pct_change(fill_method=None) > 0)\n'
              '    return up.rolling(20, min_periods=15).sum()\n')
    m = MN.Miner(panel, START, MN.MineConfig(per_round=1))
    m.baseline()
    c = m.add_round({"factors": [fac("上涨日成交额", sparse)]})[0]
    assert c.status == "low_cover" and "%" in c.reason and "50%" in c.reason
    assert c.reason in m.message() and c.reason in MN.line(c, "zh")


def test_message_has_only_mining_period(panel):
    m = MN.Miner(panel, START, MN.MineConfig(rounds=2, per_round=2, idea="量价配合", max_corr=0.9))
    prov = FakeProvider([[fac("长动量", MOM), fac("噪声", NOISE)], [fac("泄露", LEAK)]])
    res = m.run(prov)
    first, second = prov.calls[0]["message"], prov.calls[1]["message"]
    assert prov.calls[0]["schema"] is MINE_SCHEMA and "量价配合" in first and "Propose 2" in first
    assert "盈利收益率" in first and "长动量" not in first
    assert "长动量" in second and "accepted" in second
    for msg in (first, second):
        assert str(m.test_start.date()) not in msg and str(m.end.date()) not in msg
    assert res["calls"] == 2 and res["proposed"] == 3 and m.notes == ["第 1 轮", "第 2 轮"]
    acc = m.accepted[0]
    assert acc.test["n"] == len(m.test_dates) and np.isfinite(acc.test["ic"])
    assert res["combos"]["mined"]["factors"] == 1 and len(res["bench"]) == MN.TOP_EXISTING


def test_stop_and_model_error_still_finish(panel):
    m = MN.Miner(panel, START, MN.MineConfig(rounds=3, per_round=1, max_corr=0.9))
    calls = []
    res = m.run(FakeProvider([[fac("长动量", MOM)]]), log=calls.append, stop=lambda: len(m.candidates) >= 1)
    assert res["calls"] == 1 and res["accepted"] == 1 and m.accepted[0].test
    m2 = MN.Miner(panel, START, MN.MineConfig(rounds=3, per_round=1, max_corr=0.9))
    m2.existing, m2.ranks, m2.z = m.existing, {k: v for k, v in m.ranks.items() if k != "长动量"}, m.z
    res2 = m2.run(FakeProvider([[fac("长动量", MOM)], RuntimeError("network down")]), log=calls.append)
    assert res2["calls"] == 2 and res2["accepted"] == 1 and any("network down" in s for s in calls)


def test_t_needed():
    assert MN.t_needed(1) == pytest.approx(1.96, abs=0.01)
    assert MN.t_needed(50) > MN.t_needed(10) > 2.5


def test_held():
    c = MN.Candidate(round=1, name="a", idea="", code="", ic=-0.04, t=-3)
    c.test = {"ic": 0.03}
    assert c.direction == -1 and c.held
    c.test = {"ic": 0.01}
    assert not c.held


def _codes(findings):
    return [f["key"].split(".")[1] for f in findings]


def test_check_mining():
    base = {"proposed": 20, "tried": 18, "accepted": 0, "t_needed": 3.1, "n_test": 20}
    assert _codes(check_mining(base)) == ["mine_tried", "mine_none"]
    r = {**base, "accepted": 2, "lucky": 1, "held": 0, "n_test": 8,
         "combos": {"mined": {"ic": 0.01}, "existing": {"ic": 0.03, "factors": 3}}}
    assert _codes(check_mining(r)) == ["mine_tried", "mine_lucky", "mine_test_few", "mine_test_fail", "mine_below_base"]
    r.update(held=1, lucky=0, n_test=20, combos={})
    assert _codes(check_mining(r)) == ["mine_tried", "mine_test_some"]
    r.update(held=2)
    f = check_mining(r)
    assert _codes(f)[-1] == "mine_test_ok" and f[-1]["level"] == "ok"
    assert all(not isinstance(v, float) for x in f for v in x["args"].values())    # 不会被界面显示成百分数


def test_prompt_and_parse():
    sp = mine_system_prompt("zh", "stock")
    assert "def factor(p)" in sp and "never shown to you" in sp and "Simplified Chinese" in sp
    assert "def factor(p)" in factor_system_prompt("zh", "stock")          # 两个提示词共用写因子的规则
    out = propose_factors(FakeProvider([[fac(" 长动量 ", MOM.strip()), "bad"]]), "msg")
    assert out["factors"] == [{"name": "长动量", "idea": "x", "code": MOM, "desc": ""}] and out["notes"] == "第 1 轮"


def test_cut_panel(panel):
    cut = panel.calendar[300]
    p = MN.cut_panel(panel, cut)
    assert p["close"].index[-1] == cut and p.calendar[-1] == cut and len(panel.calendar) == 700


class HangingProvider:
    """模拟服务繁忙：一直不回复（DeepSeek 会持续发空行，接口的读超时不触发）"""

    def __init__(self, first):
        self.first, self.calls, self.release = first, 0, __import__("threading").Event()

    def chat(self, system, messages, schema=None):
        self.calls += 1
        if self.calls == 1:
            return json.dumps({"factors": self.first, "notes": ""}, ensure_ascii=False)
        self.release.wait(30)
        return json.dumps({"factors": [], "notes": ""})


def test_hanging_model_can_be_stopped_or_times_out(panel):
    import time
    m = MN.Miner(panel, START, MN.MineConfig(rounds=3, per_round=1, max_corr=0.9))
    prov, logs, ticks = HangingProvider([fac("长动量", MOM)]), [], []
    t0 = time.monotonic()
    res = m.run(prov, log=logs.append, progress=lambda v, s="": ticks.append(s),
                stop=lambda: time.monotonic() - t0 > 2 and prov.calls >= 2)
    prov.release.set()
    assert time.monotonic() - t0 < 15 and res["accepted"] == 1 and m.accepted[0].test   # 已有结果照常算检验期
    assert any("已停止" in s for s in logs) and any("等待模型回复" in s for s in ticks)
    m2 = MN.Miner(panel, START, MN.MineConfig(rounds=3, per_round=1, max_corr=0.9))
    m2.wait_limit = 1
    prov2, logs2 = HangingProvider([fac("长动量", MOM)]), []
    res2 = m2.run(prov2, log=logs2.append)
    prov2.release.set()
    assert res2["calls"] == 2 and res2["accepted"] == 1 and any("没有回复" in s for s in logs2)


def test_wait_for_passes_errors_through():
    with pytest.raises(ValueError, match="bad"):
        ask(type("P", (), {"chat": lambda self, *a: (_ for _ in ()).throw(ValueError("bad"))})(), "s", [])
    assert ask(type("P", (), {"chat": lambda self, *a: "ok"})(), "s", []) == "ok"


def test_markdown_record(panel):
    m = MN.Miner(panel, START, MN.MineConfig(rounds=1, per_round=2, max_corr=0.9, idea="量价"))
    m.run(FakeProvider([[fac("长动量", MOM, "动量延续"), fac("泄露", LEAK)]]))
    md = MN.to_markdown(m, "zh", "沪深300", ["提示一句"])
    assert md.startswith("# AI 因子挖掘记录 · 沪深300") and str(m.test_start.date()) in md and "量价" in md
    assert "## 可信度检查" in md and "- 提示一句" in md and "## 入选因子" in md and "## 与现有因子比较" in md
    assert MOM.strip() in md and LEAK.strip() in md and "使用了未来数据" in md and "动量延续" in md
    assert "| 长动量 | 越大越好 |" in md and MN.passes(m.accepted[0], m.result) == (abs(m.accepted[0].t) >= m.result["t_needed"])
    en = MN.to_markdown(m, "en")
    assert "# AI factor mining record" in en and not any("（" in l for l in en.splitlines() if l.startswith("- "))
